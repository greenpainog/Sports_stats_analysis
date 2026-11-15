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

from database.aliases import normalize_name, resolve_player
from database.db import get_connection
from database.repositories import link_source_id, save_game_bundle

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

# esake team id -> (English name, extra Latin aliases, Euroleague club code or None).
# Pages name clubs in Greek capitals ("ΑΕΚ"); the alias resolver only keeps
# Latin letters, so every club needs a Latin name. Clubs that also play in the
# Euroleague are linked to that existing row (one Panathinaikos, not two).
TEAMS = {
    "00000001": ("Panathinaikos", [], "PAN"),
    "00000002": ("Olympiacos", [], "OLY"),
    "00000010": ("AEK Athens", ["AEK", "AEK BC"], None),
    "00000005": ("Aris Thessaloniki", ["Aris"], None),
    "0000000C": ("PAOK", ["PAOK Thessaloniki"], None),
    "0000000D": ("Peristeri", ["Peristeri Betsson"], None),
    "2A25C696": ("Promitheas Patras", ["Promitheas", "Promitheas Vikos Cola"], None),
    "0000000A": ("Kolossos Rhodes", ["Kolossos", "Kolossos H Hotels"], None),
    "0000000F": ("Maroussi", ["Marousi"], None),
    "00000011": ("Iraklis Thessaloniki", ["Iraklis", "Heraklis"], None),
    "B742845D": ("Karditsa", ["Karditsa Iaponiki"], None),
    "BB4B460F": ("Mykonos", ["Mykonos Betsson"], None),
    "3CA10C07": ("Doxa Lefkadas", ["Doxa", "Doxa Lefkadas Elabet"], None),
    "0041ADCB": ("Vikos Falcons", ["Falcons"], None),
    # Only in older seasons:
    "00000003": ("Panionios", ["Panionios Cosmorama Travel"], None),
    "F7D46144": ("Lavrio", ["Lavrio DHI", "Lavrio Megabolt"], None),
}
# esake championship id per season, from the results page's season list.
CHAMPIONSHIPS = {"2026-27": "184645B9", "2025-26": "44B80BEB", "2024-25": "4820C134"}
# esake player id -> Euroleague player id, for the same person spelled
# differently (reviewed by hand after the first 2026-27 load). Not linked on
# purpose: Zeb Jackson (not Edwin Jackson), Jordan Floyd (not Jordan Loyd).
MANUAL_PLAYER_LINKS = {
    "D8361DB3": "P004292",  # Vasilis / Vassilis Toliopoulos
    "5AA30EE4": "P006085",  # Nenad / Neno Dimitrijevic
    "32F994EF": "P012585",  # Matt / Matthew Morgan
    "3793B035": "P012610",  # Tyson Ward (written with a Greek capital Tau on esake)
    "8A20EF38": "P010091",  # Nikos / Nikolaos Plotas
    "2C26B404": "P011085",  # Nikos / Nikolaos Rogkavopoulos
    "4C0E5F4B": "P004451",  # Giannis / Ioannis Kouzeloglou
    "7F6E6DE3": "P007866",  # Kostas / Konstantinos Mitoglou (found by the surname check)
    "52ABD8D7": "P010042",  # Jean Claudio / Jean Montero
    # Written only in Greek letters on the 2024-25 / 2025-26 pages. Matched on
    # the transliterated name, then checked by club and dates: none of them has
    # Euroleague games for another club in the weeks he played in Greece.
    "AB45FD1B": "P008829",  # Alec Peters
    "B8E81349": "P011442",  # Alexandros Samodurov
    "61009549": "P014741",  # Alvaro Cardenas
    "B111BBC9": "P000956",  # Danilo Andjusic
    "8BDDC1DE": "P013250",  # David DeJulius
    "78F9CAB0": "P011194",  # Filip Petrusev
    "3E67D9D0": "P006591",  # Frank Ntilikina
    "AA38A9DC": "P013383",  # Wenyen Gabriel
    "8E3AD877": "P005161",  # Ioannis Papapetrou
    "CCA46AB9": "P011284",  # Kenneth Faried
    "E2C7A697": "P011218",  # Keenan Evans
    "7923E460": "P014220",  # Chris Silva
    "5785DE89": "P009622",  # Lorenzo Brown
    "6781D158": "P007947",  # Luca Vildoza
    "DC12D2F3": "P002328",  # Marius Grigonis
    "9A9260A9": "P013514",  # Nathan Mensah
    "6B0BDE16": "P005353",  # Omer Yurtseven
    "66DCB70A": "P014011",  # Richaun Holmes
    "2D883EB4": "P013389",  # Saben Lee
    "92C27E93": "P012608",  # TJ Shorts
    "6C630C6A": "P008855",  # Thomas Walkup
    "C49389A3": "P012878",  # Moses Wright
    "FC26E3ED": "P007853",  # Shaquielle McKissic
    "EC835FC9": "P007982",  # Nigel Williams-Goss
    "3BB61443": "P014212",  # Monte Morris
}
# esake player id -> the real spelling, for players written only in Greek
# letters who aren't linked to a Euroleague row (transliteration gives
# 'Gkountgouin Tsevez' for Chavez Goodwin).
# Worked out by hand from the Greek spelling and the club; players I couldn't
# identify with confidence keep the transliteration (correct them here).
MANUAL_NAMES = {
    "139F0EBB": "Gabriel Galvanini", "405192E7": "Cameron Reynolds", "2A79ECAD": "Mindaugas Kuzminskas",
    "0B4BA1C7": "Kendrick Ray", "88A8B311": "Nate Watson", "F2C4F5E2": "Andrew Goudelock",
    "D8AAA670": "Marko Pecarski", "2D7697E1": "Prentiss Hubb", "91A60582": "Grant Golden",
    "C33BFBFA": "Hunter Hale", "9B095932": "Elvar Fridriksson", "52E19D5D": "Wes Iwundu",
    "F06E1579": "Rashad Vaughn", "7B8D6590": "Shavar Reynolds", "9FFCB24C": "Dan Akin",
    "6A0C83E7": "Jake VanTubbergen", "2C0B2DE8": "Trevon Allen", "FFF395A0": "Chavez Goodwin",
    "A526A086": "Jarell Eddie", "FC6A957C": "Ryan Woolridge", "BCAE1788": "Devin Davis",
    "4BAE384D": "Brandon Jefferson", "56ACC9C5": "Damien Jefferson", "A65676A5": "CJ Bryce",
    "915AFB05": "Cyril Langevine", "9C923A09": "Josh Roberts", "025BC51F": "Landry Nnoko",
    "53E8F901": "Jacorey Williams", "899F2E06": "Langston Hall", "6E6CF8F4": "Myles Hesson",
    "C8E63EEC": "Brandon Slater", "02211B0A": "Rion Brown", "BD97A63B": "Alexander Madsen",
    "24D310BE": "Kenny Williams", "B992196A": "Taz Sherman", "4517CD53": "Amine Noua",
    "0949B704": "Sam Griffin", "67B12642": "Tobin Carberry", "22136494": "Daryl Macon",
    "7CFC487F": "Simi Shittu", "BA572D1F": "Nick McGlynn", "8FAF270E": "Oleksandr Lypovyy",
    "17958650": "Justin Wright-Foreman", "694251BC": "Shahada Wells", "514FA5E4": "Marcus Keene",
    "B4466CDD": "Sharife Cooper", "645F5095": "Rob Gray", "64243210": "LJ Figueroa",
    "EE4F4DEC": "Kyle Foster", "C46AB3E9": "Chasson Randle", "551A78E6": "Isaiah Reese",
    "558B338D": "Riley Abercrombie", "A93771DC": "Tyler Nelson", "C2B1E86B": "JP Macura",
    "F816BB2E": "Markel Starks", "201D9D5C": "Miroslav Raduljica", "F972EF29": "Steven Enoch",
    "EBD85CCA": "Ahmed Hill", "CEB2F808": "Warren Washington", "00000339": "Patrick Beverley",
    "8DD008E0": "Nate Laszewski", "61FA1CC2": "Josiah Strong", "B3D545F0": "Shakur Juiston",
    "9B3A6626": "Jordan Tucker", "F6449AB4": "Jaylen Hands", "51D6DE99": "Paulius Danusevicius",
    "00047A7D": "Ryan Taylor", "0C71621C": "Marcus Thornton", "2F41166C": "Kendall Smith",
    "B88F05D1": "Jordan Davis", "833F16B8": "Ismael Sanogo", "B1496F92": "Chad Brown",
    "B80002A2": "Donovan Jackson", "4AE48ED4": "Dewan Hernandez", "015493AB": "Darius Perry",
    "A33E8C47": "Mojave King", "0BBCFBD3": "Jeremy Evans", "3BC0C2B5": "Mike Bothwell",
    "779434DD": "Rayjon Tucker", "6DC2622D": "Gorjok Gak", "C39EFC18": "Joshua Obiesie",
    "BD12A9AD": "Josh Adams", "3183C90C": "Jacob Grandison", "6C0472C9": "Tim Lambrecht",
    "DA93B593": "Rawle Alkins", "9EB41ADD": "Keron DeShields", "0C9CD259": "Ronaldo Segu",
}
# Same surname, different people (reviewed): the Antetokounmpo and Kalaitzakis
# brothers, and every common surname (Jones, Smith, Williams, Moore, ...).
# Greek capitals that look like Latin ones. Pages sometimes mix them into
# Latin text: "ΟΤ1" (overtime) and "ΤYSON WARD" both start with Greek letters.
LOOKALIKES = str.maketrans("ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEZHIKMNOPTYX")

