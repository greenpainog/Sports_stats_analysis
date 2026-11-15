"""Team rankings and matchups on the hand-made league (see test_stat_query).

Team rebounds are 30 + game code at home and 20 + code away, so in 2024-25:
  Alpha: g2 32, g3 23 (away), g4 34 (neutral), g7 37   -> 126 / 4 = 31.5, record 2-2
  Beta:  g1 31, g2 22 (away), g4 24 (neutral), g5 25 (away) -> 102 / 4 = 25.5, record 3-1
  Gamma: g1 21 (away), g3 33, g7 27 (away), g5 35      -> 116 / 4 = 29.0, record 1-3
"""
import pytest

import database.db as db
from analysis.rankings import TeamRow, _rank, matchup, team_rankings
from analysis.stat_query import QueryError
from tests.test_stat_query import build_test_league


@pytest.fixture(autouse=True)
def league(tmp_path):
    original = db.DATABASE_FILE
    db.DATABASE_FILE = str(tmp_path / "league.db")
    build_test_league()
    yield
    db.DATABASE_FILE = original


def table(rankings, stat):
    return [(row.team, row.values[stat], row.ranks[stat]) for row in rankings.teams]


def test_per_game_ranking():
    r = team_rankings(["total_rebounds"], season="2024-25")
    assert table(r, "total_rebounds") == [("Alpha", 31.5, 1), ("Gamma", 29.0, 2), ("Beta", 25.5, 3)]
    assert [row.games for row in r.teams] == [4, 4, 4]


def test_totals_and_default_stats():
    r = team_rankings(season="2024-25", per="total")
    assert r.stats == ["total_rebounds", "win_pct"]  # every stat this database has
    assert table(r, "total_rebounds") == [("Alpha", 126, 1), ("Gamma", 116, 2), ("Beta", 102, 3)]
    beta = next(row for row in r.teams if row.team == "Beta")
    assert (beta.values["win_pct"], beta.ranks["win_pct"], beta.summary["win_pct"].wins) == (75.0, 1, 3)


def test_rows_are_sorted_by_the_first_stat():
    r = team_rankings(["win_pct", "total_rebounds"], season="2024-25")
    assert [row.team for row in r.teams] == ["Beta", "Alpha", "Gamma"]


def test_game_filters():
    # Away: Alpha g3 23; Beta g2 22, g5 25; Gamma g1 21, g7 27. g4 is neutral, not away.
    away = team_rankings(["total_rebounds"], season="2024-25", venue="away")
    assert table(away, "total_rebounds") == [("Gamma", 24.0, 1), ("Beta", 23.5, 2), ("Alpha", 23.0, 3)]
    # Wins: Alpha g3, g4; Beta g1, g2, g5; Gamma g7.
    won = team_rankings(["total_rebounds"], season="2024-25", result="won")
    assert table(won, "total_rebounds") == [("Alpha", 28.5, 1), ("Gamma", 27.0, 2), ("Beta", 26.0, 3)]
    # Decided by 5 or fewer: g2, g4, g5, g7 (g1 by 10 and g3 by 30 drop out).
    close = team_rankings(["total_rebounds"], season="2024-25", max_margin=5)
    assert {row.team: row.games for row in close.teams} == {"Alpha": 3, "Beta": 3, "Gamma": 2}
    assert close.filters == {"season": "2024-25", "max_margin": 5}


def test_last_n_is_per_team():
    r = team_rankings(["total_rebounds"], season="2024-25", last_n=1)
    assert table(r, "total_rebounds") == [("Alpha", 37.0, 1), ("Gamma", 35.0, 2), ("Beta", 25.0, 3)]


def test_no_season_counts_every_season():
    r = team_rankings(["total_rebounds"])
    assert next(row for row in r.teams if row.team == "Alpha").games == 5  # plus g6 in 2025-26


