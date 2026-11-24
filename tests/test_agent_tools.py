"""The agent's tools on the small hand-made league from test_stat_query.

No LLM is involved: run_tool gets the arguments an LLM would send.
"""
import pytest

import database.db as db
from agent.tools import TOOLS, run_tool
from tests.test_stat_query import build_test_league


@pytest.fixture(scope="module", autouse=True)
def league(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("agent") / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


def test_query_stats_gives_the_same_answer_as_the_query_layer():
    # Shooter: 1/1, 6/14, 2/5 threes -> 9/20.
    text = run_tool("query_stats", {"player": "Sam Shooter", "stats": ["fg3_pct"]})
    assert "9/20 = 45.0%" in text


def test_compare_runs_every_side():
    text = run_tool("query_stats", {
        "team": "Alpha", "stats": ["win_pct"],
        "compare": [{"label": "home", "venue": "home"}, {"label": "away", "venue": "away"}]})
    assert "home" in text and "away" in text


def test_team_roster_lists_who_played():
    text = run_tool("team_roster", {"team": "Alpha", "season": "2024-25"})
    assert text.startswith("Alpha, euroleague 2024-25: 4 games played.")
    assert "Sam Shooter: 3 games" in text and "Sid Kick: 3 games" in text
    assert "Olly Kick" not in text  # plays for Gamma


def test_resolve_name_reports_ambiguity():
    assert "ambiguous" in run_tool("resolve_name", {"kind": "player", "name": "Kick"})
    assert run_tool("resolve_name", {"kind": "team", "name": "Alpha"}) == "'Alpha' is Alpha."


@pytest.mark.parametrize("name, args, expected", [
    ("query_stats", {"player": "Nobody", "stats": ["assists"]}, "Error: No player named"),
    ("query_stats", {"player": "Kick", "stats": ["assists"]}, "Candidates: "),
    ("query_stats", {"player": "Sam Shooter", "stats": ["dunks"]}, "Error: invalid arguments for query_stats: stats.0"),
    ("team_roster", {"team": "Alpha", "season": "last year"}, "Error: invalid arguments for team_roster: season"),
    ("team_roster", {"team": "Nowhere", "season": "2024-25"}, "Error: Team 'Nowhere' is not found"),
    ("drop_tables", {}, "Error: there is no tool named 'drop_tables'"),
])
def test_problems_come_back_as_text_not_exceptions(name, args, expected):
    assert expected in run_tool(name, args)


def test_every_tool_has_an_argument_model_and_a_description():
    for tool in TOOLS.values():
        assert tool.description and tool.args.model_json_schema()["type"] == "object"
