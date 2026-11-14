"""Answer stat questions from the competition-aware tables.

    query_stats(["fg3_pct"], player="Nunn", last_n=5)
    query_stats(["turnovers", "assists"], player="Jerian Grant", opponent_over_500=True)
    query_stats(["fg3_pct"], player="Jerian Grant", opponent="Real Madrid", venue="away")
    query_stats(["total_rebounds"], team="Panathinaikos", opponent="Zalgiris",
                venue="away", without_player="Cedi Osman")

Names are resolved through the alias tables. The SQL is always built here from
these arguments, never written by a caller (later, never by the LLM). Filters
are applied first and `last_n` is applied last: "last 5 games against Real"
means the 5 most recent games against Real.
"""
from dataclasses import dataclass, field

from database.aliases import resolve_player, resolve_team
from database.db import get_connection

# Percentages are computed from summed makes and attempts. Averaging each
# game's percentage would give a 1/1 game the same weight as a 6/14 game.
PERCENTAGE_STATS = {
    "fg2_pct": (("fg2_made",), ("fg2_attempted",)),
    "fg3_pct": (("fg3_made",), ("fg3_attempted",)),
    "ft_pct": (("ft_made",), ("ft_attempted",)),
    "fg_pct": (("fg2_made", "fg3_made"), ("fg2_attempted", "fg3_attempted")),
}

VENUES = ("home", "away", "neutral")


class QueryError(ValueError):
    """The question cannot be answered as asked (unknown name, bad filter, ...).

    `candidates` lists possible matches when a name was ambiguous.
    """

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class GameLine:
    game_id: int
    start_time: str
    opponent: str
    venue: str
    team_score: int
    opponent_score: int
    values: dict  # stat name -> value; percentages as "made/attempted"


@dataclass
class StatSummary:
    stat: str
    games: int
    total: float = None
    per_game: float = None
    made: float = None
    attempted: float = None
    pct: float = None  # 0-100, None when there were no attempts


@dataclass
class QueryResult:
    subject: str
    subject_type: str  # "player" or "team"
    filters: dict
    games: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)  # stat -> StatSummary

    def describe(self):
        """One readable line per stat, e.g. 'fg3_pct: 7/18 = 38.9% over 5 games'."""
        conditions = ", ".join(f"{k}={v}" for k, v in self.filters.items()) or "all games"
        if not self.games:
            return f"{self.subject}: no games match ({conditions})."
        lines = [f"{self.subject} ({conditions}), {len(self.games)} games:"]
        for s in self.summary.values():
            if s.pct is not None or s.attempted is not None:
                pct = "n/a" if s.pct is None else f"{s.pct:.1f}%"
                lines.append(f"  {s.stat}: {s.made:g}/{s.attempted:g} = {pct}")
            else:
                lines.append(f"  {s.stat}: {s.per_game:.1f} per game ({s.total:g} total)")
        return "\n".join(lines)


def _resolve(resolver, text, label):
    result = resolver(text)
    if result.status == "match":
        return result.id, result.name
    if result.status == "ambiguous":
        names = ", ".join(name for _, name in result.candidates)
        raise QueryError(f"'{text}' could mean several {label}s: {names}.", result.candidates)
    raise QueryError(f"No {label} named '{text}' in the database.")


# One row per team per game. Neutral-venue games are neither home nor away.
TEAM_GAMES_CTE = """
WITH team_games AS (
    SELECT g.id AS game_id, g.season_id, g.start_time, g.phase,
           g.home_team_id AS team_id, g.away_team_id AS opponent_id,
           CASE WHEN g.is_neutral_venue = 1 THEN 'neutral' ELSE 'home' END AS venue,
           g.home_score AS team_score, g.away_score AS opponent_score
    FROM games g
    UNION ALL
    SELECT g.id, g.season_id, g.start_time, g.phase,
           g.away_team_id, g.home_team_id,
           CASE WHEN g.is_neutral_venue = 1 THEN 'neutral' ELSE 'away' END,
           g.away_score, g.home_score
    FROM games g
)
"""

# The opponent's wins/losses in the same season from games that started
# before this one: their record "as of the game date".
OPPONENT_RECORD_SQL = """
    (SELECT COUNT(*) FROM team_games p
     WHERE p.team_id = tg.opponent_id AND p.season_id = tg.season_id
       AND p.start_time < tg.start_time AND p.team_score {op} p.opponent_score)
"""