def test_opponent_side_is_what_a_team_allowed():
    # Alpha's opponents grabbed 22 (g2), 33 (g3), 24 (g4), 27 (g7).
    r = team_rankings(["total_rebounds"], season="2024-25", side="opponent")
    alpha = next(row for row in r.teams if row.team == "Alpha")
    assert alpha.values["total_rebounds"] == 26.5
    assert r.stats == ["total_rebounds"] and "win_pct" not in team_rankings(side="opponent").stats


def test_equal_values_share_a_rank():
    rows = [TeamRow(name, i, 1, values={"x": v}) for i, (name, v) in enumerate(
        [("A", 10.0), ("B", 12.0), ("C", 10.0), ("D", 9.0), ("E", None)])]
    _rank(rows, "x")
    assert {row.team: row.ranks["x"] for row in rows} == {"B": 1, "A": 2, "C": 2, "D": 4, "E": None}


@pytest.mark.parametrize("bad", [
    {"stats": ["minutes"]}, {"stats": ["dunks"]}, {"stats": ["points", "points"]},
    {"venue": "road"}, {"result": "tied"}, {"max_margin": 0}, {"last_n": 0}, {"per": "minute"},
    {"side": "both"}, {"stats": ["win_pct"], "side": "opponent"},
])
def test_bad_arguments(bad):
    with pytest.raises(QueryError):
        team_rankings(**bad)


def test_matchup_gives_both_ranks():
    m = matchup("Alpha", "Beta", ["total_rebounds", "win_pct"], season="2024-25")
    assert (m.team_a.team, m.team_a.values["total_rebounds"], m.team_a.ranks["total_rebounds"]) == ("Alpha", 31.5, 1)
    assert (m.team_b.team, m.team_b.ranks["total_rebounds"], m.team_b.ranks["win_pct"]) == ("Beta", 3, 1)
    assert len(m.rankings.teams) == 3


def test_matchup_errors():
    with pytest.raises(QueryError, match="two different"):
        matchup("Alpha", "alpha", season="2024-25")
    with pytest.raises(QueryError, match="No team"):
        matchup("Alpha", "Omega")
    with pytest.raises(QueryError, match="no games"):  # Beta has no 2025-26 games
        matchup("Alpha", "Beta", season="2025-26")


# --- POST /rankings and /matchup -----------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

client = TestClient(app)


def test_rankings_endpoint():
    body = client.post("/rankings", json={"stats": ["total_rebounds"], "season": "2024-25", "venue": "away"}).json()
    assert body["team_count"] == 3 and body["filters"] == {"season": "2024-25", "venue": "away"}
    assert [(t["team"], t["ranks"]["total_rebounds"]) for t in body["teams"]] == [("Gamma", 1), ("Beta", 2), ("Alpha", 3)]


@pytest.mark.parametrize("bad", [
    {"stats": ["minutes"]}, {"stats": []}, {"stats": ["assists", "assists"]}, {"result": "tie"},
    {"max_margin": 0}, {"per": "minute"}, {"side": "opponent", "stats": ["win_pct"]}, {"sql": "x"},
    {"season": "2024"},
])
def test_rankings_rejects_bad_specs(bad):
    assert client.post("/rankings", json=bad).status_code == 422


def test_matchup_endpoint():
    response = client.post("/matchup", json={"team_a": "Alpha", "team_b": "Beta", "season": "2024-25"})
    body = response.json()
    assert response.status_code == 200 and body["team_count"] == 3
    assert body["team_a"]["ranks"] == {"total_rebounds": 1, "win_pct": 2}
    assert body["team_b"]["values"]["win_pct"] == 75.0
    assert "31.5 (#1/3)" in body["answer"] and "75.0 (#1/3)" in body["answer"]


def test_matchup_unknown_team_is_400():
    response = client.post("/matchup", json={"team_a": "Alpha", "team_b": "Omega"})
    assert response.status_code == 400 and "Omega" in response.json()["detail"]
