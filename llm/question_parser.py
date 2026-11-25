"""Turn a plain-language question into a validated QuerySpec.

    from llm.question_parser import parse_question
    parse_question("Nunn's 3-point percentage in his last 5 games")
    # ParseResult(status="ok", spec=QuerySpec(stats=["fg3_pct"], player="Nunn", last_n=5), ...)

The LLM only fills in a structured form (OpenAI Structured Outputs). It never
writes SQL and never answers the question itself: its output is checked by the
same Pydantic QuerySpec the API uses, and the SQL is built by analysis.stat_query.

The LLM is passed in as a callable, so tests can use a fake one without a key
or network: llm(instructions, messages) -> LLMQuerySpec.
"""
import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from analysis.player_rankings import RANKABLE_PLAYER_STATS
from analysis.rankings import RANKABLE_TEAM_STATS
from api.models import (
    CompareSide,
    MatchupSpec,
    Phase,
    PlayerProfileSpec,
    PlayerRankingSpec,
    PlayerThreshold,
    QuerySpec,
    RankingSpec,
    Venue,
)
from database.db import get_connection

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gpt-6-luna"

# What the LLM fills in: every QuerySpec field (same types and descriptions)
# plus `answerable` and `reason`. Nothing has a default, because strict
# Structured Outputs requires every field to be present. Range checks
# (last_n >= 1, season format, ...) are left to QuerySpec afterwards.
def _llm_fields(model, **replace):
    """Field types and descriptions only: no defaults or range checks (QuerySpec checks those)."""
    return {
        name: (replace.get(name, info.annotation), Field(description=info.description))
        for name, info in model.model_fields.items()
    }


LLMPlayerThreshold = create_model(
    "LLMPlayerThreshold", __config__=ConfigDict(extra="forbid"), **_llm_fields(PlayerThreshold)
)
# competition is optional for the LLM: null = not set, so a compare side
# inherits the question's league instead of the model having to repeat it
# (and a forced "euroleague" on a side silently overriding a Greek question).
_THRESHOLD = {"player_threshold": Optional[LLMPlayerThreshold],
              "competition": Optional[Literal["euroleague", "eurocup", "greek_league"]]}
LLMCompareSide = create_model(
    "LLMCompareSide", __config__=ConfigDict(extra="forbid"), **_llm_fields(CompareSide, **_THRESHOLD)
)
_SPEC_FIELDS = _llm_fields(QuerySpec, **_THRESHOLD)
_SPEC_FIELDS["compare"] = (Optional[list[LLMCompareSide]], Field(description=QuerySpec.model_fields["compare"].description))
RankingKind = Literal["team_rankings", "matchup", "player_rankings", "player_profile"]
RankStat = Literal[tuple(dict.fromkeys(RANKABLE_TEAM_STATS + RANKABLE_PLAYER_STATS))]


class LLMRanking(BaseModel):
    """A question about where teams or players rank, filled in instead of the single-subject fields."""

    model_config = ConfigDict(extra="forbid")

    kind: RankingKind = Field(description=(
        "team_rankings: rank all teams (or where one team ranks); matchup: two teams side by side with ranks; "
        "player_rankings: list players by a stat (leaders); player_profile: one player's ranks."
    ))
    stats: Optional[list[RankStat]] = Field(description="Stats to rank on; null means all of them.")
    team: Optional[str] = Field(description=(
        "team_rankings: the team the question is about (its rank is pointed out); matchup: the first team; "
        "player_rankings/player_profile: only games for this team."
    ))
    other_team: Optional[str] = Field(description="matchup only: the second team.")
    player: Optional[str] = Field(description="player_profile only: the player.")
    competition: Optional[Literal["euroleague", "eurocup", "greek_league"]] = Field(
        description="null = euroleague; 'eurocup' for the EuroCup; 'greek_league' for the Greek league (GBL).")
    season: Optional[str]
    phase: Optional[Phase]
    venue: Optional[Venue]
    result: Optional[Literal["won", "lost"]] = Field(description="Only games won (or lost).")
    max_margin: Optional[int] = Field(description="Only games decided by at most this many points.")
    last_n: Optional[int] = Field(description="Each team's or player's N most recent games.")
    per: Optional[Literal["game", "total", "per40"]] = Field(description="null = per game. per40 is for players only.")
    side: Optional[Literal["team", "opponent"]] = Field(
        description="Teams only: 'opponent' for what teams allowed (points allowed, opponent 3P%, ...).")
    min_games: Optional[int] = Field(description="Players only: games needed to be ranked; null = automatic.")
    min_minutes: Optional[float] = Field(description="Players only: minutes per game needed to be ranked.")
    limit: Optional[int] = Field(description="player_rankings only: how many players to list; null = 10.")
    order: Optional[Literal["highest", "lowest"]] = Field(
        description="null = highest first. 'lowest' when the question wants the fewest/lowest (rank 1 = lowest).")


