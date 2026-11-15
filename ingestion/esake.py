"""Greek Basket League (Stoiximan GBL) games from esake.gr.

esake.gr has no data feed: each game page (EsakegameView?idgame=...&mode=3)
has the box score in its HTML. Pages are saved under data/raw/esake/ and parsed
from there; each page is downloaded at most once, at least 1 s apart.

Terms (read 2026-11-15, www.esake.gr/el/A19800D1): content is copyrighted;
copies are allowed strictly for personal use with the source cited. Anything
public (e.g. a hosted demo) needs ESAKE's written permission.

    python -m ingestion.esake --game 51FB1AFE --season 2026-27
"""
import argparse
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from database.repositories import save_game_bundle

SOURCE = "esake"
BASE_URL = "https://www.esake.gr/el/action/EsakegameView"
RAW_DIR = Path("data/raw/esake")
SECONDS_BETWEEN_REQUESTS = 1.0
COMPETITION = {"name": "Greek Basket League", "source_competition_id": "GBL", "country": "Greece", "gender": "men"}
ATHENS = ZoneInfo("Europe/Athens")

# Short Greek month names, accents removed. June and July share "ιου", so they need 4 letters.
GREEK_MONTHS = {"ιαν": 1, "φεβ": 2, "μαρ": 3, "απρ": 4, "μαι": 5, "ιουν": 6, "ιουλ": 7,
                "αυγ": 8, "σεπ": 9, "οκτ": 10, "νοε": 11, "δεκ": 12}


def _greek_month(word):
    """'Νοε' -> 11, 'Μαΐ' -> 5, 'Ιούν' -> 6."""
    plain = "".join(c for c in unicodedata.normalize("NFD", word.lower()) if not unicodedata.combining(c))
    return GREEK_MONTHS.get(plain[:4]) or GREEK_MONTHS[plain[:3]]
NAME_SUFFIXES = {"II", "III", "IV", "JR", "JR.", "SR"}

# Box score columns after the player cell, in page order, -> our stat names.
# The page's own tooltips name the two foul columns: "FOULS F" is
# "Κερδισμένα φάουλ" (fouls won = received), "FOULS M" is "Αμυντικά φάουλ"
# (fouls committed). "RANK" (Βαθμολογία) is the PIR-style rating.
COLUMNS = ("points", "fg2", "fg3", "ft", "total_rebounds", "defensive_rebounds", "offensive_rebounds",
           "assists", "blocks", "blocks_against", "fouls_received", "fouls_committed", "steals",
           "turnovers", "minutes", "pir")
HEADERS = ("P", "2PM-A", "3PM-A", "FTM-A", "REBS", "D.REBS", "O.REBS", "AST", "BLK", "BLK-A",
           "FOULS F", "FOULS M", "STL", "TO", "TIM.PL.", "RANK")
TEAM_ROW, TOTAL_ROW = "ΟΜΑΔΙΚΑ - ΠΑΓΚΟΣ", "ΣΥΝΟΛΟ"


# --- Download ----------------------------------------------------------------

_last_request_time = 0.0


def fetch_game_page(idgame, download=True):
    """The game's box score HTML from data/raw/esake/, downloaded first if missing."""
    global _last_request_time
    path = RAW_DIR / f"game_{idgame}_mode3.html"
    if not path.exists():
        if not download:
            raise FileNotFoundError(f"{path} is not cached and download=False.")
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(BASE_URL, params={"idgame": idgame, "mode": 3}, timeout=60)
        _last_request_time = time.monotonic()
        response.raise_for_status()
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_bytes(response.content)
        partial.replace(path)
    return path.read_text(encoding="utf-8")


# --- HTML ------------------------------------------------------------------------

