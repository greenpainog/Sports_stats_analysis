import pandas as pd

from database.db import get_connection


def load_team_stats():
    conn = get_connection()

    query = """
        SELECT
            date,
            team1,
            team2,
            team,
            stat_name,
            stat_value
        FROM team_stats_view
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    df["ParsedDate"] = pd.to_datetime(
        df["date"],
        format="%d.%m.%Y %H:%M",
        errors="coerce",
    )

    df = df.sort_values("ParsedDate", ascending=False)

    return df


def get_available_db_stats():
    conn = get_connection()

    query = """
        SELECT DISTINCT stat_name
        FROM team_match_stats
        ORDER BY stat_name
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    return df["stat_name"].tolist()


def get_available_db_teams():
    conn = get_connection()

    query = """
        SELECT DISTINCT team
        FROM team_match_stats
        ORDER BY team
    """

    df = pd.read_sql_query(query, conn)
    conn.close()

    return df["team"].tolist()