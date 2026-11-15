"""Advanced team stats, computed from box-score totals.

Every formula takes two dicts of summed stats over the same games: `own` (the
team being rated) and `opp` (its opponents in those games). Ratios are taken
from the sums, never averaged per game, for the same reason as percentages in
stat_query: a blowout and a close game should weigh by how many possessions
they had, not count once each.

Possessions use the common estimate FGA - OREB + TOV + 0.44 * FTA, averaged
over both teams (each team has about as many possessions as its opponent, so
the average cancels some of the estimate's noise). Pace is per 40 minutes.
"""
from dataclasses import dataclass


def _fga(s):
    return s["fg2_attempted"] + s["fg3_attempted"]


def _fgm(s):
    return s["fg2_made"] + s["fg3_made"]


def _own_possessions(s):
    return _fga(s) - s["offensive_rebounds"] + s["turnovers"] + 0.44 * s["ft_attempted"]


def possessions(own, opp):
    """Estimated possessions in these games (each team, so not doubled)."""
    return (_own_possessions(own) + _own_possessions(opp)) / 2


def _ratio(top, bottom, scale=1):
    return None if not bottom else scale * top / bottom


@dataclass(frozen=True)
class Advanced:
    formula: object  # (own, opp, games) -> value or None
    decimals: int
    needs: tuple  # stored stats the formula reads
    description: str


_SHOOTING = ("fg2_made", "fg2_attempted", "fg3_made", "fg3_attempted", "ft_made", "ft_attempted")
_POSS = ("fg2_attempted", "fg3_attempted", "offensive_rebounds", "turnovers", "ft_attempted")

ADVANCED_TEAM_STATS = {
    "possessions": Advanced(
        lambda o, p, g: _ratio(possessions(o, p), g), 1, _POSS, "Possessions per game (estimated)."),
    "pace": Advanced(
        lambda o, p, g: _ratio(40 * possessions(o, p), o["minutes"] / 5), 1, _POSS + ("minutes",),
        "Possessions per 40 minutes, so overtime games don't inflate it."),
    "off_rating": Advanced(
        lambda o, p, g: _ratio(o["points"], possessions(o, p), 100), 1, _POSS + ("points",),
        "Points scored per 100 possessions."),
    "def_rating": Advanced(
        lambda o, p, g: _ratio(p["points"], possessions(o, p), 100), 1, _POSS + ("points",),
        "Points allowed per 100 possessions (lower is better)."),
    "net_rating": Advanced(
        lambda o, p, g: _ratio(o["points"] - p["points"], possessions(o, p), 100), 1, _POSS + ("points",),
        "Offensive minus defensive rating."),
    "efg_pct": Advanced(
        lambda o, p, g: _ratio(_fgm(o) + 0.5 * o["fg3_made"], _fga(o), 100), 1, _SHOOTING,
        "Effective FG%: a made three counts 1.5 field goals."),
    "ts_pct": Advanced(
        lambda o, p, g: _ratio(o["points"], 2 * (_fga(o) + 0.44 * o["ft_attempted"]), 100), 1,
        _SHOOTING + ("points",), "True shooting %: points per shooting possession, free throws included."),
    "tov_pct": Advanced(
        lambda o, p, g: _ratio(o["turnovers"], _fga(o) + 0.44 * o["ft_attempted"] + o["turnovers"], 100), 1,
        _SHOOTING + ("turnovers",), "Turnovers per 100 plays (lower is better)."),
    "oreb_pct": Advanced(
        lambda o, p, g: _ratio(o["offensive_rebounds"], o["offensive_rebounds"] + p["defensive_rebounds"], 100), 1,
        ("offensive_rebounds", "defensive_rebounds"), "Share of the team's own misses it rebounded."),
    "dreb_pct": Advanced(
        lambda o, p, g: _ratio(o["defensive_rebounds"], o["defensive_rebounds"] + p["offensive_rebounds"], 100), 1,
        ("offensive_rebounds", "defensive_rebounds"), "Share of the opponent's misses the team rebounded."),
    "ft_rate": Advanced(
        lambda o, p, g: _ratio(o["ft_attempted"], _fga(o)), 3, _SHOOTING,
        "Free throw attempts per field goal attempt."),
    "fg3a_rate": Advanced(
        lambda o, p, g: _ratio(o["fg3_attempted"], _fga(o), 100), 1, _SHOOTING,
        "Share of field goal attempts that were threes."),
    "ast_pct": Advanced(
        lambda o, p, g: _ratio(o["assists"], _fgm(o), 100), 1, _SHOOTING + ("assists",),
        "Share of made field goals that were assisted."),
    "ast_to": Advanced(
        lambda o, p, g: _ratio(o["assists"], o["turnovers"]), 2, ("assists", "turnovers"),
        "Assists per turnover."),
    "stl_pct": Advanced(
        lambda o, p, g: _ratio(o["steals"], possessions(o, p), 100), 1, _POSS + ("steals",),
        "Steals per 100 opponent possessions."),
    "blk_pct": Advanced(
        lambda o, p, g: _ratio(o["blocks"], p["fg2_attempted"], 100), 1, ("blocks", "fg2_attempted"),
        "Share of the opponent's 2-point attempts the team blocked."),
    "pts_from_paint_pct": Advanced(
        lambda o, p, g: _ratio(o["paint_points"], o["points"], 100), 1, ("paint_points", "points"),
        "Share of the team's points scored in the paint."),
}


