"""Answer stat questions from the competition-aware tables.

    query_stats(["fg3_pct"], player="Nunn", last_n=5)
    query_stats(["turnovers", "assists"], player="Jerian Grant", opponent_over_500=True)
    query_stats(["fg3_pct"], player="Jerian Grant", opponent="Real Madrid", venue="away")
    query_stats(["total_rebounds"], team="Panathinaikos", opponent="Zalgiris",
                venue="away", without_player="Cedi Osman")
    query_stats(["points", "fg3_pct"], player="Fournier", with_player="Dorsey")
    query_stats(["points"], player="Hifi", against_player="Theis",
                seasons=["2025-26", "2026-27"])
    query_stats(["win_pct", "turnovers", "fg_pct"], team="Olympiacos", max_rest_hours=48)
    query_stats(["minutes", "plus_minus"], player="Campazzo", on_floor_with=["Tavares"])

Names are resolved through the alias tables. The SQL is always built here from
these arguments, never written by a caller (later, never by the LLM). Filters
are applied first and `last_n` is applied last: "last 5 games against Real"
means the 5 most recent games against Real.
"""
from dataclasses import dataclass, field

from database.aliases import resolve_player, resolve_staff, resolve_team
from database.db import get_connection

# Stats stored per game in game_stats. Ingestion must store exactly these
# names (a test checks the Euroleague mapping against this list).
COUNTING_STATS = (
    "points", "minutes",
    "fg2_made", "fg2_attempted", "fg3_made", "fg3_attempted", "ft_made", "ft_attempted",
    "offensive_rebounds", "defensive_rebounds", "total_rebounds",
    "assists", "steals", "turnovers", "blocks", "blocks_against",
    "fouls_committed", "fouls_received", "pir", "plus_minus",
    # From the shot list: field goals inside the paint, and points by situation.
    "paint_points", "paint_fg_made", "paint_fg_attempted",
    "fastbreak_points", "second_chance_points", "points_off_turnovers",
)

# Percentages are computed from summed makes and attempts. Averaging each
# game's percentage would give a 1/1 game the same weight as a 6/14 game.
PERCENTAGE_STATS = {
    "fg2_pct": (("fg2_made",), ("fg2_attempted",)),
    "fg3_pct": (("fg3_made",), ("fg3_attempted",)),
    "ft_pct": (("ft_made",), ("ft_attempted",)),
    "fg_pct": (("fg2_made", "fg3_made"), ("fg2_attempted", "fg3_attempted")),
    "paint_fg_pct": (("paint_fg_made",), ("paint_fg_attempted",)),
}

# Computed from the game result rather than stored. Works for teams and for
# players (the record of the player's team in the games he played).
RESULT_STATS = ("win_pct",)

# A team's points per quarter and half (Euroleague and EuroCup), from the
# header's quarter scores. Stored as "points" rows with period q1-q4 (and ot);
# read back as points_q1 ... and added up into the halves. Teams only: player
# quarters need play-by-play.
QUARTER_STATS = ("points_q1", "points_q2", "points_q3", "points_q4", "points_first_half", "points_second_half")
HALVES = {"points_first_half": ("points_q1", "points_q2"), "points_second_half": ("points_q3", "points_q4")}


# Stats of the time a lineup condition held, from the stints (see
# ingestion/play_by_play.py): minutes on the floor, plus-minus, and the
# team's points scored and allowed in that time. With on_floor_with /
# on_floor_without only these (and win_pct) can be asked; points_for and
# points_against always come from the stints. Euroleague and EuroCup only.
ON_FLOOR_STATS = ("minutes", "plus_minus", "points_for", "points_against")
STINT_ONLY_STATS = ("points_for", "points_against")
MAX_ON_FLOOR_PLAYERS = 4


def period_stat_name(stat_name, period):
    """('points', 'match') -> 'points'; ('points', 'q1') -> 'points_q1'."""
    return stat_name if period == "match" else f"{stat_name}_{period}"


def add_halves(values):
    """Add points_first_half/second_half to one game's {stat: value} when both quarters are there."""
    for half, quarters in HALVES.items():
        if all(q in values for q in quarters):
            values[half] = sum(values[q] or 0 for q in quarters)
    return values


VENUES = ("home", "away", "neutral")

