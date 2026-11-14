from database.db import get_connection


def _find_or_create_named_row(cursor, table, name, source, source_id, extra=None):
    """Return a row ID, using the Flashscore ID when it is available."""
    extra = extra or {}

    # Table and column names here come from this module, never from user input.
    if source_id is not None:
        cursor.execute(
            f"SELECT id FROM {table} WHERE source = ? AND source_{table[:-1]}_id = ?",
            (source, str(source_id)),
        )
    else:
        cursor.execute(
            f"SELECT id FROM {table} WHERE source = ? AND name = ? ORDER BY id LIMIT 1",
            (source, name),
        )

    row = cursor.fetchone()
    if row:
        row_id = row[0]
        assignments = ["name = ?"]
        values = [name]
        for column, value in extra.items():
            assignments.append(f"{column} = ?")
            values.append(value)
        values.append(row_id)
        cursor.execute(
            f"UPDATE {table} SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        return row_id

    columns = ["name", "source"]
    values = [name, source]
    if source_id is not None:
        columns.append(f"source_{table[:-1]}_id")
        values.append(str(source_id))
    for column, value in extra.items():
        columns.append(column)
        values.append(value)

    placeholders = ", ".join("?" for _ in values)
    cursor.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return cursor.lastrowid


def _get_or_create_competition(cursor, competition, source):
    name = competition["name"]
    source_id = competition.get("source_competition_id")

    if source_id is not None:
        cursor.execute(
            "SELECT id FROM competitions WHERE source = ? AND source_competition_id = ?",
            (source, str(source_id)),
        )
    else:
        cursor.execute(
            "SELECT id FROM competitions WHERE source = ? AND name = ? ORDER BY id LIMIT 1",
            (source, name),
        )

    row = cursor.fetchone()
    if row:
        competition_id = row[0]
        cursor.execute(
            """UPDATE competitions
               SET name = ?, country = ?, gender = ?
               WHERE id = ?""",
            (name, competition.get("country"), competition.get("gender"), competition_id),
        )
        return competition_id

    cursor.execute(
        """INSERT INTO competitions
           (name, country, gender, source, source_competition_id)
           VALUES (?, ?, ?, ?, ?)""",
        (name, competition.get("country"), competition.get("gender"), source,
         str(source_id) if source_id is not None else None),
    )
    return cursor.lastrowid


def _get_or_create_season(cursor, competition_id, season):
    name = season["name"] if isinstance(season, dict) else season
    details = season if isinstance(season, dict) else {}
    cursor.execute(
        "SELECT id FROM seasons WHERE competition_id = ? AND name = ?",
        (competition_id, name),
    )
    row = cursor.fetchone()
    if row:
        season_id = row[0]
        cursor.execute(
            "UPDATE seasons SET start_date = ?, end_date = ? WHERE id = ?",
            (details.get("start_date"), details.get("end_date"), season_id),
        )
        return season_id

    cursor.execute(
        """INSERT INTO seasons (competition_id, name, start_date, end_date)
           VALUES (?, ?, ?, ?)""",
        (competition_id, name, details.get("start_date"), details.get("end_date")),
    )
    return cursor.lastrowid


def _save_appearance(cursor, game_id, player_id, team_id, started=None):
    """Record that a player appeared for a team in a particular game."""
    cursor.execute(
        """INSERT INTO game_appearances (game_id, player_id, team_id, started)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(game_id, player_id) DO UPDATE SET
               team_id = excluded.team_id,
               started = COALESCE(excluded.started, game_appearances.started)""",
        (game_id, player_id, team_id, started),
    )


def save_game_bundle(game_data):
    """Save one game and its team/player stats as a single database operation.

    ``game_data`` contains competition, season, home_team, away_team, and a
    ``stats`` list. Each stat names either a team side (``home``/``away``) or
    a player and the side that player represented in this game. An optional
    ``appearances`` list records all players who actually played, including
    players whose stat lines are incomplete.

    Example stat item::

        {"entity_type": "player", "team": "away",
         "player": {"name": "Jerian Grant", "source_player_id": "123"},
         "stat_name": "3 Point Percentage", "stat_value": 42.9,
         "raw_value": "6/14", "unit": "%", "period": "match"}
    """
    source = game_data.get("source", "flashscore")
    home = game_data["home_team"]
    away = game_data["away_team"]

    conn = get_connection()
    try:
        cursor = conn.cursor()

        competition_id = _get_or_create_competition(
            cursor, game_data["competition"], source
        )
        season_id = _get_or_create_season(
            cursor, competition_id, game_data["season"]
        )

        home_team_id = _find_or_create_named_row(
            cursor, "teams", home["name"], source,
            home.get("source_team_id"), {"country": home.get("country")},
        )
        away_team_id = _find_or_create_named_row(
            cursor, "teams", away["name"], source,
            away.get("source_team_id"), {"country": away.get("country")},
        )

        source_game_id = game_data.get("source_game_id")
        if source_game_id is not None:
            cursor.execute(
                "SELECT id FROM games WHERE source = ? AND source_game_id = ?",
                (source, str(source_game_id)),
            )
        else:
            # Fallback for data sources that do not expose a stable match ID.
            cursor.execute(
                """SELECT id FROM games
                   WHERE season_id = ? AND start_time = ?
                     AND home_team_id = ? AND away_team_id = ?
                   ORDER BY id LIMIT 1""",
                (season_id, game_data["start_time"], home_team_id, away_team_id),
            )

        row = cursor.fetchone()
        neutral = game_data.get("is_neutral_venue")
        game_columns = {
            "season_id": season_id,
            "source_url": game_data.get("source_url"),
            "start_time": game_data["start_time"],
            "status": game_data.get("status"),
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_score": game_data.get("home_score"),
            "away_score": game_data.get("away_score"),
            "round_number": game_data.get("round_number"),
            "phase": game_data.get("phase"),
            "is_neutral_venue": None if neutral is None else int(bool(neutral)),
        }
        if row:
            game_id = row[0]
            assignments = ", ".join(f"{column} = ?" for column in game_columns)
            cursor.execute(
                f"UPDATE games SET {assignments} WHERE id = ?",
                (*game_columns.values(), game_id),
            )
        else:
            insert_columns = {
                "source": source,
                "source_game_id": str(source_game_id) if source_game_id is not None else None,
                **game_columns,
            }
            placeholders = ", ".join("?" for _ in insert_columns)
            cursor.execute(
                f"INSERT INTO games ({', '.join(insert_columns)}) VALUES ({placeholders})",
                tuple(insert_columns.values()),
            )
            game_id = cursor.lastrowid

        for appearance in game_data.get("appearances", []):
            side = appearance["team"]
            if side not in ("home", "away"):
                raise ValueError("Each appearance must identify 'home' or 'away'.")
            team_id = home_team_id if side == "home" else away_team_id
            player = appearance["player"]
            player_id = _find_or_create_named_row(
                cursor, "players", player["name"], source,
                player.get("source_player_id"), {"country": player.get("country")},
            )
            _save_appearance(
                cursor, game_id, player_id, team_id, appearance.get("started")
            )

        # Repeated scrapes update an existing stat instead of duplicating it.
        for stat in game_data.get("stats", []):
            entity_type = stat["entity_type"]
            side = stat.get("team")
            if side not in ("home", "away"):
                raise ValueError("Each stat must identify its team as 'home' or 'away'.")

            team_id = home_team_id if side == "home" else away_team_id
            player_id = None
            if entity_type == "player":
                player = stat["player"]
                player_id = _find_or_create_named_row(
                    cursor, "players", player["name"], source,
                    player.get("source_player_id"), {"country": player.get("country")},
                )
                # Player stats imply participation. The explicit appearances
                # list is still useful when a played player's stats are absent.
                _save_appearance(cursor, game_id, player_id, team_id)
            elif entity_type != "team":
                raise ValueError("entity_type must be 'team' or 'player'.")

            entity_column = "player_id" if player_id is not None else "team_id"
            entity_id = player_id if player_id is not None else team_id
            period = stat.get("period", "match")
            # Filter on entity_type too: player rows also carry a team_id, so
            # a team-stat lookup by team_id alone would match a player's row.
            cursor.execute(
                f"""SELECT id FROM game_stats WHERE game_id = ? AND entity_type = ?
                    AND {entity_column} = ? AND stat_name = ? AND period = ?""",
                (game_id, entity_type, entity_id, stat["stat_name"], period),
            )
            existing_stat = cursor.fetchone()
            stat_values = (
                stat.get("stat_value"), stat.get("raw_value"), stat.get("unit"),
            )
            if existing_stat:
                cursor.execute(
                    """UPDATE game_stats
                       SET entity_type = ?, team_id = ?, stat_value = ?,
                           raw_value = ?, unit = ?
                       WHERE id = ?""",
                    (entity_type, team_id, *stat_values, existing_stat[0]),
                )
            else:
                cursor.execute(
                    """INSERT INTO game_stats
                       (game_id, entity_type, team_id, player_id, stat_name,
                        stat_value, raw_value, unit, period)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (game_id, entity_type, team_id, player_id, stat["stat_name"],
                     *stat_values, period),
                )

        conn.commit()
        return game_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
