"""Measure how well the LLM turns questions into the right QuerySpec.

    python -m eval.run_eval                 # all 50 questions (costs a few cents)
    python -m eval.run_eval --only q19,q26  # just these
    python -m eval.run_eval --check-gold    # no LLM: check every gold spec runs
    python -m eval.run_eval --file eval/heldout_questions.jsonl   # questions written by the user

Scoring, per question:
  - gold is null (unanswerable): correct if the LLM said unanswerable.
  - otherwise: correct if the LLM's spec gives the same answer as the gold
    spec: same subject, same games, same summary numbers. Gold answers are
    recomputed on every run, so newly loaded games never make the set stale.
"spec exact" is a stricter extra metric: the same spec once names are resolved.
Results are saved to eval/results/ (git-ignored).
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from analysis.stat_query import QueryError
from api.models import QuerySpec
from api.service import answer_spec
from database.aliases import resolve_player, resolve_team

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = EVAL_DIR / "questions.jsonl"
RESULTS_DIR = EVAL_DIR / "results"
NAME_FIELDS = {"player": resolve_player, "team": resolve_team, "opponent": resolve_team,
               "without_player": resolve_player, "with_player": resolve_player,
               "against_player": resolve_player}


def load_questions(path=QUESTIONS_FILE):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _fingerprint(response):
    return {
        "subject": response.subject,
        "games": [game.game_id for game in response.games],
        "summary": {stat: s.model_dump() for stat, s in response.summary.items()},
    }


def run_spec(spec: QuerySpec):
    """The parts of a query result that decide whether two specs agree.

    Compare sides are matched by their results, so their order and labels don't matter.
    """
    response = answer_spec(spec)
    result = _fingerprint(response)
    result["sides"] = sorted(
        (json.dumps(_fingerprint(side.result), sort_keys=True) for side in response.comparison or []),
    )
    return result


def _resolve_names(data):
    for name, resolve in NAME_FIELDS.items():
        if data.get(name) is not None:
            match = resolve(data[name])
            data[name] = match.id if match.status == "match" else data[name]
    threshold = data.get("player_threshold")
    if threshold:
        match = resolve_player(threshold["player"])
        threshold["player"] = match.id if match.status == "match" else threshold["player"]
    return data


def normalized(spec: QuerySpec):
    """The spec with names replaced by database ids, stats as a set, and sides unordered and unlabelled."""
    data = _resolve_names(spec.model_dump(exclude={"compare"}))
    data["stats"] = sorted(data["stats"])
    data["sides"] = sorted(
        json.dumps(_resolve_names(side.model_dump(exclude={"label"}, exclude_defaults=True)), sort_keys=True)
        for side in spec.compare or []
    )
    return data


def gold_ranking(gold):
    """A gold {"ranking": {...}} entry -> RankingRequest (fields left out are null)."""
    from llm.question_parser import LLMRanking, to_ranking

    return to_ranking({**{name: None for name in LLMRanking.model_fields}, **gold["ranking"]})


_RANK_NAMES = {"team": resolve_team, "team_a": resolve_team, "team_b": resolve_team, "player": resolve_player}


def ranking_key(request):
    """A ranking request with names resolved to ids, for 'spec exact'."""
    data = {"kind": request.kind, **request.spec.model_dump()}
    if request.focus_team is not None:
        data["team"] = request.focus_team
    for name, resolve in _RANK_NAMES.items():
        if data.get(name) is not None:
            match = resolve(data[name])
            data[name] = match.id if match.status == "match" else data[name]
    if data.get("stats"):
        data["stats"] = sorted(data["stats"])
    return data


def run_ranking(request):
    """The parts of a ranking answer that decide whether two requests agree."""
    from api.service import answer_ranking

    _, result = answer_ranking(request)
    focus = resolve_team(request.focus_team).id if request.focus_team else None
    return {"kind": request.kind, "focus": focus, "result": result.model_dump(exclude={"answer"})}


def score_ranking(gold, parsed):
    if parsed.ranking is None:
        return False, False, "expected a ranking, got a single-subject spec"
    expected_request = gold_ranking(gold)
    exact = ranking_key(parsed.ranking) == ranking_key(expected_request)
    try:
        got = run_ranking(parsed.ranking)
    except QueryError as exc:
        return False, exact, f"query failed: {exc}"
    expected = run_ranking(expected_request)
    # "Who leads in steals" doesn't say how many players to list: compare the rows both show.
    if got["kind"] == expected["kind"] == "player_rankings":
        shown = min(len(got["result"]["players"]), len(expected["result"]["players"]))
        for side in (got, expected):
            side["result"]["players"] = side["result"]["players"][:shown]
    if got == expected:
        return True, exact, ""
    diffs = [part for part in ("kind", "focus", "result") if got[part] != expected[part]]
    return False, exact, "different " + ", ".join(diffs)


def score(item, parsed):
    """Return (correct, spec_exact, note) for one question."""
    gold = item["gold"]
    if gold is None:
        ok = parsed.status == "unanswerable"
        return ok, ok, "" if ok else f"expected unanswerable, got {parsed.status}"
    if parsed.status != "ok":
        return False, False, f"{parsed.status}: {parsed.reason}"
    if "ranking" in gold:
        return score_ranking(gold, parsed)
    if parsed.spec is None:
        return False, False, f"expected a single-subject spec, got ranking {parsed.ranking.kind}"

    gold_spec = QuerySpec(**gold)
    exact = normalized(parsed.spec) == normalized(gold_spec)
    try:
        got = run_spec(parsed.spec)
    except QueryError as exc:
        return False, exact, f"query failed: {exc}"
    expected = run_spec(gold_spec)
    if got == expected:
        return True, exact, ""
    diffs = [part for part in ("subject", "games", "summary", "sides") if got[part] != expected[part]]
    return False, exact, "different " + ", ".join(diffs)


def _predicted(parsed):
    if parsed.ranking is not None:
        r = parsed.ranking
        return {"ranking": {"kind": r.kind, **r.spec.model_dump(exclude_defaults=True),
                            **({"team": r.focus_team} if r.focus_team else {})}}
    return parsed.spec.model_dump(exclude_defaults=True) if parsed.spec else None


def check_gold(questions):
    """Every gold spec must be valid and run; prints how many games each matches."""
    problems = 0
    for item in questions:
        if item["gold"] is None:
            continue
        try:
            if "ranking" in item["gold"]:
                result = run_ranking(gold_ranking(item["gold"]))["result"]
                rows = result.get("teams") or result.get("players") or [result.get("player") or result.get("team_a")]
                print(f"{item['id']}  {item['gold']['ranking']['kind']:<15} {len(rows):>3} rows")
                continue
            result = run_spec(QuerySpec(**item["gold"]))
            print(f"{item['id']}  {len(result['games']):>3} games  {result['subject']}")
        except Exception as exc:
            problems += 1
            print(f"{item['id']}  PROBLEM: {exc}")
    print("All gold specs run." if not problems else f"{problems} gold spec(s) have problems.")
    return problems == 0


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")  # names like Valančiūnas on Windows consoles
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=QUESTIONS_FILE, help="Questions file (JSON lines)")
    parser.add_argument("--only", help="Comma-separated question ids, e.g. q01,q19")
    parser.add_argument("--model", help="OpenAI model (default: OPENAI_MODEL or gpt-6-luna)")
    parser.add_argument("--check-gold", action="store_true", help="Only check the gold specs; no LLM calls")
    args = parser.parse_args(argv)

    questions = load_questions(args.file)
    if args.only:
        wanted = set(args.only.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    if args.check_gold:
        return 0 if check_gold(questions) else 1

    from llm.question_parser import OpenAILLM, parse_question

    llm = OpenAILLM(model=args.model)
    rows, by_category = [], defaultdict(lambda: [0, 0])
    started = time.perf_counter()
    for item in questions:
        parsed = parse_question(item["question"], llm=llm)
        if parsed.status == "error":  # no credits, no network, bad key: stop, don't score
            print(f"Stopped at {item['id']}: {parsed.reason}")
            return 2
        correct, exact, note = score(item, parsed)
        by_category[item["category"]][0] += correct
        by_category[item["category"]][1] += 1
        rows.append({**item, "correct": correct, "spec_exact": exact, "note": note, "status": parsed.status,
                     "attempts": parsed.attempts, "predicted": _predicted(parsed), "reason": parsed.reason})
        print(f"{item['id']}  {'ok ' if correct else 'BAD'}  {item['question']}" + (f"  -> {note}" if note else ""))

    total = len(rows)
    correct = sum(r["correct"] for r in rows)
    exact = sum(r["spec_exact"] for r in rows)
    print(f"\nModel: {llm.model}   time: {time.perf_counter() - started:.0f}s   "
          f"retries: {sum(r['attempts'] > 1 for r in rows)}")
    print(f"Accuracy:   {correct}/{total} = {100 * correct / total:.1f}%")
    print(f"Spec exact: {exact}/{total} = {100 * exact / total:.1f}%")
    for category, (ok, n) in sorted(by_category.items()):
        print(f"  {category:14} {ok}/{n}")

    failures = [r for r in rows if not r["correct"]]
    if failures:
        print("\nFailures:")
        for r in failures:
            print(f"  {r['id']} {r['question']}\n     expected: {r['gold']}\n     got:      "
                  f"{r['predicted'] if r['predicted'] is not None else r['status'] + ': ' + str(r['reason'])}")

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{args.file.stem}-{llm.model}.json"
    out.write_text(json.dumps({"model": llm.model, "accuracy": correct / total, "spec_exact": exact / total,
                               "results": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {out.relative_to(EVAL_DIR.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