class _Tables(HTMLParser):
    """Collects every <table> as rows of cells: {"text", "href", "span"} (first link and first <span>)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
            self.tables[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "href": None, "span": None, "_in_span": False}
            self._row.append(self._cell)
        elif self._cell is not None and tag == "a" and self._cell["href"] is None:
            self._cell["href"] = attrs.get("href")
        elif self._cell is not None and tag == "span" and self._cell["span"] is None:
            self._cell["_in_span"], self._cell["span"] = True, ""

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "span" and self._cell is not None:
            self._cell["_in_span"] = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data
            if self._cell["_in_span"]:
                self._cell["span"] += data


def _text(cell):
    return " ".join(cell["text"].split())


@dataclass
class Line:
    """One box score row: a player, the team/bench row or the totals row."""
    values: dict  # stat name -> number (fg2/fg3/ft as (made, attempted))
    name: str = None  # display name, players only
    raw_name: str = None  # as on the page, e.g. "NOLLEY II LANDERS"
    number: str = None
    source_player_id: str = None


@dataclass
class TeamBox:
    name: str
    source_team_id: str
    players: list = field(default_factory=list)
    team_row: Line = None
    totals: Line = None


@dataclass
class EsakeGame:
    idgame: str
    round_number: int
    start_time: str  # UTC ISO 8601
    venue: str
    home: TeamBox
    away: TeamBox
    home_score: int
    away_score: int
    quarters: list  # [(home, away), ...] including overtimes


def display_name(raw, surname):
    """'NOLLEY II LANDERS' with surname 'NOLLEY' -> 'Landers Nolley II'."""
    words = raw.split()
    surname_words = surname.split()
    rest = words[len(surname_words):]
    while rest and rest[0].upper() in NAME_SUFFIXES:
        surname_words.append(rest.pop(0))
    # str.title() capitalizes after an apostrophe too: "DE'VION" -> "De'Vion" (straight or curly ’).
    pretty = [w if w.upper() in NAME_SUFFIXES else w.title() if ("'" in w or "’" in w) else w.capitalize()
              for w in rest + surname_words]
    return " ".join(pretty)


def _number(text):
    text = text.strip()
    return None if text in ("", "-") else int(text)


def _made_attempted(text):
    if text.strip() in ("", "-"):
        return None
    made, attempted = (int(x) for x in text.split("-"))
    return made, attempted


def _minutes(text):
    """'00:26:51' -> 26.85; '--:--:--' -> None."""
    if "-" in text or not text.strip():
        return None
    h, m, s = (int(x) for x in text.strip().split(":"))
    return round(60 * h + m + s / 60, 2)


def _line(cells):
    values = {}
    for name, cell in zip(COLUMNS, cells):
        text = _text(cell)
        if name in ("fg2", "fg3", "ft"):
            values[name] = _made_attempted(text)
        elif name == "minutes":
            values[name] = _minutes(text)
        else:
            values[name] = _number(text)
    return values


def _parse_box(rows, team_name, team_id):
    header = [_text(c) for c in rows[1]]
    if tuple(header[1:]) != HEADERS:
        raise ValueError(f"Unexpected box score columns for {team_name}: {header}")
    box = TeamBox(team_name, team_id)
    for row in rows[2:]:
        label = _text(row[0])
        if label == TEAM_ROW:
            box.team_row = Line(_line(row[1:]))
        elif label == TOTAL_ROW:
            box.totals = Line(_line(row[1:]))
        else:
            player_cell = row[0]
            match = re.search(r"idplayer=(\w+)", player_cell["href"] or "")
            number = re.match(r"#(\d+)", label)
            raw = re.sub(r"^#\d+\s*", "", label)
            surname = " ".join((player_cell["span"] or raw.split()[0]).split())
            box.players.append(Line(_line(row[1:]), name=display_name(raw, surname), raw_name=raw,
                                    number=number.group(1) if number else None,
                                    source_player_id=match.group(1) if match else None))
    if box.totals is None:
        raise ValueError(f"No totals row for {team_name}.")
    return box


def _start_time(date_text, clock, season):
    """'Σαβ 14 Νοε', '17:00', '2026-27' -> '2026-11-14T15:00:00Z' (Athens local time to UTC)."""
    day, month_word = date_text.split()[1:3]
    month = _greek_month(month_word)
    first_year = int(season[:4])
    year = first_year if month >= 7 else first_year + 1  # a season runs from autumn to the next summer
    hour, minute = (int(x) for x in clock.split(":"))
    local = datetime(year, month, int(day), hour, minute, tzinfo=ATHENS)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_game_page(html, idgame, season):
    """Parse a saved EsakegameView mode=3 page. `season` is e.g. '2026-27' (the page has no year)."""
    banner = re.search(r"Stoiximan GBL (\d{4})-(\d{4})", html)
    if banner and f"{banner.group(1)}-{banner.group(2)[2:]}" != season:
        raise ValueError(f"Page is for season {banner.group(1)}-{banner.group(2)}, not {season}.")
    round_match = re.search(r"<h5>(\d+)η Αγωνιστική</h5>", html)
    date_match = re.search(r"clock2\.svg[^>]*><span>([^<]+)</span>\s*(\d{1,2}:\d{2})", html)
    venue_match = re.search(r"pin2\.svg[^>]*>([^<]+)</div>", html)
    score_block = re.search(r'esake-pgame-view-final-score(.*?)</div>\s*</div>\s*</div>', html, re.S)
    if not (date_match and score_block):
        raise ValueError(f"Game {idgame}: page header not found (not played yet, or the layout changed).")
    block = score_block.group(1)
    names = [" ".join(n.split()) for n in re.findall(r"<span>([^<&]+)</span>", block) if n.strip() != "vs"]
    team_ids = re.findall(r"/dat/esaketeam/(\w+)/", block)
    score = re.search(r"<span>(\d+)&nbsp;<span>vs</span>&nbsp;(\d+)</span>", block)
    if len(names) != 2 or len(team_ids) != 2 or not score:
        raise ValueError(f"Game {idgame}: couldn't read teams and score from the header.")

    parser = _Tables()
    parser.feed(html)
    quarters = [(_number(_text(r[0])), _number(_text(r[2]))) for r in parser.tables[0]
                if len(r) == 3 and re.fullmatch(r"(Q\d|OT\d?|ΠΑΡ\d?)", _text(r[1]))]
    boxes = [t for t in parser.tables if len(t) > 2 and _text(t[1][0]) == "ΠΑΙΚΤΗΣ"]
    if len(boxes) != 2:
        raise ValueError(f"Game {idgame}: expected 2 box score tables, found {len(boxes)}.")
    # Each table follows "ΑΝΑΛΥΤΙΚΑ ΣΤΑΤΙΣΤΙΚΑ <team>"; check the order matches the header.
    labels = re.findall(r"ΑΝΑΛΥΤΙΚΑ ΣΤΑΤΙΣΤΙΚΑ\s*(?:<[^>]+>\s*)*([^<]+)", html)
    if [" ".join(label.split()) for label in labels[:2]] != names:
        raise ValueError(f"Game {idgame}: box score tables {labels[:2]} don't match teams {names}.")

    return EsakeGame(
        idgame=idgame,
        round_number=int(round_match.group(1)) if round_match else None,
        start_time=_start_time(date_match.group(1), date_match.group(2), season),
        venue=" ".join(venue_match.group(1).split()) if venue_match else None,
        home=_parse_box(boxes[0], names[0], team_ids[0]),
        away=_parse_box(boxes[1], names[1], team_ids[1]),
        home_score=int(score.group(1)),
        away_score=int(score.group(2)),
        quarters=quarters,
    )


# --- Checks and mapping ------------------------------------------------------------

_SUMMED = ("points", "total_rebounds", "defensive_rebounds", "offensive_rebounds", "assists", "blocks",
           "blocks_against", "fouls_received", "fouls_committed", "steals", "turnovers")


def check_game(game):
    """Raise if the page doesn't add up: players (+ team row) = totals, quarters = final score."""
    problems = []
    for side, score in ((game.home, game.home_score), (game.away, game.away_score)):
        if side.totals.values["points"] != score:
            problems.append(f"{side.name}: totals {side.totals.values['points']} points, header {score}")
        for stat in _SUMMED + ("fg2", "fg3", "ft"):
            lines = side.players + ([side.team_row] if side.team_row else [])
            if stat in ("fg2", "fg3", "ft"):
                got = tuple(sum((line.values[stat] or (0, 0))[i] for line in lines) for i in (0, 1))
            else:
                got = sum(line.values[stat] or 0 for line in lines)
            if got != side.totals.values[stat]:
                problems.append(f"{side.name} {stat}: players add up to {got}, totals say {side.totals.values[stat]}")
    if game.quarters:
        got = tuple(sum(q[i] for q in game.quarters) for i in (0, 1))
        if got != (game.home_score, game.away_score):
            problems.append(f"quarters add up to {got}, final score {game.home_score}-{game.away_score}")
    if problems:
        raise ValueError(f"Game {game.idgame} doesn't add up: " + "; ".join(problems))