def query_stats(
    stats,
    player=None,
    team=None,
    opponent=None,
    venue=None,
    last_n=None,
    opponent_over_500=False,
    without_player=None,
    season=None,
    phase=None,
):
    """Return a QueryResult for one player or one team.

    stats: stored stat names ("assists", "total_rebounds", ...) and/or
        percentages from PERCENTAGE_STATS ("fg3_pct", ...).
    venue: "home", "away" or "neutral" (from the subject's point of view).
    opponent_over_500: only games where the opponent had more wins than
        losses in that season before the game.
    without_player: only games in which this player did not appear.
    season: season name such as "2024-25". phase: e.g. "regular_season".
    """
    if (player is None) == (team is None):
        raise QueryError("Ask about exactly one player or one team.")
    if venue is not None and venue not in VENUES:
        raise QueryError(f"venue must be one of {', '.join(VENUES)}.")
    if last_n is not None and last_n < 1:
        raise QueryError("last_n must be at least 1.")
    if not stats:
        raise QueryError("Ask for at least one stat.")

    conn = get_connection()
    try:
        _check_stats(conn, stats)
        filters, where, params = {}, [], []

        if player is not None:
            subject_id, subject = _resolve(lambda t: resolve_player(t, conn), player, "player")
            subject_type = "player"
            source = "team_games tg JOIN game_appearances ga ON ga.game_id = tg.game_id AND ga.team_id = tg.team_id"
            where.append("ga.player_id = ?")
        else:
            subject_id, subject = _resolve(lambda t: resolve_team(t, conn), team, "team")
            subject_type = "team"
            source = "team_games tg"
            where.append("tg.team_id = ?")
        params.append(subject_id)

        if opponent is not None:
            opponent_id, filters["opponent"] = _resolve(lambda t: resolve_team(t, conn), opponent, "team")
            where.append("tg.opponent_id = ?")
            params.append(opponent_id)
        if venue is not None:
            filters["venue"] = venue
            where.append("tg.venue = ?")
            params.append(venue)
        if opponent_over_500:
            filters["opponent_over_500"] = True
            where.append(f"{OPPONENT_RECORD_SQL.format(op='>')} > {OPPONENT_RECORD_SQL.format(op='<')}")
        if without_player is not None:
            absent_id, absent_name = _resolve(lambda t: resolve_player(t, conn), without_player, "player")
            filters["without"] = absent_name
            where.append(
                "NOT EXISTS (SELECT 1 FROM game_appearances x WHERE x.game_id = tg.game_id AND x.player_id = ?)"
            )
            params.append(absent_id)
        if season is not None:
            filters["season"] = season
            where.append("tg.season_id IN (SELECT id FROM seasons WHERE name = ?)")
            params.append(season)
        if phase is not None:
            filters["phase"] = phase
            where.append("tg.phase = ?")
            params.append(phase)

        sql = f"""{TEAM_GAMES_CTE}
            SELECT tg.game_id, tg.start_time, opp.name, tg.venue, tg.team_score, tg.opponent_score
            FROM {source}
            JOIN teams opp ON opp.id = tg.opponent_id
            WHERE {' AND '.join(where)}
            ORDER BY tg.start_time DESC"""
        if last_n is not None:
            filters["last_n"] = last_n
            sql += " LIMIT ?"
            params.append(last_n)

        game_rows = conn.execute(sql, params).fetchall()
        values = _load_values(conn, subject_type, subject_id, [row[0] for row in game_rows])
    finally:
        conn.close()

    result = QueryResult(subject=subject, subject_type=subject_type, filters=filters)
    for game_id, start_time, opp_name, game_venue, team_score, opp_score in reversed(game_rows):
        result.games.append(GameLine(
            game_id, start_time, opp_name, game_venue, team_score, opp_score,
            _game_values(stats, values.get(game_id, {})),
        ))
    result.summary = {stat: _summarise(stat, values, [g.game_id for g in result.games]) for stat in stats}
    return result


def _check_stats(conn, stats):
    stored = {row[0] for row in conn.execute("SELECT DISTINCT stat_name FROM game_stats")}
    unknown = [s for s in stats if s not in stored and s not in PERCENTAGE_STATS]
    if unknown:
        raise QueryError(f"Unknown stat(s): {', '.join(unknown)}.")


def _load_values(conn, subject_type, subject_id, game_ids):
    """{game_id: {stat_name: value}} for the subject in the given games."""
    if not game_ids:
        return {}
    column = "player_id" if subject_type == "player" else "team_id"
    placeholders = ", ".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""SELECT game_id, stat_name, stat_value FROM game_stats
            WHERE entity_type = ? AND {column} = ? AND period = 'match'
              AND game_id IN ({placeholders})""",
        [subject_type, subject_id, *game_ids],
    ).fetchall()
    values = {}
    for game_id, stat_name, value in rows:
        values.setdefault(game_id, {})[stat_name] = value
    return values


def _game_values(stats, game_values):
    shown = {}
    for stat in stats:
        if stat in PERCENTAGE_STATS:
            made_cols, attempt_cols = PERCENTAGE_STATS[stat]
            made = sum(game_values.get(c) or 0 for c in made_cols)
            attempted = sum(game_values.get(c) or 0 for c in attempt_cols)
            shown[stat] = f"{made:g}/{attempted:g}"
        else:
            shown[stat] = game_values.get(stat)
    return shown


def _summarise(stat, values, game_ids):
    games = len(game_ids)
    if stat in PERCENTAGE_STATS:
        made_cols, attempt_cols = PERCENTAGE_STATS[stat]
        made = sum(values.get(g, {}).get(c) or 0 for g in game_ids for c in made_cols)
        attempted = sum(values.get(g, {}).get(c) or 0 for g in game_ids for c in attempt_cols)
        pct = round(100 * made / attempted, 1) if attempted else None
        return StatSummary(stat, games, made=made, attempted=attempted, pct=pct)
    total = sum(values.get(g, {}).get(stat) or 0 for g in game_ids)
    per_game = round(total / games, 2) if games else None
    return StatSummary(stat, games, total=total, per_game=per_game)