# Greek -> Latin (ELOT 743, the Greek passport standard, which the Euroleague
# also uses for Greek players: Rogkavopoulos, Antetokounmpo, Charalampopoulos).
# 2024-25 and 2025-26 pages write many players only in Greek ("ΚΡΙΣ ΚΟΦΙ").
_GREEK_LETTERS = {"Α": "A", "Β": "V", "Γ": "G", "Δ": "D", "Ε": "E", "Ζ": "Z", "Η": "I", "Θ": "TH",
                  "Ι": "I", "Κ": "K", "Λ": "L", "Μ": "M", "Ν": "N", "Ξ": "X", "Ο": "O", "Π": "P",
                  "Ρ": "R", "Σ": "S", "Τ": "T", "Υ": "Y", "Φ": "F", "Χ": "CH", "Ψ": "PS", "Ω": "O"}
# Pairs; the first value is used at the start of a word (ΜΠΡΑΟΥΝ -> BRAOUN).
_GREEK_PAIRS = {"ΟΥ": ("OU", "OU"), "ΑΙ": ("AI", "AI"), "ΕΙ": ("EI", "EI"), "ΟΙ": ("OI", "OI"),
                "ΜΠ": ("B", "MP"), "ΝΤ": ("D", "NT"), "ΓΚ": ("G", "GK"), "ΓΓ": ("NG", "NG"),
                "ΓΞ": ("NX", "NX"), "ΓΧ": ("NCH", "NCH"), "ΤΣ": ("TS", "TS"), "ΤΖ": ("TZ", "TZ")}
