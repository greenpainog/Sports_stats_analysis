import csv 
import os

from links import get_match_links

from playwright.sync_api import sync_playwright

URL = "https://www.flashscore.com/team/olympiacos-piraeus/ptNXXDf6/results/"

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        print("Opening page...")
        links = get_match_links(page)
        
        for link in links:
            print(f"Processing: {link}")

            page.goto(link)
            # Handle cookie popup EARLY
            try:
                page.click("text=Accept", timeout=5000)
                print("Accepted cookies")
            except:
                print("No cookie popup")

            page.wait_for_timeout(4000)


            # --- Find correct Stats tab ---
            print("Looking for correct Stats tab...")

            tabs = page.locator('[role="tab"]')
            count = tabs.count()

            for i in range(count):
                try:
                    tab_text = tabs.nth(i).inner_text().strip().lower()
                    print(f"Found tab: {tab_text}")

                    if tab_text in ["stats", "statistics", "στατιστικά"]:
                        print(f"Clicking: {tab_text}")
                        tabs.nth(i).click()
                        break

                except:
                    continue

            
            # ✅ WAIT for stats to load (VERY IMPORTANT)
            page.wait_for_timeout(3000)
            # ✅ WAIT for stats container to appear
            page.wait_for_selector('[data-testid="wcl-statistics"]', timeout=10000)
            page.wait_for_timeout(2000)
            print("Extracting TOTAL rebounds...")

            rows = page.locator('[data-testid="wcl-statistics"] >> div')

            rebounds_found = False

            for i in range(rows.count()):
                try:
                    row = rows.nth(i)
                    text = row.inner_text().strip().lower()

                    # ✅ ONLY total rebounds
                    if "total rebounds" in text or "σύνολο" in text:
                        print(f"Found row: {text}")

                        values = row.locator("div")

                        vals = []
                        for j in range(values.count()):
                            val = values.nth(j).inner_text().strip()
                            if val.isdigit():
                                vals.append(val)

                        if len(vals) >= 2:
                            print(f"🏀 TOTAL Rebounds: {vals[0]} - {vals[1]}")
                            reb1 = vals[0]
                            reb2 = vals[1]

                            # Temporary team names (we'll replace in Step 2)
                            team1 = "Olympiacos"
                            team2 = "Panathinaikos"

                            # extracting team names (FIXED)
                            print("About to extract teams...")

                            print("Extracting team names...")

                            home_team_locator = page.locator('[class*="duelParticipant__home"] [class*="participant__participantName"]')
                            away_team_locator = page.locator('[class*="duelParticipant__away"] [class*="participant__participantName"]')

                            team1 = home_team_locator.first.inner_text().strip()
                            team2 = away_team_locator.first.inner_text().strip()

                            print(f"Teams: {team1} vs {team2}")

                            #extract match date
                            print("Extracting match date...")

                            date_locator = page.locator('[class*="duelParticipant__startTime"]')

                            date = date_locator.inner_text().strip()

                            print(f"Date: {date}")

                            #file extraction

                            file_exists = os.path.isfile("rebounds_data.csv")

                            with open("rebounds_data.csv", mode="a", newline="", encoding="utf-8") as file:
                                writer = csv.writer(file)

                                if not file_exists:
                                    writer.writerow(["Date", "Team1", "Team2", "Reb1", "Reb2"])
                                
                                writer.writerow([date, team1, team2, reb1, reb2])
                            print("✅ Data saved to CSV")
                            rebounds_found = True
                            break

                except:
                    continue
        
            if not rebounds_found:
                print("❌ Total rebounds not found")
        

            
    
    input("Press ENTER to close...")
    

run()