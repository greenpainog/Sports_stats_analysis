"""The number check on its own: which numbers in an answer have a source."""
from agent.verify import numbers, unsupported_numbers

TOOL = """Panathinaikos AKTOR Athens (season=2026-27), compared:
  last 5: 5 games
  season: 13 games
  total_rebounds: last 5 37.6 per game | season 36.8 per game
  points: 1,060 total"""


def test_numbers_ignores_seasons_and_dates_and_reads_thousands():
    assert numbers("45% and 1,060 rebounds in 2026-27 on 2026-11-17") == {45.0: "45", 1060.0: "1060"}


def test_backed_numbers_pass_in_any_format():
    assert unsupported_numbers("Last 5: **37.6** vs 36.80 per game (5 vs 13 games), 1060 in total.", [TOOL]) == []


def test_invented_numbers_are_listed():
    assert unsupported_numbers("They averaged 41.2 rebounds and 9 offensive.", [TOOL]) == ["41.2", "9"]


def test_a_difference_counts_only_next_to_both_numbers():
    assert unsupported_numbers("37.6 vs 36.8: 0.8 more.", [TOOL]) == []
    assert unsupported_numbers("Up by 0.8 rebounds.", [TOOL]) == ["0.8"]


def test_no_numbers_no_problem():
    assert unsupported_numbers("I can't answer that.", []) == []
