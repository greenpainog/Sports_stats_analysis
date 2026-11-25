"""The agent eval's scoring: no LLM, facts from the hand-made league."""
import pytest

import database.db as db
from eval.run_agent_eval import declines, gold_value, has_name, has_number, load_questions
from tests.test_stat_query import build_test_league


@pytest.fixture(scope="module", autouse=True)
def league(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("agent_eval") / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


@pytest.mark.parametrize("answer, value, expected", [
    ("He averages 16.7 points.", 16.67, True),   # rounded to one decimal
    ("He averages 16.6 points.", 16.67, False),
    ("8 wins and 2 losses", 2, True),
    ("ranks 3rd", 3, True),
    ("1,060 points", 1060, True),
    ("no numbers here", 5, False),
    ("anything", None, False),
])
def test_has_number(answer, value, expected):
    assert has_number(answer, value) is expected


def test_names_match_on_surname_or_alias():
    assert has_name("Sam Shooter leads.", "player", "Sam Shooter")
    assert has_name("Shooter's 45%", "player", "Sam Shooter")
    assert not has_name("Sid Kick leads.", "player", "Sam Shooter")
    assert has_name("ALPHA are first", "team", "Alpha")


@pytest.mark.parametrize("answer, expected", [
    ("I can’t predict who will win.", True),
    ("The data doesn't have salaries.", True),
    ("Alpha will win.", False),
])
def test_declines(answer, expected):
    assert declines(answer) is expected


def test_gold_comes_from_the_reference_query():
    # Shooter's threes: 9/20 = 45.0%, from the same query_stats code /ask uses.
    fact = {"query": {"player": "Sam Shooter", "stats": ["fg3_pct"]}, "stat": "fg3_pct", "field": "pct"}
    assert gold_value(fact) == ("number", 45.0)
    assert gold_value({"declines": True}) == ("declines", None)


def test_question_file_is_well_formed():
    questions = load_questions()
    assert len({q["id"] for q in questions}) == len(questions) >= 15
    for q in questions:
        assert q["turns"] and all(turn["question"] and turn["facts"] for turn in q["turns"])