# Competition name in a spec -> competitions.source_competition_id.
COMPETITIONS = {"euroleague": "E", "eurocup": "U", "greek_league": "GBL"}
COMPETITION_SQL = """tg.season_id IN (SELECT s.id FROM seasons s JOIN competitions c ON c.id = s.competition_id
                                      WHERE c.source_competition_id = ?)"""


def competition_where(competition):
    """(SQL condition on team_games tg, params) for one competition."""
    if competition not in COMPETITIONS:
        raise QueryError(f"competition must be one of {', '.join(COMPETITIONS)}.")
    return COMPETITION_SQL, [COMPETITIONS[competition]]


_stored_cache = {}


def stored_stats(conn, competition, entity_type=None):
    """Stat names stored for this competition's games (the Greek league has no
    plus_minus or shot-list stats). Cached until the number of games changes."""
    import database.db as db

    games = conn.execute("SELECT COUNT(*), MAX(id) FROM games").fetchone()
    key = (db.DATABASE_FILE, competition, entity_type, *games)
    if key not in _stored_cache:
        sql = f"""SELECT DISTINCT stat_name, period FROM game_stats WHERE game_id IN (
                     SELECT g.id FROM games g JOIN seasons s ON s.id = g.season_id
                     JOIN competitions c ON c.id = s.competition_id WHERE c.source_competition_id = ?)
                  {"AND entity_type = ?" if entity_type else ""}"""
        params = [COMPETITIONS[competition]] + ([entity_type] if entity_type else [])
        names = {period_stat_name(stat, period) for stat, period in conn.execute(sql, params)}
        _stored_cache[key] = names | {half for half, quarters in HALVES.items() if set(quarters) <= names}
    return _stored_cache[key]


class QueryError(ValueError):
    """The question cannot be answered as asked (unknown name, bad filter, ...).

    `candidates` lists possible matches when a name was ambiguous.
    """

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = candidates or []


@dataclass
class GameLine:
    game_id: int
    start_time: str
    opponent: str
    venue: str
    team_score: int
    opponent_score: int
    rest_hours: float  # since this team's previous loaded game; None if first
    opponent_rest_hours: float
    values: dict  # stat name -> value; percentages as "made/attempted"; win_pct as "W"/"L"


@dataclass
class StatSummary:
    stat: str
    games: int
    total: float = None
    per_game: float = None
    made: float = None
    attempted: float = None
    pct: float = None  # 0-100, None when there were no attempts
    wins: int = None  # win_pct only
    losses: int = None
    value: float = None  # advanced stats (ratings, pace, ...), already rounded; on-floor: per 40 minutes


@dataclass
class QueryResult:
    subject: str
    subject_type: str  # "player" or "team"
    filters: dict
    games: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)  # stat -> StatSummary
    on_floor: bool = False  # minutes/plus-minus/points are of the on-floor time, from stints
    notes: list = field(default_factory=list)  # e.g. games left out for broken play-by-play

    def describe(self):
        """One readable line per stat, e.g. 'fg3_pct: 7/18 = 38.9% over 5 games'."""
        conditions = ", ".join(f"{k}={v}" for k, v in self.filters.items()) or "all games"
        notes = [f"  note: {note}" for note in self.notes]
        if not self.games:
            return "\n".join([f"{self.subject}: no games match ({conditions})."] + notes)
        count = len(self.games)
        lines = [f"{self.subject} ({conditions}), {count} game{'s' if count != 1 else ''}"
                 f"{' with on-floor time' if self.on_floor else ''}:"]
        for s in self.summary.values():
            if self.on_floor and s.stat == "minutes":
                lines.append(f"  minutes on the floor: {s.total:g} total ({s.per_game:.1f} per game)")
            elif self.on_floor and s.value is not None:
                sign = "+" if s.stat == "plus_minus" else ""  # +5 for a margin, 81 for points
                lines.append(f"  {s.stat}: {s.total:{sign}g} total ({s.per_game:{sign}.1f} per game, "
                             f"{s.value:{sign}.1f} per 40 minutes)")
            elif s.wins is not None:
                lines.append(f"  {s.stat}: {s.wins}-{s.losses} = {s.pct:.1f}%")
            elif s.pct is not None or s.attempted is not None:
                pct = "n/a" if s.pct is None else f"{s.pct:.1f}%"
                lines.append(f"  {s.stat}: {s.made:g}/{s.attempted:g} = {pct}")
            else:
                lines.append(f"  {s.stat}: {s.per_game:.1f} per game ({s.total:g} total)")
        return "\n".join(lines + notes)


