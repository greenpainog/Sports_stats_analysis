"""Play-by-play parsing and stints.

Most tests use tiny hand-written games, so they need no data. The last class
checks one real game (E2024 game 1, ALBA Berlin 77 - 87 Panathinaikos) against
its box score and skips itself when the cached files are missing.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.db as db
from database.repositories import save_game_bundle
from database.schema import create_tables
from ingestion.euroleague import build_game_bundle, store_game_play_by_play
from ingestion.play_by_play import (
    build_stints,
    compare_with_box,
    parse_events,
    period_start,
    player_totals,
)

RAW = Path("data/raw/euroleague")


def row(number, play_type, clock="", team="", player="", a=None, b=None):
    return {"NUMBEROFPLAY": number, "PLAYTYPE": play_type, "MARKERTIME": clock,
            "CODETEAM": team.ljust(10), "PLAYER_ID": player.ljust(10),
            "POINTS_A": a, "POINTS_B": b, "PLAYINFO": ""}


HOME = {"H1", "H2", "H3", "H4", "H5"}
AWAY = {"A1", "A2", "A3", "A4", "A5"}


def game(*quarters, overtime=None):
    data = {key: list(rows) for key, rows in zip(
        ["FirstQuarter", "SecondQuarter", "ThirdQuarter", "ForthQuarter"], quarters)}
    data.update({key: [] for key in ["FirstQuarter", "SecondQuarter", "ThirdQuarter", "ForthQuarter"]
                 if key not in data})
    data["ExtraTime"] = overtime or []
    return data


class ParseTests(unittest.TestCase):
    def test_elapsed_comes_from_period_and_countdown_clock(self):
        events = parse_events(game([row(1, "BP"), row(2, "2FGM", "09:30", "H", "H1", None, 2)],
                                   [row(3, "D", "10:00", "A", "A1")]))
        self.assertEqual([e["elapsed"] for e in events], [0, 30, 600])
        self.assertEqual(events[1]["player_code"], "H1")  # padding stripped
        self.assertIsNone(events[0]["team_code"])

    def test_substitutions_before_a_second_overtime_belong_to_it(self):
        overtime = [row(1, "BP"), row(2, "2FGM", "04:00", "H", "H1", 2, 0), row(3, "EP"),
                    row(4, "IN", "05:00", "H", "H6"), row(5, "OUT", "05:00", "H", "H1"),
                    row(6, "BP"), row(7, "D", "03:00", "A", "A1"), row(8, "EP"), row(9, "EG")]
        events = parse_events(game(overtime=overtime))
        by_number = {e["sequence"]: e for e in events}
        self.assertEqual(by_number[2]["period"], 5)
        self.assertEqual(by_number[2]["elapsed"], period_start(5) + 60)
        self.assertEqual(by_number[4]["period"], 6)
        self.assertEqual(by_number[4]["elapsed"], period_start(6))  # not back at the start of OT1
        self.assertEqual(by_number[7]["elapsed"], period_start(6) + 120)
        self.assertEqual(by_number[9]["period"], 6)  # end of game doesn't start an overtime
        self.assertEqual(period_start(6), 2700)


class StintTests(unittest.TestCase):
    def build(self, *quarters):
        events = parse_events(game(*quarters))
        return build_stints(events, {"H": HOME, "A": AWAY}, "H", "A")

    def test_substitution_splits_stints_and_points_go_to_the_lineup_on_the_floor(self):
        stints, problems = self.build([
            row(1, "BP"),
            row(2, "2FGM", "09:00", "H", "H1", 2, None),   # A not given: still 0
            row(3, "OUT", "08:00", "H", "H1"), row(4, "IN", "08:00", "H", "H6"),
            row(5, "3FGM", "07:00", "A", "A1", 2, 3),
            row(6, "D", "06:30", "H", "H6"),  # first event after the sub, later than it
        ], [], [], [row(7, "EG")])
        self.assertEqual(problems, [])
        home = [s for s in stints if s["team_code"] == "H"]
        self.assertEqual([(s["start"], s["end"]) for s in home], [(0, 120), (120, 2400)])
        self.assertEqual((home[0]["points_for"], home[0]["points_against"]), (2, 0))
        self.assertEqual((home[1]["points_for"], home[1]["points_against"]), (0, 3))
        self.assertIn("H6", home[1]["players"])
        totals = player_totals(stints)
        self.assertEqual(totals[("H", "H1")], [120, 2])
        self.assertEqual(totals[("H", "H6")], [2280, -3])
        self.assertEqual(totals[("H", "H2")], [2400, -1])
        self.assertEqual(totals[("A", "A1")], [2400, 1])  # the away lineup never changed

    def test_a_lineup_that_is_not_five_is_reported(self):
        stints, problems = self.build([row(1, "BP"), row(2, "OUT", "05:00", "H", "H1"),
                                       row(3, "D", "04:00", "A", "A1")], [], [], [row(4, "EG")])
        self.assertEqual(problems, ["H: 4 on the floor at 300s"])
        events = parse_events(game([row(1, "BP")], [], [], [row(2, "EG")]))
        _, problems = build_stints(events, {"H": HOME - {"H5"}, "A": AWAY}, "H", "A")
        self.assertIn("H: 4 starters", problems)

    def test_a_stint_never_ends_before_it_starts(self):
        # The clock steps back a second: a sub at 07:01 listed after an event at 07:00.
        stints, _ = self.build([
            row(1, "BP"), row(2, "OUT", "07:00", "H", "H1"), row(3, "IN", "07:00", "H", "H6"),
            row(4, "D", "07:00", "A", "A1"),
            row(5, "OUT", "07:01", "H", "H6"), row(6, "IN", "07:01", "H", "H1"),
            row(7, "D", "06:00", "A", "A1"),
        ], [], [], [row(8, "EG")])
        self.assertTrue(all(s["end"] >= s["start"] for s in stints))

    def test_compare_with_box(self):
        stints, _ = self.build([row(1, "BP"), row(2, "2FGM", "09:00", "H", "H1", 2, None)],
                               [], [], [row(3, "EG")])
        box = {("H", p): (2400, 2) for p in HOME} | {("A", p): (2400, -2) for p in AWAY}
        self.assertEqual(compare_with_box(stints, box), (0, 0))
        box[("H", "H1")] = (2390, 3)
        self.assertEqual(compare_with_box(stints, box), (10, 1))
        box[("H", "H9")] = (None, None)  # DNP, no minutes or plus-minus
        self.assertEqual(compare_with_box(stints, box), (10, 1))


FILES = [RAW / name for name in ("header_E2024_1.json", "boxscore_E2024_1.json",
                                 "games_E2024.json", "playbyplay_E2024_1.json")]


@unittest.skipUnless(all(f.exists() for f in FILES), "cached E2024 game 1 not found")
class RealGameTests(unittest.TestCase):
    def test_stints_match_the_box_score_of_a_real_game(self):
        header, boxscore, season, pbp = (json.loads(f.read_text(encoding="utf-8")) for f in FILES)
        schedule_game = next(g for g in season["data"] if g["gameCode"] == 1)
        bundle = build_game_bundle(header, boxscore, schedule_game, "E", 2024, 1)
        original = db.DATABASE_FILE
        with tempfile.TemporaryDirectory() as tmp:
            db.DATABASE_FILE = str(Path(tmp) / "test.db")
            try:
                create_tables()
                game_id = save_game_bundle(bundle)
                conn = sqlite3.connect(db.DATABASE_FILE)
                check = store_game_play_by_play(conn, game_id, pbp, "BER", "PAN")
                check_again = store_game_play_by_play(conn, game_id, pbp, "BER", "PAN")  # replaces
                conn.commit()
                self.assertEqual((check["minutes_max_diff"], check["plus_minus_mismatches"],
                                  check["problems"]), (0, 0, None))
                self.assertEqual(check, check_again)
                points = dict(conn.execute(
                    """SELECT t.source_team_id, SUM(points_for) FROM stints s
                       JOIN teams t ON t.id = s.team_id GROUP BY 1"""))
                self.assertEqual(points, {"BER": 77, "PAN": 87})
                last = conn.execute("""SELECT score_home, score_away FROM pbp_events
                                       ORDER BY sequence DESC LIMIT 1""").fetchone()
                self.assertEqual(last, (77, 87))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pbp_checks").fetchone()[0], 1)
                conn.close()
            finally:
                db.DATABASE_FILE = original
