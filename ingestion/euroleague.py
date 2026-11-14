"""Load Euroleague / EuroCup games into the database.

Data comes from the live.euroleague.net endpoints that the `euroleague-api`
package uses. Every response is saved under data/raw/euroleague/ and read from
there afterwards, so each game is downloaded at most once.

Run from the project root, for example:
    python -m ingestion.euroleague --season 2024 --game 1   # one game
    python -m ingestion.euroleague --season 2024            # whole season
"""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from database.repositories import save_game_bundle

SOURCE = "euroleague"
BASE_URL = "https://live.euroleague.net/api"
SEASON_GAMES_URL = "https://api-live.euroleague.net/v2/competitions"
RAW_DIR = Path("data/raw/euroleague")
SECONDS_BETWEEN_REQUESTS = 1.0

COMPETITION_NAMES = {"E": "EuroLeague", "U": "EuroCup"}

# Nicknames people use that the source does not provide, by Euroleague club
# code. The source's own short names and codes are added automatically.
TEAM_NICKNAMES = {
    "ASV": ["Asvel", "Villeurbanne", "Lyon-Villeurbanne"],
    "BAR": ["Barca", "Barça", "Barcelona"],
    "BAS": ["Vitoria", "Saski Baskonia"],
    "BER": ["Alba Berlin"],
    "IST": ["Efes", "Anadolu Efes"],
    "MAD": ["R. Madrid", "Real Madrid"],
    "MCO": ["Monaco"],
    "MIL": ["Olimpia Milano", "Olimpia Milan", "Armani Milan", "Milano", "EA7 Milan"],
    "MUN": ["Bayern Munich", "Bayern Munchen", "Bayern München"],
    "OLY": ["Olympiakos", "Olympiacos"],
    "PAN": ["Pana", "Panathinaikos Athens"],
    "PAR": ["Partizan Belgrade", "KK Partizan"],
    "PRS": ["Paris"],
    "RED": ["Red Star", "Red Star Belgrade", "Zvezda", "Crvena Zvezda Belgrade", "KK Crvena Zvezda"],
    "TEL": ["Maccabi Tel Aviv"],
    "ULK": ["Fener", "Fenerbahçe", "Fenerbahce Istanbul"],
    "VIR": ["Virtus Bologna"],
    "ZAL": ["Žalgiris", "Zalgiris Kaunas"],
}

# Season list phaseType.code -> games.phase
PHASES = {
    "RS": "regular_season",
    "PI": "play_in",
    "PO": "playoffs",
    "FF": "final_four",
}

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