def _resolve(resolver, text, label):
    result = resolver(text)
    if result.status == "match":
        return result.id, result.name
    if result.status == "ambiguous":
        names = ", ".join(name for _, name in result.candidates)
        plural = f"{label}es" if label.endswith("ch") else f"{label}s"  # coaches, players
        raise QueryError(f"'{text}' could mean several {plural}: {names}.", result.candidates)
    raise QueryError(f"No {label} named '{text}' in the database.")


# One row per team per game. Neutral-venue games are neither home nor away.
# rest_hours: hours since this team's previous game in the database (NULL for
# its first game), in any loaded competition: a Panathinaikos Greek league game
# on Sunday makes its Euroleague game on Tuesday short rest. Leagues that are
# not loaded are invisible, so for other clubs this is an upper bound.
TEAM_GAMES_CTE = """
WITH sides AS (
    SELECT g.id AS game_id, g.season_id, g.start_time, g.phase,
           g.home_team_id AS team_id, g.away_team_id AS opponent_id,
           CASE WHEN g.is_neutral_venue = 1 THEN 'neutral' ELSE 'home' END AS venue,
           g.home_score AS team_score, g.away_score AS opponent_score
    FROM games g
    UNION ALL
    SELECT g.id, g.season_id, g.start_time, g.phase,
           g.away_team_id, g.home_team_id,
           CASE WHEN g.is_neutral_venue = 1 THEN 'neutral' ELSE 'away' END,
           g.away_score, g.home_score
    FROM games g
),
team_games AS (
    SELECT sides.*,
           ROUND((julianday(start_time) - julianday(
               LAG(start_time) OVER (PARTITION BY team_id ORDER BY start_time)
           )) * 24, 1) AS rest_hours
    FROM sides
)
"""

# The opponent's wins/losses in the same season from games that started
# before this one: their record "as of the game date".
OPPONENT_RECORD_SQL = """
    (SELECT COUNT(*) FROM team_games p
     WHERE p.team_id = tg.opponent_id AND p.season_id = tg.season_id
       AND p.start_time < tg.start_time AND p.team_score {op} p.opponent_score)
"""


