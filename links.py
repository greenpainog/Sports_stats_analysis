from config import URL

def get_match_links(page, limit=10):
    print("Collecting match links...")

    page.goto(URL)
    # Handle cookie popup on RESULTS page
    try:
        page.click("text=Accept", timeout=5000)
        print("Accepted cookies (results page)")
    except:
        print("No cookie popup (results page)")
    # Force lazy-loaded matches to appear
    page.mouse.wheel(0, 5000)
    page.wait_for_timeout(2000)
    # ✅ wait for matches to actually load
    # Wait for match rows to appear instead of networkidle
    page.wait_for_selector('.event__match', timeout=10000)
    page.wait_for_timeout(2000)
    rows = page.locator('.event__match')

    links = []

    for i in range(min(rows.count(), limit)):
        try:
            row = rows.nth(i)

            link = row.locator("a").first.get_attribute("href")

            if link:
                if link.startswith("http"):
                    full_link = link
                else:
                    full_link = "https://www.flashscore.com" + link

                links.append(full_link)

        except:
            continue

    print(f"Collected {len(links)} match links")
    return links