def _cached_get(path, url, params=None):
    """Return JSON from `path`; download it from `url` first only if it is missing."""
    global _last_request_time
    if not path.exists():
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(url, params=params, timeout=60)
        _last_request_time = time.monotonic()
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(response.text, encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_json(kind, competition, season, gamecode):
    """Return one game's "Header" or "Boxscore" JSON."""
    return _cached_get(
        RAW_DIR / f"{kind.lower()}_{competition}{season}_{gamecode}.json",
        f"{BASE_URL}/{kind}",
        {"gamecode": gamecode, "seasoncode": f"{competition}{season}"},
    )


def fetch_season_games(competition, season):
    """Return {gameCode: game} for a whole season (one request, cached).

    This list has unambiguous UTC start times and clean club names.
    """
    data = _cached_get(
        RAW_DIR / f"games_{competition}{season}.json",
        f"{SEASON_GAMES_URL}/{competition}/seasons/{competition}{season}/games",
    )
    return {game["gameCode"]: game for game in data["data"]}


def _clean(value):
    return value.strip() if isinstance(value, str) else value


_SUFFIX_DISPLAY = {"JR": "Jr.", "SR": "Sr.", "II": "II", "III": "III", "IV": "IV"}


def player_display_name(source_name):
    """'NUNN, KENDRICK' -> 'Kendrick Nunn'; 'WALKER IV, LONNIE' -> 'Lonnie Walker IV'.

    Title-casing loses some capitals (McDowell becomes Mcdowell); matching is
    case-insensitive, so this only affects how names are displayed.
    """
    last, _, first = _clean(source_name).partition(",")
    words = f"{first.strip()} {last.strip()}".split()
    return " ".join(
        _SUFFIX_DISPLAY.get(word.upper().rstrip("."), word.title()) for word in words
    )


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


def utc_start_time(schedule_game):
    """Return the game's start as ISO 8601 UTC, e.g. '2024-10-03T16:45:00Z'.

    The season list has three times: `date` (Central European Time),
    `localDate` (venue local time) and `utcDate`. Only utcDate is unambiguous,
    and storing UTC everywhere keeps games from different timezones sortable.
    """
    moment = datetime.fromisoformat(schedule_game["utcDate"].replace("Z", "+00:00"))
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _team(club, header_name):
    """Team dict for save_game_bundle, with every name the sources use as aliases."""
    aliases = [
        club.get("abbreviatedName"), club.get("editorialName"), club["code"],
        club.get("tvCode"), _clean(header_name), *TEAM_NICKNAMES.get(club["code"], []),
    ]
    return {
        "name": club["name"],
        "source_team_id": club["code"],
        "aliases": [alias for alias in aliases if alias],
    }


def _phase(schedule_game, label):
    code = schedule_game["phaseType"]["code"]
    if code not in PHASES:
        # Fail loudly so a new phase (e.g. a format change) gets a proper name.
        raise ValueError(f"Unknown phase code {code!r} for {label}.")
    return PHASES[code]


def _is_neutral_venue(schedule_game, phase):
    """True when neither team plays at home.

    The source flag is unreliable for the Final Four: in 2024-25 the
    semifinals in Abu Dhabi are flagged neutral but the Final and third-place
    game at the same arena are not. The Final Four is always held at one
    pre-chosen venue, so every Final Four game counts as neutral.
    """
    return bool(schedule_game.get("isNeutralVenue")) or phase == "final_four"


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


def build_game_bundle(header, boxscore, schedule_game, competition, season, gamecode):
    """Turn one game's Header, Boxscore and season-list entry into a save_game_bundle() dict.

    The season list's "local" club is the home team. The Header's TeamA and
    the box score's first team must be that same club, or we refuse to save.
    """
    sides = ("home", "away")
    team_stats = boxscore["Stats"]
    home_club = schedule_game["local"]["club"]
    away_club = schedule_game["road"]["club"]
    codes = (home_club["code"], away_club["code"])

    label = f"{competition}{season} game {gamecode}"
    if (_clean(header["CodeTeamA"]), _clean(header["CodeTeamB"])) != codes:
        raise ValueError(f"Header teams do not match the season list for {label}.")
    for side_stats, code in zip(team_stats, codes):
        if _clean(side_stats["PlayersStats"][0]["Team"]) != code:
            raise ValueError(f"Box score team order does not match the season list for {label}.")
    phase = _phase(schedule_game, label)
    if (int(header["ScoreA"]), int(header["ScoreB"])) != (
        schedule_game["local"]["score"], schedule_game["road"]["score"]
    ):
        raise ValueError(f"Header score does not match the season list for {label}.")

    appearances, stats = [], []
    for side, side_stats in zip(sides, team_stats):
        for row in side_stats["PlayersStats"]:
            minutes = minutes_to_float(row.get("Minutes"))
            if minutes is None:
                continue  # Did not play: no appearance and no stat rows.
            player = {
                "name": player_display_name(row["Player"]),
                "source_player_id": _clean(row["Player_ID"]),
                "aliases": [_clean(row["Player"])],  # 'NUNN, KENDRICK'
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
        "home_team": _team(home_club, header["TeamA"]),
        "away_team": _team(away_club, header["TeamB"]),
        "start_time": utc_start_time(schedule_game),
        "round_number": schedule_game.get("round"),
        "phase": phase,
        "is_neutral_venue": _is_neutral_venue(schedule_game, phase),
        "status": "live" if header.get("Live") else "finished",
        "home_score": int(header["ScoreA"]),
        "away_score": int(header["ScoreB"]),
        "appearances": appearances,
        "stats": stats,
    }


def ingest_game(competition, season, gamecode):
    """Fetch (or read cached) data for one game and save it. Returns the game's DB id."""
    schedule_game = fetch_season_games(competition, season)[gamecode]
    header = fetch_json("Header", competition, season, gamecode)
    boxscore = fetch_json("Boxscore", competition, season, gamecode)
    bundle = build_game_bundle(header, boxscore, schedule_game, competition, season, gamecode)
    return save_game_bundle(bundle)


def ingest_season(competition, season, verbose=True):
    """Load every played game of a season. One bad game is reported, not fatal.

    Returns a list of (gamecode, error message) for games that failed.
    """
    games = fetch_season_games(competition, season)
    played = sorted(code for code, game in games.items() if game.get("played"))
    failures = []
    for number, gamecode in enumerate(played, start=1):
        try:
            ingest_game(competition, season, gamecode)
            status = "ok"
        except Exception as exc:  # keep going; report at the end
            failures.append((gamecode, f"{type(exc).__name__}: {exc}"))
            status = f"FAILED ({type(exc).__name__})"
        if verbose:
            print(f"[{number}/{len(played)}] game {gamecode}: {status}", flush=True)

    if verbose:
        print(f"Done: {len(played) - len(failures)} saved, {len(failures)} failed.")
        for gamecode, message in failures:
            print(f"  game {gamecode}: {message}")
    return failures


def main():
    parser = argparse.ArgumentParser(description="Load Euroleague/EuroCup games.")
    parser.add_argument("--competition", choices=sorted(COMPETITION_NAMES), default="E")
    parser.add_argument("--season", type=int, required=True, help="Start year, e.g. 2024")
    parser.add_argument("--game", type=int, help="Game code; omit to load the whole season")
    args = parser.parse_args()
    if args.game is None:
        failures = ingest_season(args.competition, args.season)
        raise SystemExit(1 if failures else 0)
    game_id = ingest_game(args.competition, args.season, args.game)
    print(f"Saved {args.competition}{args.season} game {args.game} as games.id={game_id}")


if __name__ == "__main__":
    main()
