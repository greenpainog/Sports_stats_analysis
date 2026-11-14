"""Checks the Euroleague mapping against one real game: E2024 game 1,
ALBA Berlin 77 - 87 Panathinaikos (3 Oct 2024).

The test reads the cached responses in data/raw/euroleague/, which are not
committed. If they are missing, run once:
    python -m ingestion.euroleague --season 2024 --game 1
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import database.db as db
from database.repositories import save_game_bundle
from database.schema import create_tables
from ingestion.euroleague import (
    build_game_bundle,
    minutes_to_float,
    player_display_name,
)

RAW = Path("data/raw/euroleague")
HEADER = RAW / "header_E2024_1.json"
BOXSCORE = RAW / "boxscore_E2024_1.json"


class HelperTests(unittest.TestCase):
    def test_player_display_name(self):
        self.assertEqual(player_display_name("NUNN, KENDRICK"), "Kendrick Nunn")
        self.assertEqual(player_display_name(" GRANT, JERIAN "), "Jerian Grant")

    def test_minutes_to_float(self):
        self.assertEqual(minutes_to_float("16:18"), 16.3)
        self.assertIsNone(minutes_to_float("DNP"))
        self.assertIsNone(minutes_to_float(None))


@unittest.skipUnless(HEADER.exists() and BOXSCORE.exists(), "cached E2024 game 1 not found")
class OneGameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.header = json.loads(HEADER.read_text(encoding="utf-8"))
        cls.boxscore = json.loads(BOXSCORE.read_text(encoding="utf-8"))
        cls.bundle = build_game_bundle(cls.header, cls.boxscore, "E", 2024, 1)

    def _stat(self, entity_type, side, stat_name, player_id=None):
        for s in self.bundle["stats"]:
            if (s["entity_type"], s["team"], s["stat_name"]) == (entity_type, side, stat_name):
                if player_id is None or s["player"]["source_player_id"] == player_id:
                    return s["stat_value"]
        raise KeyError(stat_name)

    def test_game_details(self):
        b = self.bundle
        self.assertEqual(b["home_team"]["source_team_id"], "BER")
        self.assertEqual(b["away_team"]["source_team_id"], "PAN")
        self.assertEqual((b["home_score"], b["away_score"]), (77, 87))
        self.assertEqual(b["start_time"], "2024-10-03T18:45")
        self.assertEqual(b["season"]["name"], "2024-25")

    def test_dnp_players_are_left_out(self):
        played = {(a["team"], a["player"]["source_player_id"]) for a in self.bundle["appearances"]}
        self.assertEqual(sum(1 for side, _ in played if side == "home"), 10)
        self.assertEqual(sum(1 for side, _ in played if side == "away"), 11)
        self.assertNotIn(("home", "P012099"), played)  # Grosber, DNP

    def test_nunn_line(self):
        nunn = "P012774"
        self.assertEqual(self._stat("player", "away", "points", nunn), 16)
        self.assertEqual(self._stat("player", "away", "fg3_made", nunn), 1)
        self.assertEqual(self._stat("player", "away", "fg3_attempted", nunn), 5)

    def test_team_totals_match_source(self):
        for side, idx in (("home", 0), ("away", 1)):
            totr = self.boxscore["Stats"][idx]["totr"]
            self.assertEqual(self._stat("team", side, "points"), totr["Points"])
            self.assertEqual(self._stat("team", side, "total_rebounds"), totr["TotalRebounds"])
            self.assertEqual(self._stat("team", side, "turnovers"), totr["Turnovers"])

    def test_player_points_add_up_to_team_points(self):
        for side in ("home", "away"):
            player_points = sum(
                s["stat_value"] for s in self.bundle["stats"]
                if s["entity_type"] == "player" and s["team"] == side and s["stat_name"] == "points"
            )
            self.assertEqual(player_points, self._stat("team", side, "points"))

    def test_saving_twice_creates_no_duplicates(self):
        original = db.DATABASE_FILE
        with tempfile.TemporaryDirectory() as tmp:
            db.DATABASE_FILE = str(Path(tmp) / "test.db")
            try:
                create_tables()
                first_id = save_game_bundle(self.bundle)
                second_id = save_game_bundle(self.bundle)
                self.assertEqual(first_id, second_id)

                conn = sqlite3.connect(db.DATABASE_FILE)
                try:
                    counts = {
                        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                        for t in ("games", "teams", "players", "game_appearances", "game_stats")
                    }
                    home_points = conn.execute(
                        """SELECT gs.stat_value FROM game_stats gs
                           JOIN teams t ON t.id = gs.team_id
                           WHERE gs.entity_type = 'team' AND gs.stat_name = 'points'
                             AND t.source_team_id = 'BER'"""
                    ).fetchone()[0]
                finally:
                    conn.close()
            finally:
                db.DATABASE_FILE = original

        self.assertEqual(counts["games"], 1)
        self.assertEqual(counts["teams"], 2)
        self.assertEqual(counts["players"], 21)
        self.assertEqual(counts["game_appearances"], 21)
        self.assertEqual(home_points, 77)
        # 21 players x 20 stats (18 mapped + minutes + plus_minus) + 2 teams x 18
        self.assertEqual(counts["game_stats"], 21 * 20 + 2 * 18)


if __name__ == "__main__":
    unittest.main()
