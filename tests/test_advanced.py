"""Advanced team stats, checked against numbers worked out by hand.

Two teams, two games; g2 went to overtime (225 team minutes).
          FG2     FG3     FT     OREB DREB AST TOV STL BLK PTS
  g1 H   20/40   8/25   14/20   10   25   18  12   6   3   78
     A   18/36  10/30   10/14    8   28   20  15   7   2   76
  g2 H   25/45   9/28   15/18   12   30   22  10   5   4   92
     A   22/44  11/32   13/16    9   27   19  14   6   5   90

Totals  H: FGA 138, FGM 62, 3PM 17, FTA 38, OREB 22, DREB 55, AST 40, TOV 22, STL 11, PTS 170
        A: FGA 142, FGA2 80, OREB 17, DREB 55, TOV 29, FTA 30, BLK 7, PTS 166; minutes 425 each
Possessions: H 138 - 22 + 22 + 0.44*38 = 154.72; A 142 - 17 + 29 + 0.44*30 = 167.2; mean 160.96
"""
import pytest

import database.db as db
from analysis.advanced import ADVANCED_TEAM_STATS, advanced_value, possessions
from analysis.rankings import RANKABLE_TEAM_STATS, team_rankings
from analysis.stat_query import QueryError
from database.repositories import save_game_bundle
from database.schema import create_tables

COLUMNS = ("fg2_made", "fg2_attempted", "fg3_made", "fg3_attempted", "ft_made", "ft_attempted",
           "offensive_rebounds", "defensive_rebounds", "assists", "turnovers", "steals", "blocks", "points")
GAMES = {
    1: ({"H": (20, 40, 8, 25, 14, 20, 10, 25, 18, 12, 6, 3, 78),
         "A": (18, 36, 10, 30, 10, 14, 8, 28, 20, 15, 7, 2, 76)}, 200),
    2: ({"H": (25, 45, 9, 28, 15, 18, 12, 30, 22, 10, 5, 4, 92),
         "A": (22, 44, 11, 32, 13, 16, 9, 27, 19, 14, 6, 5, 90)}, 225),
}


def _bundle(code, lines, minutes):
    stats = []
    for side, team in (("home", "H"), ("away", "A")):
        values = dict(zip(COLUMNS, lines[team]), minutes=minutes)
        values["total_rebounds"] = values["offensive_rebounds"] + values["defensive_rebounds"]
        stats += [{"entity_type": "team", "team": side, "stat_name": k, "stat_value": v} for k, v in values.items()]
    return {
        "source": "test", "source_game_id": f"adv{code}",
        "competition": {"name": "Test League", "source_competition_id": "E"},
        "season": {"name": "2024-25"},
        "home_team": {"name": "Home Club", "source_team_id": "H"},
        "away_team": {"name": "Away Club", "source_team_id": "A"},
        "start_time": f"2024-10-0{code}T19:00:00Z",
        "home_score": lines["H"][-1], "away_score": lines["A"][-1],
        "phase": "regular_season", "is_neutral_venue": False, "stats": stats,
    }


@pytest.fixture
def league(tmp_path):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path / "adv.db")
    create_tables()
    for code, (lines, minutes) in GAMES.items():
        save_game_bundle(_bundle(code, lines, minutes))
    yield
    db.DATABASE_FILE = original


BOX_ONLY = [s for s in ADVANCED_TEAM_STATS if s != "pts_from_paint_pct"]  # this league has no shot list


def _home(**kw):
    stats = [s for s in BOX_ONLY if not s.startswith("forced_")] if kw.get("side") == "opponent" else BOX_ONLY
    r = team_rankings(stats, **kw)
    return next(row for row in r.teams if row.team == "Home Club"), next(row for row in r.teams if row.team == "Away Club")


