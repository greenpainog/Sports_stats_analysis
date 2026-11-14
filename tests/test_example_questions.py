"""The four example questions from the project plan, on the real 2024-25 season.

Each expected answer is computed separately here, straight from the cached
raw JSON (season game list + box scores), without the database or SQL, so the
query layer is checked against an independent calculation.

Loading the full season into a throwaway database takes about 30 seconds.
Skips if data/raw/euroleague/ does not hold the whole 2024-25 season.
"""
import json
import tempfile
import unittest
from pathlib import Path

import database.db as db
from analysis.stat_query import QueryError, query_stats
from database.schema import create_tables
from ingestion.euroleague import ingest_season

RAW = Path("data/raw/euroleague")
SEASON_FILE = RAW / "games_E2024.json"

NUNN, GRANT, OSMAN = "P012774", "P011204", "P004088"


def _cached_season():
    if not SEASON_FILE.exists():
        return None
    games = [g for g in json.loads(SEASON_FILE.read_text(encoding="utf-8"))["data"] if g["played"]]
    boxscores = {}
    for g in games:
        path = RAW / f"boxscore_E2024_{g['gameCode']}.json"
        if not path.exists() or not (RAW / f"header_E2024_{g['gameCode']}.json").exists():
            return None
        boxscores[g["gameCode"]] = json.loads(path.read_text(encoding="utf-8"))
    games.sort(key=lambda g: g["utcDate"])
    return games, boxscores


SEASON = _cached_season()


def _player_line(boxscore, player_id):
    """(side index, row) for a player who played, else None."""
    for side, team in enumerate(boxscore["Stats"]):
        for row in team["PlayersStats"]:
            if row["Player_ID"].strip() == player_id and row["Minutes"] not in (None, "DNP"):
                return side, row
    return None


def _clubs(game, side):
    """(own club code, opponent club code, venue) for box score side 0 (home) or 1."""
    home, away = game["local"]["club"]["code"], game["road"]["club"]["code"]
    neutral = game["isNeutralVenue"] or game["phaseType"]["code"] == "FF"
    if side == 0:
        return home, away, "neutral" if neutral else "home"
    return away, home, "neutral" if neutral else "away"


@unittest.skipUnless(SEASON, "full cached 2024-25 season not found")
class ExampleQuestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.games, cls.boxscores = SEASON
        cls.original_db = db.DATABASE_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        db.DATABASE_FILE = str(Path(cls.tmp.name) / "season.db")
        create_tables()
        failures = ingest_season("E", 2024, verbose=False)
        assert not failures, failures

    @classmethod
    def tearDownClass(cls):
        db.DATABASE_FILE = cls.original_db
        cls.tmp.cleanup()

    def _games_played(self, player_id):
        for game in self.games:
            line = _player_line(self.boxscores[game["gameCode"]], player_id)
            if line:
                yield game, line[0], line[1]

    def test_q1_nunn_3pt_pct_last_5_games(self):
        last5 = list(self._games_played(NUNN))[-5:]
        made = sum(row["FieldGoalsMade3"] for _, _, row in last5)
        attempted = sum(row["FieldGoalsAttempted3"] for _, _, row in last5)

        result = query_stats(["fg3_pct"], player="Nunn", last_n=5)
        s = result.summary["fg3_pct"]
        self.assertEqual(len(result.games), 5)
        self.assertEqual((s.made, s.attempted), (made, attempted))
        self.assertEqual(s.pct, round(100 * made / attempted, 1))

    def test_q2_grant_turnovers_assists_vs_teams_over_500(self):
        record = {}  # club code -> [wins, losses] so far
        expected = {"games": 0, "turnovers": 0, "assists": 0}
        for game in self.games:  # in start-time order
            line = _player_line(self.boxscores[game["gameCode"]], GRANT)
            if line:
                _, opponent, _ = _clubs(game, line[0])
                wins, losses = record.get(opponent, [0, 0])
                if wins > losses:
                    expected["games"] += 1
                    expected["turnovers"] += line[1]["Turnovers"]
                    expected["assists"] += line[1]["Assistances"]
            home, away = game["local"], game["road"]
            winner, loser = (home, away) if home["score"] > away["score"] else (away, home)
            record.setdefault(winner["club"]["code"], [0, 0])[0] += 1
            record.setdefault(loser["club"]["code"], [0, 0])[1] += 1

        result = query_stats(["turnovers", "assists"], player="Jerian Grant", opponent_over_500=True)
        self.assertEqual(len(result.games), expected["games"])
        self.assertEqual(result.summary["turnovers"].total, expected["turnovers"])
        self.assertEqual(result.summary["assists"].total, expected["assists"])

    def test_q3_grant_3pt_pct_vs_real_madrid_away(self):
        lines = [
            row for game, side, row in self._games_played(GRANT)
            if _clubs(game, side)[1:] == ("MAD", "away")
        ]
        self.assertGreater(len(lines), 0)
        result = query_stats(["fg3_pct"], player="Jerian Grant", opponent="Real Madrid", venue="away")
        s = result.summary["fg3_pct"]
        self.assertEqual(len(result.games), len(lines))
        self.assertEqual(s.made, sum(r["FieldGoalsMade3"] for r in lines))
        self.assertEqual(s.attempted, sum(r["FieldGoalsAttempted3"] for r in lines))

    def test_q4_valanciunas_is_not_in_this_season(self):
        with self.assertRaises(QueryError) as ctx:
            query_stats(["total_rebounds"], team="Panathinaikos", opponent="Zalgiris",
                        venue="away", without_player="Valanciunas")
        self.assertIn("Valanciunas", str(ctx.exception))

    def test_q4_shape_pan_rebounds_at_zalgiris_without_osman(self):
        expected = []
        for game in self.games:
            box = self.boxscores[game["gameCode"]]
            if (game["local"]["club"]["code"], game["road"]["club"]["code"]) == ("ZAL", "PAN") \
                    and not _player_line(box, OSMAN):
                expected.append(box["Stats"][1]["totr"]["TotalRebounds"])
        self.assertGreater(len(expected), 0)

        result = query_stats(["total_rebounds"], team="Panathinaikos", opponent="Zalgiris",
                             venue="away", without_player="Cedi Osman")
        self.assertEqual(len(result.games), len(expected))
        self.assertEqual(result.summary["total_rebounds"].total, sum(expected))


if __name__ == "__main__":
    unittest.main()
