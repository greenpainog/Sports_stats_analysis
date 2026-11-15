"""Rank every team on its stats, and put two teams side by side.

    team_rankings(["ft_made"], season="2026-27")
    team_rankings(["points", "fg3_pct"], season="2026-27", venue="away", result="won")
    team_rankings(["points"], season="2026-27", side="opponent")   # points allowed
    matchup("Panathinaikos", "Virtus", season="2026-27")           # every stat, both ranks

The same filters apply to every team, so each team is ranked only over its
own matching games (e.g. its 6 away games). Rank 1 is always the highest
value, also for stats where less is better (turnovers, fouls): the caller
decides what "good" means. Teams with the same shown value share a rank.
"""
from dataclasses import dataclass, field

from analysis.advanced import ADVANCED_TEAM_STATS, advanced_value, possessions
from analysis.stat_query import (
    COUNTING_STATS,
    PERCENTAGE_STATS,
    RESULT_STATS,
    TEAM_GAMES_CTE,
    QueryError,
    StatSummary,
    _check_stats,
    _resolve,
    _summarise,
)
from database.aliases import resolve_team
from database.db import get_connection

# Minutes and plus-minus are player stats only; teams always play the whole game.
TEAM_STATS = tuple(s for s in COUNTING_STATS if s not in ("minutes", "plus_minus"))
RANKABLE_TEAM_STATS = TEAM_STATS + tuple(PERCENTAGE_STATS) + RESULT_STATS + tuple(ADVANCED_TEAM_STATS)
RESULTS = ("won", "lost")
SIDES = ("team", "opponent")
PER = ("game", "total")
ORDERS = ("highest", "lowest")
DECIMALS = 1  # values are ranked as shown, so 15.7 and 15.7 tie


@dataclass
class TeamRow:
    team: str
    team_id: int
    games: int
    values: dict = field(default_factory=dict)  # stat -> ranked value (per game, total or pct)
    ranks: dict = field(default_factory=dict)  # stat -> 1 = highest
    summary: dict = field(default_factory=dict)  # stat -> StatSummary (made/attempted, wins, ...)


@dataclass
class Rankings:
    stats: list
    filters: dict
    per: str
    side: str
    teams: list = field(default_factory=list)  # TeamRow, sorted by the first stat's rank

    def row(self, team_id):
        return next((row for row in self.teams if row.team_id == team_id), None)


def _ranked_value(summary, per):
    if summary.value is not None:
        return summary.value
    if summary.pct is not None or summary.attempted is not None or summary.wins is not None:
        return summary.pct  # percentages and win_pct: None when there were no attempts
    value = summary.per_game if per == "game" else summary.total
    return None if value is None else round(value, DECIMALS)


def _totals(games):
    """Sum each stored stat over a team's games (a missing value counts as 0)."""
    totals = {}
    for game in games:
        for stat, value in game.items():
            totals[stat] = totals.get(stat, 0) + (value or 0)
    return _Zero(totals)


class _Zero(dict):
    """Totals where a stat no game recorded is 0, so a formula never hits a KeyError."""

    def __missing__(self, key):
        return 0


def _rank(rows, stat, order="highest"):
    """Competition ranking (1, 2, 2, 4) on the shown value; teams with no value get None.

    order "highest": rank 1 is the highest value; "lowest": rank 1 is the lowest
    (fewest turnovers, best defensive rating).
    """
    sign = -1 if order == "highest" else 1
    ordered = sorted((r for r in rows if r.values[stat] is not None), key=lambda r: sign * r.values[stat])
    for position, row in enumerate(ordered, 1):
        previous = ordered[position - 2] if position > 1 else None
        same = previous is not None and previous.values[stat] == row.values[stat]
        row.ranks[stat] = previous.ranks[stat] if same else position
    for row in rows:
        row.ranks.setdefault(stat, None)