_VOICELESS = set("ΘΚΞΠΣΤΦΧΨ")


def _greek_word_to_latin(word):
    """One Greek word in capitals -> Latin capitals. Accents are dropped; a
    diaeresis (Ϊ, Ϋ) keeps the vowel separate (ΤΕΪΛΟΡ -> TEILOR)."""
    letters, i, out = [], 0, []
    for ch in unicodedata.normalize("NFD", word.upper()):
        if ch == "̈" and letters:  # diaeresis: mark the previous vowel
            letters[-1] += "̈"
        elif not unicodedata.combining(ch):
            letters.append(ch)
    while i < len(letters):
        pair = "".join(letters[i:i + 2])
        if pair in _GREEK_PAIRS:
            out.append(_GREEK_PAIRS[pair][0 if i == 0 else 1])
            i += 2
        elif pair in ("ΑΥ", "ΕΥ"):  # AV/EV, but AF/EF before a voiceless consonant or at the end
            nxt = letters[i + 2] if i + 2 < len(letters) else None
            out.append(pair[0].translate(LOOKALIKES) + ("F" if nxt is None or nxt in _VOICELESS else "V"))
            i += 2
        else:
            ch = letters[i].replace("̈", "")
            out.append(_GREEK_LETTERS.get(ch, ch))
            i += 1
    return "".join(out)


