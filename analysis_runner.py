import argparse

from analysis.query_engine import run_team_query
from analysis.stat_analysis import get_available_stats, get_available_teams


def run():
    parser = argparse.ArgumentParser(description="Analyze basketball team stats.")

    parser.add_argument("stat_name", nargs="?", help="Stat to analyze, e.g. 'Total rebounds'")
    parser.add_argument("--team-a", help="First team to compare")
    parser.add_argument("--team-b", help="Second team to compare")
    parser.add_argument("--head-to-head", action="store_true", help="Compare only matches between the two teams")
    parser.add_argument("--last-n", type=int, help="Use only the latest N matching games")
    parser.add_argument("--file", default="data/full_stats.csv", help="CSV file to analyze")
    parser.add_argument("--source", choices=["csv", "db"], default="csv", help="Data source to analyze")
    parser.add_argument("--list-stats", action="store_true", help="List available stats")
    parser.add_argument("--list-teams", action="store_true", help="List available teams")
    args = parser.parse_args()

    if args.list_stats:
        print("\nAvailable stats:\n")
        for stat in get_available_stats(args.file):
            print(f"- {stat}")
        return
    
    if args.list_teams:
        print("\nAvailable teams:\n")
        for team in get_available_teams(args.file):
            print(f"- {team}")
        return 

    if not args.stat_name:
        raise ValueError("Please provide a stat name or use --list-stats.")

    

    result = run_team_query(
    stat_name=args.stat_name,
    team_a=args.team_a,
    team_b=args.team_b,
    head_to_head=args.head_to_head,
    last_n=args.last_n,
    file=args.file,
    source=args.source,
    )

    print(result)

if __name__ == "__main__":
    run()