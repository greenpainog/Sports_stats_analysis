"""Lineup rankings on the hand-made league plus the hand-written stints of test_on_floor.

Alpha's stints that hold both Sam Shooter (S1) and Sid Kick (S2):
  g2: 0-600 s, 20-10   g3: 1200-2400 s, 50-30   (g7 has a broken lineup: left out)
So the duo played 30 minutes over 2 games, +30, +40.0 per 40: under 40 minutes, a small sample.
Possessions are set by hand here: 10 in the g2 stint and 30 in the g3 stint (each team), so the
duo's 40 possessions give 175.0 offense, 100.0 defense, +75.0 net rating.
"""
import tempfile
import unittest
from pathlib import Path

import database.db as db
from agent.tools import run_tool
from analysis.lineups import lineup_rankings
from analysis.stat_query import QueryError
from api.models import LineupSpec
from api.service import lineups_spec
from analysis.stat_query import query_stats
from ingestion.euroleague import update_stint_possessions
from llm.question_parser import to_ranking
from tests.test_on_floor import add_stints
from tests.test_stat_query import build_test_league


class LineupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = db.DATABASE_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        db.DATABASE_FILE = str(Path(cls.tmp.name) / "league.db")
        build_test_league()
        add_stints()
        conn = db.get_connection()
        for start, possessions in ((0, 10), (1200, 30), (600, 25)):  # g2 0-600, g3 1200-2400, g2 600-2400
            conn.execute("UPDATE stints SET own_possessions = ?, opponent_possessions = ? WHERE start_elapsed = ?",
                         (possessions, possessions, start))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        db.DATABASE_FILE = cls.original_db
        cls.tmp.cleanup()

    def test_duo_totals_come_from_the_shared_stints(self):
        result = lineup_rankings("Alpha", size=2)
        self.assertEqual(result.lineup_count, 1)
        duo = result.lineups[0]
        self.assertEqual(duo.players, ["Sam Shooter", "Sid Kick"])
        self.assertEqual((duo.games, duo.minutes, duo.points_for, duo.points_against), (2, 30, 70, 40))
        self.assertEqual((duo.plus_minus, duo.per_40(duo.plus_minus)), (30, 40.0))
        self.assertTrue(duo.small_sample)
        self.assertEqual(result.games, 2)  # g2 and g3; g4 has no play-by-play, g7 is broken
        self.assertEqual(len(result.notes), 2)

    def test_small_samples_are_listed_and_flagged(self):
        text = lineup_rankings("Alpha", size=2).describe()
        self.assertIn("1. Sam Shooter + Sid Kick: net rating +75.0 (off 175.0, def 100.0, 40 possessions); "
                      "+30 in 30.0 min over 2 games (+40.0 per 40 min", text)
        self.assertIn("[SMALL SAMPLE: under 40 minutes together]", text)
        self.assertIn("1 of these 1 lineups played under 40 minutes", text)

    def test_named_player_comes_first_and_bigger_groups_need_more_players(self):
        result = lineup_rankings("Alpha", size=2, with_players=["Sid Kick"])
        self.assertEqual(result.lineups[0].players, ["Sid Kick", "Sam Shooter"])
        self.assertEqual(lineup_rankings("Alpha", size=3).lineups, [])  # the test stints list only 1-2 players

    def test_mistakes_are_errors(self):
        for kwargs, message in [(dict(size=2, with_players=["Sid Kick", "Sam Shooter"]), "at most 1"),
                                (dict(competition="greek_league"), "Greek"),
                                (dict(size=6), "size")]:
            with self.subTest(kwargs=kwargs), self.assertRaises(QueryError) as caught:
                lineup_rankings("Alpha", **kwargs)
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(ValueError):
            LineupSpec(team="Alpha", size=2, with_players=["A", "B"])

    def test_api_response_and_agent_tool(self):
        response = lineups_spec(LineupSpec(team="Alpha"))
        row = response.lineups[0]
        self.assertEqual((row.minutes, row.plus_minus, row.net_per_40, row.small_sample), (30, 30, 40.0, True))
        self.assertEqual((row.possessions, row.off_rating, row.def_rating, row.net_rating), (40, 175.0, 100.0, 75.0))
        text = run_tool("lineup_rankings", {"team": "Alpha", "size": 2})
        self.assertIn("Sam Shooter + Sid Kick", text)

    def test_parser_mapping(self):
        request = to_ranking({"kind": "lineups", "team": "Alpha", "lineup_size": 5, "lineup_players": ["Kick"],
                              "lineup_sort": "minutes", "order": None, "limit": 3})
        self.assertEqual((request.spec.size, request.spec.with_players, request.spec.sort, request.spec.limit),
                         (5, ["Kick"], "minutes", 3))
        with self.assertRaises(ValueError):
            to_ranking({"kind": "lineups", "team": "Alpha", "stats": ["points"]})
        with self.assertRaises(ValueError):
            to_ranking({"kind": "lineups", "lineup_size": 2})

    def test_on_floor_ratings_divide_summed_points_by_summed_possessions(self):
        result = query_stats(["possessions", "off_rating", "def_rating", "net_rating"], player="Shooter",
                             on_floor_with=["Sid Kick"])
        values = {stat: s.value for stat, s in result.summary.items()}
        # 70 scored, 40 allowed in 40 possessions; possessions per 40 minutes = 40 / 30 * 40.
        self.assertEqual(values, {"possessions": 53.3, "off_rating": 175.0, "def_rating": 100.0, "net_rating": 75.0})
        self.assertEqual([g.values["net_rating"] for g in result.games], [100.0, 66.7])  # +10 / 10, +20 / 30
        self.assertIn("net_rating: +75.0 per 100 possessions", result.describe())
        with self.assertRaises(QueryError):
            query_stats(["net_rating"], player="Shooter", clutch=True)

    def test_possessions_are_counted_from_the_events(self):
        conn = db.get_connection()
        game_id = conn.execute("SELECT id FROM games WHERE source_game_id = 'g7'").fetchone()[0]
        alpha, gamma = (conn.execute("SELECT id FROM teams WHERE name = ?", (n,)).fetchone()[0] for n in ("Alpha", "Gamma"))
        stint = conn.execute("SELECT id FROM stints WHERE game_id = ?", (game_id,)).fetchone()[0]
        # Alpha: 2 shots, a turnover, an offensive rebound, 2 free throws = 2 + 1 - 1 + 0.88; Gamma: one 3.
        plays = [(alpha, "2FGA"), (alpha, "O"), (alpha, "2FGM"), (alpha, "TO"), (alpha, "FTM"), (alpha, "FTA"),
                 (gamma, "3FGM"), (alpha, "AS"), (None, "BP")]
        conn.executemany(
            """INSERT INTO pbp_events (game_id, sequence, period, elapsed, team_id, play_type, score_home, score_away,
                                       home_stint_id) VALUES (?, ?, 1, 0, ?, ?, 0, 0, ?)""",
            [(game_id, i, team, play, stint) for i, (team, play) in enumerate(plays)])
        self.assertEqual(update_stint_possessions(conn, [game_id]), 1)
        own, opp = conn.execute("SELECT own_possessions, opponent_possessions FROM stints WHERE id = ?",
                                (stint,)).fetchone()
        conn.rollback()
        conn.close()
        self.assertAlmostEqual(own, 2.88)
        self.assertEqual(opp, 1)
