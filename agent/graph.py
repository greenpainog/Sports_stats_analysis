"""The multi-step agent as a LangGraph graph.

    question -> agent (LLM) --wants tools--> tools (run_tool) --> agent ...
                          \\--done--> END
                          \\--step limit reached--> finish (LLM, no tools) --> END

The state is the list of messages (question, LLM turns, tool results) plus a
count of tool calls. The LLM is passed in, so tests use a scripted fake and
never call OpenAI. The tools are agent/tools.py: the LLM only picks one and
fills in its validated arguments; it never writes SQL.
"""
import operator
from datetime import date
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from agent.tools import TOOLS, run_tool

MAX_TOOL_CALLS = 6  # past this the agent must answer with what it has


class AgentState(TypedDict):
    # add_messages appends each node's new messages instead of replacing the list.
    messages: Annotated[list[AnyMessage], add_messages]
    tool_calls: Annotated[int, operator.add]  # each node returns how many it made


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
- If a name is ambiguous, or the data can't answer the question, say so plainly instead of guessing.
- Answer in a few short sentences: the conclusion first, then the numbers behind it, with the number of games each is based on."""


def build_graph(llm, today=None, seasons=None):
    """A compiled graph. `llm` is a LangChain chat model (or a fake with bind_tools/invoke)."""
    instructions = SystemMessage(system_prompt(today, seasons))
    with_tools = llm.bind_tools(tool_schemas())

    def agent(state: AgentState):
        reply = with_tools.invoke([instructions, *state["messages"]])
        return {"messages": [reply], "tool_calls": 0}

    def tools(state: AgentState):
        calls = state["messages"][-1].tool_calls
        results = [ToolMessage(run_tool(call["name"], call["args"]), tool_call_id=call["id"], name=call["name"])
                   for call in calls]
        return {"messages": results, "tool_calls": len(calls)}

    def finish(state: AgentState):
        # The LLM asked for more tools after the limit: drop that request and
        # make it answer from the results it already has (no tools bound).
        note = HumanMessage(f"You have used all {MAX_TOOL_CALLS} tool calls. Answer now from the tool "
                            "results above, and say what you could not check.")
        reply = llm.invoke([instructions, *state["messages"][:-1], note])
        return {"messages": [reply], "tool_calls": 0}

    def route(state: AgentState):
        last = state["messages"][-1]
        if not getattr(last, "tool_calls", None):
            return END
        if state["tool_calls"] + len(last.tool_calls) > MAX_TOOL_CALLS:
            return "finish"
        return "tools"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_node("finish", finish)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", "finish", END])
    graph.add_edge("tools", "agent")
    graph.add_edge("finish", END)
    return graph.compile()


def run(graph, question):
    """Ask one question. Returns (answer text, [(tool name, args, result), ...])."""
    state = graph.invoke({"messages": [HumanMessage(question)], "tool_calls": 0})
    steps, args_by_id = [], {}
    for message in state["messages"]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                args_by_id[call["id"]] = (call["name"], call["args"])
        elif isinstance(message, ToolMessage) and message.tool_call_id in args_by_id:
            name, args = args_by_id[message.tool_call_id]
            steps.append((name, args, message.content))
    # .text joins the text parts (newer models return content as a list of blocks).
    return state["messages"][-1].text, steps


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
