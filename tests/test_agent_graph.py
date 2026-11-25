"""The agent graph's wiring, with a scripted fake LLM: no OpenAI calls.

The fake replays AIMessages in order and records what it was sent, so each
test checks a path through the graph: answer directly, call tools and
continue, recover from a tool error, and stop at the step limit.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

import database.db as db
from agent import graph as agent_graph
from agent.graph import MAX_TOOL_CALLS, MAX_TURNS, build_graph, recent, run
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
        return type(self)(None, bound=True, shared=self.shared)  # subclasses keep their behaviour

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
    result = run(graph_for(llm), "hi")
    assert (result.answer, result.steps, result.check) == ("Hello.", [], "passed")
    assert "query_stats" in llm.tool_names and "team_roster" in llm.tool_names


def test_tool_result_goes_back_to_the_llm():
    llm = FakeLLM([
        AIMessage("", tool_calls=[call("query_stats", {"player": "Sam Shooter", "stats": ["fg3_pct"]})]),
        AIMessage("Sam Shooter shot 45.0% (9/20) on threes."),
    ])
    result = run(graph_for(llm), "Shooter 3P%?")
    assert result.answer == "Sam Shooter shot 45.0% (9/20) on threes."
    assert result.check == "passed"
    assert [name for name, _, _ in result.steps] == ["query_stats"]
    assert "9/20 = 45.0%" in result.steps[0][2]
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
    result = run(graph_for(llm), "Kick assists")
    steps = result.steps
    assert [name for name, _, _ in steps] == ["query_stats", "team_roster", "query_stats"]
    assert steps[0][2].startswith("Error:") and "Candidates" in steps[0][2]
    assert "Sid Kick: 3 games" in steps[1][2]
    assert result.answer == "Sid Kick: 8 assists in 3 games."


def test_step_limit_forces_an_answer_without_tools():
    roster = call("team_roster", {"team": "Alpha", "season": "2024-25"})
    endless = [AIMessage("", tool_calls=[{**roster, "id": f"call_{i}"}]) for i in range(MAX_TOOL_CALLS + 1)]
    llm = FakeLLM([*endless, AIMessage("Alpha's roster is Sam Shooter and Sid Kick.")])
    result = run(graph_for(llm), "loop forever")
    assert len(result.steps) == MAX_TOOL_CALLS  # the 7th request was never run
    assert result.answer == "Alpha's roster is Sam Shooter and Sid Kick."
    bound, messages = llm.calls[-1]
    assert not bound  # the finishing call has no tools
    assert "used all" in messages[-1].content


def test_prompt_names_the_current_season():
    prompt = agent_graph.system_prompt(seasons=["2025-26", "2026-27"])
    assert "the current one is 2026-27" in prompt and "Every number in your answer must come from a tool result" in prompt


SHOOTER_3P = call("query_stats", {"player": "Sam Shooter", "stats": ["fg3_pct"]})


def test_an_invented_number_is_sent_back_once_and_fixed():
    llm = FakeLLM([
        AIMessage("", tool_calls=[SHOOTER_3P]),
        AIMessage("Sam Shooter shot 52.3% on threes."),  # not in the tool result
        AIMessage("Sam Shooter shot 45.0% (9/20) on threes."),
    ])
    result = run(graph_for(llm), "Shooter 3P%?")
    assert (result.answer, result.check) == ("Sam Shooter shot 45.0% (9/20) on threes.", "fixed")
    retry_note = llm.calls[-1][1][-1]
    assert "52.3" in retry_note.content and "no tool result" in retry_note.content


def test_a_second_invented_number_falls_back_to_the_tool_results():
    llm = FakeLLM([
        AIMessage("", tool_calls=[SHOOTER_3P]),
        AIMessage("Sam Shooter shot 52.3% on threes."),
        AIMessage("Sam Shooter shot 61.0% on threes."),
    ])
    result = run(graph_for(llm), "Shooter 3P%?")
    assert result.check == "fallback"
    assert result.answer.startswith("I couldn't confirm every number")
    assert "9/20 = 45.0%" in result.answer and "61.0" not in result.answer


def test_numbers_from_the_question_are_allowed():
    llm = FakeLLM([AIMessage("", tool_calls=[call("query_stats", {"team": "Alpha", "stats": ["win_pct"], "last_n": 3})]),
                   AIMessage("In their last 3 games Alpha went 2-1.")])
    result = run(graph_for(llm), "Alpha record last 3 games")
    assert result.check == "passed", result.answer


# --- Conversations (follow-up questions) ---------------------------------------

def remembering(llm):
    return build_graph(llm, seasons=["2024-25"], checkpointer=InMemorySaver())


def test_a_follow_up_sees_the_earlier_question_and_results():
    llm = FakeLLM([
        AIMessage("", tool_calls=[call("query_stats", {"player": "Sam Shooter", "stats": ["fg3_pct"]})]),
        AIMessage("Sam Shooter: 45.0%."),
        AIMessage("", tool_calls=[call("query_stats", {"player": "Sid Kick", "stats": ["fg3_pct"]}, n=2)]),
        AIMessage("Sid Kick shoots less well than Sam Shooter's 45.0%."),
    ])
    graph = remembering(llm)
    first = run(graph, "Sam Shooter 3P%?", "c1")
    second = run(graph, "and Sid Kick?", "c1")
    sent = llm.calls[2][1]  # what the LLM saw for the follow-up
    assert any(isinstance(m, HumanMessage) and m.text == "Sam Shooter 3P%?" for m in sent)
    assert any(isinstance(m, ToolMessage) and "9/20 = 45.0%" in m.text for m in sent)
    # Each result is about its own question: one step each, and 45.0 from the
    # first question's tool result passes the follow-up's number check.
    assert [s[1]["player"] for s in first.steps] == ["Sam Shooter"]
    assert [s[1]["player"] for s in second.steps] == ["Sid Kick"]
    assert second.check == "passed"


def test_conversations_are_kept_apart():
    llm = FakeLLM([AIMessage("One."), AIMessage("Two.")])
    graph = remembering(llm)
    run(graph, "first question", "a")
    run(graph, "second question", "b")
    assert [m.text for m in llm.calls[1][1][1:]] == ["second question"]  # after the system prompt


def test_the_step_limit_counts_each_question_separately():
    calls = [call("resolve_name", {"kind": "team", "name": "Alpha"}, n=i) for i in range(MAX_TOOL_CALLS)]
    llm = FakeLLM([AIMessage("", tool_calls=calls), AIMessage("Done."),
                   AIMessage("", tool_calls=[call("resolve_name", {"kind": "team", "name": "Beta"}, n=9)]),
                   AIMessage("Done again.")])
    graph = remembering(llm)
    run(graph, "use every tool call", "c")
    second = run(graph, "one more lookup", "c")
    assert second.answer == "Done again." and len(second.steps) == 1  # not sent to finish


def test_old_questions_are_dropped_at_a_question_boundary():
    messages = []
    for i in range(MAX_TURNS + 2):
        messages += [HumanMessage(f"q{i}"),
                     AIMessage("", tool_calls=[call("resolve_name", {"kind": "team", "name": "Alpha"}, n=i)]),
                     ToolMessage("'Alpha' is Alpha.", tool_call_id=f"call_resolve_name_{i}"),
                     AIMessage(f"a{i}")]
    kept = recent(messages)
    assert kept[0].text == "q2" and len(kept) == 4 * MAX_TURNS
