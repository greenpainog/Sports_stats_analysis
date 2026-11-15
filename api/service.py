"""Run a validated QuerySpec, including comparisons. Used by /query, /ask and the eval."""
from dataclasses import asdict

from analysis.advanced import ADVANCED_PLAYER_STATS, ADVANCED_TEAM_STATS
from analysis.player_rankings import player_profile, player_rankings
from analysis.rankings import matchup, team_rankings
from analysis.stat_query import query_stats
from api.models import (
    ComparisonSide,
    MatchupResponse,
    MatchupSpec,
    PlayerProfileResponse,
    PlayerProfileSpec,
    PlayerRankingRow,
    PlayerRankingSpec,
    PlayerRankingsResponse,
    QueryResponse,
    QuerySpec,
    RankingRow,
    RankingSpec,
    RankingsResponse,
)


def _run(spec: QuerySpec) -> QueryResponse:
    result = query_stats(**spec.model_dump(exclude={"compare"}))
    return QueryResponse(
        subject=result.subject,
        subject_type=result.subject_type,
        filters=result.filters,
        games=[asdict(game) for game in result.games],
        summary={stat: asdict(summary) for stat, summary in result.summary.items()},
        answer=result.describe(),
    )


def _value(s):
    """One stat summary as short text: '45.0% (9/20)', '3-2 (60.0%)', '12.4 per game'."""
    if s.games == 0:
        return "no games"
    if s.wins is not None:
        return f"{s.wins}-{s.losses} ({s.pct:.1f}%)"
    if s.attempted is not None:
        return "no attempts" if s.pct is None else f"{s.pct:.1f}% ({s.made:g}/{s.attempted:g})"
    return f"{s.per_game:.1f} per game"


def comparison_text(base: QueryResponse, sides: list[ComparisonSide]) -> str:
    conditions = ", ".join(f"{k}={v}" for k, v in base.filters.items()) or "all games"
    lines = [f"{base.subject} ({conditions}), compared:"]
    for side in sides:
        count = len(side.result.games)
        lines.append(f"  {side.label}: {count} game{'s' if count != 1 else ''}")
    for stat in base.summary:
        parts = [f"{side.label} {_value(side.result.summary[stat])}" for side in sides]
        lines.append(f"  {stat}: " + " | ".join(parts))
    return "\n".join(lines)


def answer_spec(spec: QuerySpec) -> QueryResponse:
    """The result for the shared filters, plus one result per side when comparing.

    Raises QueryError for unknown or ambiguous names, like query_stats.
    """
    response = _run(spec)
    if spec.compare:
        response.comparison = [
            ComparisonSide(label=label, result=_run(side)) for label, side in spec.side_specs()
        ]
        response.answer = comparison_text(response, response.comparison)
    return response


# --- Rankings ----------------------------------------------------------------

def _row(row) -> RankingRow:
    return RankingRow(team=row.team, games=row.games, values=row.values, ranks=row.ranks,
                      summary={stat: asdict(s) for stat, s in row.summary.items()})


def rankings_spec(spec: RankingSpec) -> RankingsResponse:
    r = team_rankings(**spec.model_dump())
    return RankingsResponse(stats=r.stats, filters=r.filters, per=r.per, side=r.side,
                            team_count=len(r.teams), teams=[_row(row) for row in r.teams])


def _cell(value, rank, count, decimals=1):
    shown = "-" if value is None else f"{value:.{decimals}f}"
    return f"{shown} (#{rank}/{count})" if rank is not None else shown


def matchup_text(m) -> str:
    count = len(m.rankings.teams)
    a, b = m.team_a, m.team_b
    width = max(len(stat) for stat in m.rankings.stats)
    lines = [f"{a.team} ({a.games} games) vs {b.team} ({b.games} games), ranks out of {count} teams:"]
    for stat in m.rankings.stats:
        # ft_rate (0.352) and ast_to (1.37) need more than one decimal.
        decimals = ADVANCED_TEAM_STATS[stat].decimals if stat in ADVANCED_TEAM_STATS else 1
        lines.append(f"  {stat:<{width}}  {_cell(a.values[stat], a.ranks[stat], count, decimals):>16}"
                     f"  {_cell(b.values[stat], b.ranks[stat], count, decimals):>16}")
    return "\n".join(lines)


def matchup_spec(spec: MatchupSpec) -> MatchupResponse:
    filters = spec.model_dump(exclude={"team_a", "team_b"})
    m = matchup(spec.team_a, spec.team_b, **filters)
    r = m.rankings
    return MatchupResponse(stats=r.stats, filters=r.filters, per=r.per, side=r.side, team_count=len(r.teams),
                           team_a=_row(m.team_a), team_b=_row(m.team_b), answer=matchup_text(m))


# --- Player rankings ---------------------------------------------------------

def _player_row(row) -> PlayerRankingRow:
    return PlayerRankingRow(player=row.player, team=row.team, games=row.games,
                            minutes_per_game=row.minutes_per_game, qualified=row.qualified,
                            values=row.values, ranks=row.ranks,
                            summary={stat: asdict(s) for stat, s in row.summary.items()})


def _player_decimals(stat):
    return ADVANCED_PLAYER_STATS[stat].decimals if stat in ADVANCED_PLAYER_STATS else 1


def player_rankings_spec(spec: PlayerRankingSpec) -> PlayerRankingsResponse:
    r = player_rankings(**spec.model_dump())
    return PlayerRankingsResponse(stats=r.stats, filters=r.filters, per=r.per, min_games=r.min_games,
                                  ranked_count=r.ranked_count, unranked=r.unranked,
                                  players=[_player_row(row) for row in r.players])


def profile_text(p) -> str:
    row, r = p.player, p.rankings
    head = f"{row.player} ({row.team}), {row.games} games, {row.minutes_per_game:.1f} min per game"
    if not row.qualified:
        head += f": not ranked, needs {r.min_games}+ games"
    lines = [head + f"; ranks out of {r.ranked_count} players:"]
    width = max(len(stat) for stat in r.stats)
    for stat in r.stats:
        lines.append(f"  {stat:<{width}}  {_cell(row.values[stat], row.ranks[stat], r.ranked_count, _player_decimals(stat))}")
    return "\n".join(lines)


def player_profile_spec(spec: PlayerProfileSpec) -> PlayerProfileResponse:
    p = player_profile(**spec.model_dump(exclude={"limit"}))
    r = p.rankings
    return PlayerProfileResponse(stats=r.stats, filters=r.filters, per=r.per, min_games=r.min_games,
                                 ranked_count=r.ranked_count, player=_player_row(p.player), answer=profile_text(p))
