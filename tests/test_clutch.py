"""Clutch time, a player's quarters and box-score stats on the floor, from events.

Uses the league and stints of test_on_floor, plus these events in g2
(Alpha 70 - 75 Beta, Alpha at home). Stint s1 = 0-600 s Shooter + Kick,
s2 = 600-2400 s Shooter alone:
    Q1 100 s  Shooter 3FGM        s1
    Q1 200 s  Shooter assist      s1
    Q2 700 s  Shooter FTM         s2
    Q4 2300 s Shooter 2FGM        s2  clutch
    Q4 2310 s Shooter 2FGA miss   s2  clutch
    Q4 2350 s Beta 3FGM           s2  clutch
g3 has play-by-play but no events of his; g4 has none (no pbp_checks row).
"""
import tempfile
import unittest
from pathlib import Path

import database.db as db
from analysis.stat_query import QueryError, query_stats
from api.models import QuerySpec
from ingestion.play_by_play import build_stints, is_clutch, parse_events
from tests.test_on_floor import add_stints
from tests.test_play_by_play import AWAY, HOME, game, row
from tests.test_stat_query import build_test_league

EVENTS = [  # (period, elapsed, team, player, play_type, home_points, away_points, clutch, stint)
    (1, 100, "ALP", "S1", "3FGM", 3, 0, 0, 0),
    (1, 200, "ALP", "S1", "AS", 0, 0, 0, 0),
    (2, 700, "ALP", "S1", "FTM", 1, 0, 0, 1),
    (4, 2300, "ALP", "S1", "2FGM", 2, 0, 1, 1),
    (4, 2310, "ALP", "S1", "2FGA", 0, 0, 1, 1),
    (4, 2350, "BET", None, "3FGM", 0, 3, 1, 1),
]


def add_events():
    conn = db.get_connection()
    g2 = conn.execute("SELECT id FROM games WHERE source_game_id = 'g2'").fetchone()[0]
    stints = [s for (s,) in conn.execute("SELECT id FROM stints WHERE game_id = ? ORDER BY start_elapsed", (g2,))]
    teams = dict(conn.execute("SELECT source_team_id, id FROM teams"))
    players = dict(conn.execute("SELECT source_player_id, id FROM players"))
    for sequence, (period, elapsed, team, player, play_type, home, away, clutch, stint) in enumerate(EVENTS):
        conn.execute(
            """INSERT INTO pbp_events (game_id, sequence, period, elapsed, team_id, player_id, play_type,
                                       score_home, score_away, home_points, away_points, clutch, home_stint_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)""",
            (g2, sequence, period, elapsed, teams[team], players.get(player), play_type, home, away, clutch,
             stints[stint]))
    conn.commit()
    conn.close()


class EventQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = db.DATABASE_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        db.DATABASE_FILE = str(Path(cls.tmp.name) / "league.db")
        build_test_league()
        add_stints()
        add_events()

    @classmethod
    def tearDownClass(cls):
        db.DATABASE_FILE = cls.original_db
        cls.tmp.cleanup()

    def test_player_clutch_stats(self):
        result = query_stats(["points", "fg2_pct", "plus_minus", "points_against"], player="Shooter", clutch=True)
        self.assertEqual([g.game_id for g in result.games], [2])  # only g2 had clutch time
        s = result.summary
        self.assertEqual(s["points"].total, 2)
        self.assertEqual((s["fg2_pct"].made, s["fg2_pct"].attempted), (1, 2))
        self.assertEqual((s["plus_minus"].total, s["points_against"].total), (-1, 3))
        self.assertIn("with clutch time", result.describe())
        self.assertIn("clutch", result.filters)

    def test_team_clutch_points_and_record(self):
        result = query_stats(["points_for", "points_against", "win_pct"], team="Alpha", clutch=True)
        s = result.summary
        self.assertEqual((s["points_for"].total, s["points_against"].total), (2, 3))
        self.assertEqual((s["win_pct"].wins, s["win_pct"].losses), (0, 1))

    def test_player_quarter_points(self):
        result = query_stats(["points_q1", "points_q2", "points_q4", "points_first_half"], player="Shooter")
        s = result.summary
        # g2 and g3 have play-by-play (g3 with no points: zeros); g4 has none and isn't counted.
        self.assertEqual({stat: (x.games, x.total) for stat, x in s.items()},
                         {"points_q1": (2, 3), "points_q2": (2, 1), "points_q4": (2, 2),
                          "points_first_half": (2, 4)})
        self.assertEqual(s["points_q1"].per_game, 1.5)

    def test_box_score_stats_with_a_teammate_on_the_floor(self):
        result = query_stats(["points", "assists", "minutes"], player="Shooter", on_floor_with=["Sid Kick"])
        s = result.summary
        self.assertEqual((s["points"].total, s["assists"].total, s["minutes"].total), (3, 1, 30))
        self.assertEqual(s["points"].value, 4.0)  # 3 points in 30 minutes = 4.0 per 40

    def test_what_clutch_cannot_answer(self):
        with self.assertRaises(QueryError) as caught:
            query_stats(["minutes"], player="Shooter", clutch=True)
        self.assertIn("Minutes played in clutch time", str(caught.exception))
        with self.assertRaises(QueryError):
            query_stats(["points"], player="Shooter", clutch=True, competition="greek_league")
        with self.assertRaises(ValueError):
            QuerySpec(stats=["minutes"], player="Shooter", clutch=True)
        with self.assertRaises(ValueError):
            QuerySpec(stats=["points_q4"], player="Shooter", clutch=True)
        with self.assertRaises(ValueError):
            QuerySpec(stats=["points_q4"], player="Shooter", competition="greek_league")


class ClutchFlagTests(unittest.TestCase):
    def test_is_clutch(self):
        self.assertTrue(is_clutch(4, 2100, 5))       # exactly 5:00 left, up 5
        self.assertTrue(is_clutch(4, 2399, -3))
        self.assertFalse(is_clutch(4, 2099, 0))      # 5:01 left
        self.assertFalse(is_clutch(4, 2300, 6))      # up 6
        self.assertFalse(is_clutch(3, 1790, 0))      # 3rd quarter
        self.assertTrue(is_clutch(5, 2400, 2))       # all of overtime counts
        self.assertTrue(is_clutch(6, 2750, -1))

    def test_events_get_points_clutch_and_stints(self):
        events = parse_events(game([row(1, "BP")], [], [],
                                   [row(2, "3FGM", "06:00", "H", "H1", 3, None),   # 6:00 left: not clutch
                                    row(3, "2FGM", "04:00", "A", "A1", 3, 2),      # 3-0 before: clutch
                                    row(4, "OUT", "03:00", "H", "H1"), row(5, "IN", "03:00", "H", "H6"),
                                    row(6, "FTM", "02:00", "H", "H6", 4, 2),
                                    row(7, "EG")]))
        stints, _ = build_stints(events, {"H": HOME, "A": AWAY}, "H", "A")
        by_number = {e["sequence"]: e for e in events}
        self.assertEqual([by_number[n]["clutch"] for n in (2, 3, 6)], [False, True, True])
        self.assertEqual([(by_number[n]["points_a"], by_number[n]["points_b"]) for n in (2, 3, 6)],
                         [(3, 0), (0, 2), (1, 0)])
        self.assertIn("H1", by_number[3]["stint_a"]["players"])
        self.assertIn("H6", by_number[6]["stint_a"]["players"])  # after the substitution
        self.assertTrue(all(e["stint_a"] in stints and e["stint_b"] in stints for e in events))
