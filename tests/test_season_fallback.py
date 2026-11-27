"""This season with few or no games falls back on last season, unless the user named the season.

In the hand-made league 2025-26 is the current season and Alpha has played
1 game in it (g6 vs Gamma); 2024-25 has the rest. Alpha never met Beta in 2025-26.
"""
import pytest
from fastapi.testclient import TestClient

import database.db as db
from agent.tools import run_tool
from api.main import app, get_llm
from api.models import QuerySpec
from api.service import answer_spec_for_question, names_season, previous_season
from tests.test_question_parser import FakeLLM, llm_output
from tests.test_stat_query import build_test_league


@pytest.fixture(scope="module", autouse=True)
def league(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("fallback") / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


@pytest.mark.parametrize("question, named", [
    ("Alpha record this season", True),
    ("Alpha record in 2024-25", True),
    ("Alpha record 24/25", True),
    ("Alpha record so far", True),
    ("Alpha record last year", True),
    ("Alpha thsi season record", True),
    ("Alpha record", False),
    ("Alpha record against Beta away", False),
    ("Shooter points in his last 5 games", False),
])
def test_names_season(question, named):
    assert names_season(question) is named


def test_previous_season_only_for_the_current_one():
    assert previous_season("2025-26") == "2024-25"
    assert previous_season("2024-25") is None
    assert previous_season(None) is None


def test_few_games_adds_last_season():
    answer = answer_spec_for_question(QuerySpec(stats=["win_pct"], team="Alpha", season="2025-26"), False)
    assert answer.fallback == "with_previous_season"
    assert len(answer.result.games) == 1 and len(answer.previous_result.games) > 1
    assert answer.text.startswith("SMALL SAMPLE: only 1 game in 2025-26 so far")
    assert "Last season (2024-25), for context:" in answer.text


def test_no_games_uses_last_season_instead():
    answer = answer_spec_for_question(QuerySpec(stats=["win_pct"], team="Alpha", opponent="Beta",
                                                season="2025-26"), False)
    assert answer.fallback == "previous_season"
    assert answer.result.filters["season"] == "2024-25"
    assert answer.text.startswith("No matching games in 2025-26 yet, so this is last season (2024-25):")


def test_named_season_last_n_and_old_seasons_are_left_alone():
    for spec, named in [(QuerySpec(stats=["win_pct"], team="Alpha", season="2025-26"), True),
                        (QuerySpec(stats=["win_pct"], team="Alpha", last_n=2), False),
                        (QuerySpec(stats=["win_pct"], team="Alpha", season="2024-25"), False)]:
        assert answer_spec_for_question(spec, named).fallback is None


def test_ask_reports_the_fallback():
    client = TestClient(app)
    app.dependency_overrides[get_llm] = lambda: FakeLLM(llm_output(stats=["win_pct"], team="Alpha", season="2025-26"))
    try:
        body = client.post("/ask", json={"question": "Alpha record"}).json()
        assert body["season_fallback"] == "with_previous_season"
        assert body["fallback_season"] == "2024-25"
        assert body["previous_season_result"]["filters"]["season"] == "2024-25"
        app.dependency_overrides[get_llm] = lambda: FakeLLM(
            llm_output(stats=["win_pct"], team="Alpha", season="2025-26"))
        body = client.post("/ask", json={"question": "Alpha record this season"}).json()
        assert body["season_fallback"] is None and "SMALL SAMPLE" not in body["message"]
    finally:
        app.dependency_overrides.clear()


def test_agent_tools_follow_the_question():
    args = {"stats": ["win_pct"], "team": "Alpha", "season": "2025-26"}
    assert "SMALL SAMPLE" in run_tool("query_stats", args, season_named=False)
    assert "SMALL SAMPLE" not in run_tool("query_stats", args, season_named=True)
    ranked = run_tool("team_rankings", {"stats": ["total_rebounds"], "season": "2025-26"}, season_named=False)
    assert "Last season (2024-25)" in ranked