def query_stats(
    stats,
    player=None,
    team=None,
    opponent=None,
    venue=None,
    last_n=None,
    opponent_over_500=False,
    without_player=None,
    with_player=None,
    against_player=None,
    season=None,
    seasons=None,
    phase=None,
    competition="euroleague",
    max_rest_hours=None,
    min_rest_hours=None,
    player_threshold=None,
    coach=None,
    referee=None,
    on_floor_with=None,
    on_floor_without=None,
):
    """Return a QueryResult for one player or one team.

    stats: stored stat names ("assists", "total_rebounds", ...) and/or
        percentages from PERCENTAGE_STATS ("fg3_pct", ...).
    venue: "home", "away" or "neutral" (from the subject's point of view).
    opponent_over_500: only games where the opponent had more wins than
        losses in that season before the game.
    without_player: only games this player missed, in seasons when he played
        for the subject's team or the opponent (so games from before he
        joined do not count). Season-level: after a mid-season move, his old
        team's later games still count as "without" him.
    with_player: only games in which this player appeared (for either team).
    against_player: only games in which this player appeared for the
        subject's opponent (for a player subject: the other team, so games
        where they were teammates never count).
    season: season name such as "2024-25". seasons: several season names,
        e.g. ["2025-26", "2026-27"]; not with `season`.
    phase: e.g. "regular_season".
    coach: only games where the subject's own team (a player's team that game)
        was coached by this person. referee: only games this person refereed.
    competition: "euroleague" (default), "eurocup" or "greek_league". A player
        in several leagues is one player; this decides which games count.
    max_rest_hours / min_rest_hours: hours since the subject's team played
        its previous loaded game (e.g. max_rest_hours=48 for two games within
        48 hours). A team's first loaded game has no rest value and is left
        out by these filters. Games in every loaded league count (see above).
    player_threshold: {"player", "stat", "min", "max"}: only games in which
        this player played and his `stat` in that game was at least `min`
        and/or at most `max` (both inclusive), e.g. Lessort, minutes, min 30.
        Games he missed never count, not even for "max".
    on_floor_with / on_floor_without: lists of teammates who must be on the
        floor / off the floor at the same time as the subject (a player
        subject is on the floor himself). Stats then cover only that time,
        from the stints: ON_FLOOR_STATS and win_pct. Games are first chosen
        by the other filters (last_n too), and then only games where the
        condition held for some time count. Games with broken substitution
        data (pbp_checks.problems) are left out and listed in `notes`.
    """
    for name, hours in (("max_rest_hours", max_rest_hours), ("min_rest_hours", min_rest_hours)):
        if hours is not None and hours <= 0:
            raise QueryError(f"{name} must be more than 0.")
    if max_rest_hours is not None and min_rest_hours is not None and min_rest_hours > max_rest_hours:
        raise QueryError("min_rest_hours cannot be more than max_rest_hours.")
    if (player is None) == (team is None):
        raise QueryError("Ask about exactly one player or one team.")
    if venue is not None and venue not in VENUES:
        raise QueryError(f"venue must be one of {', '.join(VENUES)}.")
    if last_n is not None and last_n < 1:
        raise QueryError("last_n must be at least 1.")
    if not stats:
        raise QueryError("Ask for at least one stat.")
    if season is not None and seasons:
        raise QueryError("Give either season or seasons, not both.")
    if player_threshold is not None:
        threshold_stat = player_threshold.get("stat")
        low, high = player_threshold.get("min"), player_threshold.get("max")
        if threshold_stat not in COUNTING_STATS:
            raise QueryError(f"player_threshold stat must be one of: {', '.join(COUNTING_STATS)}.")
        if low is None and high is None:
            raise QueryError("player_threshold needs a min, a max or both.")
        if low is not None and high is not None and low > high:
            raise QueryError("player_threshold min cannot be more than max.")

    on_floor_with, on_floor_without = list(on_floor_with or []), list(on_floor_without or [])
    on_floor = bool(on_floor_with or on_floor_without or set(stats) & set(STINT_ONLY_STATS))
    if on_floor:
        other = [s for s in stats if s not in ON_FLOOR_STATS + RESULT_STATS]
        if other:
            raise QueryError(
                f"On-floor questions (who was on the court together) can report only {', '.join(ON_FLOOR_STATS)} "
                f"and win_pct for now, not {', '.join(other)}.")
        if competition == "greek_league":
            raise QueryError("On-floor stats need play-by-play, which the Greek league data doesn't have.")
        if len(on_floor_with) + len(on_floor_without) > MAX_ON_FLOOR_PLAYERS:
            raise QueryError(f"Name at most {MAX_ON_FLOOR_PLAYERS} on-floor players.")

    conn = get_connection()
    try:
        competition_sql, competition_params = competition_where(competition)
        # A team's plus-minus is its point margin, taken from the final score
        # (the source only gives plus-minus for players), so it is never stored.
        # On-floor stats come from the stints, not game_stats.
        if not on_floor:
            _check_stats(conn, [s for s in stats if not (team is not None and s == "plus_minus")], competition)
        if player is not None and set(stats) & set(QUARTER_STATS):
            raise QueryError("Quarter and half points are team stats only for now (a player's quarters need "
                             "play-by-play data, which isn't loaded).")
        filters, where, params = {}, [competition_sql], list(competition_params)
        if competition != "euroleague":
            filters["competition"] = competition

        if player is not None:
            subject_id, subject = _resolve(lambda t: resolve_player(t, conn), player, "player")
            subject_type = "player"
            source = "team_games tg JOIN game_appearances ga ON ga.game_id = tg.game_id AND ga.team_id = tg.team_id"
            where.append("ga.player_id = ?")
        else:
            subject_id, subject = _resolve(lambda t: resolve_team(t, conn), team, "team")
            subject_type = "team"
            source = "team_games tg"
            where.append("tg.team_id = ?")
        params.append(subject_id)

        if opponent is not None:
            opponent_id, filters["opponent"] = _resolve(lambda t: resolve_team(t, conn), opponent, "team")
            where.append("tg.opponent_id = ?")
            params.append(opponent_id)
        if venue is not None:
            filters["venue"] = venue
            where.append("tg.venue = ?")
            params.append(venue)
        if opponent_over_500:
            filters["opponent_over_500"] = True
            where.append(f"{OPPONENT_RECORD_SQL.format(op='>')} > {OPPONENT_RECORD_SQL.format(op='<')}")
        if without_player is not None:
            absent_id, absent_name = _resolve(lambda t: resolve_player(t, conn), without_player, "player")
            filters["without_player"] = absent_name
            # Missed this game, AND played for one of these two teams in the
            # same season. Without the second part, every game from before
            # the player joined would count as a game "without" him.
            where.append("""
                NOT EXISTS (SELECT 1 FROM game_appearances x
                            WHERE x.game_id = tg.game_id AND x.player_id = ?)
                AND EXISTS (SELECT 1 FROM game_appearances r JOIN games rg ON rg.id = r.game_id
                            WHERE r.player_id = ? AND rg.season_id = tg.season_id
                              AND r.team_id IN (tg.team_id, tg.opponent_id))""")
            params += [absent_id, absent_id]
        if with_player is not None:
            present_id, filters["with_player"] = _resolve(
                lambda t: resolve_player(t, conn), with_player, "player"
            )
            if without_player is not None and present_id == absent_id:
                raise QueryError("A player cannot be both 'with' and 'without' in the same question.")
            where.append(
                "EXISTS (SELECT 1 FROM game_appearances w WHERE w.game_id = tg.game_id AND w.player_id = ?)"
            )
            params.append(present_id)
        if against_player is not None:
            rival_id, filters["against_player"] = _resolve(
                lambda t: resolve_player(t, conn), against_player, "player"
            )
            if rival_id == subject_id and subject_type == "player":
                raise QueryError("A player cannot play against himself.")
            # He appeared for the opponent's side of this game.
            where.append("""EXISTS (SELECT 1 FROM game_appearances v WHERE v.game_id = tg.game_id
                                    AND v.team_id = tg.opponent_id AND v.player_id = ?)""")
            params.append(rival_id)
        if season is not None:
            filters["season"] = season
            where.append("tg.season_id IN (SELECT id FROM seasons WHERE name = ?)")
            params.append(season)
        if seasons:
            names = sorted(set(seasons))
            filters["seasons"] = ", ".join(names)
            where.append(f"tg.season_id IN (SELECT id FROM seasons WHERE name IN ({', '.join('?' for _ in names)}))")
            params += names
        if phase is not None:
            filters["phase"] = phase
            where.append("tg.phase = ?")
            params.append(phase)
        # Coach: the subject's own team (a player's team in that game) was coached
        # by them. Referee: they refereed the game. Both from the game headers.
        for role, name, team_condition in (("coach", coach, "AND st.team_id = tg.team_id"),
                                           ("referee", referee, "")):
            if name is None:
                continue
            staff_id, filters[role] = _resolve(lambda t: resolve_staff(role, t, conn), name, role)
            where.append(f"""EXISTS (SELECT 1 FROM game_staff st WHERE st.game_id = tg.game_id AND st.role = ?
                             {team_condition} AND st.normalized = (SELECT normalized FROM game_staff WHERE id = ?))""")
            params += [role, staff_id]
        if max_rest_hours is not None:
            filters["max_rest_hours"] = max_rest_hours
            where.append("tg.rest_hours <= ?")
            params.append(max_rest_hours)
        if min_rest_hours is not None:
            filters["min_rest_hours"] = min_rest_hours
            where.append("tg.rest_hours >= ?")
            params.append(min_rest_hours)

        if player_threshold is not None:
            threshold_id, threshold_name = _resolve(
                lambda t: resolve_player(t, conn), player_threshold["player"], "player"
            )
            # Uses the uq_game_player_stat index (game_id, player_id, stat_name).
            condition = ["ps.game_id = tg.game_id", "ps.entity_type = 'player'",
                         "ps.player_id = ?", "ps.stat_name = ?"]
            threshold_params = [threshold_id, threshold_stat]
            bounds = []
            if low is not None:
                condition.append("ps.stat_value >= ?")
                threshold_params.append(low)
                bounds.append(f">= {low:g}")
            if high is not None:
                condition.append("ps.stat_value <= ?")
                threshold_params.append(high)
                bounds.append(f"<= {high:g}")
            filters["player_threshold"] = f"{threshold_name} {threshold_stat} {' and '.join(bounds)}"
            where.append(f"EXISTS (SELECT 1 FROM game_stats ps WHERE {' AND '.join(condition)})")
            params += threshold_params

        sql = f"""{TEAM_GAMES_CTE}
            SELECT tg.game_id, tg.start_time, opp.name, tg.venue, tg.team_score, tg.opponent_score,
                   tg.rest_hours, ot.rest_hours
            FROM {source}
            JOIN teams opp ON opp.id = tg.opponent_id
            JOIN team_games ot ON ot.game_id = tg.game_id AND ot.team_id = tg.opponent_id
            WHERE {' AND '.join(where)}
            ORDER BY tg.start_time DESC"""
        if last_n is not None:
            filters["last_n"] = last_n
            sql += " LIMIT ?"
            params.append(last_n)

        game_rows = conn.execute(sql, params).fetchall()
        notes = []
        if on_floor:
            values = _on_floor_values(conn, subject_type, subject_id, [row[0] for row in game_rows],
                                      on_floor_with, on_floor_without, filters, notes)
            game_rows = [row for row in game_rows if row[0] in values]
        else:
            values = _load_values(conn, subject_type, subject_id, [row[0] for row in game_rows])
    finally:
        conn.close()

    result = QueryResult(subject=subject, subject_type=subject_type, filters=filters,
                         on_floor=on_floor, notes=notes)
    for game_id, start_time, opp_name, game_venue, team_score, opp_score, rest, opp_rest in reversed(game_rows):
        # Basketball has no draws, so a game is either a win or a loss.
        values.setdefault(game_id, {})["win"] = 1 if team_score > opp_score else 0
        if subject_type == "team" and not on_floor:
            values[game_id]["plus_minus"] = team_score - opp_score
        result.games.append(GameLine(
            game_id, start_time, opp_name, game_venue, team_score, opp_score, rest, opp_rest,
            {k: round(v, 2) if on_floor and k == "minutes" else v
             for k, v in _game_values(stats, values[game_id]).items()},
        ))
    result.summary = {stat: _summarise(stat, values, [g.game_id for g in result.games]) for stat in stats}
    if on_floor:
        minutes = sum(values[g.game_id]["minutes"] for g in result.games)
        for stat, summary in result.summary.items():
            if stat in ON_FLOOR_STATS and stat != "minutes" and minutes:
                summary.value = round(summary.total / minutes * 40, 1)
            if summary.total is not None:
                summary.total = round(summary.total, 2)
    return result


