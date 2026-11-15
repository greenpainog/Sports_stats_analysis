"""Player rankings, the minimum-games rule, and player formulas worked out by hand.

The hand-made league (see test_stat_query) in 2024-25:
  Sam Shooter (Alpha): g2, g3, g4; assists 2, 4, 6; threes 1/1, 6/14, 2/5; record 2-1
  Sid Kick (Alpha):    g2, g3, g7; assists 5, 1, 2; threes 0/2, 1/1, 0/1; record 1-2
  Olly Kick (Gamma):   g5 only; all zeros
"""
import pytest

import database.db as db
from analysis.advanced import ADVANCED_PLAYER_STATS, count_in_game, player_advanced_value
from analysis.player_rankings import player_profile, player_rankings
from analysis.rankings import _Zero
from analysis.stat_query import QueryError
from tests.test_stat_query import build_test_league


@pytest.fixture(autouse=True)
def league(tmp_path):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


def table(r, stat):
    return [(row.player, row.values[stat], row.ranks[stat]) for row in r.players]


def test_per_game_and_the_automatic_minimum():
    r = player_rankings(["assists"], season="2024-25")
    # Most games: 3, so 2 are needed; Olly Kick (1 game) is not ranked.
    assert (r.min_games, r.ranked_count, r.unranked) == (2, 2, 1)
    assert table(r, "assists") == [("Sam Shooter", 4.0, 1), ("Sid Kick", 2.7, 2)]


def test_totals_and_no_minimum():
    r = player_rankings(["assists"], season="2024-25", per="total", min_games=0)
    assert table(r, "assists") == [("Sam Shooter", 12, 1), ("Sid Kick", 8, 2), ("Olly Kick", 0, 3)]


def test_percentages_and_record():
    r = player_rankings(["fg3_pct", "win_pct"], season="2024-25")
    assert table(r, "fg3_pct") == [("Sam Shooter", 45.0, 1), ("Sid Kick", 25.0, 2)]  # 9/20 and 1/4
    assert [row.values["win_pct"] for row in r.players] == [66.7, 33.3]


def test_filters():
    assert [row.player for row in player_rankings(["assists"], season="2024-25", team="Gamma").players] == ["Olly Kick"]
    # Each player's own last game; everyone has 1 game, so everyone qualifies.
    last = player_rankings(["assists"], season="2024-25", last_n=1)
    assert table(last, "assists") == [("Sam Shooter", 6.0, 1), ("Sid Kick", 2.0, 2), ("Olly Kick", 0.0, 3)]
    away = player_rankings(["assists"], season="2024-25", venue="away", min_games=0)
    assert table(away, "assists") == [("Sam Shooter", 4.0, 1), ("Sid Kick", 1.0, 2)]  # both only at g3


def test_limit_keeps_ranks_out_of_everyone():
    r = player_rankings(["assists"], season="2024-25", limit=1)
    assert [row.player for row in r.players] == ["Sam Shooter"] and r.ranked_count == 2


def test_profile_of_a_player_below_the_minimum_has_values_but_no_ranks():
    p = player_profile("Olly Kick", ["assists"], season="2024-25")
    assert (p.player.qualified, p.player.values["assists"], p.player.ranks["assists"]) == (False, 0.0, None)
    shooter = player_profile("Shooter", ["assists", "fg3_pct"], season="2024-25")
    assert shooter.player.ranks == {"assists": 1, "fg3_pct": 1} and shooter.rankings.ranked_count == 2


def test_profile_errors():
    with pytest.raises(QueryError, match="several"):
        player_profile("Kick")
    with pytest.raises(QueryError, match="no games"):
        player_profile("Sam Shooter", season="2025-26")


def test_default_stats_skip_what_the_database_lacks():
    stats = player_rankings(season="2024-25").stats
    assert {"assists", "fg3_pct", "win_pct", "starts"} <= set(stats)
    assert "points" not in stats and "usage_pct" not in stats  # no points or minutes in this league


@pytest.mark.parametrize("bad", [
    {"stats": ["dunks"]}, {"stats": ["assists", "assists"]}, {"per": "minute"}, {"result": "tie"},
    {"limit": 0}, {"min_games": -1}, {"stats": ["usage_pct"]},  # needs minutes this league lacks
])
def test_bad_arguments(bad):
    with pytest.raises(QueryError):
        player_rankings(**bad)


# --- Formulas ----------------------------------------------------------------
# Player, 30 of his team's 200 minutes (on the floor 0.75 of the game):
#   FG2 5/10, FG3 2/5, FT 4/5, OREB 2, DREB 6, AST 5, TOV 2, STL 1, BLK 1, PTS 20
# Team: FG2 25/50, FG3 10/30, FT 15/20, OREB 10, DREB 25, TOV 12, PTS 95
# Opponents: FG2 24/48, FG3 9/27, FT 12/16, OREB 9, DREB 30, TOV 14