LLMQuerySpec = create_model(
    "LLMQuerySpec",
    __config__=ConfigDict(extra="forbid"),
    answerable=(bool, Field(description="False if the data cannot answer this question.")),
    reason=(Optional[str], Field(description="When not answerable: a short explanation for the user.")),
    ranking=(Optional[LLMRanking], Field(description=(
        "Only for ranking/leaderboard questions. Then leave the single-subject fields empty."))),
    **_SPEC_FIELDS,
)


@dataclass
class RankingRequest:
    kind: str
    spec: BaseModel  # RankingSpec, MatchupSpec, PlayerRankingSpec or PlayerProfileSpec
    focus_team: Optional[str] = None  # team_rankings: the team the question is about


# Which LLMRanking fields each kind may use (besides kind and the shared filters).
_SHARED = ("stats", "season", "phase", "venue", "result", "max_margin", "last_n", "per", "order", "competition")
_KIND_FIELDS = {
    "team_rankings": ("team", "side"),
    "matchup": ("team", "other_team", "side"),
    "player_rankings": ("team", "min_games", "min_minutes", "limit"),
    "player_profile": ("player", "team", "min_games", "min_minutes"),
}


def to_ranking(raw):
    """LLMRanking output (a dict) -> RankingRequest with a validated spec.

    Raises ValueError (or pydantic's ValidationError, also a ValueError) when a
    field doesn't fit the kind, so parse_question can retry with the message.
    """
    kind = raw["kind"]
    allowed = set(_SHARED) | set(_KIND_FIELDS[kind])
    wrong = sorted(k for k, v in raw.items() if k != "kind" and v is not None and k not in allowed)
    if wrong:
        raise ValueError(f"{kind} does not use: {', '.join(wrong)}.")
    given = {k: v for k, v in raw.items() if k != "kind" and v is not None}
    if kind in ("team_rankings", "matchup") and given.get("per") == "per40":
        raise ValueError("per40 is for players only.")
    if kind == "team_rankings":
        focus = given.pop("team", None)
        return RankingRequest(kind, RankingSpec(**given), focus)
    if kind == "matchup":
        if "team" not in given or "other_team" not in given:
            raise ValueError("matchup needs team and other_team.")
        given["team_a"], given["team_b"] = given.pop("team"), given.pop("other_team")
        return RankingRequest(kind, MatchupSpec(**given))
    if kind == "player_rankings":
        given.setdefault("limit", 10)
        return RankingRequest(kind, PlayerRankingSpec(**given))
    if "player" not in given:
        raise ValueError("player_profile needs a player.")
    return RankingRequest(kind, PlayerProfileSpec(**given))

LLM = Callable[[str, list[dict]], BaseModel]


@dataclass
class ParseResult:
    status: Literal["ok", "unanswerable", "invalid", "error"]
    spec: Optional[QuerySpec] = None
    ranking: Optional[RankingRequest] = None  # set instead of spec for ranking questions
    reason: Optional[str] = None  # message for the user when status != "ok"
    attempts: int = 0  # LLM calls made
    outputs: list[dict] = field(default_factory=list)  # raw LLM outputs, for debugging and the eval


# --- Prompt ------------------------------------------------------------------

def loaded_seasons(competition_id="E"):
    """Season names with games in one competition, oldest first, e.g. ['2024-25', '2025-26']."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT s.name FROM seasons s JOIN games g ON g.season_id = s.id
               JOIN competitions c ON c.id = s.competition_id WHERE c.source_competition_id = ?
               ORDER BY s.name""", (competition_id,)
        ).fetchall()
    return [name for (name,) in rows]


