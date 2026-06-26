import pandas as pd


DEFAULT_FILE = "data/full_stats.csv"

LOWER_IS_BETTER_STATS = {
    "Turnovers",
}


def get_better_team(team_a, team_a_average, team_b, team_b_average, stat_name):
    if team_a_average == team_b_average:
        return "Tie"

    if stat_name in LOWER_IS_BETTER_STATS:
        return team_a if team_a_average < team_b_average else team_b

    return team_a if team_a_average > team_b_average else team_b

def load_matches(file=DEFAULT_FILE):
    df = pd.read_csv(file)

    df["ParsedDate"] = pd.to_datetime(
        df["Date"],
        format="%d.%m.%Y %H:%M",
        errors="coerce",
    )

    df = df.sort_values("ParsedDate", ascending=False)

    return df

def get_available_stats(file=DEFAULT_FILE):
    df = load_matches(file)

    stats = [
        column[:-2]
        for column in df.columns
        if column.endswith("_1")
    ]

    return stats

def get_available_teams(file=DEFAULT_FILE):
    df = load_matches(file)

    teams = pd.concat([df["Team1"], df["Team2"]])
    teams = teams.dropna().drop_duplicates().sort_values()

    return teams.tolist()

def _find_stat_columns(df, stat_name):
    team1_col = f"{stat_name}_1"
    team2_col = f"{stat_name}_2"

    if team1_col in df.columns and team2_col in df.columns:
        return team1_col, team2_col

    available_stats = [
        column[:-2]
        for column in df.columns
        if column.endswith("_1")
    ]

    raise ValueError(
        f"Stat '{stat_name}' was not found. "
        f"Available stats: {available_stats}"
    )


def analyze_stat(file=DEFAULT_FILE, stat_name=None):
    if not stat_name:
        raise ValueError("You must provide a stat_name.")

    df = load_matches(file)

    team1_col, team2_col = _find_stat_columns(df, stat_name)

    df[team1_col] = pd.to_numeric(df[team1_col], errors="coerce")
    df[team2_col] = pd.to_numeric(df[team2_col], errors="coerce")

    team_rows = []

    for _, row in df.iterrows():
        team_rows.append({
            "Team": row["Team1"],
            "Value": row[team1_col],
        })

        team_rows.append({
            "Team": row["Team2"],
            "Value": row[team2_col],
        })

    teams_df = pd.DataFrame(team_rows)

    result = (
        teams_df
        .groupby("Team")
        .agg(
            Games=("Value", "count"),
            Average=("Value", "mean"),
        )
        .reset_index()
        .sort_values("Average", ascending=False)
    )

    return result


def compare_teams(team_a, team_b, stat_name, file=DEFAULT_FILE, last_n=None):
    df = load_matches(file)

    team1_col, team2_col = _find_stat_columns(df, stat_name)

    df[team1_col] = pd.to_numeric(df[team1_col], errors="coerce")
    df[team2_col] = pd.to_numeric(df[team2_col], errors="coerce")

    def get_team_values(team_name):
        team_matches = df[(df["Team1"] == team_name) | (df["Team2"] == team_name)]

        if last_n is not None:
            team_matches = team_matches.head(last_n)

        values = []

        for _, row in team_matches.iterrows():
            if row["Team1"] == team_name:
                values.append(row[team1_col])
            else:
                values.append(row[team2_col])

        return pd.Series(values)

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


def compare_teams_head_to_head(team_a, team_b, stat_name, file=DEFAULT_FILE, last_n= None):
    df = load_matches(file)

    team1_col, team2_col = _find_stat_columns(df, stat_name)

    df[team1_col] = pd.to_numeric(df[team1_col], errors="coerce")
    df[team2_col] = pd.to_numeric(df[team2_col], errors="coerce")

    head_to_head_df = df[
        ((df["Team1"] == team_a) & (df["Team2"] == team_b)) |
        ((df["Team1"] == team_b) & (df["Team2"] == team_a))
    ]

    if head_to_head_df.empty:
        raise ValueError(f"No head-to-head matches found between {team_a} and {team_b}.")

    if last_n is not None:
        head_to_head_df = head_to_head_df.head(last_n)    

    team_a_values = []
    team_b_values = []
    games = []

    for _, row in head_to_head_df.iterrows():
        if row["Team1"] == team_a:
            team_a_value = row[team1_col]
            team_b_value = row[team2_col]
        else:
            team_a_value = row[team2_col]
            team_b_value = row[team1_col]

        team_a_values.append(team_a_value)
        team_b_values.append(team_b_value)

        games.append({
            "date": row["Date"],
            team_a: team_a_value,
            team_b: team_b_value,
        })

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
        "matches": games,
    }

def format_comparison(comparison, stat_name):
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