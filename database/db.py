import sqlite3


DATABASE_FILE = "data/sports_analytics.db"


def get_connection():
    return sqlite3.connect(DATABASE_FILE)