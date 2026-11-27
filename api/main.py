"""HTTP API for stat questions.

Run locally from the project root:
    uvicorn api.main:app --reload
then open http://127.0.0.1:8000/docs for interactive documentation.
"""
from datetime import date
from functools import lru_cache
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from langgraph.checkpoint.memory import InMemorySaver
from openai import OpenAIError

from analysis.stat_query import (
    COUNTING_STATS,
    PERCENTAGE_STATS,
    RESULT_STATS,
    QueryError,
)
from api.service import (
    answer_ranking_for_question,
    answer_spec,
    answer_spec_for_question,
    lineups_spec,
    matchup_spec,
    names_season,
    player_profile_spec,
    player_rankings_spec,
    rankings_spec,
)
from api.models import (
    AgentRequest,
    AgentResponse,
    AgentStep,
    AskRequest,
    AskResponse,
    ErrorResponse,
    LineupSpec,
    LineupsResponse,
    MatchupResponse,
    MatchupSpec,
    PlayerProfileResponse,
    PlayerProfileSpec,
    PlayerRankingSpec,
    PlayerRankingsResponse,
    QueryResponse,
    QuerySpec,
    RankingSpec,
    RankingsResponse,
    ResolveResponse,
)
from agent.graph import build_graph, openai_llm, run as run_agent
from database.aliases import resolve_player, resolve_team
from llm.question_parser import OpenAILLM, parse_question

app = FastAPI(
    title="European basketball stats",
    description="Answers stat questions about Euroleague games from a structured QuerySpec.",
    version="0.1.0",
)


@app.exception_handler(QueryError)
def query_error_handler(request: Request, exc: QueryError):
    """A question that can't be answered as asked (unknown or ambiguous name, ...)."""
    body = ErrorResponse(
        detail=str(exc),
        candidates=[{"id": i, "name": n} for i, n in exc.candidates],
    )
    return JSONResponse(status_code=400, content=body.model_dump())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats")
def list_stats():
    """The stat names a QuerySpec may use."""
    return {
        "counting": list(COUNTING_STATS),
        "percentages": list(PERCENTAGE_STATS),
        "results": list(RESULT_STATS),
    }


@app.get("/resolve/{kind}", response_model=ResolveResponse)
def resolve(kind: Literal["team", "player"], name: str = Query(min_length=1)):
    """Look up a messy team or player name, e.g. /resolve/team?name=Real."""
    result = (resolve_team if kind == "team" else resolve_player)(name)
    return ResolveResponse(
        query=result.query,
        status=result.status,
        method=result.method,
        candidates=[{"id": i, "name": n} for i, n in result.candidates],
    )


@app.post(
    "/query",
    response_model=QueryResponse,
    responses={400: {"model": ErrorResponse, "description": "Unknown or ambiguous name"}},
)
def run_query(spec: QuerySpec):
    """Answer one QuerySpec: the matching games plus a summary per stat (and per side, with compare)."""
    return answer_spec(spec)


@app.post("/rankings", response_model=RankingsResponse)
def rankings(spec: RankingSpec):
    """Every team's value and rank for each stat, e.g. free throws made per game this season."""
    return rankings_spec(spec)


@app.post(
    "/matchup",
    response_model=MatchupResponse,
    responses={400: {"model": ErrorResponse, "description": "Unknown or ambiguous team, or no games"}},
)
def team_matchup(spec: MatchupSpec):
    """Two teams side by side with their league rank for every stat, e.g. before they play each other."""
    return matchup_spec(spec)


@app.post("/player-rankings", response_model=PlayerRankingsResponse)
def rank_players(spec: PlayerRankingSpec):
    """Every qualified player's value and rank for each stat: traditional, advanced, scoring and misc."""
    return player_rankings_spec(spec)


@app.post(
    "/player-profile",
    response_model=PlayerProfileResponse,
    responses={400: {"model": ErrorResponse, "description": "Unknown or ambiguous player, or no games"}},
)
def profile(spec: PlayerProfileSpec):
    """One player's value and league rank in every stat."""
    return player_profile_spec(spec)


@app.post(
    "/lineups",
    response_model=LineupsResponse,
    responses={400: {"model": ErrorResponse, "description": "Unknown or ambiguous team or player"}},
)
def lineups(spec: LineupSpec):
    """A team's duos, trios or 5-man lineups ranked by plus-minus per 40 minutes together (or another sort).

    No minimum-minutes cutoff: every group that shared the floor is listed, and
    groups under 40 minutes together are flagged small_sample.
    """
    return lineups_spec(spec)