def build_instructions(today=None, seasons=None, greek_seasons=None, eurocup_seasons=None):
    today = today or date.today()
    seasons = seasons if seasons is not None else loaded_seasons()
    greek_seasons = greek_seasons if greek_seasons is not None else loaded_seasons("GBL")
    eurocup_seasons = eurocup_seasons if eurocup_seasons is not None else loaded_seasons("U")
    current = seasons[-1] if seasons else None
    previous = seasons[-2] if len(seasons) > 1 else None
    return f"""You turn a basketball stats question into a query spec. You never answer the question yourself; code runs the query on a database.

Data: three leagues. EuroLeague seasons: {", ".join(seasons) or "none"}. EuroCup seasons: {", ".join(eurocup_seasons) or "none"}. Greek league (Stoiximan GBL) seasons: {", ".join(greek_seasons) or "none"}. Today is {today.isoformat()}, so "{current}" is the current season, still in progress.
A spec has exactly one subject: one player OR one team.
- competition: "eurocup" when the question names the EuroCup ("EuroCup", "Eurocup", "7DAYS EuroCup"). "greek_league" when the question names the Greek league ("Greek league", "GBL", "Stoiximan GBL", "Basket League", "in Greece", "domestically" for a Greek club); otherwise "euroleague", including when no league is named. E.g. "Bartley points in the Greek league this season" -> player "Bartley", competition "greek_league", season "{current}". The same goes in ranking.competition (null there means euroleague). A compare side may set competition ("in the Euroleague vs the Greek league").
- The Greek league has no player plus_minus (a team's plus_minus is its point margin, which works everywhere) and no shot-list stats (paint_points, fastbreak_points, second_chance_points, points_off_turnovers, paint_fg_pct, pts_from_paint_pct): asking for those in the Greek league is not answerable.

How to fill the spec:
- Copy player and team names as the user wrote them (code resolves nicknames, surnames, accents and typos). Do not expand or correct them.
- Users make typos in the rest of the question too. Read each one as its closest sensible meaning and apply it the same way every time: e.g. "his season" or "thsi season" at the end of a question means "this season".
- "this season" -> season "{current}"; "last season" -> season "{previous}"; a season like "2024/25" -> "2024-25". No season mentioned -> season null (all loaded seasons).
- Several seasons -> seasons (a list) and season null: "last N seasons"/"past N seasons" -> the N most recent loaded seasons, counting the current one (e.g. "last 3 seasons" -> the last 3 of the loaded list); "this season and last" -> ["{previous}", "{current}"] (one combined answer: "X and Y" joins seasons, only "X vs Y"/"compared to"/"each season" makes a compare); "since 2024-25" -> every loaded season from 2024-25 on. A single season always goes in season, never in seasons.
- "last N games" -> last_n N. "recently"/"lately" with no number -> last_n 5.
- "at home" -> venue "home"; "on the road"/"away" -> venue "away".
- "against <team>" or "<subject> vs <team>" -> opponent. For a team question like "Panathinaikos rebounds vs Zalgiris", the subject is the first team and the other is the opponent.
- "against teams over .500"/"winning teams"/"teams with a winning record" -> opponent_over_500 true.
- "when X doesn't play"/"without X" -> without_player; "with X"/"when X plays" -> with_player.
- "against <player>"/"when facing <player>"/"vs <player>" where the name is a player, not a team -> against_player (he played for the other team), e.g. "Hifi points against Theis" -> player "Hifi", against_player "Theis". opponent is only ever a team name: never a player, and never a description like "winning teams" (that is opponent_over_500).
- "back-to-back", "short rest", "two games in two days", "within 48 hours" -> max_rest_hours 48. "N days of rest or less" -> max_rest_hours N*24. "well rested", "3+ days of rest" -> min_rest_hours 72 (or N*24 if a number of days is given).
- A condition on another player's own numbers in the game ("when Theis plays 30+ minutes", "when Hifi scores 20 or more", "when Nebo gets under 5 rebounds") -> player_threshold {{player, stat, min and/or max}}. min and max are inclusive: "30+ minutes"/"at least 30"/"more than 30 minutes" -> min 30; "under 15 minutes"/"less than 15 minutes" -> max 15; minutes are not whole numbers, so keep the number as written for minutes ("more than 30 minutes" -> min 30, "fewer than 12 minutes" -> max 12); only for whole-number stats (points, rebounds, assists, makes, ...) shift by one: "more than 20 points" -> min 21, "fewer than 5 rebounds" -> max 4. A threshold on the subject player's own stat works the same way.
- "playoffs" -> phase "playoffs"; "Final Four" -> "final_four"; "regular season" -> "regular_season"; "play-in" -> "play_in".
- Stats: rebounds -> total_rebounds; "3-point %"/"three-point shooting" -> fg3_pct; "threes"/"3s made" -> fg3_made; "free throw %" -> ft_pct; "field goal %" or plain "shooting" -> fg_pct; "2-point %" -> fg2_pct; PIR/efficiency/index rating -> pir; "points in the paint"/"paint points" -> paint_points; "paint shooting"/"paint FG%" -> paint_fg_pct; "fast break points" -> fastbreak_points; "second chance points" -> second_chance_points; "points off turnovers" -> points_off_turnovers; fouls -> fouls_committed; "fouls drawn" -> fouls_received; "record"/"win percentage"/"how many wins"/"how did they do" -> win_pct. If no stat is named ("how did he play", "his stats") -> points, total_rebounds, assists.
- Leave every filter that the question does not mention at null (or false for opponent_over_500).
- compare is null unless the question compares conditions for the same subject ("X vs Y", "when ... and when ...", "more or less"). Then put what both sides share (subject, stats, season, ...) at the top level and give each side a short label and only the filters that differ. Examples:
  "Punter points with and without Satoransky this season" -> player "Punter", season "{current}", compare [{{label "with Satoransky", with_player "Satoransky"}}, {{label "without Satoransky", without_player "Satoransky"}}].
  "Olympiacos record home vs away" -> compare [{{label "home", venue "home"}}, {{label "away", venue "away"}}].
  "Real turnovers on short rest vs rested" -> compare [{{label "short rest", max_rest_hours 48}}, {{label "rested", min_rest_hours 72}}].
  "Nunn points this season vs last season" -> compare [{{label "{current}", season "{current}"}}, {{label "{previous}", season "{previous}"}}].

Rankings: for "who leads", "top scorers", "best/worst teams in X", "where does X rank", "rank all teams", "compare team A and team B" or "a full stat sheet with ranks", fill ranking and leave the single-subject fields empty (stats [], everything else null/false, compare null). ranking.kind:
- team_rankings: every team ranked. If the question is about one team ("where does Bayern rank in assists"), put it in ranking.team.
  A team's OWN stats without a rank ("Bayern rebounds", "Bayern 3P% last season") are never rankings: they use the single-subject fields. The one exception is what a team ALLOWS, which the single-subject fields can't express: a question with "allow"/"conceded"/"opponents ... against <team>" ("how many offensive rebounds does Bayern allow", "Bayern's points conceded per game", "what 3P% do opponents shoot against Bayern") -> team_rankings with ranking.team and side "opponent". If it also needs a filter rankings don't have (one opponent, with/without a player, rest), it is unanswerable.
- matchup: two teams side by side with their ranks ("Dubai vs Hapoel stats", "compare Valencia and Besiktas"): team and other_team.
- player_rankings: players listed by the first stat ("who leads in steals", "top 5 scorers"): limit = the number asked for (null = 10). ranking.team only limits it to one team's players ("ASVEL's best rebounders").
- player_profile: one player's ranks in every stat ("where does Hifi rank", "Hifi's ranks this season"): player.
Rank 1 is the highest value. When the question wants the fewest/lowest ("fewest turnovers", "best defense" = lowest def_rating, "allows the fewest points") set order "lowest".
"allowed"/"conceded"/"opponents' X" -> side "opponent" (teams only). side applies to every stat in the question. Turnovers a team forces ("forced turnovers", "turnovers it causes", "opponent turnovers", "opponent turnover %") -> stats forced_turnovers / forced_tov_pct with side "team", never side "opponent": e.g. "forced turnovers and steals" -> stats [forced_turnovers, steals], side "team" (opponent steals would be the steals made against the team). "per 40 minutes" -> per "per40" (players only); "totals"/"in total" -> per "total". "The most"/"the fewest" of a countable thing with no per-game wording ("who has the most assists", "most double-doubles") is a total -> per "total"; "leads in", "leader", "top scorers", "best", "per game", "averages" -> per null (per game).
Stats for rankings also include advanced ones: possessions, pace, off_rating, def_rating, net_rating, efg_pct, ts_pct, tov_pct, oreb_pct, dreb_pct, ft_rate, fg3a_rate, ast_pct, ast_to, stl_pct, blk_pct, pts_from_paint_pct, forced_turnovers, forced_tov_pct (teams only); for players also usage_pct, reb_pct, pts_from_2pt_pct, pts_from_3pt_pct, pts_from_ft_pct, points_per_fga, team_pts_share, double_doubles, triple_doubles, starts, games_20_points. "all stats"/"full stats"/no stat named -> stats null (all of them).
Close games ("decided by 5 or less", "+-5") -> max_margin 5; "in wins"/"in losses" -> result "won"/"lost". The season rules above apply to ranking.season too.

Set answerable false, fill reason with one short sentence, and leave the other fields empty/null/false when the question:
- is about another competition (NBA, Basketball Champions League, Greek Cup, ACB, national teams, ...) or a EuroCup season that isn't loaded;
- needs data we do not have (shot locations, salaries, height, dunks, injuries, transfers, coaches, attendance, ...);
- compares two or more named players, or asks which of a few named players or teams is better ("Hifi or Theis", "who is better") — rankings over the whole league are fine, see ranking;
- asks for a prediction or opinion, or is not a basketball stats question at all;
- asks about players being ON THE FLOOR / ON THE COURT at the same time: "on the floor with", "on the court together", "shares the court", "lineup", "minutes together", "+/- when both are on the floor". The data knows who played in each game, not who was on the court at each moment (that needs play-by-play). Reason: "A query can only filter by games a player played or missed, not by who was on the floor together." Only treat a question as this when it explicitly says floor, court, lineup or minutes together. Every other way of saying two players played together or apart ("with X", "when X plays", "when X sits/is out", "without X", "teamed with X") is game-level: use with_player/without_player.
The reason must only say what the query can't express (e.g. "A query can't filter by the team a player was on."). You cannot see the database, so never claim facts about players, teams, transfers or results."""