def _line(**v):
    v.setdefault("total_rebounds", v.get("offensive_rebounds", 0) + v.get("defensive_rebounds", 0))
    return _Zero(v)


PL = _line(minutes=30, fg2_made=5, fg2_attempted=10, fg3_made=2, fg3_attempted=5, ft_made=4, ft_attempted=5,
           offensive_rebounds=2, defensive_rebounds=6, assists=5, turnovers=2, steals=1, blocks=1, points=20,
           paint_points=8)
TM = _line(minutes=200, fg2_made=25, fg2_attempted=50, fg3_made=10, fg3_attempted=30, ft_made=15, ft_attempted=20,
           offensive_rebounds=10, defensive_rebounds=25, assists=20, turnovers=12, points=95)
OP = _line(fg2_made=24, fg2_attempted=48, fg3_made=9, fg3_attempted=27, ft_made=12, ft_attempted=16,
           offensive_rebounds=9, defensive_rebounds=30, turnovers=14)


def test_player_formulas_match_hand_calculation():
    got = {stat: player_advanced_value(stat, PL, TM, OP, 1) for stat in ADVANCED_PLAYER_STATS}
    assert got == {
        "ts_pct": 58.1,            # 20 / (2 * (15 + 0.44 * 5))
        "efg_pct": 53.3,           # (7 + 0.5 * 2) / 15
        "usage_pct": 25.4,         # 19.2 plays / (0.75 * 100.8 team plays)
        "ast_pct": 26.0,           # 5 / (0.75 * 35 team FGM - 7 his FGM)
        "oreb_pct": 6.7,           # 2 / (0.75 * (10 + 30))
        "dreb_pct": 23.5,          # 6 / (0.75 * (25 + 9))
        "reb_pct": 14.4,           # 8 / (0.75 * (35 + 39))
        "tov_pct": 10.4,           # 2 / 19.2
        "stl_pct": 1.5,            # 1 / (0.75 * 88.92 possessions)
        "blk_pct": 2.8,            # 1 / (0.75 * 48 opponent 2PA)
        "ast_to": 2.5,
        "pts_from_2pt_pct": 50.0,  # 10 of 20
        "pts_from_3pt_pct": 30.0,
        "pts_from_ft_pct": 20.0,
        "pts_from_paint_pct": 40.0,  # 8 of 20
        "points_per_fga": 1.33,    # 20 / 15
        "fg3a_rate": 33.3,
        "ft_rate": 0.333,
        "team_pts_share": 21.1,    # 20 / 95
    }


def test_formulas_without_minutes_or_attempts_give_none():
    benched = _line(minutes=0)
    assert player_advanced_value("usage_pct", benched, TM, OP, 1) is None
    assert player_advanced_value("ts_pct", benched, TM, OP, 1) is None


def test_double_and_triple_doubles():
    game = {"points": 12, "total_rebounds": 10, "assists": 3}
    assert (count_in_game("double_doubles", game, 0), count_in_game("triple_doubles", game, 0)) == (1, 0)
    game["assists"] = 10
    assert count_in_game("triple_doubles", game, 0) == 1
    assert count_in_game("games_20_points", {"points": 20}, 0) == 1 and count_in_game("starts", {}, 1) == 1


# --- POST /player-rankings and /player-profile ----------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)


def test_player_rankings_endpoint():
    body = client.post("/player-rankings", json={"stats": ["assists"], "season": "2024-25"}).json()
    assert (body["min_games"], body["ranked_count"], body["unranked"]) == (2, 2, 1)
    assert [(p["player"], p["ranks"]["assists"]) for p in body["players"]] == [("Sam Shooter", 1), ("Sid Kick", 2)]


@pytest.mark.parametrize("bad", [
    {"stats": ["dunks"]}, {"stats": []}, {"per": "minute"}, {"min_minutes": 50}, {"limit": 0}, {"sql": "x"},
])
def test_player_rankings_rejects_bad_specs(bad):
    assert client.post("/player-rankings", json=bad).status_code == 422


def test_player_profile_endpoint():
    body = client.post("/player-profile", json={"player": "Olly Kick", "stats": ["assists"], "season": "2024-25"}).json()
    assert body["player"]["qualified"] is False and "not ranked, needs 2+ games" in body["answer"]
    body = client.post("/player-profile", json={"player": "Shooter", "stats": ["assists"], "season": "2024-25"}).json()
    assert body["player"]["ranks"] == {"assists": 1} and "4.0 (#1/2)" in body["answer"]
    assert client.post("/player-profile", json={"player": "Kick"}).status_code == 400  # ambiguous
