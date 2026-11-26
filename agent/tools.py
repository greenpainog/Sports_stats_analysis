"""Read-only tools for the multi-step agent.

Each tool is a plain function that takes one validated Pydantic model and
returns text: the same answer text /ask shows, which is what the LLM reads.
Nothing here imports LangGraph. The graph (agent/graph.py) wraps TOOLS, so the
tools can be tested without an LLM and the agent framework could be swapped.

The LLM never writes SQL: it can only pick one of these tools and fill in its
argument model, which Pydantic validates before anything runs. Every tool only
reads the database.

A tool never raises for a bad question: unknown names, impossible filters and
invalid arguments come back as text starting with "Error:", so the agent can
correct itself (for example, look the name up with resolve_name and retry).
"""
from dataclasses import dataclass
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from analysis.stat_query import COMPETITIONS, QueryError
from api.models import MatchupSpec, PlayerProfileSpec, PlayerRankingSpec, QuerySpec, RankingSpec
from api.service import (
    answer_spec,
    matchup_spec,
    player_profile_spec,
    player_rankings_spec,
    player_rankings_text,
    rankings_spec,
    team_rankings_text,
)
from database.aliases import resolve_player, resolve_team
from database.db import get_connection

Competition = Literal["euroleague", "eurocup", "greek_league"]


# --- Argument models -----------------------------------------------------------
# Where an API model already exists it is reused as is, so a tool call is
# validated exactly like the matching endpoint.

class TeamRankingsArgs(RankingSpec):
    team: Optional[str] = Field(None, description="The team the question is about: its rank is pointed out.")


class PlayerRankingsArgs(PlayerRankingSpec):
    # A shorter default than the endpoint's 50: every row costs the LLM tokens.
    limit: Optional[int] = Field(10, ge=1, le=50, description="Players listed; ranks are still out of everyone.")


class ResolveNameArgs(BaseModel):
    kind: Literal["team", "player"]
    name: str = Field(min_length=1, description="A name as the user wrote it, e.g. 'Juancho' or 'Real'.")


class TeamRosterArgs(BaseModel):
    team: str = Field(min_length=1)
    season: str = Field(pattern=r"^\d{4}-\d{2}$", description="e.g. '2026-27'.")
    competition: Competition = "euroleague"


# --- Tools -------------------------------------------------------------------

def query_stats(spec: QuerySpec) -> str:
    """One player's or team's stats with any filters (opponent, venue, last N, with/without a player, ...)."""
    return answer_spec(spec).answer


def team_rankings(args: TeamRankingsArgs) -> str:
    focus = args.team
    result = rankings_spec(RankingSpec(**args.model_dump(exclude={"team"})))
    return team_rankings_text(result, focus)


def matchup(spec: MatchupSpec) -> str:
    return matchup_spec(spec).answer


def player_rankings(args: PlayerRankingsArgs) -> str:
    return player_rankings_text(player_rankings_spec(args))


def player_profile(spec: PlayerProfileSpec) -> str:
    return player_profile_spec(spec).answer


def resolve_name(args: ResolveNameArgs) -> str:
    result = (resolve_team if args.kind == "team" else resolve_player)(args.name)
    if result.status == "not_found":
        return f"No {args.kind} matches '{args.name}'."
    names = ", ".join(name for _, name in result.candidates)
    if result.status == "match":
        return f"'{args.name}' is {names}."
    return f"'{args.name}' is ambiguous: {names}. Ask the user which one they mean."


ROSTER_SQL = """
SELECT p.name, COUNT(*) AS games,
       SUM(ga.started) AS starts,
       AVG(CASE WHEN gs.stat_name = 'minutes' THEN gs.stat_value END) AS minutes
FROM game_appearances ga
JOIN games g ON g.id = ga.game_id
JOIN seasons s ON s.id = g.season_id
JOIN competitions c ON c.id = s.competition_id
JOIN players p ON p.id = ga.player_id
LEFT JOIN game_stats gs ON gs.game_id = ga.game_id AND gs.player_id = ga.player_id
                       AND gs.entity_type = 'player' AND gs.period = 'match' AND gs.stat_name = 'minutes'
WHERE ga.team_id = ? AND s.name = ? AND c.source_competition_id = ?
GROUP BY p.id
ORDER BY minutes DESC, games DESC
"""


