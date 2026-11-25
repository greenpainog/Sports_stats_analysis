"""Team and player name aliases, and resolving messy names to database rows.

Every name we know for a team or player ("Real", "Real Madrid", "RMB",
"R. Madrid") is stored in team_aliases / player_aliases in a normalised form
(lowercase, no accents, no punctuation). Resolving a name first looks for an
exact normalised match and then falls back to fuzzy matching for typos.
Ambiguous names ("Hernangomez") are reported as ambiguous instead of guessed.
"""
import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from database.db import get_connection

# Letters that Unicode decomposition does not turn into plain ASCII.
_EXTRA_LETTERS = str.maketrans({
    "đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L",
    "ı": "i", "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
})

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

FUZZY_CUTOFF = 0.85


def normalize_name(text):
    """'Žalgiris  Kaunas' -> 'zalgiris kaunas'; 'R. Madrid' -> 'r madrid'; "Nunn's" -> 'nunn'."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text.translate(_EXTRA_LETTERS))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # A possessive is not part of the name: "Pana's" -> "pana", "Nunn’s" -> "nunn".
    text = re.sub(r"['’]s\b", "", text.lower())
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def player_name_variants(full_name):
    """'Kendrick Nunn' -> ['Kendrick Nunn', 'K. Nunn', 'Nunn'].

    Suffixes are dropped from the short forms: 'Lonnie Walker IV' also gives
    'L. Walker' and 'Walker'. Multi-word surnames stay together ('De Colo').
    """
    parts = full_name.split()
    while len(parts) > 2 and normalize_name(parts[-1]) in NAME_SUFFIXES:
        parts = parts[:-1]
    variants = [full_name]
    if len(parts) >= 2:
        first, last = parts[0], " ".join(parts[1:])
        variants += [f"{first[0]}. {last}", last, " ".join(parts)]
    return variants


_TABLES = {
    "team": ("team_aliases", "team_id", "teams"),
    "player": ("player_aliases", "player_id", "players"),
}


def save_aliases(cursor, kind, entity_id, names, expand=False):
    """Store each name as an alias of one team or player. Duplicates are ignored.

    With expand=True (for a player's 'First Last' display name), the short
    forms 'F. Last' and 'Last' are stored too. Source spellings such as
    'NUNN, KENDRICK' must not be expanded, or 'Kendrick' would become a surname.
    """
    alias_table, id_column, _ = _TABLES[kind]
    if expand:
        names = [variant for name in names for variant in player_name_variants(name)]
    for name in names:
        normalized = normalize_name(name)
        if normalized:
            cursor.execute(
                f"""INSERT OR IGNORE INTO {alias_table} ({id_column}, alias, normalized)
                    VALUES (?, ?, ?)""",
                (entity_id, name, normalized),
            )


@dataclass
class Resolution:
    """Result of resolving a name.

    status: 'match' (exactly one entity), 'ambiguous' or 'not_found'.
    method: 'exact', 'fuzzy' (the spelling was corrected) or 'first_name'
            (players only, a last resort for players known by their first
            name, like 'Juancho'; several players sharing it is 'ambiguous').
    candidates: [(id, name), ...] sorted by name.
    """
    query: str
    status: str
    method: str = None
    candidates: list = field(default_factory=list)

    @property
    def id(self):
        return self.candidates[0][0] if self.status == "match" else None

    @property
    def name(self):
        return self.candidates[0][1] if self.status == "match" else None


def _entities_for(cursor, kind, normalized_names):
    alias_table, id_column, entity_table = _TABLES[kind]
    placeholders = ", ".join("?" for _ in normalized_names)
    rows = cursor.execute(
        f"""SELECT DISTINCT e.id, e.name FROM {alias_table} a
            JOIN {entity_table} e ON e.id = a.{id_column}
            WHERE a.normalized IN ({placeholders})
            ORDER BY e.name""",
        list(normalized_names),
    ).fetchall()
    return [tuple(row) for row in rows]


def _players_by_first_name(cursor, first):
    rows = cursor.execute("SELECT id, name FROM players ORDER BY name").fetchall()
    return [(pid, name) for pid, name in rows if normalize_name(name).split(" ")[0] == first]


def _resolve(kind, text, conn=None):
    own_connection = conn is None
    conn = conn or get_connection()
    try:
        cursor = conn.cursor()
        normalized = normalize_name(text)
        if not normalized:
            return Resolution(text, "not_found")

        found = _entities_for(cursor, kind, [normalized])
        method = "exact"
        if not found:
            alias_table = _TABLES[kind][0]
            known = [row[0] for row in cursor.execute(
                f"SELECT DISTINCT normalized FROM {alias_table}"
            )]
            scored = [
                (difflib.SequenceMatcher(None, normalized, alias).ratio(), alias)
                for alias in known
            ]
            scored = [(score, alias) for score, alias in scored if score >= FUZZY_CUTOFF]
            if scored:
                best = max(score for score, _ in scored)
                # Keep only the aliases that are (nearly) as good as the best one.
                closest = [alias for score, alias in scored if score >= best - 0.02]
                found = _entities_for(cursor, kind, closest)
                method = "fuzzy"

        if not found and kind == "player" and " " not in normalized:
            found = _players_by_first_name(cursor, normalized)
            method = "first_name"

        if not found:
            return Resolution(text, "not_found")
        status = "match" if len(found) == 1 else "ambiguous"
        return Resolution(text, status, method, found)
    finally:
        if own_connection:
            conn.close()


def resolve_team(text, conn=None):
    """Resolve a team name such as 'Real', 'PAO' or 'Žalgiris'."""
    return _resolve("team", text, conn)


def resolve_player(text, conn=None):
    """Resolve a player name such as 'Nunn', 'K. Nunn' or 'Jerrian Grant'."""
    return _resolve("player", text, conn)


def resolve_staff(role, text, conn=None):
    """Resolve a coach or referee name ('Ataman', 'Ergin Ataman', 'Atamn').

    Staff have no alias table: their names come from game_staff. A match is
    the full name, the surname (last word, or last words for 'Garcia
    Gonzalez'), a close spelling, or a first name alone ('Zeljko'). The candidate id is the name's first
    game_staff row; the filter itself goes by the normalized name.
    """
    own_connection = conn is None
    conn = conn or get_connection()
    try:
        normalized = normalize_name(text)
        if not normalized:
            return Resolution(text, "not_found")
        people = conn.execute(
            """SELECT MIN(id), MIN(name), normalized FROM game_staff WHERE role = ?
               GROUP BY normalized ORDER BY MIN(name)""", (role,)).fetchall()
        found, method = [p for p in people if p[2] == normalized], "exact"
        if not found:
            found = [p for p in people if p[2].endswith(" " + normalized)]
        if not found:
            scored = [(difflib.SequenceMatcher(None, normalized, p[2]).ratio(), p) for p in people]
            scored += [(difflib.SequenceMatcher(None, normalized, p[2].split(" ")[-1]).ratio(), p) for p in people]
            scored = [(score, p) for score, p in scored if score >= FUZZY_CUTOFF]
            if scored:
                best = max(score for score, _ in scored)
                found = sorted({p for score, p in scored if score >= best - 0.02}, key=lambda p: p[1])
                method = "fuzzy"
        if not found and " " not in normalized:  # a first name alone: "Zeljko", "Saras"
            found, method = [p for p in people if p[2].split(" ")[0] == normalized], "first_name"
        if not found:
            return Resolution(text, "not_found")
        return Resolution(text, "match" if len(found) == 1 else "ambiguous", method,
                          [(staff_id, name) for staff_id, name, _ in found])
    finally:
        if own_connection:
            conn.close()
