from database.db import get_connection


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

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
        SELECT
            m.date,
            m.team1,
            m.team2,
            s.team,
            s.stat_name,
            s.stat_value
        FROM team_match_stats s
        JOIN matches m ON m.id = s.match_id
    """)
    
    
    conn.commit()
    conn.close()