def to_latin(text):
    """(Latin text, transliterated?) for a name from a page.

    Pages mix lookalike capitals both ways: "ΤYSON" (Latin, Greek Τ) and
    "AΛEK" (Greek, Latin A and E). A letter only one alphabet has (Λ, Π, Σ / L,
    R, S) decides; a word of lookalikes only goes with most of its letters.
    """
    words, transliterated = [], False
    for word in text.split():
        greek = sum(1 for ch in word if _is_greek(ch))
        latin = sum(1 for ch in word if ch.isascii() and ch.isalpha())
        greek_only = any(_is_greek(ch) and ch.upper() not in _LOOKALIKE_GREEK for ch in word)
        latin_only = any(ch.isascii() and ch.isalpha() and ch.upper() not in _LOOKALIKE_LATIN for ch in word)
        if greek and (greek_only or (not latin_only and greek > latin)):
            words.append(_greek_word_to_latin(word.upper().translate(_LATIN_TO_GREEK)))
            transliterated = True
        else:
            words.append(word.translate(LOOKALIKES))
    return " ".join(words), transliterated


_LOOKALIKE_GREEK, _LOOKALIKE_LATIN = "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEZHIKMNOPTYX"
_LATIN_TO_GREEK = str.maketrans(_LOOKALIKE_LATIN, _LOOKALIKE_GREEK)


def _is_greek(ch):
    return "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿"


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
    transliterated: bool = False  # name was written in Greek letters on the page


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
    # str.title() also capitalizes after an apostrophe or hyphen: De'Vion, Mitrou-Long.
    pretty = [w if w.upper() in NAME_SUFFIXES else w.title() for w in rest + surname_words]
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
            raw, transliterated = to_latin(re.sub(r"^#\d+\s*", "", label))
            surname = to_latin(player_cell["span"])[0] if player_cell["span"] else raw.split()[0]
            box.players.append(Line(_line(row[1:]), name=display_name(raw, surname), raw_name=raw,
                                    transliterated=transliterated,
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


def _page_championship(html):
    """The championship id (hex, as in CHAMPIONSHIPS) of a game page, or None.

    Player links carry it in decimal minus 100,000,000 (checked on all three
    seasons: 1052912363 = 0x44B80BEB - 10**8 for 2025-26).
    """
    found = set(re.findall(r"idchampionship=(\d+)", html))
    if len(found) > 1:
        raise ValueError(f"Page mixes championships {sorted(found)}.")
    return f"{int(found.pop()) + 10**8:08X}" if found else None


def parse_game_page(html, idgame, season):
    """Parse a saved EsakegameView mode=3 page. `season` is e.g. '2026-27' (the page has no year)."""
    # The "Stoiximan GBL 2026-2027" banner is the site header (always the
    # current season), so check the championship in the player links instead.
    page_championship = _page_championship(html)
    if page_championship is not None and page_championship != CHAMPIONSHIPS[season]:
        raise ValueError(f"Game {idgame}: page is for championship {page_championship}, not {season}.")
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
                if len(r) == 3 and re.fullmatch(r"(Q\d|OT\d?)", _text(r[1]).translate(LOOKALIKES))]
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


def build_game_bundle(game, season, phase="regular_season"):
    """An EsakeGame -> a save_game_bundle() dict (same stat names as the Euroleague)."""
    check_game(game)
    appearances, stats = [], []
    for side, box in (("home", game.home), ("away", game.away)):
        for line in box.players:
            if not line.values.get("minutes"):
                continue  # listed but did not play
            player = {"name": line.name, "source_player_id": line.source_player_id,
                      "aliases": [line.raw_name], "transliterated": line.transliterated}
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
        "home_team": _team(game.home),
        "away_team": _team(game.away),
        "start_time": game.start_time,
        "round_number": game.round_number,
        "phase": phase,
        "is_neutral_venue": False,
        "status": "finished",
        "home_score": game.home_score,
        "away_score": game.away_score,
        "appearances": appearances,
        "stats": stats,
    }


