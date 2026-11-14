"""API tests (pytest + FastAPI's TestClient) on the hand-made league.

TestClient calls the app directly, so no server has to be running.
"""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import database.db as db
from analysis.stat_query import COUNTING_STATS
from api.main import app
from api.models import QuerySpec
from ingestion.euroleague import STAT_COLUMNS
from tests.test_stat_query import build_test_league


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("api") / "league.db")
    build_test_league()
    yield TestClient(app)
    db.DATABASE_FILE = original


# --- QuerySpec validation (no database needed) -------------------------------

def test_queryspec_accepts_a_valid_question():
    spec = QuerySpec(stats=["fg3_pct"], player="Nunn", last_n=5)
    assert spec.venue is None and spec.opponent_over_500 is False


@pytest.mark.parametrize("bad", [
    {"stats": ["fg3_pct"]},                                        # no subject
    {"stats": ["fg3_pct"], "player": "Nunn", "team": "Panathinaikos"},  # two subjects
    {"stats": [], "player": "Nunn"},                               # no stats
    {"stats": ["dunks"], "player": "Nunn"},                        # unknown stat
    {"stats": ["assists", "assists"], "player": "Nunn"},           # duplicate stat
    {"stats": ["assists"], "player": "Nunn", "venue": "road"},     # bad venue
    {"stats": ["assists"], "player": "Nunn", "last_n": 0},         # last_n too small
    {"stats": ["assists"], "player": "Nunn", "season": "2024"},    # bad season format
    {"stats": ["assists"], "player": "Nunn", "sql": "DROP TABLE"},  # unknown field
    {"stats": ["assists"], "player": "Nunn", "max_rest_hours": -5},  # negative rest
    {"stats": ["assists"], "player": "Nunn", "min_rest_hours": 72, "max_rest_hours": 48},
])
def test_queryspec_rejects_invalid_questions(bad):
    with pytest.raises(ValidationError):
        QuerySpec(**bad)


def test_stat_list_matches_what_ingestion_stores():
    assert set(COUNTING_STATS) == set(STAT_COLUMNS.values()) | {"minutes", "plus_minus"}


# --- Endpoints ---------------------------------------------------------------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_stats_endpoint(client):
    body = client.get("/stats").json()
    assert "assists" in body["counting"] and "fg3_pct" in body["percentages"]


def test_query_returns_rows_and_summary(client):
    response = client.post("/query", json={"stats": ["fg3_pct", "assists"], "player": "Shooter"})
    assert response.status_code == 200
    body = response.json()
    assert body["subject"] == "Sam Shooter"
    assert [g["values"]["fg3_pct"] for g in body["games"]] == ["1/1", "6/14", "2/5"]
    assert body["summary"]["fg3_pct"]["pct"] == 45.0
    assert body["summary"]["assists"]["total"] == 12
    assert "9/20 = 45.0%" in body["answer"]


def test_query_with_filters(client):
    body = client.post("/query", json={
        "stats": ["total_rebounds"], "team": "Alpha", "without_player": "Sid Kick",
    }).json()
    assert [g["game_id"] for g in body["games"]] == [4]
    assert body["filters"] == {"without_player": "Sid Kick"}


def test_query_with_player(client):
    body = client.post("/query", json={
        "stats": ["fg3_pct"], "player": "Shooter", "with_player": "Sid Kick",
    }).json()
    assert [g["game_id"] for g in body["games"]] == [2, 3]
    assert body["filters"] == {"with_player": "Sid Kick"}


def test_query_rest_and_win_pct(client):
    body = client.post("/query", json={
        "stats": ["win_pct", "total_rebounds"], "team": "Alpha", "max_rest_hours": 48,
    }).json()
    (game,) = body["games"]
    assert (game["start_time"][:10], game["rest_hours"], game["opponent_rest_hours"]) == ("2024-10-24", 48, 216)
    assert game["values"]["win_pct"] == "L"
    assert (body["summary"]["win_pct"]["wins"], body["summary"]["win_pct"]["losses"]) == (0, 1)


def test_invalid_spec_is_422(client):
    response = client.post("/query", json={"stats": ["dunks"], "player": "Shooter"})
    assert response.status_code == 422


def test_unknown_name_is_400(client):
    response = client.post("/query", json={"stats": ["assists"], "player": "Nobody Atall"})
    assert response.status_code == 400
    assert "Nobody Atall" in response.json()["detail"]


def test_ambiguous_name_is_400_with_candidates(client):
    response = client.post("/query", json={"stats": ["assists"], "player": "Kick"})
    assert response.status_code == 400
    names = [c["name"] for c in response.json()["candidates"]]
    assert names == ["Olly Kick", "Sid Kick"]


def test_resolve_endpoint(client):
    body = client.get("/resolve/team", params={"name": "alpha"}).json()
    assert body["status"] == "match" and body["candidates"][0]["name"] == "Alpha"
    assert client.get("/resolve/player", params={"name": "Kick"}).json()["status"] == "ambiguous"
    assert client.get("/resolve/coach", params={"name": "x"}).status_code == 422


def test_resolve_by_first_name_as_last_resort(client):
    body = client.get("/resolve/player", params={"name": "Sam"}).json()
    assert (body["status"], body["method"], body["candidates"][0]["name"]) == ("match", "first_name", "Sam Shooter")
    # A surname still wins over a first name.
    assert client.get("/resolve/player", params={"name": "Shooter"}).json()["method"] == "exact"