def advanced_value(stat, own, opp, games):
    """One advanced stat from summed `own` and `opp` totals over `games` games."""
    if not games:
        return None
    spec = ADVANCED_TEAM_STATS[stat]
    value = spec.formula(own, opp, games)
    return None if value is None else round(value, spec.decimals)


# --- Players -----------------------------------------------------------------
# A player's formulas take `pl` (his totals), `tm` (his team's totals) and
# `op` (the opponents' totals), all summed over the games he played. Shares of
# team plays are scaled by his minutes: "on the floor" = minutes / (team minutes / 5).

def _on_floor(pl, tm):
    """Share of the team's game time he was on the floor (0-1), or None."""
    if not pl["minutes"] or not tm["minutes"]:
        return None
    return pl["minutes"] / (tm["minutes"] / 5)


def _while_on(share_of, pl, tm, top):
    """`top` as a % of `share_of`, counting only the part of `share_of` while he played."""
    floor = _on_floor(pl, tm)
    return None if floor is None else _ratio(top, floor * share_of, 100)


def _ast_pct(pl, tm, op):
    floor = _on_floor(pl, tm)
    return None if floor is None else _ratio(pl["assists"], floor * _fgm(tm) - _fgm(pl), 100)


_PL_SHOOT = _SHOOTING + ("points",)
_PL_POSS = _POSS + ("minutes",)