def team_rankings(
    stats=None,
    season=None,
    phase=None,
    venue=None,
    result=None,
    max_margin=None,
    last_n=None,
    per="game",
    side="team",
    order="highest",
):
    """Rank every team that has matching games.

    stats: names from RANKABLE_TEAM_STATS; None means every one the database has.
    venue: "home", "away" or "neutral". result: "won" or "lost".
    max_margin: only games decided by at most this many points (5 = "+-5 games").
    last_n: each team's own N most recent matching games.
    per: "game" (per-game average) or "total". Percentages ignore it.
    side: "team" for the team's own stats, "opponent" for what it allowed
        (e.g. points with side="opponent" are points conceded).
    order: "highest" (rank 1 = highest value, the default) or "lowest".
    """
    if order not in ORDERS:
        raise QueryError("order must be 'highest' or 'lowest'.")
    all_stats = not stats
    stats = list(stats) if stats else [
        s for s in RANKABLE_TEAM_STATS if not (side == "opponent" and s == "win_pct")]
    unknown = [s for s in stats if s not in RANKABLE_TEAM_STATS]
    if unknown:
        raise QueryError(f"Can't rank teams on: {', '.join(unknown)}.")
    if len(set(stats)) != len(stats):
        raise QueryError("Each stat may be listed only once.")
    if venue is not None and venue not in ("home", "away", "neutral"):
        raise QueryError("venue must be home, away or neutral.")
    if result is not None and result not in RESULTS:
        raise QueryError("result must be 'won' or 'lost'.")
    if max_margin is not None and max_margin < 1:
        raise QueryError("max_margin must be at least 1.")
    if last_n is not None and last_n < 1:
        raise QueryError("last_n must be at least 1.")
    if per not in PER:
        raise QueryError("per must be 'game' or 'total'.")
    if side not in SIDES:
        raise QueryError("side must be 'team' or 'opponent'.")
    if side == "opponent" and "win_pct" in stats:
        raise QueryError("win_pct is the team's own record; use side 'team'.")

    filters, where, params = {}, [], []
    if season is not None:
        filters["season"] = season
        where.append("tg.season_id IN (SELECT id FROM seasons WHERE name = ?)")
        params.append(season)
    if phase is not None:
        filters["phase"] = phase
        where.append("tg.phase = ?")
        params.append(phase)
    if venue is not None:
        filters["venue"] = venue
        where.append("tg.venue = ?")
        params.append(venue)
    if result is not None:
        filters["result"] = result
        where.append("tg.team_score > tg.opponent_score" if result == "won" else "tg.team_score < tg.opponent_score")
    if max_margin is not None:
        filters["max_margin"] = max_margin
        where.append("ABS(tg.team_score - tg.opponent_score) <= ?")
        params.append(max_margin)
    if last_n is not None:
        filters["last_n"] = last_n
    # Games without a final score (not played yet) never count.
    where.append("tg.team_score IS NOT NULL AND tg.opponent_score IS NOT NULL")

    sql = f"""{TEAM_GAMES_CTE},
        picked AS (
            SELECT tg.game_id, tg.team_id, tg.opponent_id, tg.team_score, tg.opponent_score,
                   ROW_NUMBER() OVER (PARTITION BY tg.team_id ORDER BY tg.start_time DESC) AS recent
            FROM team_games tg WHERE {' AND '.join(where)}
        )
        SELECT p.game_id, p.team_id, p.opponent_id, p.team_score, p.opponent_score, t.name
        FROM picked p JOIN teams t ON t.id = p.team_id
        WHERE ? IS NULL OR p.recent <= ?"""
    params += [last_n, last_n]

    conn = get_connection()
    try:
        if all_stats:
            stored = {row[0] for row in conn.execute(
                "SELECT DISTINCT stat_name FROM game_stats WHERE entity_type = 'team'")}
            stats = [s for s in stats if s in stored or s in RESULT_STATS or (
                s in PERCENTAGE_STATS and set(sum(PERCENTAGE_STATS[s], ())) <= stored) or (
                s in ADVANCED_TEAM_STATS and set(ADVANCED_TEAM_STATS[s].needs) <= stored)]
        # An advanced stat is checked through the stored stats its formula reads.
        needed = {need for s in stats if s in ADVANCED_TEAM_STATS for need in ADVANCED_TEAM_STATS[s].needs}
        _check_stats(conn, sorted(needed | {s for s in stats if s not in RESULT_STATS
                                            and s not in ADVANCED_TEAM_STATS}))
        games = conn.execute(sql, params).fetchall()
        # Both sides' team rows for the matching games (at most ~850 ids, under SQLite's limit).
        game_ids = sorted({row[0] for row in games})
        stat_rows = conn.execute(
            f"""SELECT game_id, team_id, stat_name, stat_value FROM game_stats
                WHERE entity_type = 'team' AND period = 'match'
                  AND game_id IN ({', '.join('?' for _ in game_ids)})""",
            game_ids,
        ).fetchall() if game_ids else []
    finally:
        conn.close()

    by_side = {}
    for game_id, team_id, stat_name, value in stat_rows:
        by_side.setdefault((game_id, team_id), {})[stat_name] = value

    per_team, other_side, names = {}, {}, {}
    for game_id, team_id, opponent_id, team_score, opponent_score, name in games:
        names[team_id] = name
        rated, other = (team_id, opponent_id) if side == "team" else (opponent_id, team_id)
        values = dict(by_side.get((game_id, rated), {}))
        values["win"] = 1 if team_score > opponent_score else 0
        per_team.setdefault(team_id, {})[game_id] = values
        other_side.setdefault(team_id, {})[game_id] = by_side.get((game_id, other), {})

    rows = []
    for team_id, values in per_team.items():
        game_ids = list(values)
        row = TeamRow(names[team_id], team_id, len(game_ids))
        own = _totals(values.values())
        opp = _totals(other_side[team_id].values())
        for stat in stats:
            if stat in ADVANCED_TEAM_STATS:
                value = advanced_value(stat, own, opp, len(game_ids))
                if stat == "possessions" and per == "total" and value is not None:
                    value = round(possessions(own, opp), 1)
                row.summary[stat] = StatSummary(stat, len(game_ids), value=value)
            else:
                row.summary[stat] = _summarise(stat, values, game_ids)
            row.values[stat] = _ranked_value(row.summary[stat], per)
        rows.append(row)
    for stat in stats:
        _rank(rows, stat, order)
    first = stats[0]
    rows.sort(key=lambda r: (r.ranks[first] is None, r.ranks[first] or 0, r.team))
    if order == "lowest":
        filters["order"] = "lowest"
    return Rankings(stats=stats, filters=filters, per=per, side=side, teams=rows)



@dataclass
class Matchup:
    rankings: Rankings  # every team, so ranks are out of len(rankings.teams)
    team_a: TeamRow
    team_b: TeamRow


def matchup(team_a, team_b, stats=None, **filters):
    """Two teams side by side: each one's value and league rank for every stat.

    Takes the same filters as team_rankings (season, venue, side, ...).
    """
    conn = get_connection()
    try:
        a_id, a_name = _resolve(lambda t: resolve_team(t, conn), team_a, "team")
        b_id, b_name = _resolve(lambda t: resolve_team(t, conn), team_b, "team")
    finally:
        conn.close()
    if a_id == b_id:
        raise QueryError("A matchup needs two different teams.")
    rankings = team_rankings(stats, **filters)
    rows = []
    for team_id, name in ((a_id, a_name), (b_id, b_name)):
        row = rankings.row(team_id)
        if row is None:
            raise QueryError(f"{name} has no games matching these filters.")
        rows.append(row)
    return Matchup(rankings, *rows)
