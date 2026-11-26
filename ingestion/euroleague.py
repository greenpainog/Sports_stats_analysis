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

from database.db import get_connection
from database.repositories import save_game_bundle
from ingestion.play_by_play import build_stints, compare_with_box, parse_events

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

# The source gave one player a second code partway through a season (same
# name, same club, no overlapping games): second code -> the first one, so he
# stays one player. Checked 2026-11-16.
PLAYER_CODE_FIXES = {
    "P015182": "P009911",  # Gary Browne, Bosna Sarajevo (EuroCup 2026-27)
    "P014175": "P003215",  # Aurimas Majauskas, Neptunas Klaipeda (EuroCup 2025-26)
}


def _player_code(raw):
    code = _clean(raw)
    return PLAYER_CODE_FIXES.get(code, code)


# Season list phaseType.code -> games.phase
PHASES = {
    "RS": "regular_season",
    "PI": "play_in",
    "PO": "playoffs",
    "FF": "final_four",
    # EuroCup knockout rounds (eighthfinals, quarterfinals, semifinals, finals)
    "8F": "playoffs",
    "4F": "playoffs",
    "2F": "playoffs",
    "Final": "playoffs",
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


def _cached_get(path, url, params=None, download=True, max_age_hours=None):
    """Return JSON from `path`; download it from `url` first only if it is missing.

    With max_age_hours, a saved copy older than that is downloaded again (the
    season's game list, so new results show up). With download=False a missing
    file raises NotCached instead (tests use this, so they never make a request).
    """
    global _last_request_time
    if not path.exists() and not download:
        raise NotCached(f"{path} is not cached and download=False.")
    too_old = (max_age_hours is not None and path.exists()
               and time.time() - path.stat().st_mtime > max_age_hours * 3600)
    if not path.exists() or (too_old and download):
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
    """Return one game's "Header", "Boxscore", "Points" (shot list) or "PlaybyPlay" JSON."""
    return _cached_get(
        RAW_DIR / f"{kind.lower()}_{competition}{season}_{gamecode}.json",
        f"{BASE_URL}/{kind}",
        {"gamecode": gamecode, "seasoncode": f"{competition}{season}"},
        download,
    )


def fetch_season_games(competition, season, download=True, max_age_hours=None):
    """Return {gameCode: game} for a whole season (one request, cached).

    This list has unambiguous UTC start times and clean club names. Pass
    max_age_hours to download it again when the saved copy is older.
    """
    data = _cached_get(
        RAW_DIR / f"games_{competition}{season}.json",
        f"{SEASON_GAMES_URL}/{competition}/seasons/{competition}{season}/games",
        download=download,
        max_age_hours=max_age_hours,
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


def quarter_points(header, letter):
    """{'q1': 17, 'q2': 19, 'q3': 21, 'q4': 20} (+ 'ot') for team "A" or "B".

    The header's quarter scores are running totals (17, 36, 57, 77), and the
    extra-time score is the final score when there was overtime (0 if not).
    Several overtimes come as one number, so they are one 'ot' period.
    """
    totals = [int(header[f"ScoreQuarter{i}{letter}"]) for i in range(1, 5)]
    final = int(header[f"Score{letter}"])
    if any(later < earlier for earlier, later in zip(totals, totals[1:])):
        raise ValueError(f"Quarter scores for team {letter} are not running totals: {totals}.")
    extra = int(header.get(f"ScoreExtraTime{letter}") or 0)
    if (extra or totals[3]) != final:
        raise ValueError(f"Quarter scores for team {letter} don't add up to the final score {final}.")
    points = {f"q{i + 1}": total - (totals[i - 1] if i else 0) for i, total in enumerate(totals)}
    if extra:
        points["ot"] = final - totals[3]
    return points


# One-off spellings of a coach in the headers -> the name used in his other
# games. Each was checked: same team, same weeks, one game spelled differently
# (2026-11-25). Different people who share a surname (Zeljko and Sasa
# Obradovic, TJ and Tony Parker) are left apart.
STAFF_NAME_FIXES = {
    "Sarunas Jasikevicius": "Saras Jasikevicius",
    "Ty Gary Harrelson": "Ty Harrelson",
    "Rodrigo Augusto Pastore": "Rodrigo Pastore",
    "Nenad Zoric Markovic": "Nenad Markovic",
}


def _staff_name(source_name):
    name = player_display_name(source_name)
    return STAFF_NAME_FIXES.get(name, name)


def header_staff(header):
    """The game's coaches (with their side) and referees, display names."""
    staff = [{"role": "coach", "team": side, "name": _staff_name(header[key])}
             for side, key in (("home", "CoachA"), ("away", "CoachB")) if _clean(header.get(key))]
    staff += [{"role": "referee", "name": _staff_name(header[key])}
              for key in ("Referee1", "Referee2", "Referee3") if _clean(header.get(key))]
    return staff


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
    for side, letter in zip(sides, "AB"):  # header team A is the home team (checked above)
        stats.extend({"entity_type": "team", "team": side, "stat_name": "points", "stat_value": points,
                      "period": period} for period, points in quarter_points(header, letter).items())
    for side, side_stats, code in zip(sides, team_stats, codes):
        played = {}  # source player id -> player dict, for the shot-list stats below
        for row in side_stats["PlayersStats"]:
            minutes = minutes_to_float(row.get("Minutes"))
            if minutes is None:
                continue  # Did not play: no appearance and no stat rows.
            player = {
                "name": player_display_name(row["Player"]),
                "source_player_id": _player_code(row["Player_ID"]),
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
                by_player.setdefault(_player_code(row["ID_PLAYER"]), []).append(row)
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
        "staff": header_staff(header),
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


class GameInProgress(Exception):
    """The source still marks the game live; nothing was cached or saved."""


def _finished_long_enough(game, now=None):
    """True when the list says played and the game started more than 3 hours ago."""
    start = datetime.strptime(game["utcDate"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return bool(game.get("played")) and ((now or datetime.now(timezone.utc)) - start).total_seconds() > 3 * 3600


def ingest_new_games(competition, season, conn, list_max_age_hours=20, now=None):
    """Load the season's finished games that aren't in the database yet.

    The game list is downloaded again when the saved copy is older than
    list_max_age_hours. A game counts as finished once the list says played
    and it started more than 3 hours ago; if its header still says live, its
    cached files are removed (so a half-time box score is never kept) and it
    is tried again next time. Returns {"saved": [...], "notes": [...],
    "waiting": [...], "failed": [...]}, each a list of (gamecode, text).
    """
    games = fetch_season_games(competition, season, max_age_hours=list_max_age_hours)
    saved_ids = {row[0] for row in conn.execute(
        "SELECT source_game_id FROM games WHERE source = 'euroleague' AND source_game_id LIKE ?",
        (f"{competition}{season}-%",))}
    report = {"saved": [], "notes": [], "waiting": [], "failed": []}
    for gamecode in sorted(games):
        game = games[gamecode]
        if f"{competition}{season}-{gamecode}" in saved_ids or not game.get("played"):
            continue
        if not _finished_long_enough(game, now):
            report["waiting"].append((gamecode, "started less than 3 hours ago"))
            continue
        try:
            header = fetch_json("Header", competition, season, gamecode)
            if header.get("Live"):
                for kind in ("Header", "Boxscore", "Points"):
                    (RAW_DIR / f"{kind.lower()}_{competition}{season}_{gamecode}.json").unlink(missing_ok=True)
                raise GameInProgress("header still says live")
            _, note = ingest_game(competition, season, gamecode)
            report["saved"].append((gamecode, f"{game['local']['club']['name']} - {game['road']['club']['name']}"))
            if note:
                report["notes"].append((gamecode, note))
        except GameInProgress as exc:
            report["waiting"].append((gamecode, str(exc)))
        except Exception as exc:  # keep going; report at the end
            report["failed"].append((gamecode, f"{type(exc).__name__}: {exc}"))
    return report


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


def fetch_play_by_play(conn, competitions=("E", "U"), verbose=True):
    """Cache the play-by-play of every loaded game that doesn't have it yet.

    Only fetches and saves the raw JSON under data/raw/ (once per game, at least
    SECONDS_BETWEEN_REQUESTS apart); nothing is written to the database.
    Returns [(source_game_id, error)] for games that failed.
    """
    rows = conn.execute(
        f"""SELECT source_game_id FROM games WHERE source = ? AND status = 'finished'
            AND substr(source_game_id, 1, 1) IN ({', '.join('?' for _ in competitions)})
            ORDER BY source_game_id""", (SOURCE, *competitions)).fetchall()
    games = []
    for (source_game_id,) in rows:  # 'E2024-17'
        season_code, gamecode = source_game_id.split("-")
        games.append((source_game_id, season_code[0], int(season_code[1:]), int(gamecode)))
    missing = [g for g in games if not (RAW_DIR / f"playbyplay_{g[1]}{g[2]}_{g[3]}.json").exists()]
    if verbose:
        print(f"{len(games)} games, {len(games) - len(missing)} already cached, {len(missing)} to fetch "
              f"(~{len(missing) * SECONDS_BETWEEN_REQUESTS / 60:.0f} min).", flush=True)
    failures = []
    for number, (source_game_id, competition, season, gamecode) in enumerate(missing, start=1):
        try:
            fetch_json("PlaybyPlay", competition, season, gamecode)
        except Exception as exc:  # keep going; report at the end
            failures.append((source_game_id, f"{type(exc).__name__}: {exc}"))
        if verbose and (number % 50 == 0 or number == len(missing)):
            print(f"[{number}/{len(missing)}] {len(failures)} failed so far", flush=True)
    for source_game_id, message in failures:
        print(f"  {source_game_id}: {message}")
    return failures


def _box_for_stints(conn, game_id):
    """Starters, box-score (seconds, plus-minus) and code->id maps for one game."""
    starters, box, player_ids = {}, {}, {}
    stats = {(player_id, name): (raw, value) for player_id, name, raw, value in conn.execute(
        """SELECT player_id, stat_name, raw_value, stat_value FROM game_stats
           WHERE game_id = ? AND entity_type = 'player' AND period = 'match'
             AND stat_name IN ('minutes', 'plus_minus')""", (game_id,))}
    for team_code, player_code, player_id, started in conn.execute(
            """SELECT t.source_team_id, p.source_player_id, a.player_id, a.started
               FROM game_appearances a JOIN players p ON p.id = a.player_id
               JOIN teams t ON t.id = a.team_id WHERE a.game_id = ?""", (game_id,)):
        player_ids[player_code] = player_id
        if started:
            starters.setdefault(team_code, set()).add(player_code)
        raw_minutes = stats.get((player_id, "minutes"), (None, None))[0]
        seconds = None
        if raw_minutes and ":" in raw_minutes:
            minutes, secs = raw_minutes.split(":")
            seconds = int(minutes) * 60 + int(secs)
        box[(team_code, player_code)] = (seconds, stats.get((player_id, "plus_minus"), (None, None))[1])
    return starters, box, player_ids


def store_game_play_by_play(conn, game_id, data, home_code, away_code):
    """Save one game's events, stints and their box-score check (no commit).

    Replaces whatever was stored for the game before, so it can be re-run
    after a change to the stint builder. Returns the pbp_checks row as a dict.
    """
    starters, box, player_ids = _box_for_stints(conn, game_id)
    team_ids = {home_code: conn.execute("SELECT home_team_id FROM games WHERE id = ?", (game_id,)).fetchone()[0],
                away_code: conn.execute("SELECT away_team_id FROM games WHERE id = ?", (game_id,)).fetchone()[0]}
    events = parse_events(data, player_code=_player_code)
    stints, problems = build_stints(events, starters, home_code, away_code)
    max_diff, mismatches = compare_with_box(stints, box)

    for table in ("pbp_events", "stints", "pbp_checks"):
        conn.execute(f"DELETE FROM {table} WHERE game_id = ?", (game_id,))
    stint_ids = {}  # id(stint dict) -> stints.id
    for stint in stints:
        stint_ids[id(stint)] = stint_id = conn.execute(
            """INSERT INTO stints (game_id, team_id, start_elapsed, end_elapsed, points_for, points_against)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, team_ids[stint["team_code"]], stint["start"], stint["end"],
             stint["points_for"], stint["points_against"])).lastrowid
        unknown = [code for code in stint["players"] if code not in player_ids]
        if unknown:
            problems.append(f"{stint['team_code']}: unknown player {', '.join(unknown)} on the floor")
        conn.executemany("INSERT OR IGNORE INTO stint_players (stint_id, player_id) VALUES (?, ?)",
                         [(stint_id, player_ids[code]) for code in stint["players"] if code in player_ids])
    home, away = 0, 0
    rows = []
    for sequence, event in enumerate(events):
        home = home if event["score_a"] is None else event["score_a"]
        away = away if event["score_b"] is None else event["score_b"]
        rows.append((game_id, sequence, event["sequence"], event["period"], event["elapsed"],
                     team_ids.get(event["team_code"]), player_ids.get(event["player_code"]),
                     event["play_type"], home, away, event["info"],
                     event["points_a"], event["points_b"], int(event["clutch"]),
                     stint_ids.get(id(event.get("stint_a"))), stint_ids.get(id(event.get("stint_b")))))
    conn.executemany(
        """INSERT INTO pbp_events (game_id, sequence, play_number, period, elapsed, team_id, player_id,
                                   play_type, score_home, score_away, info,
                                   home_points, away_points, clutch, home_stint_id, away_stint_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    check = {"game_id": game_id, "events": len(events), "stints": len(stints),
             "minutes_max_diff": max_diff, "plus_minus_mismatches": mismatches,
             "problems": "; ".join(dict.fromkeys(problems)) or None}
    conn.execute(
        """INSERT INTO pbp_checks (game_id, events, stints, minutes_max_diff, plus_minus_mismatches, problems)
           VALUES (:game_id, :events, :stints, :minutes_max_diff, :plus_minus_mismatches, :problems)""", check)
    return check


def store_play_by_play(conn, rebuild=False, verbose=True):
    """Store events and stints for every game with a cached play-by-play file.

    Games already in pbp_checks are skipped unless `rebuild`. Reads only
    data/raw/ (no requests). Returns the list of pbp_checks rows written.
    """
    rows = conn.execute(
        """SELECT g.id, g.source_game_id, th.source_team_id, ta.source_team_id
           FROM games g JOIN teams th ON th.id = g.home_team_id JOIN teams ta ON ta.id = g.away_team_id
           WHERE g.source = ? AND g.status = 'finished'
             AND (? OR g.id NOT IN (SELECT game_id FROM pbp_checks))
           ORDER BY g.source_game_id""", (SOURCE, int(rebuild))).fetchall()
    checks = []
    for game_id, source_game_id, home_code, away_code in rows:
        season_code, gamecode = source_game_id.split("-")
        path = RAW_DIR / f"playbyplay_{season_code}_{gamecode}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        checks.append(store_game_play_by_play(conn, game_id, data, home_code, away_code))
        conn.commit()
    if verbose:
        exact = sum(1 for c in checks if c["plus_minus_mismatches"] == 0)
        print(f"Stored play-by-play for {len(checks)} games; plus-minus matches the box score "
              f"exactly in {exact}.")
    return checks


def main():
    parser = argparse.ArgumentParser(description="Load Euroleague/EuroCup games.")
    parser.add_argument("--competition", choices=sorted(COMPETITION_NAMES), default="E")
    parser.add_argument("--season", type=int, help="Start year, e.g. 2024 (needed unless --play-by-play)")
    parser.add_argument("--game", type=int, help="Game code; omit to load the whole season")
    parser.add_argument("--cached-only", action="store_true",
                        help="Reload from data/raw/ without any request (e.g. after a loader change)")
    parser.add_argument("--play-by-play", action="store_true",
                        help="Fetch the play-by-play of every loaded Euroleague/EuroCup game that lacks it, "
                             "then store its events and stints")
    parser.add_argument("--rebuild-stints", action="store_true",
                        help="With --play-by-play: rebuild events and stints of all games from data/raw/")
    args = parser.parse_args()
    if args.play_by_play:
        conn = get_connection()
        try:
            failures = [] if args.cached_only else fetch_play_by_play(conn)
            store_play_by_play(conn, rebuild=args.rebuild_stints)
            raise SystemExit(1 if failures else 0)
        finally:
            conn.close()
    if args.season is None:
        parser.error("--season is required")
    download = not args.cached_only
    if args.game is None:
        failures = ingest_season(args.competition, args.season, download=download)
        raise SystemExit(1 if failures else 0)
    game_id, note = ingest_game(args.competition, args.season, args.game, download)
    if note:
        print(f"Note: {note}")
    print(f"Saved {args.competition}{args.season} game {args.game} as games.id={game_id}")


if __name__ == "__main__":
    main()