def _on_floor_values(conn, subject_type, subject_id, game_ids, with_names, without_names, filters, notes):
    """{game_id: {minutes, plus_minus, points_for, points_against}} of the on-floor time.

    Only games where the condition held for more than 0 seconds are returned.
    Names are resolved here and written into `filters`; games left out
    because their play-by-play is missing or broken are explained in `notes`.
    """
    with_ids, without_ids = [], []
    for names, ids, key in ((with_names, with_ids, "on_floor_with"),
                            (without_names, without_ids, "on_floor_without")):
        shown = []
        for name in names:
            player_id, player_name = _resolve(lambda t: resolve_player(t, conn), name, "player")
            ids.append(player_id)
            shown.append(player_name)
        if shown:
            filters[key] = ", ".join(shown)
    named = with_ids + without_ids
    if len(set(named)) != len(named):
        raise QueryError("Each on-floor player may be named only once (not both on and off the floor).")
    if subject_type == "player" and subject_id in named:
        raise QueryError("The player asked about is always on the floor in his own on-floor stats; "
                         "name only his teammates.")
    if not game_ids:
        return {}

    placeholders = ", ".join("?" for _ in game_ids)
    checked = dict(conn.execute(
        f"SELECT game_id, problems FROM pbp_checks WHERE game_id IN ({placeholders})", game_ids).fetchall())
    missing = [g for g in game_ids if g not in checked]
    broken = [g for g in game_ids if checked.get(g)]
    if missing:
        notes.append(f"{len(missing)} matching game(s) have no play-by-play yet and are left out.")
    if broken:
        notes.append(f"{len(broken)} game(s) left out: the source's substitutions there don't add up to "
                     "five players on the floor.")
    usable = [g for g in game_ids if g in checked and not checked[g]]
    if not usable:
        return {}
    placeholders = ", ".join("?" for _ in usable)

    # Each named player must be the subject's teammate in at least one of the
    # games; otherwise "without him" would be true of every stint.
    for player_id, name in zip(named, list(with_names) + list(without_names)):
        if subject_type == "player":
            sql = f"""SELECT COUNT(*) FROM game_appearances o JOIN game_appearances me
                        ON me.game_id = o.game_id AND me.team_id = o.team_id
                      WHERE me.player_id = ? AND o.player_id = ? AND o.game_id IN ({placeholders})"""
        else:
            sql = f"""SELECT COUNT(*) FROM game_appearances o
                      WHERE o.team_id = ? AND o.player_id = ? AND o.game_id IN ({placeholders})"""
        if not conn.execute(sql, [subject_id, player_id, *usable]).fetchone()[0]:
            raise QueryError(f"'{name}' wasn't on the same team as the subject in any of these games.")

    if subject_type == "player":
        conditions = ["EXISTS (SELECT 1 FROM stint_players me WHERE me.stint_id = s.id AND me.player_id = ?)"]
    else:
        conditions = ["s.team_id = ?"]
    params = [subject_id]
    for ids, sign in ((with_ids, "EXISTS"), (without_ids, "NOT EXISTS")):
        for player_id in ids:
            conditions.append(f"{sign} (SELECT 1 FROM stint_players o WHERE o.stint_id = s.id AND o.player_id = ?)")
            params.append(player_id)
    rows = conn.execute(
        f"""SELECT s.game_id, SUM(s.end_elapsed - s.start_elapsed), SUM(s.points_for), SUM(s.points_against)
            FROM stints s WHERE s.game_id IN ({placeholders}) AND {' AND '.join(conditions)}
            GROUP BY s.game_id HAVING SUM(s.end_elapsed - s.start_elapsed) > 0""",
        [*usable, *params]).fetchall()
    return {game_id: {"minutes": seconds / 60, "plus_minus": scored - allowed,
                      "points_for": scored, "points_against": allowed}
            for game_id, seconds, scored, allowed in rows}


