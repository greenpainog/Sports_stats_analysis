import csv
import os


def match_exists(filename, match_data):
    if not os.path.isfile(filename):
        return False

    with open(filename, mode="r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            same_date = row["Date"] == match_data["date"]
            same_team1 = row["Team1"] == match_data["team1"]
            same_team2 = row["Team2"] == match_data["team2"]

            if same_date and same_team1 and same_team2:
                return True

    return False


def save_to_csv(match_data, filename="data/full_stats.csv"):
    if match_exists(filename, match_data):
        return False

    file_exists = os.path.isfile(filename)

    stats = match_data["stats"]

    headers = ["Date", "Team1", "Team2"]
    row = [
        match_data["date"],
        match_data["team1"],
        match_data["team2"],
    ]

    for stat_name, values in stats.items():
        headers.append(f"{stat_name}_1")
        headers.append(f"{stat_name}_2")

        row.append(values["team1"])
        row.append(values["team2"])

    with open(filename, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow(headers)

        writer.writerow(row)

    return True