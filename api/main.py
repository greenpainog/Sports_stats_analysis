"""HTTP API for stat questions.

Run locally from the project root:
    uvicorn api.main:app --reload
then open http://127.0.0.1:8000/docs for interactive documentation.
"""
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from analysis.stat_query import COUNTING_STATS, PERCENTAGE_STATS, QueryError, query_stats
from api.models import ErrorResponse, QueryResponse, QuerySpec, ResolveResponse
from database.aliases import resolve_player, resolve_team

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
    return {"counting": list(COUNTING_STATS), "percentages": list(PERCENTAGE_STATS)}


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
    """Answer one QuerySpec: the matching games plus a summary per stat."""
    result = query_stats(**spec.model_dump())
    return QueryResponse(
        subject=result.subject,
        subject_type=result.subject_type,
        filters=result.filters,
        games=[asdict(game) for game in result.games],
        summary={stat: asdict(summary) for stat, summary in result.summary.items()},
        answer=result.describe(),
    )
