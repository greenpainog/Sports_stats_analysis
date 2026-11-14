from legacy.config import STATS_TO_EXTRACT
from legacy.scraper.stats_extractor import extract_stats



def extract_match_data(page, link):
    page.goto(link)

    # Cookies
    try:
        page.click("text=Accept", timeout=5000)
    except:
        pass

    page.wait_for_timeout(1500)

    # --- Stats tab ---
    tabs = page.locator('[role="tab"]')

    for i in range(tabs.count()):
        try:
            tab_text = tabs.nth(i).inner_text().strip().lower()

            if tab_text in ["stats", "statistics", "στατιστικά"]:
                tabs.nth(i).click()
                break
        except:
            continue

    page.wait_for_timeout(1000)
    page.wait_for_selector('[data-testid="wcl-statistics"]', timeout=10000)

        # Teams
    home = page.locator('[class*="duelParticipant__home"] [class*="participant__participantName"]').first.inner_text().strip()
    away = page.locator('[class*="duelParticipant__away"] [class*="participant__participantName"]').first.inner_text().strip()

    # Date
    date = page.locator('[class*="duelParticipant__startTime"]').inner_text().strip()

    return {
        "date": date,
        "team1": home,
        "team2": away,
        "stats": extract_stats(page, STATS_TO_EXTRACT),
    }