def _check_stats(conn, stats, competition="euroleague"):
    stored = stored_stats(conn, competition)
    unknown = [s for s in stats if s not in RESULT_STATS and not (
        s in stored or (s in PERCENTAGE_STATS and set(sum(PERCENTAGE_STATS[s], ())) <= stored))]
    if unknown:
        where = "" if competition == "euroleague" else f" for the {competition.replace('_', ' ')}"
        raise QueryError(f"Unknown stat(s){where}: {', '.join(unknown)}.")


def _load_values(conn, subject_type, subject_id, game_ids):
    """{game_id: {stat_name: value}} for the subject in the given games."""
    if not game_ids:
        return {}
    column = "player_id" if subject_type == "player" else "team_id"
    placeholders = ", ".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""SELECT game_id, stat_name, period, stat_value FROM game_stats
            WHERE entity_type = ? AND {column} = ? AND game_id IN ({placeholders})""",
        [subject_type, subject_id, *game_ids],
    ).fetchall()
    values = {}
    for game_id, stat_name, period, value in rows:
        values.setdefault(game_id, {})[period_stat_name(stat_name, period)] = value
    return {game_id: add_halves(game) for game_id, game in values.items()}


def _game_values(stats, game_values):
    shown = {}
    for stat in stats:
        if stat == "win_pct":
            shown[stat] = "W" if game_values["win"] else "L"
        elif stat in PERCENTAGE_STATS:
            made_cols, attempt_cols = PERCENTAGE_STATS[stat]
            made = sum(game_values.get(c) or 0 for c in made_cols)
            attempted = sum(game_values.get(c) or 0 for c in attempt_cols)
            shown[stat] = f"{made:g}/{attempted:g}"
        else:
            shown[stat] = game_values.get(stat)
    return shown


def _summarise(stat, values, game_ids):
    if stat in QUARTER_STATS:  # only games whose quarter scores were recorded, not zeros for the rest
        game_ids = [g for g in game_ids if stat in values.get(g, {})]
    games = len(game_ids)
    if stat == "win_pct":
        wins = sum(values[g]["win"] for g in game_ids)
        pct = round(100 * wins / games, 1) if games else None
        return StatSummary(stat, games, pct=pct, wins=wins, losses=games - wins)
    if stat in PERCENTAGE_STATS:
        made_cols, attempt_cols = PERCENTAGE_STATS[stat]
        made = sum(values.get(g, {}).get(c) or 0 for g in game_ids for c in made_cols)
        attempted = sum(values.get(g, {}).get(c) or 0 for g in game_ids for c in attempt_cols)
        pct = round(100 * made / attempted, 1) if attempted else None
        return StatSummary(stat, games, made=made, attempted=attempted, pct=pct)
    total = sum(values.get(g, {}).get(stat) or 0 for g in game_ids)
    per_game = round(total / games, 2) if games else None
    return StatSummary(stat, games, total=total, per_game=per_game)
