"""HTTP API for stat questions.

Run locally from the project root:
    uvicorn api.main:app --reload
then open http://127.0.0.1:8000/docs for interactive documentation.
"""
from typing import Literal

from functools import lru_cache

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from analysis.stat_query import (
    COUNTING_STATS,
    PERCENTAGE_STATS,
    RESULT_STATS,
    QueryError,
)
from api.service import answer_ranking, answer_spec, matchup_spec, player_profile_spec, player_rankings_spec, rankings_spec
from api.models import (
    AskRequest,
    AskResponse,
    ErrorResponse,
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
            message, result = answer_ranking(ranking)
        except QueryError as exc:
            return AskResponse(
                question=question, status="needs_clarification", message=str(exc),
                ranking_kind=ranking.kind, ranking_spec=spec,
                candidates=[{"id": i, "name": n} for i, n in exc.candidates],
            )
        return AskResponse(question=question, status="answered", message=message,
                           ranking_kind=ranking.kind, ranking_spec=spec, ranking_result=result)

    try:
        result = answer_spec(parsed.spec)
    except QueryError as exc:
        return AskResponse(
            question=question, status="needs_clarification", message=str(exc), spec=parsed.spec,
            candidates=[{"id": i, "name": n} for i, n in exc.candidates],
        )
    return AskResponse(question=question, status="answered", message=result.answer,
                       spec=parsed.spec, result=result)
