"""Stats from play-by-play: on-floor lineups, clutch time and a player's quarters.

query_stats (analysis/stat_query.py) chooses the games with its usual filters
and calls these functions for the parts the box score can't answer:

- stint_values: minutes, plus-minus and team points while a lineup
  condition held (who was on the floor), from the stints table.
- event_values: box-score stats counted from the events (ingestion/
  play_by_play.EVENT_STATS), limited to clutch time and/or to a lineup
  condition, plus the team's points scored and allowed in that time.
- player_quarter_values: a player's points in each quarter.

All SQL is built here from ids and fixed column names; names were resolved
by the caller.
"""
from ingestion.play_by_play import CLUTCH_MARGIN, CLUTCH_SECONDS_LEFT, EVENT_STATS

# Stats that come from the stints (time on the floor).
STINT_STATS = ("minutes", "plus_minus", "points_for", "points_against", "possessions",
               "off_rating", "def_rating", "net_rating")
# Points per 100 possessions on the floor. Possessions are the usual estimate
# (FGA - OREB + TOV + 0.44 * FTA) counted from the events, averaged over both
# teams, as in analysis/advanced.py. Summaries divide summed points by summed
# possessions; they never average per-game ratings.
RATING_STATS = ("off_rating", "def_rating", "net_rating")


def ratings(points_for, points_against, possessions):
    """{off_rating, def_rating, net_rating} per 100 possessions; None without possessions."""
    if not possessions:
        return {stat: None for stat in RATING_STATS}
    off, defense = 100 * points_for / possessions, 100 * points_against / possessions
    return {"off_rating": off, "def_rating": defense, "net_rating": off - defense}
# Percentages that can be built from event counts (not paint_fg_pct: that
# needs shot coordinates).
EVENT_PERCENTAGES = ("fg2_pct", "fg3_pct", "ft_pct", "fg_pct")
CLUTCH_DESCRIPTION = (f"the last {CLUTCH_SECONDS_LEFT // 60} minutes of the 4th quarter or overtime, "
                      f"score within {CLUTCH_MARGIN}")


def usable_games(conn, game_ids, need_lineups, notes):
    """The games that have play-by-play (and, if need_lineups, sound lineups).

    Explains in `notes` how many were left out and why.
    """
    if not game_ids:
        return []
    placeholders = ", ".join("?" for _ in game_ids)
    checked = dict(conn.execute(
        f"SELECT game_id, problems FROM pbp_checks WHERE game_id IN ({placeholders})", game_ids).fetchall())
    missing = [g for g in game_ids if g not in checked]
    broken = [g for g in game_ids if checked.get(g)] if need_lineups else []
    if missing:
        notes.append(f"{len(missing)} matching game(s) have no play-by-play yet and are left out.")
    if broken:
        notes.append(f"{len(broken)} game(s) left out: the source's substitutions there don't add up to "
                     "five players on the floor.")
    return [g for g in game_ids if g in checked and g not in broken]


def check_teammates(conn, subject_type, subject_id, game_ids, named):
    """Each (player_id, name) must be the subject's teammate in one of the games.

    Otherwise "with him on the bench" would be true of every minute. Returns
    the first name that fails, or None.
    """
    placeholders = ", ".join("?" for _ in game_ids)
    for player_id, name in named:
        if subject_type == "player":
            sql = f"""SELECT COUNT(*) FROM game_appearances o JOIN game_appearances me
                        ON me.game_id = o.game_id AND me.team_id = o.team_id
                      WHERE me.player_id = ? AND o.player_id = ? AND o.game_id IN ({placeholders})"""
        else:
            sql = f"""SELECT COUNT(*) FROM game_appearances o
                      WHERE o.team_id = ? AND o.player_id = ? AND o.game_id IN ({placeholders})"""
        if not conn.execute(sql, [subject_id, player_id, *game_ids]).fetchone()[0]:
            return name
    return None


def _lineup_conditions(stint_sql, subject_type, subject_id, with_ids, without_ids):
    """SQL conditions (and params) on the stint `stint_sql` for the lineup filters."""
    conditions, params = [], []
    if subject_type == "player":
        conditions.append(f"EXISTS (SELECT 1 FROM stint_players me WHERE me.stint_id = {stint_sql} "
                          "AND me.player_id = ?)")
        params.append(subject_id)
    for ids, sign in ((with_ids, "EXISTS"), (without_ids, "NOT EXISTS")):
        for player_id in ids:
            conditions.append(f"{sign} (SELECT 1 FROM stint_players o WHERE o.stint_id = {stint_sql} "
                              "AND o.player_id = ?)")
            params.append(player_id)
    return conditions, params