def _team(box):
    if box.source_team_id not in TEAMS:
        raise ValueError(f"Unknown esake team {box.source_team_id} ({box.name}): add it to TEAMS.")
    name, aliases, _ = TEAMS[box.source_team_id]
    return {"name": name, "source_team_id": box.source_team_id, "aliases": aliases}


# --- Linking to rows from other sources --------------------------------------------

def link_teams(conn):
    """Link the Greek clubs that also play in the Euroleague to their Euroleague rows."""
    cursor = conn.cursor()
    for esake_id, (_, _, euroleague_code) in TEAMS.items():
        if euroleague_code is None:
            continue
        row = cursor.execute("SELECT id FROM teams WHERE source = 'euroleague' AND source_team_id = ?",
                             (euroleague_code,)).fetchone()
        if row is None:
            raise ValueError(f"No Euroleague team {euroleague_code} to link esake team {esake_id} to.")
        link_source_id(cursor, "team", SOURCE, esake_id, row[0], "manual")
    conn.commit()


def link_players(conn, bundle):
    """Link this game's new esake players to existing players from other sources.

    A link is made only when the esake name exactly matches one player (by
    name or by a full-name alias such as 'NUNN, KENDRICK'). Returns
    (linked, review): linked = [(esake name, player name)], review = esake
    players left unlinked although a similar name exists (a different spelling
    such as Kostas/Konstantinos); add those to MANUAL_PLAYER_LINKS if they match.
    """
    cursor = conn.cursor()
    linked, review = [], []
    for appearance in bundle["appearances"]:
        player = appearance["player"]
        esake_id = player["source_player_id"]
        known = cursor.execute(
            """SELECT 1 FROM source_links WHERE kind = 'player' AND source = ? AND source_id = ?
               UNION SELECT 1 FROM players WHERE source = ? AND source_player_id = ?""",
            (SOURCE, esake_id, SOURCE, esake_id)).fetchone()
        if known:
            continue
        if esake_id in MANUAL_PLAYER_LINKS:
            target = cursor.execute("SELECT id FROM players WHERE source = 'euroleague' AND source_player_id = ?",
                                    (MANUAL_PLAYER_LINKS[esake_id],)).fetchone()
            if target is None:
                raise ValueError(f"MANUAL_PLAYER_LINKS: no Euroleague player {MANUAL_PLAYER_LINKS[esake_id]}.")
            link_source_id(cursor, "player", SOURCE, esake_id, target[0], "manual")
            continue
        names = {normalize_name(player["name"]), *(normalize_name(a) for a in player.get("aliases", []))}
        marks = ", ".join("?" for _ in names)
        matches = cursor.execute(
            f"""SELECT DISTINCT p.id, p.name FROM players p JOIN player_aliases a ON a.player_id = p.id
                WHERE p.source <> ? AND a.normalized IN ({marks})""",
            (SOURCE, *names)).fetchall()
        if len(matches) == 1:
            link_source_id(cursor, "player", SOURCE, esake_id, matches[0][0], "name")
            linked.append((player["name"], matches[0][1]))
        else:
            # Not linked. Flag it if a similar name exists, including the same
            # surname: "Kostas" vs "Konstantinos" Mitoglou is too far apart for
            # the fuzzy check, and a missed link makes the name ambiguous.
            similar = resolve_player(player["name"], conn)
            candidates = {name for _, name in similar.candidates} if similar.status != "not_found" else set()
            surname = normalize_name(player["name"]).split()[-1:]
            candidates |= {row[0] for row in cursor.execute(
                """SELECT DISTINCT p.name FROM players p JOIN player_aliases a ON a.player_id = p.id
                   WHERE p.source <> ? AND a.normalized = ?""", (SOURCE, *surname))} if surname else set()
            if candidates:
                review.append((player["name"], esake_id, sorted(candidates)))
    conn.commit()
    return linked, review


