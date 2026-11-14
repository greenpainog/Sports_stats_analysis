import re
from analysis.stat_analysis import get_available_stats, get_available_teams
from database.queries import get_available_db_stats, get_available_db_teams

def parse_team_query(text, file="data/full_stats.csv", source="csv"):
    text_lower = text.lower()

    if source == "db":
        stats = get_available_db_stats()
        teams = get_available_db_teams()
    else:
        stats = get_available_stats(file)
        teams = get_available_teams(file)

    matched_stat = None
    for stat in stats:
        if stat.lower() in text_lower:
            matched_stat = stat 
            break

    matched_teams = []
    for team in teams:
        if team.lower() in text_lower:
            matched_teams.append(team)

    head_to_head = any(
        phrase in text_lower
        for phrase in ["head to head", "head-to-head", "between them"]
    )

    last_n = None
    last_n_match = re.search(r"last\s+(\d+)", text_lower)
    if last_n_match:
        last_n = int(last_n_match.group(1))

    return {
        "stat_name": matched_stat,
        "team_a": matched_teams[0] if len(matched_teams) >= 1 else None,
        "team_b": matched_teams[1] if len(matched_teams) >= 2 else None,
        "head_to_head": head_to_head,
        "last_n": last_n,
    }