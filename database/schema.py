"""Database schema for current and legacy basketball data.

The original ``matches`` and ``team_match_stats`` tables are kept in place so
existing CSV/database analysis continues to work. New collection code should
use the competition-aware tables below.
"""

from database.db import get_connection

# Columns added to `games` after the table was first created. CREATE TABLE IF
# NOT EXISTS never changes an existing table, so older databases get these
# through ALTER TABLE in create_tables().
#   phase: 'regular_season', 'play_in', 'playoffs' or 'final_four'
#   is_neutral_venue: 1 when neither team is at home (e.g. Final Four)
GAMES_ADDED_COLUMNS = {
    "round_number": "INTEGER",
    "phase": "TEXT",
    "is_neutral_venue": "INTEGER",
}


def _add_missing_columns(cursor, table, columns):
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
    for name, column_type in columns.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


def create_tables():
    """Create the database tables without deleting existing data."""
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Legacy schema. Kept until the old scraper and analysis commands have
        # been migrated to the competition-aware schema.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                team1 TEXT NOT NULL,
                team2 TEXT NOT NULL,
                UNIQUE(date, team1, team2)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_match_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                team TEXT NOT NULL,
                stat_name TEXT NOT NULL,
                stat_value REAL,
                FOREIGN KEY (match_id) REFERENCES matches(id),
                UNIQUE(match_id, team, stat_name)
            )
        """)

        cursor.execute("""
            CREATE VIEW IF NOT EXISTS team_stats_view AS
            SELECT m.date, m.team1, m.team2, s.team, s.stat_name, s.stat_value
            FROM team_match_stats s
            JOIN matches m ON m.id = s.match_id
        """)

        # A competition is a tournament such as EuroLeague or the Greek A1.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS competitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                country TEXT,
                gender TEXT,
                source TEXT NOT NULL DEFAULT 'flashscore',
                source_competition_id TEXT,
                UNIQUE(source, source_competition_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competition_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                start_date TEXT,
                end_date TEXT,
                FOREIGN KEY (competition_id) REFERENCES competitions(id),
                UNIQUE(competition_id, name)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                country TEXT,
                source TEXT NOT NULL DEFAULT 'flashscore',
                source_team_id TEXT,
                UNIQUE(source, source_team_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                country TEXT,
                source TEXT NOT NULL DEFAULT 'flashscore',
                source_player_id TEXT,
                UNIQUE(source, source_player_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'flashscore',
                source_game_id TEXT,
                source_url TEXT,
                start_time TEXT NOT NULL,
                status TEXT,
                home_team_id INTEGER NOT NULL,
                away_team_id INTEGER NOT NULL,
                home_score INTEGER,
                away_score INTEGER,
                round_number INTEGER,
                phase TEXT,
                is_neutral_venue INTEGER,
                FOREIGN KEY (season_id) REFERENCES seasons(id),
                FOREIGN KEY (home_team_id) REFERENCES teams(id),
                FOREIGN KEY (away_team_id) REFERENCES teams(id),
                UNIQUE(source, source_game_id),
                CHECK(home_team_id <> away_team_id)
            )
        """)

        # Keep participation separate from stats: a player may have appeared
        # even when a particular stat is missing or equal to zero.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_appearances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                started INTEGER,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                FOREIGN KEY (player_id) REFERENCES players(id),
                FOREIGN KEY (team_id) REFERENCES teams(id),
                UNIQUE(game_id, player_id)
            )
        """)

        # One table stores any named stat for either a team or player. For a
        # player stat, team_id records the team they represented in that game.
        # raw_value preserves the source text (for example "10/18"), while
        # stat_value is populated when the value is numeric and analyzable.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('team', 'player')),
                team_id INTEGER,
                player_id INTEGER,
                stat_name TEXT NOT NULL,
                stat_value REAL,
                raw_value TEXT,
                unit TEXT,
                period TEXT NOT NULL DEFAULT 'match',
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                FOREIGN KEY (team_id) REFERENCES teams(id),
                FOREIGN KEY (player_id) REFERENCES players(id),
                CHECK(
                    (entity_type = 'team' AND team_id IS NOT NULL AND player_id IS NULL)
                    OR
                    (entity_type = 'player' AND team_id IS NOT NULL AND player_id IS NOT NULL)
                )
            )
        """)

        # SQLite treats NULL values as distinct in regular unique constraints,
        # so use separate indexes to prevent duplicate stats for each entity.
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_game_team_stat
            ON game_stats(game_id, team_id, stat_name, period)
            WHERE entity_type = 'team'
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_game_player_stat
            ON game_stats(game_id, player_id, stat_name, period)
            WHERE entity_type = 'player'
        """)

        _add_missing_columns(cursor, "games", GAMES_ADDED_COLUMNS)

        # Coaches and referees of each game, from the Euroleague/EuroCup header.
        # A coach has the team they coached; a referee has team_id NULL.
        # `normalized` is the name as normalize_name() writes it, for lookups.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS game_staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('coach', 'referee')),
                team_id INTEGER,
                name TEXT NOT NULL,
                normalized TEXT NOT NULL,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                FOREIGN KEY (team_id) REFERENCES teams(id),
                CHECK((role = 'coach') = (team_id IS NOT NULL)),
                UNIQUE(game_id, role, normalized)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_game_staff_lookup ON game_staff(role, normalized)")

        # Every known name for a team or player, for resolving user questions.
        # `normalized` is lowercase without accents or punctuation (see
        # database/aliases.py). One alias may belong to several entities,
        # e.g. the surname "Hernangomez"; the resolver reports that as ambiguous.
        for kind in ("team", "player"):
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {kind}_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    {kind}_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    FOREIGN KEY ({kind}_id) REFERENCES {kind}s(id) ON DELETE CASCADE,
                    UNIQUE({kind}_id, normalized)
                )
            """)
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{kind}_aliases_normalized "
                f"ON {kind}_aliases(normalized)"
            )

        # One person or club seen by several sources (Kendrick Nunn in the
        # Euroleague and the Greek league) is one row. A link sends another
        # source's ID to that existing row; method is 'manual' (hand-written
        # list) or 'name' (exact name match, see ingestion/esake.py).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS source_links (
                kind TEXT NOT NULL CHECK (kind IN ('team', 'player')),
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                PRIMARY KEY (kind, source, source_id)
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS ix_games_start_time ON games(start_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_games_season_time ON games(season_id, start_time)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_game_stats_name ON game_stats(stat_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_game_appearances_player ON game_appearances(player_id, game_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_players_name ON players(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_teams_name ON teams(name)")

        conn.commit()
    finally:
        conn.close()
