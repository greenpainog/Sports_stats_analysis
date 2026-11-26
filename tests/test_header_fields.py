"""Quarter points and the coach/referee filters, on the hand-made league.

Games g2 and g3 are saved again with quarter scores and staff (the loader
does this from each game's header); g4, g6 and g7 have neither.
    g2: Alpha (home, coach Ann Coach) 70 - 75 Beta (coach Bob Bench); referee Rita Ref
    g3: Gamma (home, coach Ben Bench) 60 - 90 Alpha (coach Ann Coach); referees Rita Ref, Ray Whistle
"""
import copy

import pytest

import database.db as db
from analysis.rankings import team_rankings
from analysis.stat_query import QueryError, query_stats
from database.repositories import save_game_bundle
from tests.test_stat_query import GAMES, build_test_league

QUARTERS = {"g2": ([20, 15, 20, 15], [20, 20, 20, 15]), "g3": ([15, 15, 15, 15], [25, 25, 20, 20])}
STAFF = {
    "g2": [{"role": "coach", "team": "home", "name": "Ann Coach"}, {"role": "coach", "team": "away", "name": "Bob Bench"},
           {"role": "referee", "name": "Rita Ref"}],
    "g3": [{"role": "coach", "team": "home", "name": "Ben Bench"}, {"role": "coach", "team": "away", "name": "Ann Coach"},
           {"role": "referee", "name": "Rita Ref"}, {"role": "referee", "name": "Ray Whistle"}],
}


@pytest.fixture(scope="module", autouse=True)
def league(tmp_path_factory):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path_factory.mktemp("header") / "league.db")
    build_test_league()
    for game in GAMES:
        if game["source_game_id"] in QUARTERS:
            game = copy.deepcopy(game)
            for side, points in zip(("home", "away"), QUARTERS[game["source_game_id"]]):
                game["stats"] += [{"entity_type": "team", "team": side, "stat_name": "points",
                                   "stat_value": p, "period": f"q{i}"} for i, p in enumerate(points, start=1)]
            game["staff"] = STAFF[game["source_game_id"]]
            save_game_bundle(game)
    yield
    db.DATABASE_FILE = original


def test_quarter_points_average_only_games_that_have_them():
    s = query_stats(["points_q1", "points_first_half"], team="Alpha").summary
    assert (s["points_q1"].games, s["points_q1"].total, s["points_q1"].per_game) == (2, 45, 22.5)  # 20 + 25
    assert s["points_first_half"].total == 85  # g2 20+15, g3 25+25


def test_quarter_points_are_ranked_and_allowed():
    allowed = {row.team: row.summary["points_q4"].per_game
               for row in team_rankings(["points_q4"], side="opponent").teams}
    assert allowed["Alpha"] == 15  # Beta 15 in g2, Gamma 15 in g3


def test_quarter_points_are_team_only():
    with pytest.raises(QueryError, match="team stats only"):
        query_stats(["points_q1"], player="Sam Shooter")


def test_coach_filter_uses_the_subjects_own_team():
    assert [g.game_id for g in query_stats(["win_pct"], team="Alpha", coach="Ann Coach").games] == [2, 3]
    # Bob Bench coached Beta in g2: Alpha was never "under" him.
    assert query_stats(["win_pct"], team="Alpha", coach="Bob Bench").games == []
    assert [g.game_id for g in query_stats(["assists"], player="Sam Shooter", coach="Ann").games] == [2, 3]


def test_referee_filter_and_name_lookup():
    assert [g.game_id for g in query_stats(["win_pct"], team="Alpha", referee="Whistle").games] == [3]
    assert [g.game_id for g in query_stats(["win_pct"], team="Alpha", referee="Rita").games] == [2, 3]


def test_shared_surname_is_ambiguous():
    with pytest.raises(QueryError, match="could mean several coaches") as exc:
        query_stats(["win_pct"], team="Alpha", coach="Bench")
    assert [name for _, name in exc.value.candidates] == ["Ben Bench", "Bob Bench"]


def test_team_roster_names_the_coach():
    from agent.tools import run_tool

    assert "Head coach: Ann Coach (2 games)" in run_tool("team_roster", {"team": "Alpha", "season": "2024-25"})
