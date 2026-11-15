"""The eval set is well formed, and the scorer grades answers correctly."""
import pytest

import database.db as db
from api.models import QuerySpec
from eval.run_eval import gold_ranking, load_questions, score
from llm.question_parser import ParseResult
from tests.test_stat_query import build_test_league


def test_eval_set_is_well_formed():
    questions = load_questions()
    assert len(questions) >= 50
    assert len({q["id"] for q in questions}) == len(questions)
    for q in questions:
        if q["gold"] is not None and "ranking" in q["gold"]:
            gold_ranking(q["gold"])  # raises if a gold ranking request is invalid
        elif q["gold"] is not None:
            QuerySpec(**q["gold"])  # raises if a gold spec is invalid
    assert sum(q["gold"] is None for q in questions) >= 5  # some must be unanswerable


@pytest.fixture
def league(tmp_path):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


def ok(**spec):
    return ParseResult(status="ok", spec=QuerySpec(**spec))


GOLD = {"gold": {"stats": ["fg3_pct", "assists"], "player": "Sam Shooter"}}


def test_same_answer_with_a_different_name_spelling_is_correct(league):
    correct, exact, _ = score(GOLD, ok(stats=["assists", "fg3_pct"], player="Shooter"))
    assert correct and exact


def test_an_extra_filter_is_wrong(league):
    correct, _, note = score(GOLD, ok(stats=["fg3_pct", "assists"], player="Shooter", last_n=1))
    assert not correct and "games" in note


def test_a_missing_stat_is_wrong(league):
    correct, _, _ = score(GOLD, ok(stats=["fg3_pct"], player="Shooter"))
    assert not correct


def test_an_unresolvable_name_is_wrong(league):
    correct, _, note = score(GOLD, ok(stats=["fg3_pct", "assists"], player="Kick"))
    assert not correct and "query failed" in note


def test_unanswerable_scoring():
    assert score({"gold": None}, ParseResult(status="unanswerable", reason="NBA"))[0]
    assert not score({"gold": None}, ok(stats=["points"], player="Shooter"))[0]
    assert not score(GOLD, ParseResult(status="unanswerable", reason="?"))[0]


def test_compare_sides_are_scored_by_result_not_order_or_label(league):
    gold = {"gold": {"stats": ["total_rebounds"], "team": "Alpha", "compare": [
        {"label": "without", "without_player": "Sid Kick"}, {"label": "with", "with_player": "Sid Kick"}]}}
    swapped = ok(stats=["total_rebounds"], team="Alpha", compare=[
        {"label": "Kick plays", "with_player": "S. Kick"},
        {"label": "Kick out", "without_player": "Sid Kick"}])
    assert score(gold, swapped)[:2] == (True, True)
    one_side_wrong = ok(stats=["total_rebounds"], team="Alpha", compare=[
        {"without_player": "Sid Kick"}, {"venue": "home"}])
    correct, _, note = score(gold, one_side_wrong)
    assert not correct and "sides" in note
