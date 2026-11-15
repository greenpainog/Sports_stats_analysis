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
    "BES": ["Beşiktaş", "Besiktas Istanbul"],
    "DUB": ["Dubai Basketball"],
    "HTA": ["Hapoel Tel Aviv", "Hapoel"],
    "IST": ["Efes", "Anadolu Efes"],
    "MAD": ["R. Madrid", "Real Madrid"],
    "MCO": ["Monaco"],
    "MIL": ["Olimpia Milano", "Olimpia Milan", "Armani Milan", "Milano", "EA7 Milan"],
    "MUN": ["Bayern Munich", "Bayern Munchen", "Bayern München"],
    "OLY": ["Olympiakos", "Olympiacos"],
    "PAM": ["Valencia Basket", "Valencia BC"],
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

# From the shot list ("Points" endpoint): one row per shot and made free throw.
SHOT_STATS = (
    "paint_points", "paint_fg_made", "paint_fg_attempted",
    "fastbreak_points", "second_chance_points", "points_off_turnovers",
)
# The FIBA paint in shot-list coordinates (cm, basket at 0,0, y towards half
# court): 4.9 m wide, and 5.8 m from the baseline, which is 1.575 m behind
# the basket. The edges count as inside.
PAINT_HALF_WIDTH = 245
PAINT_FRONT, PAINT_BACK = 422.5, -157.5
_FIELD_GOALS = {"2FGM": (2, True), "2FGA": (2, False), "3FGM": (3, True), "3FGA": (3, False)}

_last_request_time = 0.0


class NotCached(FileNotFoundError):
    """A file isn't in data/raw and downloading was turned off."""


def _cached_get(path, url, params=None, download=True):
    """Return JSON from `path`; download it from `url` first only if it is missing.

    With download=False a missing file raises NotCached instead (tests use this,
    so they never make a request).
    """
    global _last_request_time
    if not path.exists() and not download:
        raise NotCached(f"{path} is not cached and download=False.")
    if not path.exists():
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(url, params=params, timeout=60)
        _last_request_time = time.monotonic()
        response.raise_for_status()
        json.loads(response.text)  # never cache a body that isn't JSON
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so another process never reads a half-written file.
        partial = path.with_suffix(".part")
        partial.write_text(response.text, encoding="utf-8")
        partial.replace(path)
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_json(kind, competition, season, gamecode, download=True):
    """Return one game's "Header", "Boxscore" or "Points" (shot list) JSON."""
    return _cached_get(
        RAW_DIR / f"{kind.lower()}_{competition}{season}_{gamecode}.json",
        f"{BASE_URL}/{kind}",
        {"gamecode": gamecode, "seasoncode": f"{competition}{season}"},
        download,
    )


