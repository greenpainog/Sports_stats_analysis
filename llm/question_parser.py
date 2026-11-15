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

from api.models import CompareSide, PlayerThreshold, QuerySpec
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
_THRESHOLD = {"player_threshold": Optional[LLMPlayerThreshold]}
LLMCompareSide = create_model(
    "LLMCompareSide", __config__=ConfigDict(extra="forbid"), **_llm_fields(CompareSide, **_THRESHOLD)
)
_SPEC_FIELDS = _llm_fields(QuerySpec, **_THRESHOLD)
_SPEC_FIELDS["compare"] = (Optional[list[LLMCompareSide]], Field(description=QuerySpec.model_fields["compare"].description))
LLMQuerySpec = create_model(
    "LLMQuerySpec",
    __config__=ConfigDict(extra="forbid"),
    answerable=(bool, Field(description="False if the data cannot answer this question.")),
    reason=(Optional[str], Field(description="When not answerable: a short explanation for the user.")),
    **_SPEC_FIELDS,
)

LLM = Callable[[str, list[dict]], BaseModel]


@dataclass
class ParseResult:
    status: Literal["ok", "unanswerable", "invalid", "error"]
    spec: Optional[QuerySpec] = None
    reason: Optional[str] = None  # message for the user when status != "ok"
    attempts: int = 0  # LLM calls made
    outputs: list[dict] = field(default_factory=list)  # raw LLM outputs, for debugging and the eval


# --- Prompt ------------------------------------------------------------------

def loaded_seasons():
    """Season names that have games, oldest first, e.g. ['2024-25', '2025-26']."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT s.name FROM seasons s JOIN games g ON g.season_id = s.id ORDER BY s.name"
        ).fetchall()
    return [name for (name,) in rows]


def build_instructions(today=None, seasons=None):
    today = today or date.today()
    seasons = seasons if seasons is not None else loaded_seasons()
    current = seasons[-1] if seasons else None
    previous = seasons[-2] if len(seasons) > 1 else None
    return f"""You turn a basketball stats question into a query spec. You never answer the question yourself; code runs the query on a database.

Data: EuroLeague games only. Loaded seasons: {", ".join(seasons) or "none"}. Today is {today.isoformat()}, so "{current}" is the current season, still in progress.
A spec has exactly one subject: one player OR one team.

