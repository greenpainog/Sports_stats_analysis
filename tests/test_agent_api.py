"""POST /agent with a scripted fake LLM: the graph is real, OpenAI is not called."""
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from openai import APIConnectionError

import database.db as db
from agent.graph import build_graph
from api.main import app, get_agent
from tests.test_agent_graph import FakeLLM, call
from tests.test_stat_query import build_test_league


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("agent_api") / "league.db")
    build_test_league()
    yield TestClient(app)
    app.dependency_overrides.pop(get_agent, None)
    db.DATABASE_FILE = original


def use_llm(llm):
    app.dependency_overrides[get_agent] = lambda: build_graph(llm, seasons=["2024-25"])


def test_answer_comes_with_its_steps_and_check(client):
    use_llm(FakeLLM([
        AIMessage("", tool_calls=[call("query_stats", {"player": "Sam Shooter", "stats": ["fg3_pct"]})]),
        AIMessage("Sam Shooter made 45.0% of his threes (9/20)."),
    ]))
    body = client.post("/agent", json={"question": "Sam Shooter 3P%?"}).json()
    assert body["status"] == "answered" and body["check"] == "passed"
    assert body["answer"] == "Sam Shooter made 45.0% of his threes (9/20)."
    [step] = body["steps"]
    assert step["tool"] == "query_stats" and step["args"]["player"] == "Sam Shooter"
    assert "9/20 = 45.0%" in step["result"]


def test_failed_number_check_is_reported(client):
    use_llm(FakeLLM([AIMessage("He shoots 52.3%."), AIMessage("Still 52.3%.")]))
    body = client.post("/agent", json={"question": "Sam Shooter 3P%?"}).json()
    assert body["check"] == "fallback" and "52.3" not in body["answer"]


class BrokenLLM(FakeLLM):
    def invoke(self, messages):
        raise APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))


def test_llm_failure_is_a_503_not_a_crash(client):
    use_llm(BrokenLLM([]))
    response = client.post("/agent", json={"question": "Sam Shooter 3P%?"})
    assert response.status_code == 503 and response.json()["status"] == "error"


def test_question_is_validated(client):
    use_llm(FakeLLM([]))
    assert client.post("/agent", json={"question": "hi"}).status_code == 422


def test_null_arguments_are_left_out_of_the_steps(client):
    args = {"player": "Sam Shooter", "stats": ["fg3_pct"], "venue": None,
            "compare": [{"label": "home", "venue": "home", "last_n": None}, {"label": "away", "venue": "away"}]}
    use_llm(FakeLLM([AIMessage("", tool_calls=[call("query_stats", args)]), AIMessage("Done.")]))
    [step] = client.post("/agent", json={"question": "Sam Shooter home vs away"}).json()["steps"]
    assert "venue" not in step["args"] and step["args"]["compare"][0] == {"label": "home", "venue": "home"}
