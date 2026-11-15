"""Rank every player on his stats, and show one player's rank in all of them.

    player_rankings(["points"], season="2026-27")                  # top scorers per game
    player_rankings(["usage_pct", "ts_pct"], season="2026-27", min_games=8)
    player_rankings(["fg3_made"], season="2026-27", per="total", team="Panathinaikos")
    player_profile("Nunn", season="2026-27")                       # every stat, with ranks

Only players with enough games are ranked, so a player with 2 good games
can't top a list. The default (min_games=None) is half the games of the most
active player under the same filters, rounded up. Rank 1 is the highest value,
as for teams. A player who changed teams mid-season is one row; his team
column shows his latest team.
"""
import math
from dataclasses import dataclass, field

from analysis.advanced import (
    ADVANCED_PLAYER_STATS,
    PLAYER_COUNT_STATS,
    count_in_game,
    player_advanced_value,
)
from analysis.rankings import ORDERS, RESULTS, _rank, _totals
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
from database.aliases import resolve_player, resolve_team
from database.db import get_connection

TRADITIONAL_PLAYER_STATS = COUNTING_STATS + tuple(PERCENTAGE_STATS) + RESULT_STATS
RANKABLE_PLAYER_STATS = TRADITIONAL_PLAYER_STATS + tuple(ADVANCED_PLAYER_STATS) + tuple(PLAYER_COUNT_STATS)
PLAYER_PER = ("game", "total", "per40")


@dataclass
class PlayerRow:
    player: str
    player_id: int
    team: str  # latest team in the matching games
    games: int
    minutes_per_game: float
    values: dict = field(default_factory=dict)
    ranks: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    qualified: bool = True  # enough games/minutes to be ranked


@dataclass
class PlayerRankings:
    stats: list
    filters: dict
    per: str
    min_games: int
    players: list = field(default_factory=list)  # ranked players, sorted by the first stat
    ranked_count: int = 0  # ranks are out of this many (limit doesn't change it)
    unranked: int = 0  # players left out by min_games / min_minutes
    all_rows: dict = field(default_factory=dict, repr=False)  # player_id -> row, ranked or not

    def row(self, player_id):
        return self.all_rows.get(player_id)


