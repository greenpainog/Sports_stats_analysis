"""Question -> QuerySpec parsing and POST /ask, with a fake LLM (no key, no network)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import database.db as db
from api.main import app, get_llm
from api.models import QuerySpec
from llm.question_parser import LLMQuerySpec, build_instructions, parse_question
from tests.test_stat_query import build_test_league


def llm_output(**fields):
    """An LLMQuerySpec with every field empty except the ones given."""
    base = {name: None for name in LLMQuerySpec.model_fields}
    base.update(answerable=True, stats=[], opponent_over_500=False)
    base.update(fields)
    return LLMQuerySpec(**base)


class FakeLLM:
    """Returns the prepared outputs in order and records what it was sent."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, instructions, messages):
        self.calls.append((instructions, list(messages)))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


SEASONS = ["2024-25", "2025-26"]


def parse(question, *outputs):
    llm = FakeLLM(*outputs)
    return parse_question(question, llm=llm, today=date(2025, 11, 1), seasons=SEASONS), llm


# --- parse_question ----------------------------------------------------------

def test_llm_schema_has_every_queryspec_field():
    assert set(LLMQuerySpec.model_fields) == set(QuerySpec.model_fields) | {"answerable", "reason"}


def test_instructions_name_the_loaded_and_current_season():
    text = build_instructions(date(2025, 11, 1), SEASONS)
    assert "2024-25, 2025-26" in text and '"2025-26" is the current season' in text
    assert "2025-11-01" in text


def test_valid_output_becomes_a_queryspec():
    result, llm = parse("Nunn 3P% last 5", llm_output(stats=["fg3_pct"], player="Nunn", last_n=5))
    assert result.status == "ok" and result.attempts == 1
    assert result.spec == QuerySpec(stats=["fg3_pct"], player="Nunn", last_n=5)
    assert llm.calls[0][1] == [{"role": "user", "content": "Nunn 3P% last 5"}]


def test_unanswerable_passes_the_reason_through():
    result, _ = parse("LeBron in the NBA", llm_output(answerable=False, reason="Only EuroLeague is loaded."))
    assert (result.status, result.reason, result.spec) == ("unanswerable", "Only EuroLeague is loaded.", None)


def test_invalid_output_is_retried_with_the_errors():
    result, llm = parse(
        "Nunn's last 0 games",
        llm_output(stats=["points"], player="Nunn", last_n=0),   # fails QuerySpec: last_n >= 1
        llm_output(stats=["points"], player="Nunn", last_n=1),
    )
    assert result.status == "ok" and result.attempts == 2 and result.spec.last_n == 1
    retry_messages = llm.calls[1][1]
    assert retry_messages[1]["role"] == "assistant"
    assert "last_n" in retry_messages[2]["content"]


def test_still_invalid_after_retry():
    bad = llm_output(stats=["points"], player="Nunn", team="Alpha")  # two subjects
    result, llm = parse("Nunn and Alpha points", bad, bad)
    assert result.status == "invalid" and result.attempts == 2 and len(llm.calls) == 2
    assert "exactly one" in result.reason


def test_llm_failure_is_reported_not_raised():
    result, _ = parse("anything", RuntimeError("429 no credits"))
    assert result.status == "error" and "429 no credits" in result.reason


# --- POST /ask -----------------------------------------------------------------

@pytest.fixture
def ask(tmp_path):
    """Returns ask(question, *llm_outputs) -> response, against the hand-made league."""
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path / "league.db")
    build_test_league()
    client = TestClient(app)

    def _ask(question, *outputs):
        app.dependency_overrides[get_llm] = lambda: FakeLLM(*outputs)
        return client.post("/ask", json={"question": question})

    yield _ask
    app.dependency_overrides.clear()
    db.DATABASE_FILE = original


def test_ask_answers(ask):
    response = ask("Shooter's 3 point percentage", llm_output(stats=["fg3_pct"], player="Shooter"))
    body = response.json()
    assert response.status_code == 200 and body["status"] == "answered"
    assert body["result"]["summary"]["fg3_pct"]["pct"] == 45.0
    assert body["spec"]["player"] == "Shooter" and "9/20 = 45.0%" in body["message"]


def test_ask_unanswerable(ask):
    body = ask("Who wins the title?", llm_output(answerable=False, reason="Predictions aren't stats.")).json()
    assert (body["status"], body["message"], body["result"]) == ("unanswerable", "Predictions aren't stats.", None)


def test_ask_ambiguous_name_needs_clarification(ask):
    body = ask("Kick's assists", llm_output(stats=["assists"], player="Kick")).json()
    assert body["status"] == "needs_clarification"
    assert [c["name"] for c in body["candidates"]] == ["Olly Kick", "Sid Kick"]


def test_ask_llm_down_is_503(ask):
    response = ask("Shooter's points", RuntimeError("timeout"))
    assert response.status_code == 503 and response.json()["status"] == "error"


def test_ask_rejects_empty_question(ask):
    assert ask("").status_code == 422
