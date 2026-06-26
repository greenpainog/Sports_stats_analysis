from database.db import get_connection


def save_match_data(match_data):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR IGNORE INTO matches (date, team1, team2)
        VALUES (?, ?, ?)
        """,
        (
            match_data["date"],
            match_data["team1"],
            match_data["team2"],
        ),
    )

    cursor.execute(
        """
        SELECT id FROM matches
        WHERE date = ? AND team1 = ? AND team2 = ?
        """,
        (
            match_data["date"],
            match_data["team1"],
            match_data["team2"],
        ),
    )

    match_id = cursor.fetchone()[0]

    for stat_name, values in match_data["stats"].items():
        cursor.execute(
            """
            INSERT OR REPLACE INTO team_match_stats (
                match_id,
                team,
                stat_name,
                stat_value
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                match_id,
                match_data["team1"],
                stat_name,
                values["team1"],
            ),
        )

        cursor.execute(
            """
            INSERT OR REPLACE INTO team_match_stats (
                match_id,
                team,
                stat_name,
                stat_value
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                match_id,
                match_data["team2"],
                stat_name,
                values["team2"],
            ),
        )

    conn.commit()
    conn.close()