# kind: "rate" (computed from totals) or "count" (number of games, e.g. double-doubles).
ADVANCED_PLAYER_STATS = {
    # Advanced
    "ts_pct": Advanced(lambda pl, tm, op, g: _ratio(pl["points"], 2 * (_fga(pl) + 0.44 * pl["ft_attempted"]), 100),
                       1, _PL_SHOOT, "True shooting %."),
    "efg_pct": Advanced(lambda pl, tm, op, g: _ratio(_fgm(pl) + 0.5 * pl["fg3_made"], _fga(pl), 100),
                        1, _SHOOTING, "Effective FG%."),
    "usage_pct": Advanced(
        lambda pl, tm, op, g: _while_on(_fga(tm) + 0.44 * tm["ft_attempted"] + tm["turnovers"], pl, tm,
                                        _fga(pl) + 0.44 * pl["ft_attempted"] + pl["turnovers"]),
        1, _PL_POSS, "Share of team plays he used (shot, free throws or turnover) while on the floor."),
    "ast_pct": Advanced(lambda pl, tm, op, g: _ast_pct(pl, tm, op), 1, _SHOOTING + ("assists", "minutes"),
                        "Share of teammates' made shots he assisted while on the floor."),
    "oreb_pct": Advanced(
        lambda pl, tm, op, g: _while_on(tm["offensive_rebounds"] + op["defensive_rebounds"], pl, tm,
                                        pl["offensive_rebounds"]),
        1, ("offensive_rebounds", "defensive_rebounds", "minutes"), "Share of available offensive rebounds he got."),
    "dreb_pct": Advanced(
        lambda pl, tm, op, g: _while_on(tm["defensive_rebounds"] + op["offensive_rebounds"], pl, tm,
                                        pl["defensive_rebounds"]),
        1, ("offensive_rebounds", "defensive_rebounds", "minutes"), "Share of available defensive rebounds he got."),
    "reb_pct": Advanced(
        lambda pl, tm, op, g: _while_on(tm["total_rebounds"] + op["total_rebounds"], pl, tm, pl["total_rebounds"]),
        1, ("total_rebounds", "minutes"), "Share of all available rebounds he got."),
    "tov_pct": Advanced(
        lambda pl, tm, op, g: _ratio(pl["turnovers"], _fga(pl) + 0.44 * pl["ft_attempted"] + pl["turnovers"], 100),
        1, _SHOOTING + ("turnovers",), "Turnovers per 100 of his plays (lower is better)."),
    "stl_pct": Advanced(lambda pl, tm, op, g: _while_on(possessions(tm, op), pl, tm, pl["steals"]),
                        1, _PL_POSS + ("steals",), "Steals per 100 opponent possessions while on the floor."),
    "blk_pct": Advanced(lambda pl, tm, op, g: _while_on(op["fg2_attempted"], pl, tm, pl["blocks"]),
                        1, ("blocks", "fg2_attempted", "minutes"), "Share of opponent 2-point attempts he blocked."),
    "ast_to": Advanced(lambda pl, tm, op, g: _ratio(pl["assists"], pl["turnovers"]),
                       2, ("assists", "turnovers"), "Assists per turnover."),
    # Scoring
    "pts_from_2pt_pct": Advanced(lambda pl, tm, op, g: _ratio(2 * pl["fg2_made"], pl["points"], 100),
                                 1, ("fg2_made", "points"), "Share of his points from 2-pointers."),
    "pts_from_3pt_pct": Advanced(lambda pl, tm, op, g: _ratio(3 * pl["fg3_made"], pl["points"], 100),
                                 1, ("fg3_made", "points"), "Share of his points from 3-pointers."),
    "pts_from_ft_pct": Advanced(lambda pl, tm, op, g: _ratio(pl["ft_made"], pl["points"], 100),
                                1, ("ft_made", "points"), "Share of his points from free throws."),
    "pts_from_paint_pct": Advanced(lambda pl, tm, op, g: _ratio(pl["paint_points"], pl["points"], 100),
                                   1, ("paint_points", "points"), "Share of his points scored in the paint."),
    "points_per_fga": Advanced(lambda pl, tm, op, g: _ratio(pl["points"], _fga(pl)), 2, _PL_SHOOT,
                               "Points per field goal attempt (free throws included in the points)."),
    "fg3a_rate": Advanced(lambda pl, tm, op, g: _ratio(pl["fg3_attempted"], _fga(pl), 100), 1, _SHOOTING,
                          "Share of his field goal attempts that were threes."),
    "ft_rate": Advanced(lambda pl, tm, op, g: _ratio(pl["ft_attempted"], _fga(pl)), 3, _SHOOTING,
                        "Free throw attempts per field goal attempt."),
    "team_pts_share": Advanced(lambda pl, tm, op, g: _ratio(pl["points"], tm["points"], 100), 1, ("points",),
                               "Share of his team's points in his games."),
}

# Misc counts, decided per game.
_DOUBLE_STATS = ("points", "total_rebounds", "assists", "steals", "blocks")
PLAYER_COUNT_STATS = {
    "double_doubles": "Games with 10+ in at least two of points, rebounds, assists, steals, blocks.",
    "triple_doubles": "Games with 10+ in at least three of them.",
    "starts": "Games he started.",
    "games_20_points": "Games with 20 or more points.",
}


def count_in_game(stat, game, started):
    """1 if this game counts for a misc count stat, else 0."""
    if stat == "starts":
        return 1 if started else 0
    if stat == "games_20_points":
        return 1 if (game.get("points") or 0) >= 20 else 0
    tens = sum((game.get(s) or 0) >= 10 for s in _DOUBLE_STATS)
    return 1 if tens >= (2 if stat == "double_doubles" else 3) else 0


def player_advanced_value(stat, pl, tm, op, games):
    if not games:
        return None
    spec = ADVANCED_PLAYER_STATS[stat]
    value = spec.formula(pl, tm, op, games)
    return None if value is None else round(value, spec.decimals)