def test_formulas_match_hand_calculation(league):
    home, away = _home()
    assert home.games == 2
    assert home.values == {
        "possessions": 80.5,   # 160.96 / 2
        "pace": 75.7,          # 40 * 160.96 / (425 / 5): overtime doesn't inflate it
        "off_rating": 105.6,   # 100 * 170 / 160.96
        "def_rating": 103.1,   # 100 * 166 / 160.96
        "net_rating": 2.5,
        "efg_pct": 51.1,       # (62 + 0.5 * 17) / 138
        "ts_pct": 54.9,        # 170 / (2 * (138 + 0.44 * 38))
        "tov_pct": 12.4,       # 22 / (138 + 0.44 * 38 + 22)
        "oreb_pct": 28.6,      # 22 / (22 + 55)
        "dreb_pct": 76.4,      # 55 / (55 + 17)
        "ft_rate": 0.275,      # 38 / 138
        "fg3a_rate": 38.4,     # 53 / 138
        "ast_pct": 64.5,       # 40 / 62
        "ast_to": 1.82,        # 40 / 22
        "stl_pct": 6.8,        # 100 * 11 / 160.96
        "blk_pct": pytest.approx(8.75, abs=0.051),  # 7 / 80 opponent 2-point attempts
        "forced_turnovers": 14.5,  # Away Club's 29 turnovers in 2 games
        "forced_tov_pct": 15.7,    # 29 / (142 + 0.44 * 30 + 29)
    }
    assert (away.values["blk_pct"], away.values["possessions"]) == (8.2, 80.5)  # 7 / 85; same possessions


def test_both_teams_have_the_same_possessions_and_mirror_ratings(league):
    home, away = _home()
    assert home.values["off_rating"] == away.values["def_rating"]
    assert home.values["net_rating"] == -away.values["net_rating"]
    assert home.values["oreb_pct"] + away.values["dreb_pct"] == pytest.approx(100, abs=0.1)


def test_opponent_side_rates_the_opponents(league):
    # What Home Club's opponents did against it is Away Club's own line.
    home, away = _home(side="opponent")
    own_home, own_away = _home()
    assert home.values["off_rating"] == own_home.values["def_rating"]
    assert home.values["efg_pct"] == own_away.values["efg_pct"]


def test_forced_turnovers_are_the_opponents_turnovers(league):
    # The same numbers as turnovers and tov_pct with side="opponent", but they
    # can sit next to the team's own stats (steals) in one ranking.
    home = next(r for r in team_rankings(["forced_turnovers", "forced_tov_pct", "steals"]).teams
                if r.team == "Home Club")
    allowed, _ = _home(side="opponent")
    assert home.values["forced_tov_pct"] == allowed.values["tov_pct"]
    assert home.values["steals"] == 5.5  # Home Club's own 11 steals, not the opponents' 13
    total = next(r for r in team_rankings(["forced_turnovers"], per="total").teams if r.team == "Home Club")
    assert total.values["forced_turnovers"] == 29
    with pytest.raises(QueryError):
        team_rankings(["forced_turnovers"], side="opponent")


def test_total_possessions(league):
    home = next(r for r in team_rankings(["possessions", "pace"], per="total").teams if r.team == "Home Club")
    assert home.values == {"possessions": 161.0, "pace": 75.7}  # pace is a rate: per doesn't change it


def test_filters_apply_to_advanced_stats(league):
    # Only g2 (Home won 92-90): H 73 FGA - 12 + 10 + 7.92 = 78.92; A 76 - 9 + 14 + 7.04 = 88.04; mean 83.48
    home = next(r for r in team_rankings(["off_rating", "pace"], last_n=1).teams if r.team == "Home Club")
    assert home.values == {"off_rating": 110.2, "pace": 74.2}  # 9200 / 83.48; 40 * 83.48 / 45


def test_paint_share():
    assert advanced_value("pts_from_paint_pct", {"paint_points": 30, "points": 80}, {}, 1) == 37.5


def test_formulas_directly():
    own = {"fg2_attempted": 10, "fg3_attempted": 0, "offensive_rebounds": 0, "turnovers": 0, "ft_attempted": 0}
    assert possessions(own, own) == 10
    assert advanced_value("ast_to", {"assists": 5, "turnovers": 0}, {}, 1) is None  # no turnovers: no ratio
    assert advanced_value("ast_to", {"assists": 5, "turnovers": 2}, {}, 0) is None  # no games


def test_advanced_stats_need_their_inputs(tmp_path):
    from tests.test_stat_query import build_test_league   # stores only team rebounds
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path / "small.db")
    try:
        build_test_league()
        assert not set(team_rankings().stats) & set(ADVANCED_TEAM_STATS)  # left out of "all stats"
        with pytest.raises(QueryError):
            team_rankings(["off_rating"])
    finally:
        db.DATABASE_FILE = original


def test_every_advanced_stat_is_rankable():
    assert set(ADVANCED_TEAM_STATS) <= set(RANKABLE_TEAM_STATS)
