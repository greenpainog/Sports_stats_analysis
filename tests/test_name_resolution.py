"""Resolving messy team and player names (phase 2).

The resolution tests load round 1 of 2024-25 (games 1-9, all 18 teams) from
the cached responses in data/raw/euroleague/ into a throwaway database. They
skip if the cache is missing; run `python -m ingestion.euroleague --season 2024`.
"""
import tempfile
import unittest
from pathlib import Path

import database.db as db
from database.aliases import (
    normalize_name,
    player_name_variants,
    resolve_player,
    resolve_team,
)
from database.schema import create_tables
from ingestion.euroleague import ingest_game, player_display_name

RAW = Path("data/raw/euroleague")
ROUND_ONE = range(1, 10)
CACHED = (RAW / "games_E2024.json").exists() and all(
    (RAW / f"{kind}_E2024_{code}.json").exists()
    for kind in ("header", "boxscore") for code in ROUND_ONE
)


class NormalizeTests(unittest.TestCase):
    def test_normalize_name(self):
        self.assertEqual(normalize_name("Žalgiris  Kaunas"), "zalgiris kaunas")
        self.assertEqual(normalize_name("R. Madrid"), "r madrid")
        self.assertEqual(normalize_name("Barça"), "barca")
        self.assertEqual(normalize_name("Đorđević"), "dordevic")
        self.assertEqual(normalize_name("NUNN, KENDRICK"), "nunn kendrick")
        self.assertEqual(normalize_name("  "), "")
        self.assertEqual(normalize_name("Pana's"), "pana")
        self.assertEqual(normalize_name("Nunn’s"), "nunn")
        self.assertEqual(normalize_name("O'Shea"), "o shea")  # an apostrophe inside a name stays a split

    def test_player_name_variants(self):
        self.assertEqual(
            player_name_variants("Kendrick Nunn"),
            ["Kendrick Nunn", "K. Nunn", "Nunn", "Kendrick Nunn"],
        )
        self.assertIn("Walker", player_name_variants("Lonnie Walker IV"))
        self.assertIn("De Colo", player_name_variants("Nando De Colo"))

    def test_display_name_suffixes(self):
        self.assertEqual(player_display_name("WALKER IV, LONNIE"), "Lonnie Walker IV")
        self.assertEqual(player_display_name("SMITH JR, DENNIS"), "Dennis Smith Jr.")


@unittest.skipUnless(CACHED, "cached round 1 of E2024 not found")
class ResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = db.DATABASE_FILE
        cls.tmp = tempfile.TemporaryDirectory()
        db.DATABASE_FILE = str(Path(cls.tmp.name) / "names.db")
        create_tables()
        for gamecode in ROUND_ONE:
            ingest_game("E", 2024, gamecode, download=False)

    @classmethod
    def tearDownClass(cls):
        db.DATABASE_FILE = cls.original_db
        cls.tmp.cleanup()

    def assertResolves(self, resolver, query, expected_name):
        result = resolver(query)
        self.assertEqual(
            (result.status, result.name), ("match", expected_name),
            f"{query!r} -> {result}",
        )

    def test_messy_team_names(self):
        cases = {
            "Real": "Real Madrid",
            "real madrid": "Real Madrid",
            "R. Madrid": "Real Madrid",
            "PAO": "Panathinaikos AKTOR Athens",
            "Panathinaikos": "Panathinaikos AKTOR Athens",
            "Olympiakos": "Olympiacos Piraeus",
            "Fener": "Fenerbahce Beko Istanbul",
            "Red Star": "Crvena Zvezda Meridianbet Belgrade",
            "Žalgiris": "Zalgiris Kaunas",
            "Barça": "FC Barcelona",
            "Olimpia Milano": "EA7 Emporio Armani Milan",
            "Bayern München": "FC Bayern Munich",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertResolves(resolve_team, query, expected)

    def test_messy_player_names(self):
        cases = {
            "Nunn": "Kendrick Nunn",
            "K. Nunn": "Kendrick Nunn",
            "NUNN, KENDRICK": "Kendrick Nunn",
            "jerian grant": "Jerian Grant",
            "Nando de Colo": "Nando De Colo",
            "Sloukas": "Kostas Sloukas",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertResolves(resolve_player, query, expected)

    def test_typos_are_corrected_and_flagged(self):
        for query, expected in {
            "Jerrian Grant": "Jerian Grant",
            "Costas Sloukas": "Kostas Sloukas",
            "Mathias Lessor": "Mathias Lessort",
        }.items():
            with self.subTest(query=query):
                result = resolve_player(query)
                self.assertEqual((result.status, result.name), ("match", expected))
                self.assertEqual(result.method, "fuzzy")

    def test_shared_surname_is_ambiguous(self):
        result = resolve_player("Hernangomez")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(
            [name for _, name in result.candidates],
            ["Juancho Hernangomez", "Willy Hernangomez"],
        )
        self.assertIsNone(result.id)

    def test_first_name_from_source_spelling_is_not_a_surname(self):
        # 'NUNN, KENDRICK' must not make 'Kendrick' an alias. It is only
        # found by the first-name fallback, never as an exact alias.
        result = resolve_player("Kendrick")
        self.assertNotEqual(result.method, "exact")
        if result.status == "match":
            self.assertEqual((result.method, result.name), ("first_name", "Kendrick Nunn"))

    def test_unknown_names(self):
        self.assertEqual(resolve_team("Lakers").status, "not_found")
        self.assertEqual(resolve_player("LeBron James").status, "not_found")


if __name__ == "__main__":
    unittest.main()
