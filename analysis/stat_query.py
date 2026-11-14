"""Answer stat questions from the competition-aware tables.

    query_stats(["fg3_pct"], player="Nunn", last_n=5)
    query_stats(["turnovers", "assists"], player="Jerian Grant", opponent_over_500=True)
    query_stats(["fg3_pct"], player="Jerian Grant", opponent="Real Madrid", venue="away")
    query_stats(["total_rebounds"], team="Panathinaikos", opponent="Zalgiris",
                venue="away", without_player="Cedi Osman")
    query_stats(["points", "fg3_pct"], player="Fournier", with_player="Dorsey")
    query_stats(["win_pct", "turnovers", "fg_pct"], team="Olympiacos", max_rest_hours=48)

Names are resolved through the alias tables. The SQL is always built here from
these arguments, never written by a caller (later, never by the LLM). Filters
are applied first and `last_n` is applied last: "last 5 games against Real"
means the 5 most recent games against Real.
"""
from dataclasses import dataclass, field

from database.aliases import resolve_player, resolve_team
from database.db import get_connection

# Stats stored per game in game_stats. Ingestion must store exactly these
# names (a test checks the Euroleague mapping against this list).
COUNTING_STATS = (
    "points", "minutes",
    "fg2_made", "fg2_attempted", "fg3_made", "fg3_attempted", "ft_made", "ft_attempted",
    "offensive_rebounds", "defensive_rebounds", "total_rebounds",
    "assists", "steals", "turnovers", "blocks", "blocks_against",
    "fouls_committed", "fouls_received", "pir", "plus_minus",
)

# Percentages are computed from summed makes and attempts. Averaging each
# game's percentage would give a 1/1 game the same weight as a 6/14 game.
PERCENTAGE_STATS = {
    "fg2_pct": (("fg2_made",), ("fg2_attempted",)),
    "fg3_pct": (("fg3_made",), ("fg3_attempted",)),
    "ft_pct": (("ft_made",), ("ft_attempted",)),
    "fg_pct": (("fg2_made", "fg3_made"), ("fg2_attempted", "fg3_attempted")),
}

# Computed from the game result rather than stored. Works for teams and for
# players (the record of the player's team in the games he played).
RESULT_STATS = ("win_pct",)

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
    rest_hours: float  # since this team's previous loaded game; None if first
    opponent_rest_hours: float
    values: dict  # stat name -> value; percentages as "made/attempted"; win_pct as "W"/"L"


