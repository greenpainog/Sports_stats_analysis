"""Pydantic models for the API.

QuerySpec is the one structured description of a stat question. The API takes
it as input, and in phase 5 the LLM's only job is to produce a valid QuerySpec.
Field descriptions end up in the JSON schema, which the LLM will see.
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from analysis.stat_query import COUNTING_STATS, PERCENTAGE_STATS

# Literal[...] from the tuples gives the JSON schema an exact list of allowed
# values, so an unknown stat name is rejected before any SQL runs.
StatName = Literal[COUNTING_STATS + tuple(PERCENTAGE_STATS)]
Venue = Literal["home", "away", "neutral"]
Phase = Literal["regular_season", "play_in", "playoffs", "final_four"]


class QuerySpec(BaseModel):
    """A stat question about exactly one player or one team."""

    model_config = ConfigDict(extra="forbid")  # unknown fields are an error, not ignored

    stats: list[StatName] = Field(
        min_length=1, max_length=8,
        description="Stats to report. Percentages (fg3_pct, ...) are computed from makes and attempts.",
    )
    player: Optional[str] = Field(None, description="Player name, e.g. 'Kendrick Nunn' or 'Nunn'.")
    team: Optional[str] = Field(None, description="Team name, e.g. 'Panathinaikos' or 'Real'.")
    opponent: Optional[str] = Field(None, description="Only games against this team.")
    venue: Optional[Venue] = Field(None, description="From the subject's point of view.")
    last_n: Optional[int] = Field(None, ge=1, le=100, description="Most recent N games after other filters.")
    opponent_over_500: bool = Field(
        False, description="Only games where the opponent had a winning record in that season before the game."
    )
    without_player: Optional[str] = Field(None, description="Only games in which this player did not appear.")
    season: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}$", description="Season such as '2024-25'.")
    phase: Optional[Phase] = None

    @model_validator(mode="after")
    def one_subject_and_unique_stats(self):
        if (self.player is None) == (self.team is None):
            raise ValueError("Give exactly one of 'player' or 'team'.")
        if len(set(self.stats)) != len(self.stats):
            raise ValueError("Each stat may be listed only once.")
        return self


class GameRow(BaseModel):
    game_id: int
    start_time: str
    opponent: str
    venue: Venue
    team_score: Optional[int]
    opponent_score: Optional[int]
    values: dict[str, Optional[float | str]]


class StatSummary(BaseModel):
    stat: str
    games: int
    total: Optional[float] = None
    per_game: Optional[float] = None
    made: Optional[float] = None
    attempted: Optional[float] = None
    pct: Optional[float] = None


class QueryResponse(BaseModel):
    subject: str
    subject_type: Literal["player", "team"]
    filters: dict
    games: list[GameRow]
    summary: dict[str, StatSummary]
    answer: str = Field(description="Readable summary of the result.")


class Candidate(BaseModel):
    id: int
    name: str


class ResolveResponse(BaseModel):
    query: str
    status: Literal["match", "ambiguous", "not_found"]
    method: Optional[Literal["exact", "fuzzy"]] = None
    candidates: list[Candidate]


class ErrorResponse(BaseModel):
    detail: str
    candidates: list[Candidate] = []
