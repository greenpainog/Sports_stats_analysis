"""Pydantic models for the API.

QuerySpec is the one structured description of a stat question. The API takes
it as input, and in phase 5 the LLM's only job is to produce a valid QuerySpec.
Field descriptions end up in the JSON schema, which the LLM will see.
"""
import json
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


from analysis.player_rankings import RANKABLE_PLAYER_STATS
from analysis.rankings import RANKABLE_TEAM_STATS
from analysis.stat_query import COUNTING_STATS, PERCENTAGE_STATS, RESULT_STATS

# Literal[...] from the tuples gives the JSON schema an exact list of allowed
# values, so an unknown stat name is rejected before any SQL runs.
StatName = Literal[COUNTING_STATS + tuple(PERCENTAGE_STATS) + RESULT_STATS]
CountingStatName = Literal[COUNTING_STATS]
Venue = Literal["home", "away", "neutral"]
Competition = Literal["euroleague", "eurocup", "greek_league"]
COMPETITION_DESCRIPTION = (
    "Which league's games: 'euroleague' (default), 'eurocup' or 'greek_league' (Stoiximan GBL). "
    "A player or club in several leagues is one subject; this picks the games."
)
SeasonName = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}$")]
Phase = Literal["regular_season", "play_in", "playoffs", "final_four"]