# --- The real LLM ------------------------------------------------------------

class OpenAILLM:
    """Calls the OpenAI Responses API with Structured Outputs."""

    def __init__(self, model=None, reasoning_effort="low"):
        from dotenv import load_dotenv
        from openai import OpenAI

        load_dotenv(PROJECT_ROOT / ".env")
        self.model = model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        self.reasoning_effort = reasoning_effort
        self.client = OpenAI()

    def __call__(self, instructions, messages):
        response = self.client.responses.parse(
            model=self.model,
            instructions=instructions,
            input=messages,
            text_format=LLMQuerySpec,
            reasoning={"effort": self.reasoning_effort},
        )
        if response.output_parsed is None:  # e.g. the model refused
            raise ValueError("The model returned no structured output.")
        return response.output_parsed


# --- Parsing -----------------------------------------------------------------

def _error_summary(exc):
    if not isinstance(exc, ValidationError):
        return str(exc)
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'spec'}: {e['msg']}" for e in exc.errors()
    )


def _drop_unset_competition(fields):
    """competition null -> left out, so QuerySpec's default (or the base, for a side) applies."""
    if fields.get("competition") is None:
        fields.pop("competition", None)
    for side in fields.get("compare") or []:
        if side.get("competition") is None:
            side.pop("competition", None)
    return fields


