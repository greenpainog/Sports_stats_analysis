"""On-floor filters on the hand-made league of test_stat_query, plus hand-written stints.

Alpha's stints (only the named players are listed; the query never needs all five):
  g2 (Alpha 70 - 75 Beta):  0-600 s   Shooter + Kick   20-10
                            600-2400  Shooter          50-65
  g3 (Gamma 60 - 90 Alpha): 0-1200    Shooter          40-30
                            1200-2400 Shooter + Kick   50-30
  g4: no play-by-play (no pbp_checks row) -> left out with a note
  g7: play-by-play with a broken lineup (pbp_checks.problems) -> left out with a note

So Shooter with Kick on the floor: 30 minutes, +30 (g2 +10, g3 +20), +40.0 per 40.
Shooter with Kick off the floor: 50 minutes, -5 (g2 -15, g3 +10), 90 scored, 95 allowed.
"""
import tempfile
import unittest
from pathlib import Path

import database.db as db
from analysis.stat_query import QueryError, query_stats
from api.models import QuerySpec
from api.service import answer_spec
from tests.test_stat_query import build_test_league

STINTS = {  # source_game_id: [(start, end, points_for, points_against, players)]
    "g2": [(0, 600, 20, 10, ("S1", "S2")), (600, 2400, 50, 65, ("S1",))],
    "g3": [(0, 1200, 40, 30, ("S1",)), (1200, 2400, 50, 30, ("S1", "S2"))],
    "g7": [(0, 2400, 88, 90, ("S2",))],
}


def add_stints():
    conn = db.get_connection()
    ids = dict(conn.execute("SELECT source_game_id, id FROM games"))
    players = dict(conn.execute("SELECT source_player_id, id FROM players"))
    alpha = conn.execute("SELECT id FROM teams WHERE name = 'Alpha'").fetchone()[0]
    for game, stints in STINTS.items():
        for start, end, scored, allowed, codes in stints:
            stint_id = conn.execute(
                """INSERT INTO stints (game_id, team_id, start_elapsed, end_elapsed, points_for, points_against)
                   VALUES (?, ?, ?, ?, ?, ?)""", (ids[game], alpha, start, end, scored, allowed)).lastrowid
            conn.executemany("INSERT INTO stint_players VALUES (?, ?)", [(stint_id, players[c]) for c in codes])
        problems = "ALP: 4 on the floor at 100s" if game == "g7" else None
        conn.execute("INSERT INTO pbp_checks VALUES (?, 1, ?, 0, 0, ?)", (ids[game], len(stints), problems))
    conn.commit()
    conn.close()


class OnFloorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = db.DATABASE_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        db.DATABASE_FILE = str(Path(cls.tmp.name) / "league.db")
        build_test_league()
        add_stints()

    @classmethod
    def tearDownClass(cls):
        db.DATABASE_FILE = cls.original_db
        cls.tmp.cleanup()

    def test_with_a_teammate_on_the_floor(self):
        result = query_stats(["minutes", "plus_minus", "points_for"], player="Shooter", on_floor_with=["Sid Kick"])
        self.assertEqual([g.values["plus_minus"] for g in result.games], [10, 20])
        self.assertEqual([g.values["minutes"] for g in result.games], [10, 20])
        minutes, plus_minus = result.summary["minutes"], result.summary["plus_minus"]
        self.assertEqual((minutes.total, minutes.per_game), (30, 15))
        self.assertEqual((plus_minus.total, plus_minus.value), (30, 40.0))  # +30 in 30 minutes = +40 per 40
        self.assertEqual(result.summary["points_for"].total, 70)
        self.assertEqual(result.filters["on_floor_with"], "Sid Kick")
        self.assertEqual(result.notes, ["1 matching game(s) have no play-by-play yet and are left out."])
        self.assertIn("plus_minus: +30 total (+15.0 per game, +40.0 per 40 minutes)", result.describe())

    def test_with_a_teammate_on_the_bench(self):
        result = query_stats(["minutes", "plus_minus", "points_for", "points_against"],
                             player="Shooter", on_floor_without=["Sid Kick"])
        totals = {stat: s.total for stat, s in result.summary.items()}
        self.assertEqual(totals, {"minutes": 50, "plus_minus": -5, "points_for": 90, "points_against": 95})

    def test_team_subject_and_broken_games_are_left_out(self):
        result = query_stats(["plus_minus", "win_pct"], team="Alpha", on_floor_with=["Sid Kick"])
        self.assertEqual(result.summary["plus_minus"].total, 30)  # g7 has Kick on the floor but is broken
        self.assertEqual(len(result.games), 2)
        self.assertTrue(any("left out: the source's substitutions" in note for note in result.notes))
        self.assertEqual((result.summary["win_pct"].wins, result.summary["win_pct"].losses), (1, 1))

    def test_points_for_without_filters_is_the_subjects_own_on_floor_time(self):
        result = query_stats(["points_for", "points_against"], player="Shooter")
        self.assertEqual((result.summary["points_for"].total, result.summary["points_against"].total),
                         (160, 135))

    def test_last_n_picks_games_first(self):
        # Shooter's last game is g4, which has no play-by-play: nothing is left.
        result = query_stats(["plus_minus"], player="Shooter", on_floor_with=["Sid Kick"], last_n=1)
        self.assertEqual(result.games, [])
        self.assertIn("no games match", result.describe())

    def test_mistakes_are_errors(self):
        cases = [
            (dict(stats=["pir"], on_floor_with=["Sid Kick"]), "can't be asked"),
            (dict(stats=["plus_minus"], on_floor_with=["Olly Kick"]), "same team"),
            (dict(stats=["plus_minus"], on_floor_with=["Sam Shooter"]), "always on the floor"),
            (dict(stats=["plus_minus"], on_floor_with=["Sid Kick"], on_floor_without=["Sid Kick"]), "only once"),
            (dict(stats=["plus_minus"], on_floor_with=["Sid Kick"], competition="greek_league"), "Greek"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(QueryError) as caught:
                query_stats(player="Sam Shooter", **kwargs)
            self.assertIn(message, str(caught.exception))

    def test_spec_validation_and_compare(self):
        with self.assertRaises(ValueError):
            QuerySpec(stats=["pir"], player="Shooter", on_floor_with=["Kick"])
        with self.assertRaises(ValueError):
            QuerySpec(stats=["points_for"], player="Shooter", competition="greek_league")
        # points_for, not plus_minus: the shared (no on-floor filter) part of a compare reads
        # box-score plus_minus, which this test league doesn't store.
        spec = QuerySpec(stats=["points_for"], player="Shooter", compare=[
            {"label": "Kick on", "on_floor_with": ["Sid Kick"]},
            {"label": "Kick off", "on_floor_without": ["Sid Kick"]}])
        response = answer_spec(spec)
        self.assertEqual([side.result.summary["points_for"].total for side in response.comparison], [70, 90])
        self.assertIn("Kick on 70 total (93.3 per 40 min)", response.answer)
        self.assertEqual(response.comparison[0].result.notes,
                         ["1 matching game(s) have no play-by-play yet and are left out."])
