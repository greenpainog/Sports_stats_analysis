"""Query-layer logic on a tiny hand-made league where every answer is known.

Teams Alpha, Beta, Gamma. Games in order:
  g1 2024-10-01  Beta 80 - 70 Gamma   (Beta 1-0, Gamma 0-1)
  g2 2024-10-08  Alpha 70 - 75 Beta   Beta was 1-0 before this game -> over .500
  g3 2024-10-15  Gamma 60 - 90 Alpha  Gamma was 0-1 -> not over .500
  g4 2024-10-22  Alpha 85 - 80 Beta   neutral venue; Beta was 2-0 -> over .500

Shooter (Alpha) plays g2, g3, g4. Sidekick (Alpha) plays g2, g3 only.
g7 2024-10-24  Alpha 88 - 90 Gamma  48 hours after Alpha's g4 (Sid Kick plays)
g5 2024-10-29  Gamma 70 - 72 Beta (adds Olly Kick, so "Kick" is ambiguous)
g6 2025-10-07  Alpha 77 - 70 Gamma, season 2025-26, no Alpha player stats
"""
import tempfile
import unittest
from pathlib import Path

import database.db as db
from analysis.stat_query import QueryError, query_stats
from database.repositories import save_game_bundle
from database.schema import create_tables

TEAMS = {
    "ALP": {"name": "Alpha", "source_team_id": "ALP"},
    "BET": {"name": "Beta", "source_team_id": "BET"},
    "GAM": {"name": "Gamma", "source_team_id": "GAM"},
}
SHOOTER = {"name": "Sam Shooter", "source_player_id": "S1"}
SIDEKICK = {"name": "Sid Kick", "source_player_id": "S2"}
OTHER_KICK = {"name": "Olly Kick", "source_player_id": "S3"}  # makes "Kick" ambiguous


def _player_stats(player, side, made3, att3, assists):
    return [
        {"entity_type": "player", "team": side, "player": player,
         "stat_name": name, "stat_value": value}
        for name, value in (("fg3_made", made3), ("fg3_attempted", att3), ("assists", assists))
    ]


def _game(code, day, home, away, home_score, away_score, neutral=False, alpha_players=(),
          season="2024-25"):
    alpha_side = "home" if home == "ALP" else "away"
    stats = [
        {"entity_type": "team", "team": "home", "stat_name": "total_rebounds", "stat_value": 30 + code},
        {"entity_type": "team", "team": "away", "stat_name": "total_rebounds", "stat_value": 20 + code},
    ]
    for player, made3, att3, assists in alpha_players:
        stats += _player_stats(player, alpha_side, made3, att3, assists)
    return {
        "source": "test", "source_game_id": f"g{code}",
        "competition": {"name": "Test League", "source_competition_id": "T"},
        "season": {"name": season},
        "home_team": TEAMS[home], "away_team": TEAMS[away],
        "start_time": f"{day}T19:00:00Z",
        "home_score": home_score, "away_score": away_score,
        "phase": "regular_season", "is_neutral_venue": neutral,
        "stats": stats,
    }


GAMES = [
    _game(1, "2024-10-01", "BET", "GAM", 80, 70,
          alpha_players=()),
    _game(2, "2024-10-08", "ALP", "BET", 70, 75,
          alpha_players=[(SHOOTER, 1, 1, 2), (SIDEKICK, 0, 2, 5)]),
    _game(3, "2024-10-15", "GAM", "ALP", 60, 90,
          alpha_players=[(SHOOTER, 6, 14, 4), (SIDEKICK, 1, 1, 1)]),
    _game(4, "2024-10-22", "ALP", "BET", 85, 80, neutral=True,
          alpha_players=[(SHOOTER, 2, 5, 6)]),
    # Exactly 48 hours after Alpha's g4; Gamma last played g3 (216 hours before).
    _game(7, "2024-10-24", "ALP", "GAM", 88, 90,
          alpha_players=[(SIDEKICK, 0, 1, 2)]),
    # Next season; Sid Kick has not played for Alpha in 2025-26.
    _game(6, "2025-10-07", "ALP", "GAM", 77, 70, season="2025-26"),
]


def _day(game):
    return game.start_time[:10]


