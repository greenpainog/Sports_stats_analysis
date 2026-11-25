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
    _is_neutral_venue,
    build_game_bundle,
    minutes_to_float,
    player_display_name,
    utc_start_time,
)

RAW = Path("data/raw/euroleague")
HEADER = RAW / "header_E2024_1.json"
BOXSCORE = RAW / "boxscore_E2024_1.json"
SEASON_GAMES = RAW / "games_E2024.json"


class HelperTests(unittest.TestCase):
    def test_player_display_name(self):
        self.assertEqual(player_display_name("NUNN, KENDRICK"), "Kendrick Nunn")
        self.assertEqual(player_display_name(" GRANT, JERIAN "), "Jerian Grant")

    def test_minutes_to_float(self):
        self.assertEqual(minutes_to_float("16:18"), 16.3)
        self.assertIsNone(minutes_to_float("DNP"))
        self.assertIsNone(minutes_to_float(None))


class SchemaMigrationTests(unittest.TestCase):
    def test_old_games_table_gets_new_columns_and_keeps_rows(self):
        original = db.DATABASE_FILE
        with tempfile.TemporaryDirectory() as tmp:
            db.DATABASE_FILE = str(Path(tmp) / "old.db")
            try:
                conn = sqlite3.connect(db.DATABASE_FILE)
                conn.execute("""CREATE TABLE games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, season_id INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'flashscore', source_game_id TEXT,
                    source_url TEXT, start_time TEXT NOT NULL, status TEXT,
                    home_team_id INTEGER NOT NULL, away_team_id INTEGER NOT NULL,
                    home_score INTEGER, away_score INTEGER)""")
                conn.execute("""INSERT INTO games (season_id, start_time, home_team_id, away_team_id)
                                VALUES (1, '2024-10-03T16:45:00Z', 1, 2)""")
                conn.commit()
                conn.close()

                create_tables()
                create_tables()  # running twice must be harmless

                conn = sqlite3.connect(db.DATABASE_FILE)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
                rows = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
                conn.close()
            finally:
                db.DATABASE_FILE = original

        self.assertTrue({"round_number", "phase", "is_neutral_venue"} <= columns)
        self.assertEqual(rows, 1)


class NeutralVenueTests(unittest.TestCase):
    def test_final_four_is_neutral_even_when_source_flag_is_false(self):
        # The 2024-25 Final in Abu Dhabi has isNeutralVenue = False in the source.
        self.assertTrue(_is_neutral_venue({"isNeutralVenue": False}, "final_four"))
        self.assertTrue(_is_neutral_venue({"isNeutralVenue": True}, "final_four"))
        self.assertFalse(_is_neutral_venue({"isNeutralVenue": False}, "playoffs"))


class StartTimeTests(unittest.TestCase):
    def test_utc_start_time(self):
        # Final Four 2025 in Abu Dhabi: 21:00 local (UTC+4) = 17:00 UTC.
        game = {"utcDate": "2025-05-25T17:00:00Z", "localDate": "2025-05-25T21:00:00"}
        self.assertEqual(utc_start_time(game), "2025-05-25T17:00:00Z")


@unittest.skipUnless(
    HEADER.exists() and BOXSCORE.exists() and SEASON_GAMES.exists(),
    "cached E2024 game 1 not found",
)
class OneGameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.header = json.loads(HEADER.read_text(encoding="utf-8"))
        cls.boxscore = json.loads(BOXSCORE.read_text(encoding="utf-8"))
        season_games = json.loads(SEASON_GAMES.read_text(encoding="utf-8"))["data"]
        cls.schedule_game = next(g for g in season_games if g["gameCode"] == 1)
        cls.bundle = build_game_bundle(
            cls.header, cls.boxscore, cls.schedule_game, "E", 2024, 1
        )

    def test_mismatched_home_team_is_rejected(self):
        swapped = dict(self.schedule_game, local=self.schedule_game["road"],
                       road=self.schedule_game["local"])
        with self.assertRaises(ValueError):
            build_game_bundle(self.header, self.boxscore, swapped, "E", 2024, 1)

    def _stat(self, entity_type, side, stat_name, player_id=None, period="match"):
        for s in self.bundle["stats"]:
            if (s["entity_type"], s["team"], s["stat_name"], s.get("period", "match")) == (
                    entity_type, side, stat_name, period):
                if player_id is None or s["player"]["source_player_id"] == player_id:
                    return s["stat_value"]
        raise KeyError(stat_name)

    def test_game_details(self):
        b = self.bundle
        self.assertEqual(b["home_team"]["source_team_id"], "BER")
        self.assertEqual(b["away_team"]["source_team_id"], "PAN")
        self.assertEqual((b["home_score"], b["away_score"]), (77, 87))
        # Header said 18:45 (Berlin, UTC+2); stored as UTC.
        self.assertEqual(b["start_time"], "2024-10-03T16:45:00Z")
        self.assertEqual(b["home_team"]["name"], "ALBA Berlin")
        self.assertEqual(b["round_number"], 1)
        self.assertEqual(b["phase"], "regular_season")
        self.assertFalse(b["is_neutral_venue"])
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

    def test_quarter_points_add_up_to_the_final_score(self):
        # The header's quarter scores are running totals; the bundle stores each quarter's own points.
        home = [self._stat("team", "home", "points", period=f"q{i}") for i in range(1, 5)]
        self.assertEqual(home, [17, 19, 21, 20])
        for side in ("home", "away"):
            quarters = sum(self._stat("team", side, "points", period=f"q{i}") for i in range(1, 5))
            self.assertEqual(quarters, self._stat("team", side, "points"))

    def test_coaches_and_referees(self):
        staff = self.bundle["staff"]
        self.assertEqual([p["team"] for p in staff if p["role"] == "coach"], ["home", "away"])
        self.assertEqual(sum(p["role"] == "referee" for p in staff), 3)
        self.assertIn({"role": "coach", "team": "away", "name": "Ergin Ataman"}, staff)

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
                        for t in ("games", "teams", "players", "game_appearances", "game_stats", "game_staff")
                    }
                    home_points = conn.execute(
                        """SELECT gs.stat_value FROM game_stats gs
                           JOIN teams t ON t.id = gs.team_id
                           WHERE gs.entity_type = 'team' AND gs.stat_name = 'points'
                             AND gs.period = 'match' AND t.source_team_id = 'BER'"""
                    ).fetchone()[0]
                    saved_game = conn.execute(
                        "SELECT round_number, phase, is_neutral_venue FROM games"
                    ).fetchone()
                finally:
                    conn.close()
            finally:
                db.DATABASE_FILE = original

        self.assertEqual(saved_game, (1, "regular_season", 0))
        self.assertEqual(counts["games"], 1)
        self.assertEqual(counts["teams"], 2)
        self.assertEqual(counts["players"], 21)
        self.assertEqual(counts["game_appearances"], 21)
        self.assertEqual(home_points, 77)
        # 21 players x 20 stats (18 mapped + minutes + plus_minus) + 2 teams x 19 (18 + minutes)
        # + 2 teams x 4 quarters of points
        self.assertEqual(counts["game_stats"], 21 * 20 + 2 * 19 + 2 * 4)
        self.assertEqual(counts["game_staff"], 5)  # 2 coaches + 3 referees, once


if __name__ == "__main__":
    unittest.main()
