"""Run a validated QuerySpec, including comparisons. Used by /query, /ask and the eval."""
import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from analysis.advanced import ADVANCED_PLAYER_STATS, ADVANCED_TEAM_STATS
from analysis.lineups import lineup_rankings
from analysis.player_rankings import player_profile, player_rankings
from analysis.rankings import matchup, team_rankings
from analysis.stat_query import COMPETITIONS, QueryError, _resolve, query_stats
from database.aliases import resolve_team
from api.models import (
    ComparisonSide,
    LineupRow,
    LineupSpec,
    LineupsResponse,
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
        notes=result.notes,
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
    if s.value is not None and s.total is not None:  # on-floor: the total and per 40 minutes say more
        sign = "+" if s.stat == "plus_minus" else ""
        return f"{s.total:{sign}g} total ({s.value:{sign}.1f} per 40 min)"
    if s.stat == "minutes":
        return f"{s.per_game:.1f} per game ({s.total:g} total)"
    return f"{s.per_game:.1f} per game"


def comparison_text(base: QueryResponse, sides: list[ComparisonSide]) -> str:
    conditions = ", ".join(f"{k}={v}" for k, v in base.filters.items()) or "all games"
    lines = [f"{base.subject} ({conditions}), compared:"]
    for side in sides:
        count = len(side.result.games)
        lines.append(f"  {side.label}: {count} game{'s' if count != 1 else ''}")
        lines += [f"    note: {note}" for note in side.result.notes]
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


# --- Ranking questions from /ask ---------------------------------------------

def _filters_text(filters):
    return ", ".join(f"{k}={v}" for k, v in filters.items()) or "all games"


def team_rankings_text(r: RankingsResponse, focus_team=None) -> str:
    """Where one team ranks in each stat, or the full table for the first stat."""
    count = r.team_count
    head = f"Team rankings ({_filters_text(r.filters)}; per {r.per}"
    head += ", what teams allowed" if r.side == "opponent" else ""
    if not r.teams:
        return head + "): no games match."
    if focus_team is not None:
        team_id, name = _resolve(resolve_team, focus_team, "team")
        row = next((t for t in r.teams if t.team == name), None)
        if row is None:
            return f"{head}): {name} has no games matching these filters."
        lines = [f"{head}), {name} ({row.games} games), ranks out of {count} teams:"]
        for stat in r.stats:
            leader = min((t for t in r.teams if t.ranks[stat] is not None), key=lambda t: t.ranks[stat], default=None)
            best = f"   (#1 {leader.team} {_shown(leader.values[stat], stat)})" if leader and leader.team != name else ""
            lines.append(f"  {stat}: {_cell(row.values[stat], row.ranks[stat], count, _decimals(stat))}{best}")
        return "\n".join(lines)
    stat = r.stats[0]
    lines = [f"{head}), by {stat}:"]
    lines += [f"  {t.ranks[stat] or '-':>2}. {t.team}  {_shown(t.values[stat], stat)}" for t in r.teams]
    if len(r.stats) > 1:
        lines.append(f"  (Ranks for {', '.join(r.stats[1:])} are in the result.)")
    return "\n".join(lines)


def player_rankings_text(r: PlayerRankingsResponse) -> str:
    stat = r.stats[0]
    lines = [f"Player rankings by {stat} ({_filters_text(r.filters)}; per {r.per}; "
             f"{r.min_games}+ games, out of {r.ranked_count} players):"]
    for p in r.players:
        lines.append(f"  {p.ranks[stat] or '-':>3}. {p.player} ({p.team})  {_shown(p.values[stat], stat)}"
                     f"  [{p.games} games]")
    return "\n".join(lines)


def _decimals(stat):
    for table in (ADVANCED_TEAM_STATS, ADVANCED_PLAYER_STATS):
        if stat in table:
            return table[stat].decimals
    return 1


def _shown(value, stat):
    return "-" if value is None else f"{value:.{_decimals(stat)}f}"


# --- Lineups ------------------------------------------------------------------

def lineups_spec(spec: LineupSpec) -> LineupsResponse:
    r = lineup_rankings(**spec.model_dump())
    rows = [LineupRow(players=l.players, games=l.games, minutes=round(l.minutes, 2), points_for=l.points_for,
                      points_against=l.points_against, plus_minus=l.plus_minus,
                      net_per_40=round(l.per_40(l.plus_minus), 1), small_sample=l.small_sample)
            for l in r.lineups]
    return LineupsResponse(team=r.team, size=r.size, sort=r.sort, order=r.order, filters=r.filters, games=r.games,
                           lineup_count=r.lineup_count, lineups=rows, notes=r.notes, answer=r.describe())


def answer_ranking(request):
    """Run a RankingRequest from /ask. Returns (message, response model)."""
    if request.kind == "lineups":
        result = lineups_spec(request.spec)
        return result.answer, result
    if request.kind == "team_rankings":
        result = rankings_spec(request.spec)
        return team_rankings_text(result, request.focus_team), result
    if request.kind == "matchup":
        result = matchup_spec(request.spec)
        return result.answer, result
    if request.kind == "player_rankings":
        result = player_rankings_spec(request.spec)
        return player_rankings_text(result), result
    result = player_profile_spec(request.spec)
    return result.answer, result


# --- This season with few or no games: fall back on last season ---------------
# A question with no season or time span means this season (the parser and the
# agent fill in the current season). Early in a season, or for a matchup that
# hasn't been played yet, that can mean no games or only a handful. Then:
#   0 games          -> answer with last season instead, and say so;
#   1 to FEW_GAMES-1 -> answer this season, flagged as a small sample, and add
#                       last season next to it.
# Not when the user named the season ("this season", "2026-27"): then they get
# exactly what they asked for.

FEW_GAMES = 5

# Words that show the user chose the season or time span themselves. Typos of
# "season" ("his season") still contain it.
_SEASON_WORDS = re.compile(
    r"\bseasons?\b|\bszn\b|\byears?\b|\b(?:19|20)\d\d\b|\b\d\d\s*[/-]\s*\d\d\b|\bso far\b|\bcurrent(?:ly)?\b",
    re.IGNORECASE)


def names_season(question: str) -> bool:
    """True when the question itself names a season or year ("this season", "2025-26", "so far")."""
    return bool(_SEASON_WORDS.search(question or ""))


def previous_season(season, competition="euroleague"):
    """Last season's name when `season` is the competition's current (latest loaded) one, else None."""
    from llm.question_parser import loaded_seasons

    seasons = loaded_seasons(COMPETITIONS.get(competition, "E"))
    if len(seasons) < 2 or season != seasons[-1]:
        return None
    return seasons[-2]


@dataclass
class SeasonAnswer:
    text: str
    result: Any  # the response model `text` describes (last season's, when it replaced this one)
    previous_result: Any = None  # last season's, when it was added next to this season's
    fallback: Optional[str] = None  # None, "previous_season" (instead) or "with_previous_season" (as well)
    season: Optional[str] = None
    previous: Optional[str] = None


def _plural(n, word="game"):
    return f"{n} {word}{'s' if n != 1 else ''}"


def _instead(season, previous, earlier_text, earlier):
    return SeasonAnswer(f"No matching games in {season} yet, so this is last season ({previous}):\n{earlier_text}",
                        earlier, fallback="previous_season", season=season, previous=previous)


def with_season_fallback(run: Callable, count: Callable, season, competition, season_named) -> SeasonAnswer:
    """run(None for the season as asked, or a season name) -> (text, result); count(result) -> games."""
    previous = None if season_named else previous_season(season, competition)
    if previous is None:
        text, result = run(None)
        return SeasonAnswer(text, result)
    try:
        text, result = run(None)
    except QueryError as exc:  # e.g. a matchup or profile with no games this season yet
        try:
            earlier_text, earlier = run(previous)
        except QueryError:
            raise exc
        if not count(earlier):
            raise exc
        return _instead(season, previous, earlier_text, earlier)
    games = count(result)
    if games >= FEW_GAMES:
        return SeasonAnswer(text, result)
    try:
        earlier_text, earlier = run(previous)
    except QueryError:
        return SeasonAnswer(text, result)
    if not count(earlier):
        return SeasonAnswer(text, result)
    if games == 0:
        return _instead(season, previous, earlier_text, earlier)
    text = (f"SMALL SAMPLE: only {_plural(games)} in {season} so far, so last season is shown too.\n"
            f"This season ({season}):\n{text}\n\nLast season ({previous}), for context:\n{earlier_text}")
    return SeasonAnswer(text, result, earlier, "with_previous_season", season, previous)


def _sides_set_season(spec: QuerySpec):
    return any(side.season or side.seasons or side.last_n for side in spec.compare or [])


def answer_spec_for_question(spec: QuerySpec, season_named: bool) -> SeasonAnswer:
    """answer_spec with the this-season fallback (see FEW_GAMES)."""
    def run(season):
        response = answer_spec(spec if season is None else spec.model_copy(update={"season": season}))
        return response.answer, response

    return with_season_fallback(run, lambda r: len(r.games), spec.season, spec.competition,
                                season_named or bool(spec.last_n) or _sides_set_season(spec))


def _ranking_games(kind, result, focus_team=None):
    """How many games a ranking answer rests on, for the fallback."""
    if kind == "team_rankings":
        if focus_team is not None:
            _, name = _resolve(resolve_team, focus_team, "team")
            return next((t.games for t in result.teams if t.team == name), 0)
        return max((t.games for t in result.teams), default=0)
    if kind == "matchup":
        return min(result.team_a.games, result.team_b.games)
    if kind == "player_rankings":
        return max((p.games for p in result.players), default=0)
    if kind == "player_profile":
        return result.player.games
    return result.games  # lineups


def answer_ranking_for_question(request, season_named: bool) -> SeasonAnswer:
    """answer_ranking with the this-season fallback."""
    focus = getattr(request, "focus_team", None)

    def run(season):
        if season is None:
            return answer_ranking(request)
        changed = type(request)(request.kind, request.spec.model_copy(update={"season": season}), focus)
        return answer_ranking(changed)

    spec = request.spec
    named = season_named or bool(getattr(spec, "seasons", None)) or bool(getattr(spec, "last_n", None))
    return with_season_fallback(run, lambda r: _ranking_games(request.kind, r, focus),
                                spec.season, spec.competition, named)