# --- Loading ------------------------------------------------------------------------

def ingest_game(idgame, season, download=True, phase="regular_season"):
    """Save one game (linking its players first). Returns (game id, linked, review)."""
    html = fetch_game_page(idgame, download)
    bundle = build_game_bundle(parse_game_page(html, idgame, season), season, phase)
    conn = get_connection()
    try:
        link_teams(conn)
        linked, review = link_players(conn, bundle)
        _keep_known_spellings(conn, bundle)
    finally:
        conn.close()
    return save_game_bundle(bundle), linked, review


def _keep_known_spellings(conn, bundle):
    """A transliterated name never replaces a saved one, or MANUAL_NAMES.

    Saving a game renames a known player to that game's spelling, so loading
    2024-25 (Greek letters) after 2026-27 would turn 'Chavez Goodwin' back into
    'Gkountgouin Tsavez'. Latin spellings from newer pages still win.
    """
    for appearance in bundle["appearances"]:
        player = appearance["player"]
        if player.pop("transliterated", False):
            if player["source_player_id"] in MANUAL_NAMES:
                player["name"] = MANUAL_NAMES[player["source_player_id"]]
                continue
            row = conn.execute("SELECT name FROM players WHERE source = ? AND source_player_id = ?",
                               (SOURCE, player["source_player_id"])).fetchone()
            if row:
                player["name"] = row[0]


PLAYOFFS = "00000002"  # esake's "Β Φάση" (second phase): quarterfinals, semifinals, finals