def player_rankings(
    stats=None,
    season=None,
    phase=None,
    venue=None,
    result=None,
    max_margin=None,
    last_n=None,
    team=None,
    per="game",
    min_games=None,
    min_minutes=None,
    limit=None,
    order="highest",
):
    """Rank every player with enough matching games.

    stats: names from RANKABLE_PLAYER_STATS; None means every one the database has.
    team: only players' games for this team.
    per: "game", "total" or "per40" (per 40 minutes). Percentages, advanced
        stats and counts (double_doubles, ...) ignore it.
    min_games: games needed to be ranked; None = automatic (see module doc).
    min_minutes: minutes per game needed to be ranked.
    limit: return only the first N rows (ranks are still out of everyone ranked).
    order: "highest" (rank 1 = highest value, the default) or "lowest".
    """
    if order not in ORDERS:
        raise QueryError("order must be 'highest' or 'lowest'.")
    all_stats = not stats
    stats = list(stats) if stats else list(RANKABLE_PLAYER_STATS)
    unknown = [s for s in stats if s not in RANKABLE_PLAYER_STATS]
    if unknown:
        raise QueryError(f"Can't rank players on: {', '.join(unknown)}.")
    if len(set(stats)) != len(stats):
        raise QueryError("Each stat may be listed only once.")
    if venue is not None and venue not in ("home", "away", "neutral"):
        raise QueryError("venue must be home, away or neutral.")
    if result is not None and result not in RESULTS:
        raise QueryError("result must be 'won' or 'lost'.")
    for name, value in (("max_margin", max_margin), ("last_n", last_n), ("limit", limit)):
        if value is not None and value < 1:
            raise QueryError(f"{name} must be at least 1.")
    if per not in PLAYER_PER:
        raise QueryError("per must be 'game', 'total' or 'per40'.")
    if min_games is not None and min_games < 0 or min_minutes is not None and min_minutes < 0:
        raise QueryError("min_games and min_minutes can't be negative.")

    filters, where, params = {}, [], []
    conn = get_connection()
    try:
        if team is not None:
            team_id, filters["team"] = _resolve(lambda t: resolve_team(t, conn), team, "team")
            where.append("tg.team_id = ?")
            params.append(team_id)
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
            where.append("tg.team_score > tg.opponent_score" if result == "won"
                         else "tg.team_score < tg.opponent_score")
        if max_margin is not None:
            filters["max_margin"] = max_margin
            where.append("ABS(tg.team_score - tg.opponent_score) <= ?")
            params.append(max_margin)
        if last_n is not None:
            filters["last_n"] = last_n
        where.append("tg.team_score IS NOT NULL AND tg.opponent_score IS NOT NULL")

        stored = {row[0] for row in conn.execute(
            "SELECT DISTINCT stat_name FROM game_stats WHERE entity_type = 'player'")}
        stored_team = {row[0] for row in conn.execute(
            "SELECT DISTINCT stat_name FROM game_stats WHERE entity_type = 'team'")}
        if all_stats:
            stats = [s for s in stats if _available(s, stored, stored_team)]
        missing = [s for s in stats if not _available(s, stored, stored_team)]
        if missing:
            raise QueryError(f"Unknown stat(s) for this database: {', '.join(missing)}.")
        _check_stats(conn, [s for s in stats if s in COUNTING_STATS])

        games = conn.execute(f"""{TEAM_GAMES_CTE},
            picked AS (
                SELECT ga.player_id, ga.started, tg.game_id, tg.team_id, tg.opponent_id,
                       tg.team_score, tg.opponent_score, tg.start_time,
                       ROW_NUMBER() OVER (PARTITION BY ga.player_id ORDER BY tg.start_time DESC) AS recent
                FROM team_games tg
                JOIN game_appearances ga ON ga.game_id = tg.game_id AND ga.team_id = tg.team_id
                WHERE {' AND '.join(where)}
            )
            SELECT p.player_id, pl.name, t.name, p.started, p.game_id, p.team_id, p.opponent_id,
                   p.team_score, p.opponent_score
            FROM picked p JOIN players pl ON pl.id = p.player_id JOIN teams t ON t.id = p.team_id
            WHERE ? IS NULL OR p.recent <= ?
            ORDER BY p.start_time""", params + [last_n, last_n]).fetchall()

        game_ids = sorted({row[4] for row in games})
        marks = ", ".join("?" for _ in game_ids)
        player_rows = conn.execute(
            f"""SELECT game_id, player_id, stat_name, stat_value FROM game_stats
                WHERE entity_type = 'player' AND period = 'match' AND game_id IN ({marks})""",
            game_ids).fetchall() if game_ids else []
        team_rows = conn.execute(
            f"""SELECT game_id, team_id, stat_name, stat_value FROM game_stats
                WHERE entity_type = 'team' AND period = 'match' AND game_id IN ({marks})""",
            game_ids).fetchall() if game_ids else []
    finally:
        conn.close()

    player_games, team_games = {}, {}
    for game_id, player_id, stat_name, value in player_rows:
        player_games.setdefault((game_id, player_id), {})[stat_name] = value
    for game_id, team_id, stat_name, value in team_rows:
        team_games.setdefault((game_id, team_id), {})[stat_name] = value

    per_player = {}
    for player_id, name, team_name, started, game_id, team_id, opponent_id, team_score, opp_score in games:
        entry = per_player.setdefault(player_id, {"name": name, "games": {}, "team": [], "opp": [], "started": {}})
        values = dict(player_games.get((game_id, player_id), {}))
        values["win"] = 1 if team_score > opp_score else 0
        entry["games"][game_id] = values
        entry["team"].append(team_games.get((game_id, team_id), {}))
        entry["opp"].append(team_games.get((game_id, opponent_id), {}))
        entry["started"][game_id] = started
        entry["latest_team"] = team_name  # rows are in date order

    most_games = max((len(e["games"]) for e in per_player.values()), default=0)
    needed = math.ceil(most_games / 2) if min_games is None else min_games

    rows = []
    for player_id, entry in per_player.items():
        values, game_ids = entry["games"], list(entry["games"])
        pl = _totals(values.values())
        tm, op = _totals(entry["team"]), _totals(entry["opp"])
        row = PlayerRow(entry["name"], player_id, entry["latest_team"], len(game_ids),
                        round(pl["minutes"] / len(game_ids), 1))
        row.qualified = len(game_ids) >= needed and (min_minutes is None or row.minutes_per_game >= min_minutes)
        for stat in stats:
            row.summary[stat] = _player_summary(stat, values, game_ids, pl, tm, op, entry["started"], per)
            row.values[stat] = _player_value(row.summary[stat], per, pl)
        rows.append(row)

    ranked = [row for row in rows if row.qualified]
    for stat in stats:
        _rank(ranked, stat, order)
    for row in rows:
        for stat in stats:
            row.ranks.setdefault(stat, None)  # below the minimum: values but no rank
    first = stats[0]
    ranked.sort(key=lambda r: (r.ranks[first] is None, r.ranks[first] or 0, r.player))
    if order == "lowest":
        filters["order"] = "lowest"
    return PlayerRankings(stats=stats, filters=filters, per=per, min_games=needed,
                          players=ranked[:limit] if limit is not None else ranked,
                          ranked_count=len(ranked), unranked=len(rows) - len(ranked),
                          all_rows={row.player_id: row for row in rows})


