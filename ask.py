import argparse

from analysis.natural_language_parser import parse_team_query
from analysis.query_engine import run_team_query


def run():
    parser = argparse.ArgumentParser(description="Ask a sports analytics question.")
    parser.add_argument("question", help="Question to answer")
    parser.add_argument("--file", default="data/full_stats.csv", help="CSV file to analyze")
    parser.add_argument("--source", choices=["csv", "db"], default="csv", help="Data source to analyze")

    args = parser.parse_args()

    query = parse_team_query(
    args.question,
    file=args.file,
    source=args.source,
)

    if not query["stat_name"]:
        raise ValueError("Could not find a known stat in the question.")

    result = run_team_query(
        stat_name=query["stat_name"],
        team_a=query["team_a"],
        team_b=query["team_b"],
        head_to_head=query["head_to_head"],
        last_n=query["last_n"],
        file=args.file,
        source=args.source,
    )

    print(result)


if __name__ == "__main__":
    run()