def build_test_league():
    """Create the tables and load the hand-made league into the current database."""
    create_tables()
    for game in GAMES:
        save_game_bundle(game)
    # A Gamma player whose surname clashes with Sid Kick.
    clash = _game(5, "2024-10-29", "GAM", "BET", 70, 72)
    clash["stats"] += _player_stats(OTHER_KICK, "home", 0, 0, 0)
    save_game_bundle(clash)


class StatQueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = db.DATABASE_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        db.DATABASE_FILE = str(Path(cls.tmp.name) / "league.db")
        build_test_league()

    @classmethod
    def tearDownClass(cls):
        db.DATABASE_FILE = cls.original_db
        cls.tmp.cleanup()

    def test_percentage_uses_total_makes_and_attempts(self):
        # Shooter's games: 1/1, 6/14, 2/5 -> 9/20 = 45.0%.
        # Averaging the per-game percentages (100%, 42.9%, 40%) would give 61.0%.
        result = query_stats(["fg3_pct"], player="Sam Shooter")
        s = result.summary["fg3_pct"]
        self.assertEqual((s.made, s.attempted, s.pct), (9, 20, 45.0))
        self.assertEqual([g.values["fg3_pct"] for g in result.games], ["1/1", "6/14", "2/5"])

    def test_last_n_takes_most_recent_games_in_date_order(self):
        result = query_stats(["assists"], player="Shooter", last_n=2)
        self.assertEqual([g.game_id for g in result.games], [3, 4])  # oldest first
        self.assertEqual(result.summary["assists"].total, 10)
        self.assertEqual(result.summary["assists"].per_game, 5)

    def test_neutral_games_are_neither_home_nor_away(self):
        venues = {v: len(query_stats(["assists"], player="Shooter", venue=v).games)
                  for v in ("home", "away", "neutral")}
        self.assertEqual(venues, {"home": 1, "away": 1, "neutral": 1})

    def test_opponent_over_500_uses_record_before_the_game(self):
        result = query_stats(["assists"], player="Shooter", opponent_over_500=True)
        # g2 (Beta 1-0) and g4 (Beta 2-0) count; g3 (Gamma 0-1) does not.
        self.assertEqual([g.game_id for g in result.games], [2, 4])

    def test_opponent_with_no_games_yet_is_not_over_500(self):
        # Before g1 Gamma had no games (0-0), so Beta's g1 is not against a winning team.
        result = query_stats(["total_rebounds"], team="Beta", opponent_over_500=True)
        self.assertNotIn(1, [g.game_id for g in result.games])

    def test_without_player(self):
        result = query_stats(["total_rebounds"], team="Alpha", without_player="Sid Kick")
        # g4: same season, he missed it. g6 (2025-26) does not count: he has
        # not played for Alpha that season, so it is not a game "without" him.
        self.assertEqual([g.game_id for g in result.games], [4])
        self.assertEqual(result.summary["total_rebounds"].total, 34)  # Alpha home in g4

    def test_without_player_only_counts_his_teams(self):
        # Beta's games vs Gamma (g1, g5) involve neither of Sid Kick's teams.
        result = query_stats(["total_rebounds"], team="Gamma", without_player="Sid Kick")
        self.assertEqual(result.games, [])

    def test_without_player_counts_when_he_plays_for_the_opponent(self):
        # Beta vs Alpha in g4: Sid Kick (Alpha, opponent) missed it.
        result = query_stats(["total_rebounds"], team="Beta", without_player="Sid Kick")
        self.assertEqual([g.game_id for g in result.games], [4])

    def test_with_player(self):
        # Sid Kick played g2 and g3 for Alpha.
        result = query_stats(["fg3_pct"], player="Shooter", with_player="Sid Kick")
        self.assertEqual([g.game_id for g in result.games], [2, 3])
        self.assertEqual(result.summary["fg3_pct"].pct, round(100 * 7 / 15, 1))
        # Works when the player is on the opponent's side too.
        beta = query_stats(["total_rebounds"], team="Beta", with_player="Sid Kick")
        self.assertEqual([g.game_id for g in beta.games], [2])

    def test_with_and_without_split_a_players_games(self):
        everything = query_stats(["assists"], player="Shooter")
        with_kick = query_stats(["assists"], player="Shooter", with_player="Sid Kick")
        without_kick = query_stats(["assists"], player="Shooter", without_player="Sid Kick")
        self.assertEqual(
            sorted(g.game_id for g in with_kick.games + without_kick.games),
            [g.game_id for g in everything.games],
        )

    def test_same_player_with_and_without_is_an_error(self):
        # Two spellings of the same player.
        with self.assertRaises(QueryError) as ctx:
            query_stats(["assists"], player="Shooter", with_player="Sid Kick", without_player="S. Kick")
        self.assertIn("both", str(ctx.exception))

    # Rest tests name games by date: database ids follow insert order, not game codes.

    def test_rest_hours_on_each_game(self):
        result = query_stats(["total_rebounds"], team="Alpha")
        rest = {_day(g): g.rest_hours for g in result.games}
        # 8 Oct (g2) is Alpha's first loaded game, so it has no rest value.
        self.assertEqual(
            {day: rest[day] for day in ("2024-10-08", "2024-10-15", "2024-10-22", "2024-10-24")},
            {"2024-10-08": None, "2024-10-15": 168, "2024-10-22": 168, "2024-10-24": 48},
        )
        g7 = next(g for g in result.games if _day(g) == "2024-10-24")
        self.assertEqual(g7.opponent_rest_hours, 216)  # Gamma: 15 Oct -> 24 Oct

    def test_max_and_min_rest_hours(self):
        short = query_stats(["total_rebounds"], team="Alpha", max_rest_hours=48)
        self.assertEqual([_day(g) for g in short.games], ["2024-10-24"])
        rested = query_stats(["total_rebounds"], team="Alpha", min_rest_hours=100)
        # The first game (8 Oct) has no rest value, so it is left out.
        self.assertEqual([_day(g) for g in rested.games], ["2024-10-15", "2024-10-22", "2025-10-07"])
        both = query_stats(["total_rebounds"], team="Alpha", min_rest_hours=100, max_rest_hours=200)
        self.assertEqual([_day(g) for g in both.games], ["2024-10-15", "2024-10-22"])

    def test_win_pct(self):
        # Alpha: g2 L, g3 W, g4 W, g7 L, g6 W -> 3-2.
        result = query_stats(["win_pct"], team="Alpha")
        s = result.summary["win_pct"]
        self.assertEqual((s.wins, s.losses, s.pct), (3, 2, 60.0))
        self.assertEqual([g.values["win_pct"] for g in result.games], ["L", "W", "W", "L", "W"])
        self.assertIn("win_pct: 3-2 = 60.0%", result.describe())

    def test_win_pct_for_a_player_is_his_teams_record(self):
        # Shooter played g2 (L), g3 (W), g4 (W).
        s = query_stats(["win_pct"], player="Shooter").summary["win_pct"]
        self.assertEqual((s.wins, s.losses), (2, 1))

    def test_win_pct_on_short_rest(self):
        s = query_stats(["win_pct"], team="Alpha", max_rest_hours=48).summary["win_pct"]
        self.assertEqual((s.wins, s.losses, s.pct), (0, 1, 0.0))

    def test_team_stats_and_scores_from_teams_point_of_view(self):
        result = query_stats(["total_rebounds"], team="Alpha", venue="away")
        (game,) = result.games
        self.assertEqual((game.opponent, game.team_score, game.opponent_score), ("Gamma", 90, 60))
        self.assertEqual(game.values["total_rebounds"], 23)  # away side in g3

    def test_no_matching_games(self):
        result = query_stats(["fg3_pct"], player="Shooter", opponent="Gamma", venue="home")
        self.assertEqual(result.games, [])
        self.assertIsNone(result.summary["fg3_pct"].pct)
        self.assertIn("no games match", result.describe())

    def test_errors(self):
        with self.assertRaises(QueryError) as ctx:
            query_stats(["assists"], player="Kick")
        self.assertEqual(len(ctx.exception.candidates), 2)
        for kwargs in (
            {"stats": ["assists"], "player": "Nobody Atall"},
            {"stats": ["dunks"], "player": "Shooter"},
            {"stats": ["assists"]},
            {"stats": ["assists"], "player": "Shooter", "team": "Alpha"},
            {"stats": ["assists"], "player": "Shooter", "venue": "road"},
            {"stats": ["assists"], "player": "Shooter", "last_n": 0},
            {"stats": ["assists"], "player": "Shooter", "max_rest_hours": 0},
            {"stats": ["assists"], "player": "Shooter", "min_rest_hours": 72, "max_rest_hours": 48},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(QueryError):
                query_stats(**kwargs)


if __name__ == "__main__":
    unittest.main()