def _available(stat, stored, stored_team):
    if stat in RESULT_STATS:
        return True
    if stat in PERCENTAGE_STATS:
        return set(sum(PERCENTAGE_STATS[stat], ())) <= stored
    if stat in ADVANCED_PLAYER_STATS:
        # Team-share formulas also read the team's totals.
        return set(ADVANCED_PLAYER_STATS[stat].needs) <= stored and (
            "minutes" not in ADVANCED_PLAYER_STATS[stat].needs or "minutes" in stored_team)
    if stat in PLAYER_COUNT_STATS:
        return True
    return stat in stored


def _player_summary(stat, values, game_ids, pl, tm, op, started, per):
    games = len(game_ids)
    if stat in ADVANCED_PLAYER_STATS:
        return StatSummary(stat, games, value=player_advanced_value(stat, pl, tm, op, games))
    if stat in PLAYER_COUNT_STATS:
        count = sum(count_in_game(stat, values[g], started[g]) for g in game_ids)
        return StatSummary(stat, games, total=count, value=float(count))
    return _summarise(stat, values, game_ids)


def _player_value(summary, per, pl):
    if summary.value is not None:
        return summary.value
    if summary.pct is not None or summary.attempted is not None or summary.wins is not None:
        return summary.pct
    if summary.total is None:
        return None
    if per == "total":
        return round(summary.total, 1)
    if per == "per40":
        return round(40 * summary.total / pl["minutes"], 1) if pl["minutes"] else None
    return round(summary.per_game, 1) if summary.per_game is not None else None


@dataclass
class PlayerProfile:
    rankings: PlayerRankings  # ranks are out of rankings.ranked_count players
    player: PlayerRow  # player.qualified is False when he has too few games: values, no ranks


def player_profile(player, stats=None, **filters):
    """One player's value and rank for every stat, among the players ranked with the same filters."""
    conn = get_connection()
    try:
        player_id, name = _resolve(lambda t: resolve_player(t, conn), player, "player")
    finally:
        conn.close()
    filters.pop("limit", None)
    rankings = player_rankings(stats, **filters)
    row = rankings.row(player_id)
    if row is None:
        raise QueryError(f"{name} has no games matching these filters.")
    return PlayerProfile(rankings, row)
