from legacy.config import MATCH_LIMIT_PER_TEAM, TEAM_RESULT_URLS


BASE_URL = "https://www.flashscore.com"


def normalize_match_url(link):
    if link.startswith("http"):
        return link

    return BASE_URL + link


def collect_match_links_from_team(page, team_name, url, limit):
    print(f"Collecting match links for {team_name}...")

    page.goto(url)

    try:
        page.click("text=Accept", timeout=5000)
        print("Accepted cookies")
    except:
        print("No cookie popup")

    page.mouse.wheel(0, 5000)
    page.wait_for_timeout(1000)

    page.wait_for_selector(".event__match", timeout=10000)
    page.wait_for_timeout(1000)

    rows = page.locator(".event__match")
    links = []

    for i in range(min(rows.count(), limit)):
        try:
            row = rows.nth(i)
            link = row.locator("a").first.get_attribute("href")

            if link:
                links.append(normalize_match_url(link))

        except:
            continue

    print(f"Collected {len(links)} links for {team_name}")
    return links


def get_match_links(page, limit=MATCH_LIMIT_PER_TEAM):
    all_links = []

    for team_name, url in TEAM_RESULT_URLS.items():
        team_links = collect_match_links_from_team(page, team_name, url, limit)
        all_links.extend(team_links)

    unique_links = list(dict.fromkeys(all_links))

    print(f"Collected {len(unique_links)} unique match links")
    return unique_links