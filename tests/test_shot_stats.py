"""Paint, fast-break, second-chance and off-turnover points from the shot list.

Hand-made shot rows first (no files needed), then the real E2024 game 1 if
its cached shot list exists (python -m ingestion.euroleague --season 2024 --game 1).
"""
import json
from pathlib import Path

import pytest

from ingestion.euroleague import (
    SHOT_STATS,
    ShotListMismatch,
    build_game_bundle,
    check_shots_match_box,
    in_paint,
    shot_totals,
)


def shot(action, x=0, y=0, points=None, team="AAA", player="P1", fb="0", second="0", off_to="0"):
    made = {"2FGM": 2, "3FGM": 3, "FTM": 1}.get(action, 0)
    return {"ID_ACTION": action, "COORD_X": x, "COORD_Y": y, "POINTS": made if points is None else points,
            "TEAM": team, "ID_PLAYER": player, "FASTBREAK": fb, "SECOND_CHANCE": second, "POINTS_OFF_TURNOVER": off_to}


def test_paint_edges():
    assert in_paint(0, 0) and in_paint(245, 422.5) and in_paint(-245, -157.5)  # edges count as inside
    assert not in_paint(246, 100) and not in_paint(0, 423) and not in_paint(0, -158)


def test_shot_totals():
    rows = [
        shot("2FGM", 10, 20, fb="1"),           # paint make on a fast break: 2 paint, 2 fast break
        shot("2FGA", -200, 300),                # paint miss
        shot("2FGM", 300, 300, second="1"),     # mid-range make, second chance
        shot("3FGM", 0, 700, off_to="1"),       # three off a turnover
        shot("3FGA", 0, 200),                   # a three is never a paint shot, wherever it is
        shot("FTM", fb="1", second="1"),        # a free throw can carry the flags
    ]
    assert shot_totals(rows) == {
        "paint_points": 2, "paint_fg_made": 1, "paint_fg_attempted": 2,
        "fastbreak_points": 3, "second_chance_points": 3, "points_off_turnovers": 3,
    }
    assert shot_totals([]) == dict.fromkeys(SHOT_STATS, 0)


def test_shot_list_must_match_the_box_score():
    rows = [shot("2FGM"), shot("2FGA"), shot("3FGA"), shot("FTM"), shot("2FGM", team="BBB")]
    box = {"FieldGoalsMade2": 1, "FieldGoalsAttempted2": 2, "FieldGoalsMade3": 0,
           "FieldGoalsAttempted3": 1, "FreeThrowsMade": 1}
    mine, missing = check_shots_match_box(rows, "AAA", box)
    assert (len(mine), missing) == (4, 0)  # only that team's rows
    # A missed attempt absent from the shot list is allowed and counted...
    assert check_shots_match_box(rows, "AAA", {**box, "FieldGoalsAttempted2": 3})[1] == 1
    # ...but different makes mean the points can't be trusted.
    with pytest.raises(ShotListMismatch):
        check_shots_match_box(rows, "AAA", {**box, "FreeThrowsMade": 2})


RAW = Path("data/raw/euroleague")
FILES = [RAW / f"{kind}_E2024_1.json" for kind in ("header", "boxscore", "points")]


@pytest.mark.skipif(not all(f.exists() for f in FILES), reason="cached E2024 game 1 files missing")
def test_real_game_shot_stats_add_up():
    header, boxscore, shots = (json.loads(f.read_text(encoding="utf-8")) for f in FILES)
    schedule = json.loads((RAW / "games_E2024.json").read_text(encoding="utf-8"))
    game = next(g for g in schedule["data"] if g["gameCode"] == 1)
    bundle = build_game_bundle(header, boxscore, game, "E", 2024, 1, shots["Rows"])
    for side in ("home", "away"):
        team = {s["stat_name"]: s["stat_value"] for s in bundle["stats"]
                if s["entity_type"] == "team" and s["team"] == side}
        players = [s for s in bundle["stats"] if s["entity_type"] == "player" and s["team"] == side]
        for stat in SHOT_STATS:  # players add up to the team
            assert sum(s["stat_value"] for s in players if s["stat_name"] == stat) == team[stat], (side, stat)
        assert team["paint_fg_made"] <= team["fg2_made"] and team["paint_fg_attempted"] <= team["fg2_attempted"]
        assert team["paint_points"] == 2 * team["paint_fg_made"]
