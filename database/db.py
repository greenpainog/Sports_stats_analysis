import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Absolute, so it works whatever folder a command or server is started from.
# Set SPORTS_DB_PATH to use another database (e.g. in Docker or for a demo).
DATABASE_FILE = os.environ.get(
    "SPORTS_DB_PATH", str(PROJECT_ROOT / "data" / "sports_analytics.db")
)


def get_connection():
    connection = sqlite3.connect(DATABASE_FILE)
    # SQLite leaves foreign-key checks off by default. Enable them for each
    # connection so the new tables cannot point at missing teams or games.
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
