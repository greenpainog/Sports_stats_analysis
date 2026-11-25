"""The multi-step agent as a LangGraph graph.

    question -> agent (LLM) -> tools (run_tool) -> agent ... while the LLM asks for tools
    agent done                    -> check
    agent over the step limit     -> finish (LLM without tools) -> check
    check: every number is backed by a tool result -> END
    check fails the first time    -> agent, told which numbers had no source
    check fails again             -> END with the raw tool results instead

The state is the list of messages (questions, LLM turns, tool results) and how
the last number check ended. With a checkpointer, each conversation_id keeps
its messages between questions, so a follow-up ("and Zalgiris?") is read with
the earlier questions and results. The step limit and the number check count
only the current question's messages.
The LLM is passed in, so tests use a scripted fake and never call OpenAI. The
tools are agent/tools.py: the LLM only picks one and fills in its validated
arguments; it never writes SQL.
"""
from dataclasses import dataclass
from datetime import date
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.tools import TOOLS, run_tool
from agent.verify import unsupported_numbers

MAX_TOOL_CALLS = 6  # per question; past this the agent must answer with what it has
MAX_TURNS = 5  # questions of a conversation the LLM sees: older ones cost tokens and rarely matter


class AgentState(TypedDict):
    # add_messages appends each node's new messages instead of replacing the list.
    messages: Annotated[list[AnyMessage], add_messages]
    check: str  # "passed", "fixed" (passed after one retry) or "fallback"


# Notes the graph itself adds to the conversation. They are left out of the
# number check's sources: a retry note quotes the very numbers it rejects.
GRAPH_NOTE = "graph_note"


def _is_question(message):
    return isinstance(message, HumanMessage) and message.name != GRAPH_NOTE


def this_turn(messages):
    """The messages from the latest question on."""
    starts = [i for i, m in enumerate(messages) if _is_question(m)]
    return messages[starts[-1]:] if starts else messages


def recent(messages, turns=MAX_TURNS):
    """The last `turns` questions and everything after them.

    Cut only at a question, so a tool result is never separated from the LLM
    turn that asked for it (OpenAI rejects a tool result without its call).
    """
    starts = [i for i, m in enumerate(messages) if _is_question(m)]
    return messages[starts[-turns]:] if len(starts) > turns else messages


def tool_schemas():
    """The tools in OpenAI's function format, from each tool's Pydantic argument model."""
    schemas = []
    for tool in TOOLS.values():
        schema = convert_to_openai_tool(tool.args)
        schema["function"]["name"] = tool.name
        schema["function"]["description"] = tool.description
        schemas.append(schema)
    return schemas


def system_prompt(today=None, seasons=None):
    from llm.question_parser import loaded_seasons

    today = today or date.today()
    seasons = seasons if seasons is not None else loaded_seasons()
    current = seasons[-1] if seasons else "unknown"
    return f"""You answer questions about European basketball (Euroleague, EuroCup, Greek league) with the tools.
Today is {today.isoformat()}. Loaded Euroleague seasons: {", ".join(seasons) or "none"}; the current one is {current}.

Rules:
- Every number in your answer must come from a tool result in this conversation. Never use numbers from memory, never estimate, never do arithmetic the tools didn't do unless it is a simple difference you state as such.
- Plan the fewest tool calls that answer the question. Use compare to get two conditions in one call (e.g. season vs last 6 games). You have at most {MAX_TOOL_CALLS} tool calls.
- "This season" means {current}. With no season named, use {current}.
- If a tool returns "Error: ...", fix the arguments and try again, or use resolve_name for a name it didn't find.
- Earlier questions in this conversation are context for follow-ups ("and Zalgiris?", "what about away games?"): keep their stats and filters unless the new question changes them. Numbers from earlier tool results may be reused.
- If a name is ambiguous, or the data can't answer the question, say so plainly instead of guessing.
- The data covers games already played. Never predict (who will win, future stats) or name a favourite: say you can't predict, and you may add the current facts (record, standings) as facts only.
- Answer in a few short sentences: the conclusion first, then the numbers behind it, with the number of games each is based on."""