def stint_values(conn, subject_type, subject_id, game_ids, with_ids, without_ids):
    """{game_id: {minutes, plus_minus, points_for, points_against, possessions, ratings}} of the on-floor time.

    Only games where the condition held for more than 0 seconds are returned.
    """
    if not game_ids:
        return {}
    placeholders = ", ".join("?" for _ in game_ids)
    conditions, params = _lineup_conditions("s.id", subject_type, subject_id, with_ids, without_ids)
    if subject_type == "team":
        conditions.append("s.team_id = ?")
        params.append(subject_id)
    rows = conn.execute(
        f"""SELECT s.game_id, SUM(s.end_elapsed - s.start_elapsed), SUM(s.points_for), SUM(s.points_against),
                   (SUM(s.own_possessions) + SUM(s.opponent_possessions)) / 2.0
            FROM stints s WHERE s.game_id IN ({placeholders}) AND {' AND '.join(conditions)}
            GROUP BY s.game_id HAVING SUM(s.end_elapsed - s.start_elapsed) > 0""",
        [*game_ids, *params]).fetchall()
    return {game_id: {"minutes": seconds / 60, "plus_minus": scored - allowed,
                      "points_for": scored, "points_against": allowed, "possessions": possessions or 0,
                      **{k: None if v is None else round(v, 1)
                         for k, v in ratings(scored, allowed, possessions).items()}}
            for game_id, seconds, scored, allowed, possessions in rows}


def _count_sql(weights):
    cases = " ".join(f"WHEN '{play_type}' THEN {weight}" for play_type, weight in weights.items())
    return f"SUM(CASE WHEN mine THEN CASE play_type {cases} ELSE 0 END ELSE 0 END)"


def event_values(conn, subject_type, subject_id, game_ids, clutch, with_ids, without_ids):
    """{game_id: {every EVENT_STATS stat, points_for, points_against, plus_minus}}.

    Counts only events in clutch time (if `clutch`) while the lineup
    condition held (the subject's team's stint at that moment; a player
    subject must be on the floor himself, so his plus-minus is his). The
    subject's own stats are its events (a player's own, or all of a
    team's); points_for/against are the team's points in that time. A game
    is returned only if it has at least one such event.
    """
    if not game_ids:
        return {}
    placeholders = ", ".join("?" for _ in game_ids)
    if subject_type == "player":
        # The player's team in each game.
        team_join = "JOIN game_appearances t ON t.game_id = e.game_id AND t.player_id = ?"
        mine = "e.player_id = ?"
    else:
        team_join = "JOIN (SELECT ? AS team_id) t"
        mine = "e.team_id = ?"
    home = "t.team_id = g.home_team_id"
    own_stint = f"(CASE WHEN {home} THEN e.home_stint_id ELSE e.away_stint_id END)"
    conditions, lineup_params = _lineup_conditions(own_stint, subject_type, subject_id, with_ids, without_ids)
    if clutch:
        conditions.append("e.clutch = 1")
    where = " AND ".join([f"e.game_id IN ({placeholders})", *conditions])
    counts = ", ".join(f"{_count_sql(weights)} AS {stat}" for stat, weights in EVENT_STATS.items())
    rows = conn.execute(
        f"""WITH scoped AS (
                SELECT e.game_id, e.play_type, ({mine}) AS mine,
                       CASE WHEN {home} THEN e.home_points ELSE e.away_points END AS scored,
                       CASE WHEN {home} THEN e.away_points ELSE e.home_points END AS allowed
                FROM pbp_events e JOIN games g ON g.id = e.game_id {team_join}
                WHERE {where})
            SELECT game_id, SUM(scored), SUM(allowed), {counts} FROM scoped GROUP BY game_id""",
        [subject_id, subject_id, *game_ids, *lineup_params]).fetchall()
    values = {}
    for game_id, scored, allowed, *stat_values in rows:
        values[game_id] = dict(zip(EVENT_STATS, stat_values))
        values[game_id].update(points_for=scored, points_against=allowed, plus_minus=scored - allowed)
    return values


def player_quarter_values(conn, player_id, game_ids):
    """{game_id: {points_q1..points_q4}} from the player's scoring events.

    Every game with play-by-play gets all four quarters (0 when he didn't
    score in one), so averages count the games he played, not only the
    quarters he scored in. Overtime points are left out, as for teams.
    """
    if not game_ids:
        return {}
    placeholders = ", ".join("?" for _ in game_ids)
    with_pbp = [g for (g,) in conn.execute(
        f"SELECT game_id FROM pbp_checks WHERE game_id IN ({placeholders})", game_ids)]
    values = {g: {f"points_q{q}": 0 for q in range(1, 5)} for g in with_pbp}
    weights = EVENT_STATS["points"]
    cases = " ".join(f"WHEN '{play_type}' THEN {weight}" for play_type, weight in weights.items())
    for game_id, period, points in conn.execute(
            f"""SELECT game_id, period, SUM(CASE play_type {cases} ELSE 0 END) FROM pbp_events
                WHERE player_id = ? AND period <= 4 AND game_id IN ({placeholders})
                GROUP BY game_id, period""", [player_id, *game_ids]):
        if game_id in values:
            values[game_id][f"points_q{period}"] = points
    return values