class PlayerThreshold(BaseModel):
    """Only games in which this player played and his stat in that game was in range."""

    model_config = ConfigDict(extra="forbid")

    player: str = Field(description="The player whose game stat decides which games count, e.g. 'Lessort'.")
    stat: CountingStatName = Field(description="A per-game stat of that player, e.g. 'minutes' or 'points'.")
    min: Optional[float] = Field(None, ge=0, description="At least this much in the game (inclusive).")
    max: Optional[float] = Field(None, ge=0, description="At most this much in the game (inclusive).")

    @model_validator(mode="after")
    def check_range(self):
        if self.min is None and self.max is None:
            raise ValueError("player_threshold needs a min, a max or both.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("player_threshold min cannot be more than max.")
        return self


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
    against_player: Optional[str] = Field(
        None, description="Only games in which this player played for the opponent, e.g. 'against Theis'."
    )
    competition: Competition = Field("euroleague", description=COMPETITION_DESCRIPTION)
    season: Optional[SeasonName] = Field(None, description="Season such as '2024-25'.")
    seasons: Optional[list[SeasonName]] = Field(
        None, min_length=2, max_length=10,
        description="Several seasons at once, e.g. ['2024-25', '2025-26', '2026-27'] for 'last 3 seasons'. Not with season.",
    )
    phase: Optional[Phase] = None
    max_rest_hours: Optional[float] = Field(
        None, gt=0, le=24 * 365,
        description=(
            "Only games played at most this many hours after the team's previous game, "
            "e.g. 48 for back-to-back weeks. Counts games in every loaded league (Euroleague and Greek league)."
        ),
    )
    min_rest_hours: Optional[float] = Field(
        None, gt=0, le=24 * 365,
        description="Only games played at least this many hours after the team's previous game.",
    )
    player_threshold: Optional[PlayerThreshold] = Field(
        None,
        description=(
            "Only games where a player's own stat was in a range, e.g. 'when Lessort plays 30+ minutes' "
            "or 'when Nunn scores 20+'. Games that player missed never count."
        ),
    )


    @field_validator("seasons")
    @classmethod
    def sort_seasons(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Each season may be listed only once.")
        return sorted(value) if value is not None else None


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
        if self.season is not None and self.seasons is not None:
            raise ValueError("Give either season or seasons, not both.")
        if self.compare:
            self.side_specs()  # raises if a side is empty, repeated or invalid once merged
        return self

    def side_specs(self):
        """[(label, QuerySpec), ...]: one plain spec per side, shared filters plus the side's own."""
        base = self.model_dump(exclude={"compare"})
        sides, seen = [], set()
        for side in self.compare or []:
            own = side.model_dump(exclude={"label"}, exclude_defaults=True)
            # A side that says "Euroleague" (the default) still sets something:
            # "in the Euroleague vs in the Greek league" has two real sides.
            if "competition" in side.model_fields_set:
                own["competition"] = side.competition
            if not own:
                raise ValueError("Each compare side needs at least one filter.")
            key = json.dumps(own, sort_keys=True)
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
        # Shared filters apply to every side and a side can't remove them, so
        # {last_n: 6} on top plus a "season" side makes both sides the last 6.
        merged = {}
        for label, spec in sides:
            key = json.dumps(spec.model_dump(), sort_keys=True, default=str)
            if key in merged:
                raise ValueError(
                    f"Compare sides '{merged[key]}' and '{label}' ask for the same games once the shared "
                    "filters are added. Top-level filters apply to every side: put the filter that differs "
                    "(e.g. last_n) only in the side that needs it.")
            merged[key] = label
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
    value: Optional[float] = Field(None, description="Advanced stats only (off_rating, pace, ...).")


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
    ranking_kind: Optional[Literal["team_rankings", "matchup", "player_rankings", "player_profile"]] = Field(
        None, description="Set for ranking questions instead of spec/result.")
    ranking_spec: Optional[dict] = Field(None, description="The ranking request the question was turned into.")
    ranking_result: Optional[
        "RankingsResponse | MatchupResponse | PlayerRankingsResponse | PlayerProfileResponse"] = None
    candidates: list[Candidate] = []


QueryResponse.model_rebuild()  # resolve the forward reference to ComparisonSide


# --- Rankings ----------------------------------------------------------------

TeamStatName = Literal[RANKABLE_TEAM_STATS]


class RankingSpec(BaseModel):
    """Rank every team on its stats, over the games that match the filters."""

    model_config = ConfigDict(extra="forbid")

    stats: Optional[list[TeamStatName]] = Field(
        None, min_length=1, description="Stats to rank on; null means every team stat. Rows are sorted by the first."
    )
    competition: Competition = Field("euroleague", description=COMPETITION_DESCRIPTION)
    season: Optional[SeasonName] = Field(None, description="Season such as '2026-27'; null means all loaded seasons.")
    phase: Optional[Phase] = None
    venue: Optional[Venue] = Field(None, description="Only each team's home (or away, neutral) games.")
    result: Optional[Literal["won", "lost"]] = Field(None, description="Only the games each team won (or lost).")
    max_margin: Optional[int] = Field(
        None, ge=1, le=50, description="Only games decided by at most this many points, e.g. 5 for close games."
    )
    last_n: Optional[int] = Field(None, ge=1, le=100, description="Each team's N most recent matching games.")
    per: Literal["game", "total"] = Field("game", description="Per-game averages or season totals.")
    order: Literal["highest", "lowest"] = Field(
        "highest", description="'lowest' makes rank 1 the lowest value, e.g. fewest turnovers."
    )
    side: Literal["team", "opponent"] = Field(
        "team", description="'opponent' ranks what teams allowed, e.g. points conceded."
    )

    @model_validator(mode="after")
    def check_stats(self):
        if self.stats and len(set(self.stats)) != len(self.stats):
            raise ValueError("Each stat may be listed only once.")
        if self.side == "opponent" and self.stats and "win_pct" in self.stats:
            raise ValueError("win_pct is the team's own record; use side 'team'.")
        if self.side == "opponent" and self.stats and {"forced_turnovers", "forced_tov_pct"} & set(self.stats):
            raise ValueError("forced_turnovers and forced_tov_pct are already the opponents' numbers; use side 'team'.")
        return self


class MatchupSpec(RankingSpec):
    """Two teams side by side, each with its league rank for every stat."""

    team_a: str = Field(description="First team, e.g. 'Panathinaikos'.")
    team_b: str = Field(description="Second team, e.g. 'Virtus'.")


class RankingRow(BaseModel):
    team: str
    games: int
    values: dict[str, Optional[float]] = Field(description="Per game, total or percentage, as ranked.")
    ranks: dict[str, Optional[int]] = Field(description="1 = highest value, also for turnovers and fouls.")
    summary: dict[str, StatSummary]


class RankingsResponse(BaseModel):
    stats: list[str]
    filters: dict
    per: str
    side: str
    team_count: int
    teams: list[RankingRow]


class MatchupResponse(BaseModel):
    stats: list[str]
    filters: dict
    per: str
    side: str
    team_count: int = Field(description="Ranks are out of this many teams.")
    team_a: RankingRow
    team_b: RankingRow
    answer: str = Field(description="A readable side-by-side table.")


# --- Player rankings ---------------------------------------------------------

PlayerStatName = Literal[RANKABLE_PLAYER_STATS]


class PlayerRankingSpec(BaseModel):
    """Rank every player with enough matching games."""

    model_config = ConfigDict(extra="forbid")

    stats: Optional[list[PlayerStatName]] = Field(
        None, min_length=1, description="Stats to rank on; null means every player stat. Rows are sorted by the first."
    )
    competition: Competition = Field("euroleague", description=COMPETITION_DESCRIPTION)
    season: Optional[SeasonName] = None
    phase: Optional[Phase] = None
    venue: Optional[Venue] = None
    result: Optional[Literal["won", "lost"]] = None
    max_margin: Optional[int] = Field(None, ge=1, le=50, description="Only games decided by at most this many points.")
    last_n: Optional[int] = Field(None, ge=1, le=100, description="Each player's N most recent matching games.")
    team: Optional[str] = Field(None, description="Only players' games for this team.")
    per: Literal["game", "total", "per40"] = Field("game", description="Per game, totals or per 40 minutes.")
    min_games: Optional[int] = Field(
        None, ge=0, le=100,
        description="Games needed to be ranked; null = half the games of the most active player, rounded up.",
    )
    min_minutes: Optional[float] = Field(None, ge=0, le=40, description="Minutes per game needed to be ranked.")
    limit: Optional[int] = Field(50, ge=1, le=500, description="Rows returned; ranks are still out of everyone ranked.")
    order: Literal["highest", "lowest"] = Field(
        "highest", description="'lowest' makes rank 1 the lowest value, e.g. fewest turnovers."
    )

    @model_validator(mode="after")
    def check_stats(self):
        if self.stats and len(set(self.stats)) != len(self.stats):
            raise ValueError("Each stat may be listed only once.")
        return self


class PlayerProfileSpec(PlayerRankingSpec):
    """One player's value and rank in every stat."""

    player: str = Field(description="Player name, e.g. 'Nunn'.")
    limit: Optional[int] = Field(None, description="Ignored for a profile.")


class PlayerRankingRow(BaseModel):
    player: str
    team: str = Field(description="Latest team in the matching games.")
    games: int
    minutes_per_game: float
    qualified: bool = Field(description="False when below min_games/min_minutes: values but no ranks.")
    values: dict[str, Optional[float]]
    ranks: dict[str, Optional[int]]
    summary: dict[str, StatSummary]


class PlayerRankingsResponse(BaseModel):
    stats: list[str]
    filters: dict
    per: str
    min_games: int = Field(description="The minimum that was applied.")
    ranked_count: int = Field(description="Ranks are out of this many players.")
    unranked: int = Field(description="Players left out by the minimum.")
    players: list[PlayerRankingRow]


class PlayerProfileResponse(BaseModel):
    stats: list[str]
    filters: dict
    per: str
    min_games: int
    ranked_count: int
    player: PlayerRankingRow
    answer: str


AskResponse.model_rebuild()  # resolve the ranking response types defined above
