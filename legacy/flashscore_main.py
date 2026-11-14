"""Legacy Flashscore scraper. Not used any more; kept for reference.

Run from the project root with: python -m legacy.flashscore_main
"""
from playwright.sync_api import sync_playwright
from legacy.config import HEADLESS
from legacy.scraper.match_links import get_match_links
from legacy.scraper.match_data import extract_match_data
from legacy.utils.csv_writer import save_to_csv
from database.repositories import save_match_data


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page()

        print("Opening page...")
        links = get_match_links(page)

        for link in links:
            print(f"Processing: {link}")

            data = extract_match_data(page, link)

            if data:
                csv_saved = save_to_csv(data)
                save_match_data(data)

                if csv_saved:
                    print("Saved to CSV and database")
                else:
                    print("Already existed in CSV; saved/updated database")

        browser.close()


if __name__ == "__main__":
    run()

