"""Rank a team's lineups (2, 3, 4 or 5 players on the floor together) from the stints.

Each stint is a stretch of game time with the same five players on the floor
for a team (ingestion/play_by_play.build_stints). A duo is on the floor
together in every stint that holds both players, so a duo's minutes and
plus-minus are the sums over those stints; the same goes for 3- and 4-man
groups. 5-man lineups are the stints' own five players.

There is no minimum-minutes cutoff: every combination that played together is
listed, and the ones under SMALL_SAMPLE_MINUTES are flagged as small samples
so the reader can judge them. A +12 in 3 minutes is +160 per 40 (and about
+200 per 100 possessions), which says little.

Net rating is points scored minus allowed per 100 possessions together, with
possessions estimated from the events in those stints (FGA - OREB + TOV +
0.44 * FTA, averaged over both teams, as for whole teams). It is the default
"best". Net per 40 (plus-minus per 40 minutes) is kept as another sort: it
favours fast-paced groups, which get more possessions per minute.
"""
from dataclasses import dataclass, field
from itertools import combinations

from analysis.pbp_query import ratings, usable_games
from analysis.stat_query import COMPETITIONS, QueryError, _resolve
from database.aliases import resolve_player, resolve_team
from database.db import get_connection

SMALL_SAMPLE_MINUTES = 40  # less than one full game together
SORTS = ("net_rating", "off_rating", "def_rating", "net_per_40", "plus_minus", "minutes",
         "points_for_per_40", "points_against_per_40")
SORT_LABELS = {
    "net_rating": "net rating (per 100 possessions)",
    "off_rating": "offensive rating (points scored per 100 possessions)",
    "def_rating": "defensive rating (points allowed per 100 possessions)",
    "net_per_40": "plus-minus per 40 minutes",
    "plus_minus": "total plus-minus",
    "minutes": "minutes together",
    "points_for_per_40": "points scored per 40 minutes",
    "points_against_per_40": "points allowed per 40 minutes",
}


@dataclass
class Lineup:
    players: list  # names, in a fixed order
    games: int
    minutes: float
    points_for: int
    points_against: int
    possessions: float = 0  # estimated, the average of both teams' possessions in that time

    @property
    def ratings(self):
        return ratings(self.points_for, self.points_against, self.possessions)

    @property
    def plus_minus(self):
        return self.points_for - self.points_against

    def per_40(self, value):
        return 40 * value / self.minutes

    @property
    def small_sample(self):
        return self.minutes < SMALL_SAMPLE_MINUTES

    def sort_value(self, sort):
        if sort in self.ratings:
            return self.ratings[sort]
        return {
            "net_per_40": lambda: self.per_40(self.plus_minus),
            "plus_minus": lambda: self.plus_minus,
            "minutes": lambda: self.minutes,
            "points_for_per_40": lambda: self.per_40(self.points_for),
            "points_against_per_40": lambda: self.per_40(self.points_against),
        }[sort]()


@dataclass
class LineupResult:
    team: str
    size: int
    sort: str
    order: str  # "highest" or "lowest" first
    filters: dict
    games: int  # games the lineups were counted over
    lineup_count: int  # every combination that played, before the limit
    lineups: list = field(default_factory=list)  # the top `limit`, ranked
    notes: list = field(default_factory=list)

    def describe(self):
        conditions = ", ".join(f"{k}={v}" for k, v in self.filters.items()) or "all games"
        first = "lowest" if self.order == "lowest" else "highest"
        head = (f"{self.team} {self.size}-man lineups ({conditions}; {self.games} games), "
                f"by {SORT_LABELS[self.sort]}, {first} first; {self.lineup_count} lineups played together")
        if not self.lineups:
            return head + ": none match."
        lines = [head + ":"]
        for rank, lineup in enumerate(self.lineups, 1):
            r = lineup.ratings
            rated = ("net rating n/a (no possessions)" if r["net_rating"] is None else
                     f"net rating {r['net_rating']:+.1f} (off {r['off_rating']:.1f}, def {r['def_rating']:.1f}, "
                     f"{lineup.possessions:.0f} possessions)")
            text = (f"  {rank}. {' + '.join(lineup.players)}: {rated}; {lineup.plus_minus:+d} in "
                    f"{lineup.minutes:.1f} min over {lineup.games} games ({lineup.per_40(lineup.plus_minus):+.1f} "
                    f"per 40 min; scored {lineup.points_for}, allowed {lineup.points_against})")
            if lineup.small_sample:
                text += f"  [SMALL SAMPLE: under {SMALL_SAMPLE_MINUTES} minutes together]"
            lines.append(text)
        small = sum(lineup.small_sample for lineup in self.lineups)
        if small:
            lines.append(f"  Note: {small} of these {len(self.lineups)} lineups played under "
                         f"{SMALL_SAMPLE_MINUTES} minutes together, so their ratings and per-40 numbers can swing a lot.")
        lines += [f"  Note: {note}" for note in self.notes]
        return "\n".join(lines)


