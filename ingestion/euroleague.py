"""Load Euroleague / EuroCup games into the database.

Data comes from the live.euroleague.net endpoints that the `euroleague-api`
package uses. Every response is saved under data/raw/euroleague/ and read from
there afterwards, so each game is downloaded at most once.

Run from the project root, for example:
    python -m ingestion.euroleague --season 2024 --game 1
"""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import requests

from database.repositories import save_game_bundle

SOURCE = "euroleague"
BASE_URL = "https://live.euroleague.net/api"
RAW_DIR = Path("data/raw/euroleague")
SECONDS_BETWEEN_REQUESTS = 1.0

COMPETITION_NAMES = {"E": "EuroLeague", "U": "EuroCup"}

# Source column -> our stat_name. Makes and attempts stay separate so that
# percentages can be computed correctly at query time.
STAT_COLUMNS = {
    "Points": "points",
    "FieldGoalsMade2": "fg2_made",
    "FieldGoalsAttempted2": "fg2_attempted",
    "FieldGoalsMade3": "fg3_made",
    "FieldGoalsAttempted3": "fg3_attempted",
    "FreeThrowsMade": "ft_made",
    "FreeThrowsAttempted": "ft_attempted",
    "OffensiveRebounds": "offensive_rebounds",
    "DefensiveRebounds": "defensive_rebounds",
    "TotalRebounds": "total_rebounds",
    "Assistances": "assists",
    "Steals": "steals",
    "Turnovers": "turnovers",
    "BlocksFavour": "blocks",
    "BlocksAgainst": "blocks_against",
    "FoulsCommited": "fouls_committed",
    "FoulsReceived": "fouls_received",
    "Valuation": "pir",
}

_last_request_time = 0.0


def _raw_path(kind, competition, season, gamecode):
    return RAW_DIR / f"{kind.lower()}_{competition}{season}_{gamecode}.json"


def fetch_json(kind, competition, season, gamecode):
    """Return one endpoint's JSON ("Header" or "Boxscore"), downloading only if not cached."""
    global _last_request_time
    path = _raw_path(kind, competition, season, gamecode)
    if not path.exists():
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            f"{BASE_URL}/{kind}",
            params={"gamecode": gamecode, "seasoncode": f"{competition}{season}"},
            timeout=60,
        )
        _last_request_time = time.monotonic()
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(response.text, encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(value):
    return value.strip() if isinstance(value, str) else value


def player_display_name(source_name):
    """'NUNN, KENDRICK' -> 'Kendrick Nunn'.

    Title-casing loses some capitals (McDowell becomes Mcdowell); the alias
    tables in a later phase will handle spelling differences.
    """
    last, _, first = _clean(source_name).partition(",")
    return f"{first.strip()} {last.strip()}".strip().title()


def minutes_to_float(text):
    """'16:18' -> 16.3. Returns None for 'DNP' or missing values."""
    text = _clean(text)
    if not text or ":" not in text:
        return None
    minutes, seconds = text.split(":")
    return round(int(minutes) + int(seconds) / 60, 2)


def season_name(season):
    """2024 -> '2024-25'."""
    return f"{season}-{(season + 1) % 100:02d}"


def parse_start_time(date_text, hour_text):
    """Header Date is day/month/year. Returns ISO 8601 without a timezone.

    The timezone of `Hour` is not stated by the source, so no offset is
    attached until it has been checked against a published tip-off time.
    """
    moment = datetime.strptime(
        f"{_clean(date_text)} {_clean(hour_text)}", "%d/%m/%Y %H:%M"
    )
    return moment.isoformat(timespec="minutes")


def _stat_items(row, entity_type, side, player=None):
    items = []
    for column, stat_name in STAT_COLUMNS.items():
        value = row.get(column)
        if value is None:
            continue
        item = {
            "entity_type": entity_type,
            "team": side,
            "stat_name": stat_name,
            "stat_value": float(value),
            "raw_value": str(value),
        }
        if player:
            item["player"] = player
        items.append(item)
    return items


def build_game_bundle(header, boxscore, competition, season, gamecode):
    """Turn one game's Header + Boxscore JSON into a save_game_bundle() dict.

    TeamA in the Header is the home team, and the box score lists TeamA first.
    """
    sides = ("home", "away")
    team_stats = boxscore["Stats"]
    codes = (_clean(header["CodeTeamA"]), _clean(header["CodeTeamB"]))
    for side_stats, code in zip(team_stats, codes):
        first_player = side_stats["PlayersStats"][0]
        if _clean(first_player["Team"]) != code:
            raise ValueError(
                f"Box score team order does not match the Header for {competition}{season} game {gamecode}."
            )

    appearances, stats = [], []
    for side, side_stats in zip(sides, team_stats):
        for row in side_stats["PlayersStats"]:
            minutes = minutes_to_float(row.get("Minutes"))
            if minutes is None:
                continue  # Did not play: no appearance and no stat rows.
            player = {
                "name": player_display_name(row["Player"]),
                "source_player_id": _clean(row["Player_ID"]),
            }
            appearances.append(
                {"team": side, "player": player, "started": int(bool(row.get("IsStarter")))}
            )
            stats.extend(_stat_items(row, "player", side, player))
            stats.append({
                "entity_type": "player", "team": side, "player": player,
                "stat_name": "minutes", "stat_value": minutes,
                "raw_value": _clean(row["Minutes"]), "unit": "min",
            })
            if row.get("Plusminus") is not None:
                stats.append({
                    "entity_type": "player", "team": side, "player": player,
                    "stat_name": "plus_minus", "stat_value": float(row["Plusminus"]),
                    "raw_value": str(row["Plusminus"]),
                })

        # "totr" is the team total, which already includes the unassigned
        # "Team" row (team rebounds, shot-clock turnovers, ...).
        stats.extend(_stat_items(side_stats["totr"], "team", side))

    return {
        "source": SOURCE,
        "source_game_id": f"{competition}{season}-{gamecode}",
        "competition": {
            "name": COMPETITION_NAMES[competition],
            "source_competition_id": competition,
            "gender": "men",
        },
        "season": {"name": season_name(season)},
        "home_team": {"name": _clean(header["TeamA"]).title(), "source_team_id": codes[0]},
        "away_team": {"name": _clean(header["TeamB"]).title(), "source_team_id": codes[1]},
        "start_time": parse_start_time(header["Date"], header["Hour"]),
        "status": "live" if header.get("Live") else "finished",
        "home_score": int(header["ScoreA"]),
        "away_score": int(header["ScoreB"]),
        "appearances": appearances,
        "stats": stats,
    }


def ingest_game(competition, season, gamecode):
    """Fetch (or read cached) data for one game and save it. Returns the game's DB id."""
    header = fetch_json("Header", competition, season, gamecode)
    boxscore = fetch_json("Boxscore", competition, season, gamecode)
    bundle = build_game_bundle(header, boxscore, competition, season, gamecode)
    return save_game_bundle(bundle)


def main():
    parser = argparse.ArgumentParser(description="Load one Euroleague/EuroCup game.")
    parser.add_argument("--competition", choices=sorted(COMPETITION_NAMES), default="E")
    parser.add_argument("--season", type=int, required=True, help="Start year, e.g. 2024")
    parser.add_argument("--game", type=int, required=True, help="Game code")
    args = parser.parse_args()
    game_id = ingest_game(args.competition, args.season, args.game)
    print(f"Saved {args.competition}{args.season} game {args.game} as games.id={game_id}")


if __name__ == "__main__":
    main()