def team_roster(args: TeamRosterArgs) -> str:
    """Who played for a team in a season: games, starts and minutes per game, most minutes first."""
    found = resolve_team(args.team)
    if found.status != "match":
        names = ", ".join(name for _, name in found.candidates) or "none"
        raise QueryError(f"Team '{args.team}' is {found.status.replace('_', ' ')} (candidates: {names}).")
    team_id, team_name = found.candidates[0]
    conn = get_connection()
    try:
        team_games = conn.execute(
            """SELECT COUNT(*) FROM games g JOIN seasons s ON s.id = g.season_id
               JOIN competitions c ON c.id = s.competition_id
               WHERE ? IN (g.home_team_id, g.away_team_id) AND s.name = ? AND c.source_competition_id = ?
                 AND g.home_score IS NOT NULL""",
            (team_id, args.season, COMPETITIONS[args.competition]),
        ).fetchone()[0]
        rows = conn.execute(ROSTER_SQL, (team_id, args.season, COMPETITIONS[args.competition])).fetchall()
        coaches = conn.execute(
            """SELECT st.name, COUNT(*) FROM game_staff st JOIN games g ON g.id = st.game_id
               JOIN seasons s ON s.id = g.season_id JOIN competitions c ON c.id = s.competition_id
               WHERE st.role = 'coach' AND st.team_id = ? AND s.name = ? AND c.source_competition_id = ?
               GROUP BY st.normalized ORDER BY MIN(g.start_time)""",
            (team_id, args.season, COMPETITIONS[args.competition]),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return f"{team_name} has no players in {args.competition} {args.season}."
    lines = [f"{team_name}, {args.competition} {args.season}: {team_games} games played."]
    if coaches:
        lines.append("Head coach: " + ", ".join(f"{name} ({games} games)" for name, games in coaches))
    lines.append("Players (games, starts, minutes per game):")
    for name, games, starts, minutes in rows:
        shown = "-" if minutes is None else f"{minutes:.1f}"
        lines.append(f"  {name}: {games} games, {starts or 0} starts, {shown} min")
    return "\n".join(lines)


# --- Registry ----------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    args: type  # the Pydantic model the LLM fills in
    run: Callable[[BaseModel], str]
    description: str  # what the LLM reads to choose a tool


TOOLS = {tool.name: tool for tool in (
    Tool("query_stats", QuerySpec, query_stats,
         "One player's or one team's stats over matching games, with filters: opponent, venue, last_n, "
         "opponent_over_500, with/without/against a player (whole games), on_floor_with/on_floor_without "
         "(teammates on the court / on the bench at the same time: stats only minutes, plus_minus, points_for, "
         "points_against, win_pct), player_threshold, rest, season(s), phase, "
         "competition, coach (the subject's own team's coach), referee, and compare: 2-4 sides, each with a label and at least one filter that differs "
         "(e.g. [{label: 'season', season: '2026-27'}, {label: 'last 6', last_n: 6}]). Top-level "
         "filters apply to EVERY side, so never put the filter that differs (like last_n) at the top level. "
         "The main tool."),
    Tool("team_rankings", TeamRankingsArgs, team_rankings,
         "Rank every team on stats (also advanced ones like off_rating, pace, forced_turnovers, and "
         "quarter/half points like points_q4, points_first_half). "
         "side='opponent' for what teams allowed. Set team to point out one team's rank."),
    Tool("matchup", MatchupSpec, matchup,
         "Two teams side by side with their value and league rank for each stat."),
    Tool("player_rankings", PlayerRankingsArgs, player_rankings,
         "List the league's (or one team's) players by a stat: leaders, top N."),
    Tool("player_profile", PlayerProfileSpec, player_profile,
         "One player's value and league rank in every player stat."),
    Tool("resolve_name", ResolveNameArgs, resolve_name,
         "Check what a team or player name refers to, or list candidates when it is ambiguous."),
    Tool("team_roster", TeamRosterArgs, team_roster,
         "Who played for and coached a team in a season, with games, starts and minutes per game."),
)}


def run_tool(name: str, raw_args: dict) -> str:
    """Validate raw_args (from the LLM) against the tool's model and run it.

    Always returns text: the answer, or "Error: ..." for an unknown tool,
    invalid arguments or a question the data can't answer.
    """
    tool = TOOLS.get(name)
    if tool is None:
        return f"Error: there is no tool named '{name}'. Tools: {', '.join(TOOLS)}."
    try:
        args = tool.args.model_validate(raw_args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors())
        return f"Error: invalid arguments for {name}: {problems}"
    try:
        return tool.run(args)
    except QueryError as exc:
        hint = f" Candidates: {', '.join(name for _, name in exc.candidates)}." if exc.candidates else ""
        return f"Error: {exc}{hint}"