def parse_question(question, llm: LLM = None, today=None, seasons=None, max_attempts=2):
    """Ask the LLM for a spec and validate it, retrying once with the errors.

    Returns a ParseResult whose status is:
      ok            spec is a valid QuerySpec, or ranking a valid RankingRequest
                    (names are not resolved yet)
      unanswerable  the LLM said the data cannot answer it; reason says why
      invalid       the LLM's output still failed validation after the retry
      error         the LLM call itself failed (network, key, quota, ...)
    """
    llm = llm or OpenAILLM()
    instructions = build_instructions(today, seasons)
    messages = [{"role": "user", "content": question}]
    result = ParseResult(status="invalid")

    for _ in range(max_attempts):
        result.attempts += 1
        try:
            output = llm(instructions, messages)
        except Exception as exc:  # the API can fail in many ways; report, don't crash
            return ParseResult(status="error", reason=f"LLM call failed: {exc}",
                               attempts=result.attempts, outputs=result.outputs)
        raw = output.model_dump()
        result.outputs.append(raw)

        if not raw["answerable"]:
            result.status = "unanswerable"
            result.reason = raw["reason"] or "This question can't be answered from the loaded data."
            return result

        fields = _drop_unset_competition(
            {k: v for k, v in raw.items() if k not in ("answerable", "reason", "ranking")})
        try:
            if raw["ranking"] is not None:
                result.ranking = to_ranking(raw["ranking"])
            else:
                result.spec = QuerySpec(**fields)
            result.status, result.reason = "ok", None
            return result
        except ValueError as exc:  # pydantic's ValidationError is a ValueError too
            result.reason = f"The question couldn't be turned into a valid query: {_error_summary(exc)}"
            messages += [
                {"role": "assistant", "content": json.dumps(raw)},
                {"role": "user", "content": (
                    f"That spec failed validation: {_error_summary(exc)}. "
                    "Return a corrected spec, or set answerable false if the question can't be expressed."
                )},
            ]
    return result
