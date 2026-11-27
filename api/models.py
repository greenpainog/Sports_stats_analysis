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
from analysis.stat_query import (
    COUNTING_STATS, MAX_ON_FLOOR_PLAYERS, PBP_BOX_STATS, PERCENTAGE_STATS, QUARTER_STATS, RESULT_STATS,
    NOT_IN_CLUTCH, STINT_ONLY_STATS,
)

# Literal[...] from the tuples gives the JSON schema an exact list of allowed
# values, so an unknown stat name is rejected before any SQL runs.
StatName = Literal[COUNTING_STATS + QUARTER_STATS + tuple(PERCENTAGE_STATS) + RESULT_STATS + STINT_ONLY_STATS]
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
        description="Only games this player missed, in seasons when he played for either team. Whole games, "
                    "not the minutes he sat on the bench (that is on_floor_without).",
    )
    with_player: Optional[str] = Field(
        None, description="Only games in which this player appeared (for either team). Whole games, not the "
                          "minutes both were on the floor (that is on_floor_with)."
    )
    on_floor_with: Optional[list[str]] = Field(
        None, min_length=1, max_length=MAX_ON_FLOOR_PLAYERS,
        description="Teammates who must be ON THE FLOOR at the same time as the subject, e.g. ['Tavares'] for "
                    "'Campazzo's plus-minus when he's on the court with Tavares'. Only the time the lineup "
                    "condition held counts: minutes and plus_minus of that time, points_for/points_against "
                    "(the team's points in it), box-score stats such as points, assists, total_rebounds or "
                    "fg_pct (the subject's own in that time), and win_pct. Euroleague and EuroCup only.",
    )
    on_floor_without: Optional[list[str]] = Field(
        None, min_length=1, max_length=MAX_ON_FLOOR_PLAYERS,
        description="Teammates who must be OFF THE FLOOR (on the bench or not playing) while the subject is "
                    "on it, e.g. 'with Tavares on the bench'. Same stats as on_floor_with.",
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
    coach: Optional[str] = Field(
        None, description="Only games where the subject's own team was coached by this person, e.g. "
                          "'under Ataman'. Euroleague and EuroCup only.")
    referee: Optional[str] = Field(
        None, description="Only games this person refereed, e.g. 'when Peruga referees'. Euroleague and "
                          "EuroCup only.")
    clutch: bool = Field(
        False, description="Only clutch time: the last 5 minutes of the 4th quarter or overtime with the score "
                           "within 5 points. Stats are counted in that time (points, fg_pct, assists, "
                           "turnovers, ...; plus_minus, points_for, points_against); minutes aren't available. "
                           "Only games with clutch time count. Euroleague and EuroCup only.")


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
            "win_pct is the win-loss record of the subject's team in the matching games. "
            "points_for / points_against: the team's points scored / allowed while the subject (and any "
            "on-floor condition) was on the floor; possessions and off_rating / def_rating / net_rating "
            "(points per 100 possessions) in that same time."
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
        pbp_mode = bool(self.on_floor_with or self.on_floor_without or self.clutch
                        or set(self.stats) & set(STINT_ONLY_STATS))
        if pbp_mode:
            allowed = {*PBP_BOX_STATS, "plus_minus", "win_pct", "minutes", *STINT_ONLY_STATS}
            if self.clutch:
                allowed -= set(NOT_IN_CLUTCH)
            other = sorted(set(self.stats) - allowed)
            if other:
                raise ValueError(f"With clutch or on-floor filters these stats aren't available: {', '.join(other)} "
                                 "(clutch time has no minutes, possessions or ratings; quarter points, shot-list and paint stats need "
                                 "the box score).")
        if (pbp_mode or (self.player is not None and set(self.stats) & set(QUARTER_STATS))) \
                and self.competition == "greek_league":
            raise ValueError("Clutch, on-floor and a player's quarter stats need play-by-play: Euroleague and "
                             "EuroCup only.")
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
    value: Optional[float] = Field(
        None, description="Advanced stats (off_rating, pace, ...); for on-floor stats, the value per 40 minutes.")


class QueryResponse(BaseModel):
    subject: str
    subject_type: Literal["player", "team"]
    filters: dict
    games: list[GameRow]
    summary: dict[str, StatSummary]
    notes: list[str] = Field([], description="E.g. games left out because their play-by-play is broken.")
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
    ranking_kind: Optional[Literal["team_rankings", "matchup", "player_rankings", "player_profile", "lineups"]] = Field(
        None, description="Set for ranking questions instead of spec/result.")
    ranking_spec: Optional[dict] = Field(None, description="The ranking request the question was turned into.")
    ranking_result: Optional[
        "RankingsResponse | MatchupResponse | PlayerRankingsResponse | PlayerProfileResponse | LineupsResponse"] = None
    candidates: list[Candidate] = []
    season_fallback: Optional[Literal["previous_season", "with_previous_season"]] = Field(None, description=(
        "Set when the question named no season and this season had too few matching games: "
        "previous_season = no games this season, so result is last season's; "
        "with_previous_season = under 5 games (a small sample), so last season is in previous_season_result too."))
    fallback_season: Optional[str] = Field(None, description="The season used for the fallback (last season).")
    previous_season_result: Optional[
        "QueryResponse | RankingsResponse | MatchupResponse | PlayerRankingsResponse | PlayerProfileResponse "
        "| LineupsResponse"] = None


class AgentRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500, examples=[
        "Is Panathinaikos rebounding better in its last 6 games than over the season?"])
    conversation_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{1,64}$", description=(
        "Leave out to start a conversation; send back the id from the response to ask a follow-up "
        "(\"and Zalgiris?\"). Conversations are kept in memory and lost when the server restarts."))


