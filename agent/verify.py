"""Check that every number in the agent's answer came from a tool result.

LLMs sometimes write a plausible number that no tool returned. Before an
answer goes out, unsupported_numbers() lists the answer's numbers that appear
in no tool result and not in the question. The graph sends those back to the
LLM once, and falls back to the raw tool results if they are still there.

Numbers are compared as values, so "45%", "45.0" and "45.0 %" match, and by
size, so "a margin of 5" matches a tool's "-5". Season names (2026-27) and
dates are not checked. A difference is allowed ("37.6 vs 36.8, 0.8 more") when
both of its numbers are themselves in the answer and in the tool results: the
prompt lets the LLM state simple differences, and requiring both ends keeps a
20-row ranking from making almost any small decimal "a difference".

Limits: numbers written as words ("five") are not checked, and a number that
happens to appear somewhere else in the tool results passes even if it is
attached to the wrong player. It catches invented numbers, not every misreading.
"""
import re
from itertools import combinations

SEASONS_AND_DATES = re.compile(r"\b\d{4}[-/]\d{2}(?:-\d{2})?\b")
THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}\b)")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers(text):
    """{value: as written} for every number in text, seasons and dates left out."""
    text = THOUSANDS.sub("", SEASONS_AND_DATES.sub(" ", text))
    return {round(float(match), 2): match for match in NUMBER.findall(text)}


def unsupported_numbers(answer, sources):
    """The numbers in `answer` (as written) found in none of the `sources` texts."""
    known = set()
    for source in sources:
        known |= set(numbers(source))
    stated = numbers(answer)
    backed = [v for v in stated if v in known]
    differences = {round(abs(a - b), 1) for a, b in combinations(backed, 2)}
    return [written for value, written in stated.items()
            if value not in known and round(value, 1) not in differences]