def fetch_season_games(competition, season, download=True):
    """Return {gameCode: game} for a whole season (one request, cached).

    This list has unambiguous UTC start times and clean club names.
    """
    data = _cached_get(
        RAW_DIR / f"games_{competition}{season}.json",
        f"{SEASON_GAMES_URL}/{competition}/seasons/{competition}{season}/games",
        download=download,
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


def in_paint(x, y):
    return abs(x) <= PAINT_HALF_WIDTH and PAINT_BACK <= y <= PAINT_FRONT


def shot_totals(rows):
    """{SHOT_STATS name: value} for a list of shot-list rows (one team's or one player's)."""
    totals = dict.fromkeys(SHOT_STATS, 0)
    for row in rows:
        action, points = row["ID_ACTION"], row.get("POINTS") or 0
        if action in _FIELD_GOALS:
            value, made = _FIELD_GOALS[action]
            # Only 2-point shots can be in the paint; a three never is.
            if value == 2 and in_paint(row["COORD_X"], row["COORD_Y"]):
                totals["paint_fg_attempted"] += 1
                totals["paint_fg_made"] += made
                totals["paint_points"] += 2 * made
        # The flags are set on made shots and made free throws only.
        totals["fastbreak_points"] += points if _clean(row.get("FASTBREAK")) == "1" else 0
        totals["second_chance_points"] += points if _clean(row.get("SECOND_CHANCE")) == "1" else 0
        totals["points_off_turnovers"] += points if _clean(row.get("POINTS_OFF_TURNOVER")) == "1" else 0
    return totals


class ShotListMismatch(ValueError):
    """The shot list's makes don't match the box score: its stats can't be trusted."""


def check_shots_match_box(rows, team_code, team_total):
    """Return (this team's rows, attempts missing from the shot list).

    Made 2s, made 3s and made free throws must match the box score, because
    every shot-list points stat is built from makes; otherwise ShotListMismatch.
    A few missed attempts are sometimes absent from the shot list (seen in 3
    of 330 games in 2024-25); that only affects paint_fg_attempted, so it is
    allowed and reported.
    """
    mine = [r for r in rows if _clean(r["TEAM"]) == team_code]
    count = {a: sum(r["ID_ACTION"] == a for r in mine) for a in ("2FGM", "2FGA", "3FGM", "3FGA", "FTM")}
    makes = (count["2FGM"], count["3FGM"], count["FTM"])
    want = (team_total["FieldGoalsMade2"], team_total["FieldGoalsMade3"], team_total["FreeThrowsMade"])
    if makes != want:
        raise ShotListMismatch(f"{team_code} shot list makes (2PT, 3PT, FT) {makes} != box score {want}.")
    attempts = count["2FGM"] + count["2FGA"] + count["3FGM"] + count["3FGA"]
    missing = team_total["FieldGoalsAttempted2"] + team_total["FieldGoalsAttempted3"] - attempts
    return mine, missing


def _shot_items(totals, entity_type, side, player=None):
    items = []
    for name, value in totals.items():
        item = {"entity_type": entity_type, "team": side, "stat_name": name, "stat_value": float(value)}
        if player:
            item["player"] = player
        items.append(item)
    return items


def build_game_bundle(header, boxscore, schedule_game, competition, season, gamecode, shots=None):
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

    appearances, stats, shot_notes = [], [], []
    for side, side_stats, code in zip(sides, team_stats, codes):
        played = {}  # source player id -> player dict, for the shot-list stats below
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
            played[player["source_player_id"]] = player
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
        # Player minutes added up: 200 for 40 minutes, 225 with one overtime.
        # Pace (possessions per 40 minutes) needs it for overtime games.
        team_minutes = minutes_to_float(side_stats["totr"].get("Minutes"))
        if team_minutes is not None:
            stats.append({
                "entity_type": "team", "team": side, "stat_name": "minutes",
                "stat_value": team_minutes, "raw_value": _clean(side_stats["totr"]["Minutes"]), "unit": "min",
            })

        if shots is not None:
            # Paint, fast-break, second-chance and off-turnover points. The shot
            # list must agree with the box score, or the game is not saved.
            team_shots, missing = check_shots_match_box(shots, code, side_stats["totr"])
            if missing:
                shot_notes.append(f"{code}: {missing} attempt(s) missing from the shot list")
            stats.extend(_shot_items(shot_totals(team_shots), "team", side))
            by_player = {}
            for row in team_shots:
                by_player.setdefault(_clean(row["ID_PLAYER"]), []).append(row)
            unknown = sorted(set(by_player) - set(played))
            if unknown:
                raise ShotListMismatch(f"{label}: shots by players not in the box score: {', '.join(unknown)}.")
            for player_id, player in played.items():  # players with no shots get zeros
                stats.extend(_shot_items(shot_totals(by_player.get(player_id, [])), "player", side, player))

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
        "shot_notes": shot_notes,  # not saved; ingest_game reports them
    }


def ingest_game(competition, season, gamecode, download=True):
    """Fetch (or read cached) data for one game and save it.

    Returns (game id, note): the note says if the shot-list stats were left
    out or are incomplete, else it is "". With download=False only cached files
    are used, and a game without a cached shot list is saved without shot stats.
    """
    schedule_game = fetch_season_games(competition, season, download)[gamecode]
    header = fetch_json("Header", competition, season, gamecode, download)
    boxscore = fetch_json("Boxscore", competition, season, gamecode, download)
    try:
        shots = fetch_json("Points", competition, season, gamecode, download)["Rows"]
    except NotCached:
        shots = None
    note = "" if shots is not None else "no shot list cached"
    try:
        bundle = build_game_bundle(header, boxscore, schedule_game, competition, season, gamecode, shots)
    except ShotListMismatch as exc:  # the box score is still fine: save it without shot stats
        bundle = build_game_bundle(header, boxscore, schedule_game, competition, season, gamecode)
        note = f"no shot stats: {exc}"
    note = note or "; ".join(bundle["shot_notes"])
    return save_game_bundle(bundle), note


def ingest_season(competition, season, verbose=True, download=True, notes=None):
    """Load every played game of a season. One bad game is reported, not fatal.

    Returns a list of (gamecode, error message) for games that failed. Pass a
    list as `notes` to collect (gamecode, note) for games with shot-list notes.
    """
    games = fetch_season_games(competition, season, download)
    played = sorted(code for code, game in games.items() if game.get("played"))
    failures = []
    for number, gamecode in enumerate(played, start=1):
        try:
            _, note = ingest_game(competition, season, gamecode, download)
            status = f"ok ({note})" if note else "ok"
            if note and notes is not None:
                notes.append((gamecode, note))
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
    game_id, note = ingest_game(args.competition, args.season, args.game)
    if note:
        print(f"Note: {note}")
    print(f"Saved {args.competition}{args.season} game {args.game} as games.id={game_id}")


if __name__ == "__main__":
    main()