def build_graph(llm, today=None, seasons=None, checkpointer=None):
    """A compiled graph. `llm` is a LangChain chat model (or a fake with bind_tools/invoke).

    With a checkpointer (e.g. InMemorySaver) conversations are remembered by
    conversation_id; without one every question starts fresh.
    """
    instructions = SystemMessage(system_prompt(today, seasons))
    with_tools = llm.bind_tools(tool_schemas())

    def agent(state: AgentState):
        reply = with_tools.invoke([instructions, *recent(state["messages"])])
        return {"messages": [reply]}

    def tools(state: AgentState):
        calls = state["messages"][-1].tool_calls
        results = [ToolMessage(run_tool(call["name"], call["args"]), tool_call_id=call["id"], name=call["name"])
                   for call in calls]
        return {"messages": results}

    def finish(state: AgentState):
        # The LLM asked for more tools after the limit: drop that request and
        # make it answer from the results it already has (no tools bound).
        note = HumanMessage(f"You have used all {MAX_TOOL_CALLS} tool calls. Answer now from the tool "
                            "results above, and say what you could not check.", name=GRAPH_NOTE)
        reply = llm.invoke([instructions, *recent(state["messages"][:-1]), note])
        return {"messages": [reply]}

    def check(state: AgentState):
        answer = state["messages"][-1].text
        # The sources are what the LLM was shown: earlier questions' tool results count too.
        sources = [m.text for m in recent(state["messages"][:-1]) if isinstance(m, ToolMessage) or _is_question(m)]
        missing = unsupported_numbers(answer, sources)
        failed_before = any(isinstance(m, HumanMessage) and m.name == GRAPH_NOTE
                            for m in this_turn(state["messages"]))
        if not missing:
            return {"check": "fixed" if failed_before else "passed"}
        if not failed_before:
            note = HumanMessage(
                f"These numbers in your answer are in no tool result: {', '.join(missing)}. "
                "Use only numbers the tools returned (call a tool if you need another one) and answer again.",
                name=GRAPH_NOTE)
            return {"messages": [note]}
        return {"messages": [AIMessage(fallback_answer(this_turn(state["messages"])))], "check": "fallback"}

    def route(state: AgentState):
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            return "check"
        used = sum(isinstance(m, ToolMessage) for m in this_turn(state["messages"]))
        if used + len(last.tool_calls) > MAX_TOOL_CALLS:
            return "finish"
        return "tools"

    def after_check(state: AgentState):
        return "agent" if isinstance(state["messages"][-1], HumanMessage) else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_node("finish", finish)
    graph.add_node("check", check)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", "finish", "check"])
    graph.add_edge("tools", "agent")
    graph.add_edge("finish", "check")
    graph.add_conditional_edges("check", after_check, ["agent", END])
    return graph.compile(checkpointer=checkpointer)


def tool_steps(messages):
    """[(tool name, args, result), ...] in the order the tools ran."""
    steps, args_by_id = [], {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                args_by_id[call["id"]] = (call["name"], call["args"])
        elif isinstance(message, ToolMessage) and message.tool_call_id in args_by_id:
            name, args = args_by_id[message.tool_call_id]
            steps.append((name, args, message.text))
    return steps


def fallback_answer(messages):
    steps = tool_steps(messages)
    if not steps:
        return "I couldn't back my answer with any tool result, so I can't answer this reliably."
    parts = ["I couldn't confirm every number in my summary, so here are the results it was based on:"]
    parts += [f"[{name}]\n{result}" for name, _, result in steps]
    return "\n\n".join(parts)


@dataclass
class AgentResult:
    answer: str
    steps: list  # (tool name, args, result)
    check: str  # "passed", "fixed" or "fallback"


def run(graph, question, conversation_id=None):
    """Ask one question (a follow-up if conversation_id was used before) and return its AgentResult."""
    config = {"configurable": {"thread_id": conversation_id}} if conversation_id else None
    state = graph.invoke({"messages": [HumanMessage(question)], "check": ""}, config)
    # .text joins the text parts (newer models return content as a list of blocks).
    turn = this_turn(state["messages"])
    return AgentResult(turn[-1].text, tool_steps(turn), state["check"])


def openai_llm(model=None, reasoning_effort="low"):
    """The real model: the same one and the same .env key as /ask."""
    import os

    from dotenv import load_dotenv
    from langchain_openai import ChatOpenAI

    from database.db import PROJECT_ROOT
    from llm.question_parser import DEFAULT_MODEL

    load_dotenv(PROJECT_ROOT / ".env")
    return ChatOpenAI(model=model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
                      reasoning_effort=reasoning_effort)