@lru_cache
def get_llm():
    """The LLM used by /ask. Tests replace it via app.dependency_overrides."""
    return OpenAILLM()


@app.post("/ask", response_model=AskResponse, responses={503: {"model": AskResponse}})
def ask(request: AskRequest, llm=Depends(get_llm)):
    """Answer a plain-language question.

    The LLM turns the question into a QuerySpec (or, for "who leads" and "where
    does X rank" questions, a ranking request); it is validated and run exactly
    like POST /query or the ranking endpoints. The LLM never writes SQL.
    """
    question = request.question
    parsed = parse_question(question, llm=llm)
    if parsed.status == "error":
        body = AskResponse(question=question, status="error",
                           message="The question service is unavailable right now. Try POST /query instead.")
        return JSONResponse(status_code=503, content=body.model_dump())
    if parsed.status in ("unanswerable", "invalid"):
        return AskResponse(question=question, status=parsed.status, message=parsed.reason)

    if parsed.ranking is not None:
        ranking = parsed.ranking
        spec = {**ranking.spec.model_dump(exclude_none=True), **({"team": ranking.focus_team}
                                                                if ranking.focus_team else {})}
        try:
            answer = answer_ranking_for_question(ranking, names_season(question))
        except QueryError as exc:
            return AskResponse(
                question=question, status="needs_clarification", message=str(exc),
                ranking_kind=ranking.kind, ranking_spec=spec,
                candidates=[{"id": i, "name": n} for i, n in exc.candidates],
            )
        return AskResponse(question=question, status="answered", message=answer.text,
                           ranking_kind=ranking.kind, ranking_spec=spec, ranking_result=answer.result,
                           **_fallback_fields(answer))

    try:
        answer = answer_spec_for_question(parsed.spec, names_season(question))
    except QueryError as exc:
        return AskResponse(
            question=question, status="needs_clarification", message=str(exc), spec=parsed.spec,
            candidates=[{"id": i, "name": n} for i, n in exc.candidates],
        )
    return AskResponse(question=question, status="answered", message=answer.text,
                       spec=parsed.spec, result=answer.result, **_fallback_fields(answer))


def _fallback_fields(answer):
    """The AskResponse fields that say whether last season stood in for, or was added to, this one."""
    if answer.fallback is None:
        return {}
    return {"season_fallback": answer.fallback, "fallback_season": answer.previous,
            "previous_season_result": answer.previous_result}


def _without_nulls(value):
    """The LLM fills every optional field with null; leave those out of the steps shown."""
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


# Every /agent conversation, by conversation_id. Shared by each day's graph so
# a conversation survives the daily rebuild. In memory: one server process
# only, and lost on restart.
CONVERSATIONS = InMemorySaver()


@lru_cache(maxsize=1)
def _agent_for(today):
    return build_graph(openai_llm(), today=today, checkpointer=CONVERSATIONS)


def get_agent():
    """The agent graph used by /agent. Tests replace it via app.dependency_overrides.

    It is built once per day: the system prompt says what today is, and
    building the graph each request would redo that work for nothing.
    """
    return _agent_for(date.today())


@app.post("/agent", response_model=AgentResponse, responses={503: {"model": AgentResponse}})
def agent(request: AgentRequest, graph=Depends(get_agent)):
    """Answer a question that may need several lookups (multi-step agent).

    The LLM plans tool calls (the same validated queries as the other
    endpoints, never SQL), reads each result and calls more until it can
    answer: at most 6 calls. Every number in the answer is checked against the
    tool results before it is returned; `steps` shows each call and result.
    Slower than /ask (several LLM calls), so use /ask for one-lookup questions.
    Send the returned conversation_id with the next question for a follow-up.
    """
    conversation_id = request.conversation_id or uuid4().hex
    try:
        result = run_agent(graph, request.question, conversation_id)
    except OpenAIError as exc:  # network, rate limit, bad key: the LLM's fault, not the question's
        body = AgentResponse(question=request.question, conversation_id=conversation_id, status="error",
                             answer=f"The agent's LLM is unavailable right now ({type(exc).__name__}). Try /ask.")
        return JSONResponse(status_code=503, content=body.model_dump())
    return AgentResponse(
        question=request.question, conversation_id=conversation_id, status="answered", answer=result.answer, check=result.check,
        steps=[AgentStep(tool=name, args=_without_nulls(args), result=text) for name, args, text in result.steps],
    )
