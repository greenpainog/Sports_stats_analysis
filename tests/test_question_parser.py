"""Question -> QuerySpec parsing and POST /ask, with a fake LLM (no key, no network)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import database.db as db
from api.main import app, get_llm
from api.models import QuerySpec
from llm.question_parser import LLMCompareSide, LLMQuerySpec, build_instructions, parse_question
from tests.test_stat_query import build_test_league


@pytest.fixture(scope="module", autouse=True)
def test_league(tmp_path_factory):
    """Every test reads the hand-made league, never data/sports_analytics.db.

    The prompt lists the loaded seasons, which build_instructions reads from
    the database: without this the tests depended on the real data being there.
    """
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("parser") / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


def llm_output(**fields):
    """An LLMQuerySpec with every field empty except the ones given."""
    base = {name: None for name in LLMQuerySpec.model_fields}
    base.update(answerable=True, stats=[], opponent_over_500=False, clutch=False)
    base.update(fields)
    return LLMQuerySpec(**base)


def side(**fields):
    """An LLMCompareSide with every filter empty except the ones given."""
    base = {name: None for name in LLMCompareSide.model_fields}
    base.update(opponent_over_500=False, clutch=False)
    base.update(fields)
    return LLMCompareSide(**base)


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
    assert set(LLMQuerySpec.model_fields) == set(QuerySpec.model_fields) | {"answerable", "reason", "ranking"}
    from api.models import CompareSide, PlayerThreshold
    from llm.question_parser import LLMPlayerThreshold
    assert set(LLMCompareSide.model_fields) == set(CompareSide.model_fields)
    assert set(LLMPlayerThreshold.model_fields) == set(PlayerThreshold.model_fields)


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


def test_ask_compare(ask):
    body = ask("Alpha rebounds with and without Kick", llm_output(
        stats=["total_rebounds"], team="Alpha",
        compare=[side(label="without Kick", without_player="Sid Kick"), side(label="with Kick", with_player="Sid Kick")],
    )).json()
    assert body["status"] == "answered"
    assert [s["label"] for s in body["result"]["comparison"]] == ["without Kick", "with Kick"]
    assert body["spec"]["compare"][0] == {**{k: None for k in LLMCompareSide.model_fields},
                                          "opponent_over_500": False, "clutch": False, "label": "without Kick",
                                          "competition": "euroleague",  # unset by the LLM -> the default
                                          "without_player": "Sid Kick"}


def test_empty_compare_side_is_retried():
    result, _ = parse(
        "Nunn home vs away",
        llm_output(stats=["points"], player="Nunn", compare=[side(label="home", venue="home"), side(label="away")]),
        llm_output(stats=["points"], player="Nunn", compare=[side(label="home", venue="home"), side(label="away", venue="away")]),
    )
    assert result.status == "ok" and result.attempts == 2
    assert [label for label, _ in result.spec.side_specs()] == ["home", "away"]


# --- Ranking questions ---------------------------------------------------------

from llm.question_parser import LLMRanking  # noqa: E402


def ranking(**fields):
    """An LLMRanking with every field empty except the ones given."""
    base = {name: None for name in LLMRanking.model_fields}
    base.update(fields)
    return LLMRanking(**base)


def test_ranking_output_becomes_a_ranking_request():
    result, _ = parse("Who leads in assists?", llm_output(ranking=ranking(kind="player_rankings", stats=["assists"])))
    assert result.status == "ok" and result.spec is None
    assert result.ranking.kind == "player_rankings" and result.ranking.spec.limit == 10  # default for /ask
    result, _ = parse("Where does Alpha rank in rebounds?", llm_output(
        ranking=ranking(kind="team_rankings", team="Alpha", stats=["total_rebounds"])))
    assert (result.ranking.focus_team, result.ranking.spec.stats) == ("Alpha", ["total_rebounds"])


def test_a_field_the_kind_doesnt_use_is_retried():
    result, llm = parse(
        "Alpha vs Beta",
        llm_output(ranking=ranking(kind="matchup", team="Alpha", player="Shooter")),  # no other_team; player
        llm_output(ranking=ranking(kind="matchup", team="Alpha", other_team="Beta")),
    )
    assert result.status == "ok" and result.attempts == 2 and result.ranking.spec.team_b == "Beta"
    assert "does not use: player" in llm.calls[1][1][2]["content"]


def test_per40_is_rejected_for_teams():
    bad = llm_output(ranking=ranking(kind="team_rankings", per="per40"))
    result, _ = parse("team points per 40", bad, bad)
    assert result.status == "invalid" and "players only" in result.reason


def test_ask_where_does_a_team_rank(ask):
    body = ask("Where does Alpha rank in rebounds?", llm_output(
        ranking=ranking(kind="team_rankings", team="alpha", stats=["total_rebounds"], season="2024-25"))).json()
    assert body["status"] == "answered" and body["ranking_kind"] == "team_rankings"
    assert "Alpha (4 games)" in body["message"] and "31.5 (#1/3)" in body["message"]
    assert body["ranking_spec"]["team"] == "alpha" and len(body["ranking_result"]["teams"]) == 3


def test_ask_full_table_and_lowest_first(ask):
    body = ask("Fewest rebounds?", llm_output(
        ranking=ranking(kind="team_rankings", stats=["total_rebounds"], season="2024-25", order="lowest"))).json()
    lines = body["message"].splitlines()
    assert lines[1].strip() == "1. Beta  25.5" and lines[3].strip() == "3. Alpha  31.5"


def test_ask_player_leaders_and_profile(ask):
    body = ask("Assist leaders", llm_output(
        ranking=ranking(kind="player_rankings", stats=["assists"], season="2024-25"))).json()
    assert "1. Sam Shooter (Alpha)  4.0  [3 games]" in body["message"]
    body = ask("Shooter's ranks", llm_output(
        ranking=ranking(kind="player_profile", player="Shooter", stats=["assists"], season="2024-25"))).json()
    assert body["ranking_kind"] == "player_profile" and "4.0 (#1/2)" in body["message"]


def test_ask_ranking_with_unknown_team_needs_clarification(ask):
    body = ask("Where does Omega rank?", llm_output(
        ranking=ranking(kind="team_rankings", team="Omega", stats=["total_rebounds"]))).json()
    assert body["status"] == "needs_clarification" and "Omega" in body["message"]
    body = ask("Kick's ranks", llm_output(ranking=ranking(kind="player_profile", player="Kick"))).json()
    assert body["status"] == "needs_clarification" and len(body["candidates"]) == 2


def test_unset_competition_keeps_the_default_and_a_side_inherits_it():
    result, _ = parse("Bartley points in the Greek league home vs away", llm_output(
        stats=["points"], player="Bartley", competition="greek_league",
        compare=[side(label="home", venue="home"), side(label="away", venue="away")]))
    assert result.spec.competition == "greek_league"
    assert [spec.competition for _, spec in result.spec.side_specs()] == ["greek_league", "greek_league"]
    plain, _ = parse("Bartley points", llm_output(stats=["points"], player="Bartley"))
    assert plain.spec.competition == "euroleague"


def test_instructions_list_each_league_and_the_eurocup_rule():
    text = build_instructions(date(2025, 11, 1), SEASONS, greek_seasons=["2025-26"], eurocup_seasons=["2025-26"])
    assert "EuroCup seasons: 2025-26" in text and '"eurocup" when the question names the EuroCup' in text