How to fill the spec:
- Copy player and team names as the user wrote them (code resolves nicknames, surnames, accents and typos). Do not expand or correct them.
- Users make typos in the rest of the question too. Read each one as its closest sensible meaning and apply it the same way every time: e.g. "his season" or "thsi season" at the end of a question means "this season".
- "this season" -> season "{current}"; "last season" -> season "{previous}"; a season like "2024/25" -> "2024-25". No season mentioned -> season null (all loaded seasons).
- Several seasons -> seasons (a list) and season null: "last N seasons"/"past N seasons" -> the N most recent loaded seasons, counting the current one (e.g. "last 3 seasons" -> the last 3 of the loaded list); "this season and last" -> ["{previous}", "{current}"]; "since 2024-25" -> every loaded season from 2024-25 on. A single season always goes in season, never in seasons.
- "last N games" -> last_n N. "recently"/"lately" with no number -> last_n 5.
- "at home" -> venue "home"; "on the road"/"away" -> venue "away".
- "against <team>" or "<subject> vs <team>" -> opponent. For a team question like "Panathinaikos rebounds vs Zalgiris", the subject is the first team and the other is the opponent.
- "against teams over .500"/"winning teams"/"teams with a winning record" -> opponent_over_500 true.
- "when X doesn't play"/"without X" -> without_player; "with X"/"when X plays" -> with_player.
- "against <player>"/"when facing <player>"/"vs <player>" where the name is a player, not a team -> against_player (he played for the other team), e.g. "Hifi points against Theis" -> player "Hifi", against_player "Theis". opponent is only ever a team name: never a player, and never a description like "winning teams" (that is opponent_over_500).
- "back-to-back", "short rest", "two games in two days", "within 48 hours" -> max_rest_hours 48. "N days of rest or less" -> max_rest_hours N*24. "well rested", "3+ days of rest" -> min_rest_hours 72 (or N*24 if a number of days is given).
- A condition on another player's own numbers in the game ("when Theis plays 30+ minutes", "when Hifi scores 20 or more", "when Nebo gets under 5 rebounds") -> player_threshold {{player, stat, min and/or max}}. min and max are inclusive: "30+ minutes"/"at least 30"/"more than 30 minutes" -> min 30; "under 15 minutes"/"less than 15 minutes" -> max 15; minutes are not whole numbers, so keep the number as written for minutes ("more than 30 minutes" -> min 30, "fewer than 12 minutes" -> max 12); only for whole-number stats (points, rebounds, assists, makes, ...) shift by one: "more than 20 points" -> min 21, "fewer than 5 rebounds" -> max 4. A threshold on the subject player's own stat works the same way.
- "playoffs" -> phase "playoffs"; "Final Four" -> "final_four"; "regular season" -> "regular_season"; "play-in" -> "play_in".
- Stats: rebounds -> total_rebounds; "3-point %"/"three-point shooting" -> fg3_pct; "threes"/"3s made" -> fg3_made; "free throw %" -> ft_pct; "field goal %" or plain "shooting" -> fg_pct; "2-point %" -> fg2_pct; PIR/efficiency/index rating -> pir; fouls -> fouls_committed; "fouls drawn" -> fouls_received; "record"/"win percentage"/"how many wins"/"how did they do" -> win_pct. If no stat is named ("how did he play", "his stats") -> points, total_rebounds, assists.
- Leave every filter that the question does not mention at null (or false for opponent_over_500).
- compare is null unless the question compares conditions for the same subject ("X vs Y", "when ... and when ...", "more or less"). Then put what both sides share (subject, stats, season, ...) at the top level and give each side a short label and only the filters that differ. Examples:
  "Punter points with and without Satoransky this season" -> player "Punter", season "{current}", compare [{{label "with Satoransky", with_player "Satoransky"}}, {{label "without Satoransky", without_player "Satoransky"}}].
  "Olympiacos record home vs away" -> compare [{{label "home", venue "home"}}, {{label "away", venue "away"}}].
  "Real turnovers on short rest vs rested" -> compare [{{label "short rest", max_rest_hours 48}}, {{label "rested", min_rest_hours 72}}].
  "Nunn points this season vs last season" -> compare [{{label "{current}", season "{current}"}}, {{label "{previous}", season "{previous}"}}].

Set answerable false, fill reason with one short sentence, and leave the other fields empty/null/false when the question:
- is about another competition (NBA, EuroCup, Greek league, ACB, national teams, ...);
- needs data we do not have (shot locations, salaries, height, dunks, injuries, transfers, coaches, attendance, ...);
- compares or ranks several different players or teams ("who scored the most", "league leaders", "Nunn or Larkin") — one subject per question (comparing conditions for one subject is fine, see compare);
- asks for a prediction or opinion, or is not a basketball stats question at all.
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

def _error_summary(exc: ValidationError):
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'spec'}: {e['msg']}" for e in exc.errors()
    )


def parse_question(question, llm: LLM = None, today=None, seasons=None, max_attempts=2):
    """Ask the LLM for a spec and validate it, retrying once with the errors.

    Returns a ParseResult whose status is:
      ok            spec is a valid QuerySpec (names are not resolved yet)
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

        fields = {k: v for k, v in raw.items() if k not in ("answerable", "reason")}
        try:
            result.spec = QuerySpec(**fields)
            result.status, result.reason = "ok", None
            return result
        except ValidationError as exc:
            result.reason = f"The question couldn't be turned into a valid query: {_error_summary(exc)}"
            messages += [
                {"role": "assistant", "content": json.dumps(raw)},
                {"role": "user", "content": (
                    f"That spec failed validation: {_error_summary(exc)}. "
                    "Return a corrected spec, or set answerable false if the question can't be expressed."
                )},
            ]
    return result
