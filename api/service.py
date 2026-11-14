"""Run a validated QuerySpec, including comparisons. Used by /query, /ask and the eval."""
from dataclasses import asdict

from analysis.stat_query import query_stats
from api.models import ComparisonSide, QueryResponse, QuerySpec


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
