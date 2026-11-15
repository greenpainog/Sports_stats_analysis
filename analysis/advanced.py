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
}


def advanced_value(stat, own, opp, games):
    """One advanced stat from summed `own` and `opp` totals over `games` games."""
    if not games:
        return None
    spec = ADVANCED_TEAM_STATS[stat]
    value = spec.formula(own, opp, games)
    return None if value is None else round(value, spec.decimals)
