"""Pydantic models for the API.

QuerySpec is the one structured description of a stat question. The API takes
it as input, and in phase 5 the LLM's only job is to produce a valid QuerySpec.
Field descriptions end up in the JSON schema, which the LLM will see.
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analysis.stat_query import COUNTING_STATS, PERCENTAGE_STATS, RESULT_STATS

# Literal[...] from the tuples gives the JSON schema an exact list of allowed
# values, so an unknown stat name is rejected before any SQL runs.
StatName = Literal[COUNTING_STATS + tuple(PERCENTAGE_STATS) + RESULT_STATS]
Venue = Literal["home", "away", "neutral"]
Phase = Literal["regular_season", "play_in", "playoffs", "final_four"]


class Filters(BaseModel):
    """Which games count. Shared by QuerySpec and each side of a comparison."""

    model_config = ConfigDict(extra="forbid")  # unknown fields are an error, not ignored

    opponent: Optional[str] = Field(None, description="Only games against this team.")
    venue: Optional[Venue] = Field(None, description="From the subject's point of view.")
    last_n: Optional[int] = Field(None, ge=1, le=100, description="Most recent N games after other filters.")
    opponent_over_500: bool = Field(
        False, description="Only games where the opponent had a winning record in that season before the game."
    )
    without_player: Optional[str] = Field(
        None,
        description="Only games this player missed, in seasons when he played for either team.",
    )
    with_player: Optional[str] = Field(
        None, description="Only games in which this player appeared (for either team)."
    )
    season: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}$", description="Season such as '2024-25'.")
    phase: Optional[Phase] = None
    max_rest_hours: Optional[float] = Field(
        None, gt=0, le=24 * 365,
        description=(
            "Only games played at most this many hours after the team's previous game, "
            "e.g. 48 for back-to-back weeks. Counts loaded (Euroleague) games only."
        ),
    )
    min_rest_hours: Optional[float] = Field(
        None, gt=0, le=24 * 365,
        description="Only games played at least this many hours after the team's previous game.",
    )


FILTER_FIELDS = tuple(Filters.model_fields)


class CompareSide(Filters):
    """One side of a comparison: only the filters that differ between the sides."""

    label: Optional[str] = Field(None, max_length=60, description="Short name for this side, e.g. 'without Nunn'.")


class QuerySpec(Filters):
    """A stat question about exactly one player or one team.

    With `compare`, the same question is answered for each side: the side's
    filters are added to the shared ones, e.g. 'with Nunn' vs 'without Nunn'.
    """

    stats: list[StatName] = Field(
        min_length=1, max_length=8,
        description=(
            "Stats to report. Percentages (fg3_pct, ...) are computed from makes and attempts; "
            "win_pct is the win-loss record of the subject's team in the matching games."
        ),
    )
    player: Optional[str] = Field(None, description="Player name, e.g. 'Kendrick Nunn' or 'Nunn'.")
    team: Optional[str] = Field(None, description="Team name, e.g. 'Panathinaikos' or 'Real'.")
    compare: Optional[list[CompareSide]] = Field(
        None, min_length=2, max_length=4,
        description=(
            "Only for questions comparing conditions for the same subject ('with X vs without X', "
            "'home vs away'). Each side lists only the filters that differ; shared filters stay above."
        ),
    )

    @model_validator(mode="after")
    def check_combinations(self):
        if (self.player is None) == (self.team is None):
            raise ValueError("Give exactly one of 'player' or 'team'.")
        if len(set(self.stats)) != len(self.stats):
            raise ValueError("Each stat may be listed only once.")
        if (self.min_rest_hours is not None and self.max_rest_hours is not None
                and self.min_rest_hours > self.max_rest_hours):
            raise ValueError("min_rest_hours cannot be more than max_rest_hours.")
        if self.compare:
            self.side_specs()  # raises if a side is empty, repeated or invalid once merged
        return self

    def side_specs(self):
        """[(label, QuerySpec), ...]: one plain spec per side, shared filters plus the side's own."""
        base = self.model_dump(exclude={"compare"})
        sides, seen = [], set()
        for side in self.compare or []:
            own = side.model_dump(exclude={"label"}, exclude_defaults=True)
            if not own:
                raise ValueError("Each compare side needs at least one filter.")
            key = tuple(sorted(own.items()))
            if key in seen:
                raise ValueError("Two compare sides have the same filters.")
            seen.add(key)
            label = side.label or ", ".join(f"{k}={v}" for k, v in own.items())
            try:
                sides.append((label, QuerySpec(**{**base, **own})))
            except ValueError as exc:
                raise ValueError(f"Compare side '{label}' is invalid: {exc}") from exc
        if len({label for label, _ in sides}) != len(sides):
            raise ValueError("Compare sides need different labels.")
        return sides


class GameRow(BaseModel):
    game_id: int
    start_time: str
    opponent: str
    venue: Venue
    team_score: Optional[int]
    opponent_score: Optional[int]
    rest_hours: Optional[float] = Field(description="Hours since this team's previous loaded game.")
    opponent_rest_hours: Optional[float]
    values: dict[str, Optional[float | str]]


class StatSummary(BaseModel):
    stat: str
    games: int
    total: Optional[float] = None
    per_game: Optional[float] = None
    made: Optional[float] = None
    attempted: Optional[float] = None
    pct: Optional[float] = None
    wins: Optional[int] = None
    losses: Optional[int] = None


class QueryResponse(BaseModel):
    subject: str
    subject_type: Literal["player", "team"]
    filters: dict
    games: list[GameRow]
    summary: dict[str, StatSummary]
    answer: str = Field(description="Readable summary of the result.")
    comparison: Optional[list["ComparisonSide"]] = Field(
        None, description="With QuerySpec.compare: the result for each side. The fields above cover all sides' shared filters."
    )


class ComparisonSide(BaseModel):
    label: str
    result: QueryResponse


class Candidate(BaseModel):
    id: int
    name: str


class ResolveResponse(BaseModel):
    query: str
    status: Literal["match", "ambiguous", "not_found"]
    method: Optional[Literal["exact", "fuzzy", "first_name"]] = None
    candidates: list[Candidate]


class ErrorResponse(BaseModel):
    detail: str
    candidates: list[Candidate] = []


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500, examples=["Nunn's 3-point percentage in his last 5 games"])


class AskResponse(BaseModel):
    """The answer to a plain-language question, or why there isn't one."""

    question: str
    status: Literal["answered", "unanswerable", "needs_clarification", "invalid", "error"] = Field(
        description=(
            "answered: result is set. unanswerable: the data can't answer it. "
            "needs_clarification: a name was unknown or ambiguous (see candidates). "
            "invalid: the question couldn't be turned into a valid query. error: the LLM call failed."
        )
    )
    message: str
    spec: Optional[QuerySpec] = Field(None, description="The structured query the question was turned into.")
    result: Optional[QueryResponse] = None
    candidates: list[Candidate] = []


QueryResponse.model_rebuild()  # resolve the forward reference to ComparisonSide
