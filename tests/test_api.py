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
    {"stats": ["assists"], "player": "Nunn", "compare": [{"venue": "home"}]},                    # one side
    {"stats": ["assists"], "player": "Nunn", "compare": [{"label": "a"}, {"venue": "home"}]},   # empty side
    {"stats": ["assists"], "player": "Nunn", "compare": [{"venue": "home"}, {"venue": "home"}]},  # same twice
    {"stats": ["assists"], "player": "Nunn", "compare": [{"player": "X"}, {"venue": "home"}]},  # subject in a side
    {"stats": ["assists"], "player": "Nunn", "max_rest_hours": 48,
     "compare": [{"min_rest_hours": 72}, {"venue": "home"}]},                                   # invalid once merged
    {"stats": ["assists"], "team": "Alpha", "player_threshold": {"player": "Shooter", "stat": "assists"}},  # no range
    {"stats": ["assists"], "team": "Alpha",
     "player_threshold": {"player": "Shooter", "stat": "fg3_pct", "min": 1}},                  # not a per-game stat
    {"stats": ["assists"], "team": "Alpha",
     "player_threshold": {"player": "Shooter", "stat": "assists", "min": 5, "max": 2}},       # min > max
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


def test_compare_with_and_without_a_player(client):
    body = client.post("/query", json={
        "stats": ["total_rebounds", "win_pct"], "team": "Alpha",
        "compare": [{"label": "without Kick", "without_player": "Sid Kick"},
                    {"label": "with Kick", "with_player": "Sid Kick"}],
    }).json()
    without, with_ = body["comparison"]
    assert (without["label"], with_["label"]) == ("without Kick", "with Kick")
    without_ids = [g["game_id"] for g in without["result"]["games"]]
    with_ids = [g["game_id"] for g in with_["result"]["games"]]
    assert without_ids == [4]
    assert not set(without_ids) & set(with_ids)
    assert set(without_ids) | set(with_ids) <= {g["game_id"] for g in body["games"]}
    assert "without Kick" in body["answer"] and "with Kick" in body["answer"]


def test_compare_home_and_away_splits_all_games(client):
    body = client.post("/query", json={
        "stats": ["total_rebounds"], "team": "Alpha",
        "compare": [{"venue": "home"}, {"venue": "away"}, {"venue": "neutral"}],  # g4 is neutral
    }).json()
    sides = [side["result"] for side in body["comparison"]]
    assert body["comparison"][0]["label"] == "venue=home"  # default label
    assert sum(len(side["games"]) for side in sides) == len(body["games"])
    assert (sum(side["summary"]["total_rebounds"]["total"] for side in sides)
            == body["summary"]["total_rebounds"]["total"])


def test_no_compare_means_no_comparison(client):
    assert client.post("/query", json={"stats": ["total_rebounds"], "team": "Alpha"}).json()["comparison"] is None


# Shooter's assists: g2 2, g3 4, g4 6. He did not play g6 or g7.
def _threshold(**bounds):
    return {"player": "Shooter", "stat": "assists", **bounds}


def test_player_threshold_min_and_max(client):
    at_least_4 = client.post("/query", json={
        "stats": ["total_rebounds"], "team": "Alpha", "player_threshold": _threshold(min=4)}).json()
    assert [g["game_id"] for g in at_least_4["games"]] == [3, 4]
    assert at_least_4["filters"] == {"player_threshold": "Sam Shooter assists >= 4"}
    # Games he missed never count, so "at most 2" is only g2, not g6 or g7.
    at_most_2 = client.post("/query", json={
        "stats": ["total_rebounds"], "team": "Alpha", "player_threshold": _threshold(max=2)}).json()
    assert [g["game_id"] for g in at_most_2["games"]] == [2]


def test_player_threshold_on_the_subjects_own_stat(client):
    body = client.post("/query", json={
        "stats": ["fg3_pct"], "player": "Shooter", "player_threshold": _threshold(min=4)}).json()
    assert body["summary"]["fg3_pct"]["made"] == 8 and body["summary"]["fg3_pct"]["attempted"] == 19


def test_compare_player_threshold_splits_his_games(client):
    body = client.post("/query", json={
        "stats": ["total_rebounds"], "team": "Alpha",
        "compare": [{"label": "4+", "player_threshold": _threshold(min=4)},
                    {"label": "under 4", "player_threshold": _threshold(max=3)}],
    }).json()
    high, low = ([g["game_id"] for g in side["result"]["games"]] for side in body["comparison"])
    assert (sorted(high), low) == ([3, 4], [2])


def test_player_threshold_unknown_player_is_400(client):
    response = client.post("/query", json={
        "stats": ["total_rebounds"], "team": "Alpha",
        "player_threshold": {"player": "Nobody Atall", "stat": "minutes", "min": 10}})
    assert response.status_code == 400
