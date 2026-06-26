from analysis.stat_analysis import analyze_stat


def analyze_rebounds(file="data/full_stats.csv"):
    return analyze_stat(file=file, stat_name="Total rebounds")