def _round_page(championship, series, download=True, playoffs=False):
    """A round's game list, saved as results_<championship>_r<NN>.html.

    With playoffs=True, `series` is a playoff series code ("201" = QF1, "401"
    = final game 1, "" = the phase's index page) and the page is saved as
    results_<championship>_B<series>.html. A saved page is downloaded again
    only while it lists a game that has started more than 3 hours ago but has
    no score yet.
    """
    global _last_request_time
    if playoffs:
        path = RAW_DIR / f"results_{championship}_B{series}.html"
    else:
        path = RAW_DIR / f"results_{championship}_r{series:02d}.html"
    season = next(s for s, c in CHAMPIONSHIPS.items() if c == championship)
    stale = path.exists() and any(
        g["start_passed"] and g["score"] is None for g in list_games(path.read_text(encoding="utf-8"), season))
    if (not path.exists() or stale) and download:
        wait = SECONDS_BETWEEN_REQUESTS - (time.monotonic() - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        response = requests.get("https://www.esake.gr/el/action/EsakeResults", timeout=60, params={
            "idchampionship": championship, "idteam": "", "idseason": PLAYOFFS if playoffs else "",
            "series": series if playoffs else f"{series:02d}"})
        _last_request_time = time.monotonic()
        response.raise_for_status()
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        partial.write_bytes(response.content)
        partial.replace(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} is not cached and download=False.")
    return path.read_text(encoding="utf-8")


def list_games(html, season):
    """[{idgame, score (home, away) or None, start_passed}] for the games on a round page.

    start_passed is True when the game started more than 3 hours ago (so a
    score is final, not a game in progress). `season` gives the year, since
    the page shows only day and month.
    """
    games = []
    for block in html.split('<div class="esake-program-game">')[1:]:
        game_id = re.search(r"EsakegameView\?idgame=(\w+)", block)
        if not game_id:
            continue
        score = re.search(r"(\d+)&nbsp;-&nbsp;(\d+)", block)
        when = re.search(r"clock\.svg[^>]*>\s*(\S+ \d{1,2} \S+) - (\d{1,2}:\d{2})", block)
        start_passed = False
        if when:
            start = datetime.strptime(_start_time(when.group(1), when.group(2), season), "%Y-%m-%dT%H:%M:%SZ")
            now = datetime.now(ZoneInfo("UTC")).replace(tzinfo=None)
            start_passed = (now - start).total_seconds() > 3 * 3600
        games.append({"idgame": game_id.group(1),
                      "score": (int(score.group(1)), int(score.group(2))) if score else None,
                      "start_passed": start_passed})
    return games


def playoff_series(html):
    """Playoff series codes from the second phase's round list: ['201', ..., '405'].

    Only quarterfinals, semifinals, the third-place game and the finals (QF1,
    SF2, 3P, F4 ...). The second phase can also hold the play-out (PO1-PO6,
    the relegation round) and placement games (CF1, CF2), which aren't playoffs.
    """
    return [code for label, code in re.findall(r"append\(new Option\('([^']*)', '(\d+)'\)", html)
            if re.fullmatch(r"(QF|SF|F)\d+|3P", label)]


def _ingest_games(games, season, download, phase, report, skip=frozenset()):
    finished = [g for g in games if g["score"] is not None and g["start_passed"]]
    report["skipped"] += len(games) - len(finished)
    for g in finished:
        if g["idgame"] in skip:
            continue
        try:
            _, linked, review = ingest_game(g["idgame"], season, download, phase)
            report["saved"] += 1
            report["new"].append(g["idgame"])
            report["linked"] += linked
            report["review"] += review
        except Exception as exc:  # keep going; report at the end
            report["failed"].append((g["idgame"], f"{type(exc).__name__}: {exc}"))
    return finished


def ingest_season(season, download=True, verbose=True, max_rounds=40, playoffs=False, only_new=False):
    """Load every finished game of a season, round by round, until a round has none.

    playoffs=True also loads the second phase (one page per series; a series
    page with no finished games, such as an unneeded final game 5, is skipped).
    only_new=True leaves games that are already saved alone (the daily update).
    Returns a report dict: saved, new (ids saved), skipped (not finished),
    failed, linked, review.
    """
    championship = CHAMPIONSHIPS[season]
    report = {"saved": 0, "new": [], "skipped": 0, "failed": [], "linked": [], "review": []}
    skip = frozenset()
    if only_new:
        conn = get_connection()
        try:
            skip = frozenset(row[0] for row in conn.execute("SELECT source_game_id FROM games WHERE source = ?", (SOURCE,)))
        finally:
            conn.close()
    for series in range(1, max_rounds + 1):
        games = list_games(_round_page(championship, series, download), season)
        finished = _ingest_games(games, season, download, "regular_season", report, skip)
        if verbose:
            print(f"round {series:02d}: {len(finished)} finished of {len(games)}", flush=True)
        if not finished:
            break  # the first round with no finished games ends the season so far
    if playoffs:
        for code in playoff_series(_round_page(championship, "", download, playoffs=True)):
            games = list_games(_round_page(championship, code, download, playoffs=True), season)
            finished = _ingest_games(games, season, download, "playoffs", report, skip)
            if verbose:
                print(f"playoff series {code}: {len(finished)} finished of {len(games)}", flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="Load Greek Basket League games from esake.gr.")
    parser.add_argument("--season", required=True, help="Season, e.g. 2026-27 (the pages have no year)")
    parser.add_argument("--game", help="One esake game id, e.g. 51FB1AFE; omit to load the whole season")
    parser.add_argument("--playoffs", action="store_true", help="Also load the playoffs (second phase)")
    args = parser.parse_args()
    if args.game:
        game_id, linked, review = ingest_game(args.game, args.season)
        print(f"Saved esake game {args.game} as games.id={game_id}; linked {linked}; review {review}")
        return
    report = ingest_season(args.season, playoffs=args.playoffs)
    print(f"Saved {report['saved']}, not finished {report['skipped']}, failed {len(report['failed'])}")
    for item in report["failed"]:
        print("FAILED", *item)
    for item in report["linked"]:
        print("LINKED", *item)
    for item in report["review"]:
        print("REVIEW", *item)


if __name__ == "__main__":
    main()
