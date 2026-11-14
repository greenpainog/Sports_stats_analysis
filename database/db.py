import sqlite3


DATABASE_FILE = "data/sports_analytics.db"


def get_connection():
    connection = sqlite3.connect(DATABASE_FILE)
    # SQLite leaves foreign-key checks off by default. Enable them for each
    # connection so the new tables cannot point at missing teams or games.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
