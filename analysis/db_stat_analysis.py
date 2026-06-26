import pandas as pd

from database.queries import load_team_stats


LOWER_IS_BETTER_STATS = {
    "Turnovers",
}


def get_better_team(team_a, team_a_average, team_b, team_b_average, stat_name):
    if team_a_average == team_b_average:
        return "Tie"

    if stat_name in LOWER_IS_BETTER_STATS:
        return team_a if team_a_average < team_b_average else team_b

    return team_a if team_a_average > team_b_average else team_b


def analyze_db_stat(stat_name):
    df = load_team_stats()

    stat_df = df[df["stat_name"] == stat_name].copy()

    if stat_df.empty:
        available_stats = sorted(df["stat_name"].dropna().unique().tolist())
        raise ValueError(
            f"Stat '{stat_name}' was not found. Available stats: {available_stats}"
        )

    stat_df["stat_value"] = pd.to_numeric(stat_df["stat_value"], errors="coerce")

    result = (
        stat_df
        .groupby("team")
        .agg(
            Games=("stat_value", "count"),
            Average=("stat_value", "mean"),
        )
        .reset_index()
        .rename(columns={"team": "Team"})
        .sort_values("Average", ascending=False)
    )

    return result


def compare_db_teams(team_a, team_b, stat_name, last_n=None):
    df = load_team_stats()

    stat_df = df[df["stat_name"] == stat_name].copy()
    stat_df["stat_value"] = pd.to_numeric(stat_df["stat_value"], errors="coerce")

    def get_team_values(team_name):
        team_df = stat_df[stat_df["team"] == team_name]

        if last_n is not None:
            team_df = team_df.head(last_n)

        return team_df["stat_value"]

    team_a_values = get_team_values(team_a)
    team_b_values = get_team_values(team_b)

    if team_a_values.empty and team_b_values.empty:
        raise ValueError("Neither team was found in the data.")

    if team_a_values.empty:
        raise ValueError(f"Team not found: {team_a}")

    if team_b_values.empty:
        raise ValueError(f"Team not found: {team_b}")

    team_a_average = team_a_values.mean()
    team_b_average = team_b_values.mean()

    better_team = get_better_team(
        team_a,
        team_a_average,
        team_b,
        team_b_average,
        stat_name,
    )

    return {
        team_a: {
            "games": int(team_a_values.count()),
            "average": float(team_a_average),
        },
        team_b: {
            "games": int(team_b_values.count()),
            "average": float(team_b_average),
        },
        "better_team": better_team,
    }


def compare_db_teams_head_to_head(team_a, team_b, stat_name, last_n=None):
    df = load_team_stats()

    head_to_head_df = df[
        (
            ((df["team1"] == team_a) & (df["team2"] == team_b))
            |
            ((df["team1"] == team_b) & (df["team2"] == team_a))
        )
        & (df["stat_name"] == stat_name)
        & (df["team"].isin([team_a, team_b]))
    ].copy()

    if head_to_head_df.empty:
        raise ValueError(f"No head-to-head matches found between {team_a} and {team_b}.")

    head_to_head_df["stat_value"] = pd.to_numeric(
        head_to_head_df["stat_value"],
        errors="coerce",
    )

    matches = []
    team_a_values = []
    team_b_values = []

    for _, match_group in head_to_head_df.groupby(["date", "team1", "team2"], sort=False):
        team_a_row = match_group[match_group["team"] == team_a]
        team_b_row = match_group[match_group["team"] == team_b]

        if team_a_row.empty or team_b_row.empty:
            continue

        team_a_value = team_a_row.iloc[0]["stat_value"]
        team_b_value = team_b_row.iloc[0]["stat_value"]
        date = team_a_row.iloc[0]["date"]

        team_a_values.append(team_a_value)
        team_b_values.append(team_b_value)

        matches.append({
            "date": date,
            team_a: team_a_value,
            team_b: team_b_value,
        })

    if last_n is not None:
        matches = matches[:last_n]
        team_a_values = team_a_values[:last_n]
        team_b_values = team_b_values[:last_n]

    team_a_series = pd.Series(team_a_values)
    team_b_series = pd.Series(team_b_values)

    team_a_average = team_a_series.mean()
    team_b_average = team_b_series.mean()

    better_team = get_better_team(
        team_a,
        team_a_average,
        team_b,
        team_b_average,
        stat_name,
    )

    return {
        team_a: {
            "games": int(team_a_series.count()),
            "average": float(team_a_average),
        },
        team_b: {
            "games": int(team_b_series.count()),
            "average": float(team_b_average),
        },
        "better_team": better_team,
        "matches": matches,
    }


def format_db_comparison(comparison, stat_name):
    teams = [team for team in comparison.keys() if team not in ["better_team", "matches"]]

    team_a = teams[0]
    team_b = teams[1]

    team_a_data = comparison[team_a]
    team_b_data = comparison[team_b]

    lines = [
        f"{stat_name} comparison:",
        f"{team_a}: {team_a_data['average']:.2f} average over {team_a_data['games']} games",
        f"{team_b}: {team_b_data['average']:.2f} average over {team_b_data['games']} games",
    ]

    if comparison["better_team"] == "Tie":
        lines.append("Result: Tie")
    else:
        lines.append(f"Better team: {comparison['better_team']}")

    if "matches" in comparison:
        lines.append("")
        lines.append("Match-by-match:")

        for match in comparison["matches"]:
            lines.append(
                f"{match['date']}: {team_a} {match[team_a]} - {team_b} {match[team_b]}"
            )

    return "\n".join(lines)