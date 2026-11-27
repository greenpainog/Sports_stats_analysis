"""Lineup rankings on the hand-made league plus the hand-written stints of test_on_floor.

Alpha's stints that hold both Sam Shooter (S1) and Sid Kick (S2):
  g2: 0-600 s, 20-10   g3: 1200-2400 s, 50-30   (g7 has a broken lineup: left out)
So the duo played 30 minutes over 2 games, +30, +40.0 per 40: under 40 minutes, a small sample.
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
        self.assertIn("1. Sam Shooter + Sid Kick: +40.0 per 40 min (+30 in 30.0 min over 2 games", text)
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