class AgentStep(BaseModel):
    """One tool call the agent made, in order."""

    tool: str
    args: dict = Field(description="The arguments the LLM filled in (validated before the tool ran).")
    result: str = Field(description="What the tool returned, which is what the LLM read.")


class AgentResponse(BaseModel):
    """The agent's answer and every tool call behind it."""

    question: str
    conversation_id: str = Field(description="Send this with the next question to continue the conversation.")
    status: Literal["answered", "error"] = Field(
        description="answered: answer is set. error: the LLM call failed.")
    answer: str
    check: Optional[Literal["passed", "fixed", "fallback"]] = Field(None, description=(
        "The number check: passed (every number came from a tool result), fixed (it passed after one "
        "retry) or fallback (it failed twice, so the answer is the raw tool results)."))
    steps: list[AgentStep] = []


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


# --- Lineups -----------------------------------------------------------------

LineupSort = Literal["net_rating", "off_rating", "def_rating", "net_per_40", "plus_minus", "minutes",
                     "points_for_per_40", "points_against_per_40"]


class LineupSpec(BaseModel):
    """Rank a team's duos, trios, 4- or 5-man lineups by how they did on the floor together."""

    model_config = ConfigDict(extra="forbid")

    team: str = Field(description="The team, e.g. 'Panathinaikos'.")
    size: Literal[2, 3, 4, 5] = Field(2, description="Players in the group: 2 = duos, 3 = trios, 5 = full lineups.")
    with_players: Optional[list[str]] = Field(
        None, max_length=4, description="Only groups that include all of these players, e.g. the best duo with one player.")
    competition: Literal["euroleague", "eurocup"] = Field(
        "euroleague", description="Lineups need play-by-play: Euroleague and EuroCup only.")
    season: Optional[SeasonName] = None
    seasons: Optional[list[SeasonName]] = Field(None, min_length=2, max_length=10)
    phase: Optional[Phase] = None
    last_n: Optional[int] = Field(None, ge=1, le=100, description="Only the team's N most recent games.")
    sort: LineupSort = Field("net_rating", description=(
        "net_rating = points scored minus allowed per 100 possessions together (the default 'best'); "
        "off_rating / def_rating = offense / defense per 100 possessions (def_rating with order 'lowest' = best "
        "defense); net_per_40 = plus-minus per 40 minutes; plus_minus = total; minutes = most used."))
    order: Literal["highest", "lowest"] = Field(
        "highest", description="'lowest' for the worst lineups, or the best defense with points_against_per_40.")
    limit: int = Field(10, ge=1, le=50, description="Lineups listed; there is no minimum-minutes cutoff.")

    @model_validator(mode="after")
    def check_lineup(self):
        if self.season is not None and self.seasons is not None:
            raise ValueError("Give either season or seasons, not both.")
        if self.with_players and len(self.with_players) >= self.size:
            raise ValueError(f"A {self.size}-man lineup can include at most {self.size - 1} named players.")
        return self


class LineupRow(BaseModel):
    players: list[str]
    games: int
    minutes: float
    points_for: int
    points_against: int
    plus_minus: int
    net_per_40: float = Field(description="Plus-minus per 40 minutes together.")
    possessions: float = Field(description="Estimated possessions together (average of both teams).")
    off_rating: Optional[float] = Field(description="Points scored per 100 possessions together.")
    def_rating: Optional[float] = Field(description="Points allowed per 100 possessions together.")
    net_rating: Optional[float] = Field(description="off_rating - def_rating.")
    small_sample: bool = Field(description="True when under 40 minutes together: read the per-40 numbers with care.")


class LineupsResponse(BaseModel):
    team: str
    size: int
    sort: str
    order: str
    filters: dict
    games: int = Field(description="Games the lineups were counted over (games with sound play-by-play).")
    lineup_count: int = Field(description="Every group that played together, before the limit.")
    lineups: list[LineupRow]
    notes: list[str] = []
    answer: str


AskResponse.model_rebuild()  # resolve the ranking response types defined above
