"""The agent graph's wiring, with a scripted fake LLM: no OpenAI calls.

The fake replays AIMessages in order and records what it was sent, so each
test checks a path through the graph: answer directly, call tools and
continue, recover from a tool error, and stop at the step limit.
"""
import pytest
from langchain_core.messages import AIMessage, ToolMessage

import database.db as db
from agent import graph as agent_graph
from agent.graph import MAX_TOOL_CALLS, build_graph, run
from tests.test_stat_query import build_test_league


class FakeLLM:
    def __init__(self, replies, bound=False, shared=None):
        # Replies and the call log are shared with the copy bind_tools returns.
        self.shared = shared or {"replies": list(replies), "calls": [], "tool_names": []}
        self.bound = bound

    @property
    def calls(self):  # (tools bound?, messages sent)
        return self.shared["calls"]

    @property
    def tool_names(self):
        return self.shared["tool_names"]

    def bind_tools(self, tools):
        self.shared["tool_names"] = [t["function"]["name"] for t in tools]
        return FakeLLM(None, bound=True, shared=self.shared)

    def invoke(self, messages):
        self.calls.append((self.bound, messages))
        return self.shared["replies"].pop(0)


def call(name, args, n=1):
    return {"name": name, "args": args, "id": f"call_{name}_{n}"}


@pytest.fixture(scope="module", autouse=True)
def league(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("graph") / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


def graph_for(llm):
    return build_graph(llm, seasons=["2024-25", "2025-26"])


def test_answers_without_tools():
    llm = FakeLLM([AIMessage("Hello.")])
    answer, steps = run(graph_for(llm), "hi")
    assert (answer, steps) == ("Hello.", [])
    assert "query_stats" in llm.tool_names and "team_roster" in llm.tool_names


def test_tool_result_goes_back_to_the_llm():
    llm = FakeLLM([
        AIMessage("", tool_calls=[call("query_stats", {"player": "Sam Shooter", "stats": ["fg3_pct"]})]),
        AIMessage("Sam Shooter shot 45.0% (9/20) on threes."),
    ])
    answer, steps = run(graph_for(llm), "Shooter 3P%?")
    assert answer == "Sam Shooter shot 45.0% (9/20) on threes."
    assert [name for name, _, _ in steps] == ["query_stats"]
    assert "9/20 = 45.0%" in steps[0][2]
    # The second LLM call saw the tool's result.
    second = llm.calls[1][1]
    assert isinstance(second[-1], ToolMessage) and "9/20" in second[-1].content


def test_several_tools_in_one_turn_and_recovering_from_an_error():
    llm = FakeLLM([
        AIMessage("", tool_calls=[call("query_stats", {"player": "Kick", "stats": ["assists"]}),
                                  call("team_roster", {"team": "Alpha", "season": "2024-25"})]),
        AIMessage("", tool_calls=[call("query_stats", {"player": "Sid Kick", "stats": ["assists"]}, n=2)]),
        AIMessage("Sid Kick: 8 assists in 3 games."),
    ])
    answer, steps = run(graph_for(llm), "Kick assists")
    assert [name for name, _, _ in steps] == ["query_stats", "team_roster", "query_stats"]
    assert steps[0][2].startswith("Error:") and "Candidates" in steps[0][2]
    assert "Sid Kick: 3 games" in steps[1][2]
    assert answer == "Sid Kick: 8 assists in 3 games."


def test_step_limit_forces_an_answer_without_tools():
    roster = call("team_roster", {"team": "Alpha", "season": "2024-25"})
    endless = [AIMessage("", tool_calls=[{**roster, "id": f"call_{i}"}]) for i in range(MAX_TOOL_CALLS + 1)]
    llm = FakeLLM([*endless, AIMessage("Alpha's roster is Sam Shooter and Sid Kick.")])
    answer, steps = run(graph_for(llm), "loop forever")
    assert len(steps) == MAX_TOOL_CALLS  # the 7th request was never run
    assert answer == "Alpha's roster is Sam Shooter and Sid Kick."
    bound, messages = llm.calls[-1]
    assert not bound  # the finishing call has no tools
    assert "used all" in messages[-1].content


def test_prompt_names_the_current_season():
    prompt = agent_graph.system_prompt(seasons=["2025-26", "2026-27"])
    assert "the current one is 2026-27" in prompt and "Every number in your answer must come from a tool result" in prompt
