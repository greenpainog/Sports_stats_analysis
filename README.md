# European basketball stats Q&A

Ask natural-language questions about Euroleague, EuroCup and the Greek league and get an
answer plus the numbers behind it, for example:

- "Nunn's 3-point percentage in his last 5 games"
- "Jerian Grant's 3P% against Real Madrid away"
- "Panathinaikos rebounds vs Zalgiris away when Valanciunas isn't playing"

**Status:** early. The database schema is in place and Euroleague ingestion is being built.
Today the CLI answers simple team-stat comparisons over a small legacy dataset.

## Setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python init_db.py        # creates any missing tables; never deletes data
```

## Usage

```powershell
python ask.py "Olympiacos turnovers vs Panathinaikos last 5"
python ask.py "Olympiacos assists vs Panathinaikos" --source db
python analysis_runner.py "Total rebounds" --team-a Olympiacos --team-b Panathinaikos --last-n 5
python analysis_runner.py --list-stats
```

## Layout

| Path | What it holds |
| --- | --- |
| `database/` | SQLite connection, schema (`schema.py`), writes (`repositories.py`), reads (`queries.py`) |
| `analysis/` | Stat analysis over CSV and DB, plus the keyword question parser |
| `ask.py`, `analysis_runner.py` | Command-line entry points |
| `legacy/` | The original Flashscore scraper, no longer used |
| `data/` | Local database and CSV exports (git-ignored); raw API/HTML responses go in `data/raw/` |

## Database

The competition-aware tables (`competitions`, `seasons`, `teams`, `players`, `games`,
`game_appearances`, `game_stats`) are the source of truth going forward.
`database.repositories.save_game_bundle()` saves one game with all of its stats in a single
transaction, and re-saving the same game updates rows instead of duplicating them.
The older `matches` / `team_match_stats` tables remain so the current CLI keeps working.

## Data sources

- **Euroleague / EuroCup:** the [`euroleague-api`](https://github.com/giasemidis/euroleague_api) package.
- **Greek league:** planned, via a polite scraper of esake.gr.

Raw responses are saved under `data/raw/`, each page is fetched once, and requests are at
least one second apart. Data files, API keys and `.env` are never committed.

## Roadmap

0. Housekeeping ✅
1. Euroleague ingestion (one full season)
2. Team/player alias tables and name resolution
3. Query layer (opponent, home/away, last N, opponent over .500, player absent)
4. FastAPI + Pydantic `QuerySpec` API
5. LLM question → validated `QuerySpec`, with a 50-question evaluation set
6. Greek league ingestion
7. Docker, demo, agent for multi-step questions, Azure deployment