def _items(values, entity_type, side, player=None):
    items = []
    for name, value in values.items():
        if value is None:
            continue
        if name in ("fg2", "fg3", "ft"):
            pairs = ((f"{name}_made", value[0]), (f"{name}_attempted", value[1]))
        else:
            pairs = ((name, value),)
        for stat_name, number in pairs:
            item = {"entity_type": entity_type, "team": side, "stat_name": stat_name, "stat_value": float(number)}
            if name == "minutes":
                item["unit"] = "min"
            if player:
                item["player"] = player
            items.append(item)
    return items


def build_game_bundle(game, season):
    """An EsakeGame -> a save_game_bundle() dict (same stat names as the Euroleague)."""
    check_game(game)
    appearances, stats = [], []
    for side, box in (("home", game.home), ("away", game.away)):
        for line in box.players:
            if not line.values.get("minutes"):
                continue  # listed but did not play
            player = {"name": line.name, "source_player_id": line.source_player_id,
                      "aliases": [line.raw_name]}
            appearances.append({"team": side, "player": player})  # the page doesn't mark starters
            stats.extend(_items(line.values, "player", side, player))
        team_values = dict(box.totals.values)
        # The totals row has no minutes; the players' minutes add up to the
        # team's (200, or 225 with one overtime), as with the Euroleague.
        team_values["minutes"] = round(sum(line.values["minutes"] or 0 for line in box.players))
        stats.extend(_items(team_values, "team", side))
    return {
        "source": SOURCE,
        "source_game_id": game.idgame,
        "competition": COMPETITION,
        "season": {"name": season},
        "home_team": {"name": game.home.name, "source_team_id": game.home.source_team_id,
                      "aliases": [game.home.name]},
        "away_team": {"name": game.away.name, "source_team_id": game.away.source_team_id,
                      "aliases": [game.away.name]},
        "start_time": game.start_time,
        "round_number": game.round_number,
        "phase": "regular_season",
        "is_neutral_venue": False,
        "status": "finished",
        "home_score": game.home_score,
        "away_score": game.away_score,
        "appearances": appearances,
        "stats": stats,
    }


def ingest_game(idgame, season, download=True):
    html = fetch_game_page(idgame, download)
    return save_game_bundle(build_game_bundle(parse_game_page(html, idgame, season), season))


def main():
    parser = argparse.ArgumentParser(description="Load Greek Basket League games from esake.gr.")
    parser.add_argument("--game", required=True, help="esake game id, e.g. 51FB1AFE")
    parser.add_argument("--season", required=True, help="Season, e.g. 2026-27 (the page has no year)")
    args = parser.parse_args()
    game_id = ingest_game(args.game, args.season)
    print(f"Saved esake game {args.game} as games.id={game_id}")


if __name__ == "__main__":
    main()