@dataclass
class StatSummary:
    stat: str
    games: int
    total: float = None
    per_game: float = None
    made: float = None
    attempted: float = None
    pct: float = None  # 0-100, None when there were no attempts
    wins: int = None  # win_pct only
    losses: int = None


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
        count = len(self.games)
        lines = [f"{self.subject} ({conditions}), {count} game{'s' if count != 1 else ''}:"]
        for s in self.summary.values():
            if s.wins is not None:
                lines.append(f"  {s.stat}: {s.wins}-{s.losses} = {s.pct:.1f}%")
            elif s.pct is not None or s.attempted is not None:
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
# rest_hours: hours since this team's previous game in the database (NULL for
# its first game). Only games we have count, so a domestic-league game in
# between is invisible: this is rest between *loaded* games, an upper bound.
TEAM_GAMES_CTE = """
WITH sides AS (
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
),
team_games AS (
    SELECT sides.*,
           ROUND((julianday(start_time) - julianday(
               LAG(start_time) OVER (PARTITION BY team_id ORDER BY start_time)
           )) * 24, 1) AS rest_hours
    FROM sides
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
    with_player=None,
    season=None,
    phase=None,
    max_rest_hours=None,
    min_rest_hours=None,
):
    """Return a QueryResult for one player or one team.

    stats: stored stat names ("assists", "total_rebounds", ...) and/or
        percentages from PERCENTAGE_STATS ("fg3_pct", ...).
    venue: "home", "away" or "neutral" (from the subject's point of view).
    opponent_over_500: only games where the opponent had more wins than
        losses in that season before the game.
    without_player: only games this player missed, in seasons when he played
        for the subject's team or the opponent (so games from before he
        joined do not count). Season-level: after a mid-season move, his old
        team's later games still count as "without" him.
    with_player: only games in which this player appeared (for either team).
    season: season name such as "2024-25". phase: e.g. "regular_season".
    max_rest_hours / min_rest_hours: hours since the subject's team played
        its previous loaded game (e.g. max_rest_hours=48 for two games within
        48 hours). A team's first loaded game has no rest value and is left
        out by these filters. Only loaded competitions count (see above).
    """
    for name, hours in (("max_rest_hours", max_rest_hours), ("min_rest_hours", min_rest_hours)):
        if hours is not None and hours <= 0:
            raise QueryError(f"{name} must be more than 0.")
    if max_rest_hours is not None and min_rest_hours is not None and min_rest_hours > max_rest_hours:
        raise QueryError("min_rest_hours cannot be more than max_rest_hours.")
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
            filters["without_player"] = absent_name
            # Missed this game, AND played for one of these two teams in the
            # same season. Without the second part, every game from before
            # the player joined would count as a game "without" him.
            where.append("""
                NOT EXISTS (SELECT 1 FROM game_appearances x
                            WHERE x.game_id = tg.game_id AND x.player_id = ?)
                AND EXISTS (SELECT 1 FROM game_appearances r JOIN games rg ON rg.id = r.game_id
                            WHERE r.player_id = ? AND rg.season_id = tg.season_id
                              AND r.team_id IN (tg.team_id, tg.opponent_id))""")
            params += [absent_id, absent_id]
        if with_player is not None:
            present_id, filters["with_player"] = _resolve(
                lambda t: resolve_player(t, conn), with_player, "player"
            )
            if without_player is not None and present_id == absent_id:
                raise QueryError("A player cannot be both 'with' and 'without' in the same question.")
            where.append(
                "EXISTS (SELECT 1 FROM game_appearances w WHERE w.game_id = tg.game_id AND w.player_id = ?)"
            )
            params.append(present_id)
        if season is not None:
            filters["season"] = season
            where.append("tg.season_id IN (SELECT id FROM seasons WHERE name = ?)")
            params.append(season)
        if phase is not None:
            filters["phase"] = phase
            where.append("tg.phase = ?")
            params.append(phase)
        if max_rest_hours is not None:
            filters["max_rest_hours"] = max_rest_hours
            where.append("tg.rest_hours <= ?")
            params.append(max_rest_hours)
        if min_rest_hours is not None:
            filters["min_rest_hours"] = min_rest_hours
            where.append("tg.rest_hours >= ?")
            params.append(min_rest_hours)

        sql = f"""{TEAM_GAMES_CTE}
            SELECT tg.game_id, tg.start_time, opp.name, tg.venue, tg.team_score, tg.opponent_score,
                   tg.rest_hours, ot.rest_hours
            FROM {source}
            JOIN teams opp ON opp.id = tg.opponent_id
            JOIN team_games ot ON ot.game_id = tg.game_id AND ot.team_id = tg.opponent_id
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
    for game_id, start_time, opp_name, game_venue, team_score, opp_score, rest, opp_rest in reversed(game_rows):
        # Basketball has no draws, so a game is either a win or a loss.
        values.setdefault(game_id, {})["win"] = 1 if team_score > opp_score else 0
        result.games.append(GameLine(
            game_id, start_time, opp_name, game_venue, team_score, opp_score, rest, opp_rest,
            _game_values(stats, values[game_id]),
        ))
    result.summary = {stat: _summarise(stat, values, [g.game_id for g in result.games]) for stat in stats}
    return result


def _check_stats(conn, stats):
    stored = {row[0] for row in conn.execute("SELECT DISTINCT stat_name FROM game_stats")}
    unknown = [s for s in stats if s not in stored and s not in PERCENTAGE_STATS
               and s not in RESULT_STATS]
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
        if stat == "win_pct":
            shown[stat] = "W" if game_values["win"] else "L"
        elif stat in PERCENTAGE_STATS:
            made_cols, attempt_cols = PERCENTAGE_STATS[stat]
            made = sum(game_values.get(c) or 0 for c in made_cols)
            attempted = sum(game_values.get(c) or 0 for c in attempt_cols)
            shown[stat] = f"{made:g}/{attempted:g}"
        else:
            shown[stat] = game_values.get(stat)
    return shown


def _summarise(stat, values, game_ids):
    games = len(game_ids)
    if stat == "win_pct":
        wins = sum(values[g]["win"] for g in game_ids)
        pct = round(100 * wins / games, 1) if games else None
        return StatSummary(stat, games, pct=pct, wins=wins, losses=games - wins)
    if stat in PERCENTAGE_STATS:
        made_cols, attempt_cols = PERCENTAGE_STATS[stat]
        made = sum(values.get(g, {}).get(c) or 0 for g in game_ids for c in made_cols)
        attempted = sum(values.get(g, {}).get(c) or 0 for g in game_ids for c in attempt_cols)
        pct = round(100 * made / attempted, 1) if attempted else None
        return StatSummary(stat, games, made=made, attempted=attempted, pct=pct)
    total = sum(values.get(g, {}).get(stat) or 0 for g in game_ids)
    per_game = round(total / games, 2) if games else None
    return StatSummary(stat, games, total=total, per_game=per_game)
