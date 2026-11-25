"""Measure the multi-step agent (POST /agent) on questions that need several lookups.

    python -m eval.run_agent_eval                # every question (real LLM, costs some cents)
    python -m eval.run_agent_eval --only a03,a11 # just these
    python -m eval.run_agent_eval --check-gold   # no LLM: run every reference query, print the facts

Each question in eval/agent_questions.jsonl is a conversation of one or more
turns. Each turn lists the facts its answer must state. A fact is never a
hard-coded number: it is a reference query the eval runs itself, plus which
value of the result to look for, so newly loaded games never make it stale.

    {"query": QuerySpec, "stat": "points", "field": "per_game", "side": "home"}
        a value from query_stats (side = a compare label)
    {"teams": RankingSpec, "team": "Panathinaikos", "stat": "assists", "field": "value" | "rank"}
        one team's value or rank in team_rankings
    {"teams": RankingSpec, "pick": 1, "field": "team"}  # or "value", with "stat"
        whoever is at that position (the answer must name it)
    {"players": PlayerRankingSpec, "pick": 1, "field": "player" | "value", "stat": ...}
        the same for player_rankings
    {"declines": true}
        the agent must say it can't answer (no prediction, no data)

A number counts when the answer has it to within 0.05 (answers round to one
decimal). A name counts when any of its aliases is in the answer ("Red Star"
for Crvena Zvezda). A turn passes when every fact is there; the eval also
reports the number check, tool calls and time. Results go to eval/results/.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from agent.verify import numbers
from api.models import PlayerRankingSpec, QuerySpec, RankingSpec
from api.service import answer_spec, player_rankings_spec, rankings_spec
from database.aliases import _TABLES, normalize_name, player_name_variants, resolve_team
from database.db import get_connection

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = EVAL_DIR / "agent_questions.jsonl"
RESULTS_DIR = EVAL_DIR / "results"
DECLINE_WORDS = ("can't", "cannot", "can not", "unable", "not able", "don't have", "do not have",
                 "doesn't have", "does not have", "no data", "not possible", "isn't possible", "not available",
                 "not supported", "isn't supported", "doesn't know", "does not know", "doesn't track", "does not track")


def load_questions(path=QUESTIONS_FILE):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --- Gold facts (recomputed on every run) --------------------------------------

def _team_row(response, team):
    found = resolve_team(team)
    if found.status != "match":
        raise ValueError(f"gold team '{team}' is {found.status}")
    name = found.candidates[0][1]
    return next(row for row in response.teams if row.team == name)


def gold_value(fact):
    """('number', value) or ('team' | 'player', name) or ('declines', None)."""
    if fact.get("declines"):
        return "declines", None
    if "query" in fact:
        result = answer_spec(QuerySpec(**fact["query"]))
        if "side" in fact:
            result = next(side.result for side in result.comparison if side.label == fact["side"])
        return "number", getattr(result.summary[fact["stat"]], fact["field"])
    if "teams" in fact:
        response = rankings_spec(RankingSpec(**fact["teams"]))
        row = response.teams[fact["pick"] - 1] if "pick" in fact else _team_row(response, fact["team"])
        if fact["field"] == "team":
            return "team", row.team
        return "number", (row.ranks if fact["field"] == "rank" else row.values)[fact["stat"]]
    if "players" in fact:
        row = player_rankings_spec(PlayerRankingSpec(**fact["players"])).players[fact["pick"] - 1]
        if fact["field"] == "player":
            return "player", row.player
        return "number", row.values[fact["stat"]]
    raise ValueError(f"unknown fact: {fact}")


# --- Does the answer state it? ---------------------------------------------------

def has_number(answer, value, tolerance=0.05):
    return value is not None and any(abs(v - value) <= tolerance + 1e-9 for v in numbers(answer))


def name_forms(kind, name):
    """Ways an answer may write a team or player: every alias of 5+ letters, or the surname."""
    if kind == "player":
        return {normalize_name(player_name_variants(name)[-2] if len(name.split()) > 1 else name)}
    alias_table, id_column, entity_table = _TABLES["team"]
    conn = get_connection()
    try:
        rows = conn.execute(f"""SELECT a.normalized FROM {alias_table} a
                                JOIN {entity_table} e ON e.id = a.{id_column} WHERE e.name = ?""", (name,))
        forms = {alias for (alias,) in rows if len(alias.replace(" ", "")) >= 5}
    finally:
        conn.close()
    return forms | {normalize_name(name)}


def has_name(answer, kind, name):
    text = f" {normalize_name(answer)} "
    return any(f" {form} " in text for form in name_forms(kind, name))


def declines(answer):
    text = answer.lower().replace("’", "'")
    return any(word in text for word in DECLINE_WORDS)


def fact_found(answer, kind, value):
    if kind == "declines":
        return declines(answer)
    if kind == "number":
        return has_number(answer, value)
    return has_name(answer, kind, value)


# --- Running ---------------------------------------------------------------------

def run_question(graph, item):
    from agent.graph import run

    conversation_id = uuid4().hex
    turns = []
    for turn in item["turns"]:
        gold = [gold_value(fact) for fact in turn["facts"]]
        start = time.time()
        result = run(graph, turn["question"], conversation_id)
        seconds = time.time() - start
        found = [fact_found(result.answer, kind, value) for kind, value in gold]
        turns.append({"question": turn["question"], "answer": result.answer, "check": result.check,
                      "tools": [name for name, _, _ in result.steps], "seconds": round(seconds, 1),
                      "facts": [{"kind": k, "gold": v, "found": f} for (k, v), f in zip(gold, found)],
                      "passed": all(found)})
    return {"id": item["id"], "category": item.get("category"), "turns": turns,
            "passed": all(t["passed"] for t in turns)}


def check_gold(questions):
    ok = True
    for item in questions:
        for turn in item["turns"]:
            try:
                facts = [gold_value(fact) for fact in turn["facts"]]
                print(f"{item['id']}  {turn['question']}\n      {facts}")
            except Exception as exc:  # a broken reference query must be fixed before running
                ok = False
                print(f"{item['id']}  {turn['question']}\n      BROKEN: {exc}")
    return ok


def summarize(results):
    turns = [t for r in results for t in r["turns"]]
    facts = [f for t in turns for f in t["facts"]]
    return {
        "questions_passed": f"{sum(r['passed'] for r in results)}/{len(results)}",
        "turns_passed": f"{sum(t['passed'] for t in turns)}/{len(turns)}",
        "facts_found": f"{sum(f['found'] for f in facts)}/{len(facts)}",
        "number_check": {c: sum(t["check"] == c for t in turns) for c in ("passed", "fixed", "fallback")},
        "avg_tool_calls": round(sum(len(t["tools"]) for t in turns) / len(turns), 1),
        "avg_seconds": round(sum(t["seconds"] for t in turns) / len(turns), 1),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=QUESTIONS_FILE)
    parser.add_argument("--only", help="Comma-separated question ids.")
    parser.add_argument("--check-gold", action="store_true", help="Run the reference queries only (no LLM).")
    args = parser.parse_args(argv)

    questions = load_questions(args.file)
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    if args.check_gold:
        return 0 if check_gold(questions) else 1

    from langgraph.checkpoint.memory import InMemorySaver

    from agent.graph import build_graph, openai_llm

    graph = build_graph(openai_llm(), checkpointer=InMemorySaver())
    results = []
    for item in questions:
        result = run_question(graph, item)
        results.append(result)
        print(f"{'PASS' if result['passed'] else 'FAIL'} {item['id']}")
        for turn in result["turns"]:
            missing = [f"{f['kind']}={f['gold']}" for f in turn["facts"] if not f["found"]]
            print(f"   {turn['question']}  [{turn['check']}, {len(turn['tools'])} tools, {turn['seconds']} s]"
                  + (f"  MISSING {missing}" if missing else ""))
            if missing:
                print(f"      answer: {turn['answer'][:300]}")
    summary = summarize(results)
    print(json.dumps(summary, indent=2))
    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"agent-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str), encoding="utf-8")
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
