from analysis.stat_analysis import (
    analyze_stat,
    compare_teams,
    compare_teams_head_to_head,
    format_comparison,
)

from analysis.db_stat_analysis import (
    analyze_db_stat,
    compare_db_teams,
    compare_db_teams_head_to_head,
    format_db_comparison,
)


def run_team_query(
    stat_name,
    team_a=None,
    team_b=None,
    head_to_head=False,
    last_n=None,
    file="data/full_stats.csv",
    source="csv",
):
    if source == "db":
        if team_a and team_b:
            if head_to_head:
                comparison = compare_db_teams_head_to_head(
                    team_a,
                    team_b,
                    stat_name,
                    last_n=last_n,
                )
            else:
                comparison = compare_db_teams(
                    team_a,
                    team_b,
                    stat_name,
                    last_n=last_n,
                )

            return format_db_comparison(comparison, stat_name)

        return analyze_db_stat(stat_name)

    if team_a and team_b:
        if head_to_head:
            comparison = compare_teams_head_to_head(
                team_a,
                team_b,
                stat_name,
                file=file,
                last_n=last_n,
            )
        else:
            comparison = compare_teams(
                team_a,
                team_b,
                stat_name,
                file=file,
                last_n=last_n,
            )

        return format_comparison(comparison, stat_name)

    return analyze_stat(file=file, stat_name=stat_name)