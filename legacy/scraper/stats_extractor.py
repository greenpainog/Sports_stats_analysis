def extract_stat(page, stat_name):
    rows = page.locator('[data-testid="wcl-statistics"] >> div')

    for i in range(rows.count()):
        row = rows.nth(i)
        text = row.inner_text().strip().lower()

        if stat_name.lower() in text:
            values = row.locator("div")

            nums = []
            for j in range(values.count()):
                val = values.nth(j).inner_text().strip()
                if val.isdigit():
                    nums.append(val)

            if len(nums) >= 2:
                return nums[0], nums[1]

    return None, None


def extract_stats(page, stat_names):
    wanted_stats = {stat_name.lower(): stat_name for stat_name in stat_names}
    results = {
        stat_name: {
            "team1": None,
            "team2": None,
        }
        for stat_name in stat_names
    }

    rows = page.locator('[data-testid="wcl-statistics"] >> div')

    for i in range(rows.count()):
        row = rows.nth(i)
        text = row.inner_text().strip().lower()

        matched_stat = None

        for stat_key, original_stat_name in wanted_stats.items():
            if stat_key in text:
                matched_stat = original_stat_name
                break

        if not matched_stat:
            continue

        values = row.locator("div")

        nums = []
        for j in range(values.count()):
            val = values.nth(j).inner_text().strip()
            if val.isdigit():
                nums.append(val)

        if len(nums) >= 2:
            results[matched_stat] = {
                "team1": nums[0],
                "team2": nums[1],
            }

    return results