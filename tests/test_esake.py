"""The esake.gr box score parser.

Hand-made cases first; then the real page for AEK 73-77 Doxa Lefkadas
(2026-27, round 7), read from data/raw/esake/ if it is cached.
"""
from pathlib import Path

import pytest

from ingestion.esake import (
    _greek_month,
    _minutes,
    _start_time,
    build_game_bundle,
    check_game,
    display_name,
    parse_game_page,
)


def test_display_name():
    assert display_name("NOLLEY II LANDERS", "NOLLEY") == "Landers Nolley II"
    assert display_name("KATSIVELIS DIMITRIS", "KATSIVELIS") == "Dimitris Katsivelis"
    assert display_name("HARMON DE’VION", "HARMON") == "De’Vion Harmon"
    assert display_name("DE COLO NANDO", "DE COLO") == "Nando De Colo"


def test_greek_months_and_dates():
    assert [_greek_month(m) for m in ("Νοε", "Μαΐ", "Ιούν", "Ιούλ", "Ιαν")] == [11, 5, 6, 7, 1]
    assert _start_time("Σαβ 14 Νοε", "17:00", "2026-27") == "2026-11-14T15:00:00Z"   # Athens is UTC+2 in winter
    assert _start_time("Κυρ 17 Μαΐ", "19:00", "2026-27") == "2027-05-17T16:00:00Z"   # spring: next year, UTC+3


def test_minutes():
    assert _minutes("00:26:51") == 26.85 and _minutes("--:--:--") is None


PAGE = Path("data/raw/esake/game_51FB1AFE_mode3.html")
needs_page = pytest.mark.skipif(not PAGE.exists(), reason="cached esake page missing")


@pytest.fixture
def game():
    return parse_game_page(PAGE.read_text(encoding="utf-8"), "51FB1AFE", "2026-27")


@needs_page
def test_real_page_header(game):
    assert (game.round_number, game.start_time, game.venue) == (7, "2026-11-14T15:00:00Z", "SUNEL ARENA")
    assert (game.home.name, game.home_score, game.away_score, game.away.name) == ("ΑΕΚ", 73, 77, "ΔΟΞΑ ΛΕΥΚΑΔΑΣ ELABET")
    assert (game.home.source_team_id, game.away.source_team_id) == ("00000010", "3CA10C07")
    assert game.quarters == [(13, 27), (24, 18), (22, 15), (14, 17)]


@needs_page
def test_real_page_box_scores(game):
    nolley = game.home.players[0]
    assert (nolley.name, nolley.source_player_id, nolley.values["minutes"]) == ("Landers Nolley II", "D82710F7", 26.85)
    assert nolley.values["fg2"] == (1, 4) and nolley.values["fg3"] == (0, 2) and nolley.values["assists"] == 4
    totals = game.home.totals.values
    assert (totals["fg2"], totals["fg3"], totals["ft"]) == ((20, 34), (5, 28), (18, 28))
    assert (totals["fouls_received"], totals["fouls_committed"], totals["pir"]) == (27, 17, 69)
    check_game(game)  # players + team row = totals, quarters = final score


@needs_page
def test_bundle(game):
    bundle = build_game_bundle(game, "2026-27")
    assert bundle["competition"]["source_competition_id"] == "GBL" and bundle["source_game_id"] == "51FB1AFE"
    assert len(bundle["appearances"]) == 17  # 4 listed players have no minutes
    team = {s["stat_name"]: s["stat_value"] for s in bundle["stats"] if s["entity_type"] == "team" and s["team"] == "home"}
    assert team["minutes"] == 200 and team["points"] == 73 and team["fg3_attempted"] == 28


@needs_page
def test_a_page_that_doesnt_add_up_is_refused(game):
    game.home.players[0].values["points"] += 1
    with pytest.raises(ValueError, match="points"):
        check_game(game)


def test_unexpected_layout_is_refused():
    with pytest.raises(ValueError, match="header"):
        parse_game_page("<html><body>no game here</body></html>", "X", "2026-27")