def _team_games(conn, team_id, season, seasons, competition, phase, last_n):
    where = ["? IN (g.home_team_id, g.away_team_id)", "g.home_score IS NOT NULL", "c.source_competition_id = ?"]
    params = [team_id, COMPETITIONS[competition]]
    if season is not None:
        where.append("s.name = ?")
        params.append(season)
    if seasons:
        where.append(f"s.name IN ({', '.join('?' for _ in seasons)})")
        params += list(seasons)
    if phase is not None:
        where.append("g.phase = ?")
        params.append(phase)
    rows = conn.execute(
        f"""SELECT g.id FROM games g JOIN seasons s ON s.id = g.season_id
            JOIN competitions c ON c.id = s.competition_id
            WHERE {' AND '.join(where)} ORDER BY g.start_time DESC""", params).fetchall()
    ids = [game_id for (game_id,) in rows]
    return ids[:last_n] if last_n else ids


def lineup_rankings(team, size=2, season=None, seasons=None, competition="euroleague", phase=None,
                    last_n=None, with_players=None, sort="net_rating", order="highest", limit=10):
    """Every `size`-player group of `team` that shared the floor, ranked by `sort`.

    with_players: only groups that include all of these players (e.g. the
    best duo for one player). Games whose substitutions don't add up are left
    out, with a note.
    """
    if size not in (2, 3, 4, 5):
        raise QueryError("Lineup size must be 2, 3, 4 or 5.")
    if sort not in SORTS:
        raise QueryError(f"sort must be one of {', '.join(SORTS)}.")
    if competition not in COMPETITIONS:
        raise QueryError(f"competition must be one of {', '.join(COMPETITIONS)}.")
    if competition == "greek_league":
        raise QueryError("Lineups need play-by-play, which the Greek league data doesn't have.")
    if season is not None and seasons:
        raise QueryError("Give either season or seasons, not both.")
    with_players = list(with_players or [])
    if len(with_players) >= size:
        raise QueryError(f"A {size}-man lineup can include at most {size - 1} named players.")

    team_id, team_name = _resolve(resolve_team, team, "team")
    named = [_resolve(resolve_player, name, "player") for name in with_players]

    filters = {"competition": competition}
    for key, value in (("season", season), ("seasons", ", ".join(sorted(seasons or [])) or None),
                       ("phase", phase), ("last_n", last_n)):
        if value is not None:
            filters[key] = value
    if named:
        filters["with"] = ", ".join(name for _, name in named)

    notes = []
    conn = get_connection()
    try:
        game_ids = usable_games(conn, _team_games(conn, team_id, season, seasons, competition, phase, last_n),
                                need_lineups=True, notes=notes)
        stints = []
        if game_ids:
            placeholders = ", ".join("?" for _ in game_ids)
            stints = conn.execute(
                f"""SELECT s.game_id, s.end_elapsed - s.start_elapsed, s.points_for, s.points_against,
                           (COALESCE(s.own_possessions, 0) + COALESCE(s.opponent_possessions, 0)) / 2.0,
                           GROUP_CONCAT(sp.player_id)
                    FROM stints s JOIN stint_players sp ON sp.stint_id = s.id
                    WHERE s.team_id = ? AND s.game_id IN ({placeholders})
                    GROUP BY s.id""", [team_id, *game_ids]).fetchall()
        player_ids = {int(p) for *_, players in stints for p in players.split(",")}
        names = {}
        if player_ids:
            placeholders = ", ".join("?" for _ in player_ids)
            names = dict(conn.execute(f"SELECT id, name FROM players WHERE id IN ({placeholders})",
                                      list(player_ids)).fetchall())
    finally:
        conn.close()

    must = {player_id for player_id, _ in named}
    totals = {}  # (player ids) -> [game ids, seconds, points for, points against, possessions]
    for game_id, seconds, scored, allowed, possessions, players in stints:
        on_floor = sorted({int(p) for p in players.split(",")})
        if not must <= set(on_floor):
            continue
        for group in combinations(on_floor, size):
            if must <= set(group):
                entry = totals.setdefault(group, [set(), 0, 0, 0, 0.0])
                entry[0].add(game_id)
                entry[1] += seconds
                entry[2] += scored
                entry[3] += allowed
                entry[4] += possessions

    # Named players first, then by surname-ish order (the name as stored), so
    # "best duo with X" reads "X + Y".
    def ordered(group):
        return [names[p] for p in group if p in must] + sorted(names[p] for p in group if p not in must)

    lineups = [Lineup(ordered(group), len(games), seconds / 60, scored, allowed, possessions)
               for group, (games, seconds, scored, allowed, possessions) in totals.items() if seconds > 0]
    sign = -1 if order == "highest" else 1
    # A group with no possessions has no rating: it goes last.
    lineups.sort(key=lambda l: (l.sort_value(sort) is None, sign * (l.sort_value(sort) or 0), -l.minutes))
    return LineupResult(team=team_name, size=size, sort=sort, order=order, filters=filters, games=len(game_ids),
                        lineup_count=len(lineups), lineups=lineups[:limit], notes=notes)
