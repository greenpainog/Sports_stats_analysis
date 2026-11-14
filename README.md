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
copy .env.example .env   # then put your OpenAI key in .env (only needed for /ask and the eval)
```

## Usage

```powershell
python ask.py "Olympiacos turnovers vs Panathinaikos last 5"
python ask.py "Olympiacos assists vs Panathinaikos" --source db
python analysis_runner.py "Total rebounds" --team-a Olympiacos --team-b Panathinaikos --last-n 5
python analysis_runner.py --list-stats

# Load one Euroleague game (season = start year). Responses are cached in data/raw/.
python -m ingestion.euroleague --season 2024 --game 1
python -m ingestion.euroleague --season 2024    # whole season (~11 min the first time)

# Run the tests
python -m pytest

# Start the API, then open http://127.0.0.1:8000/docs
uvicorn api.main:app --reload
```

Example API request (`POST /query`):

```json
{"stats": ["fg3_pct"], "player": "Jerian Grant", "opponent": "Real Madrid", "venue": "away"}
```

Plain-language questions (`POST /ask`) need an OpenAI key in `.env`:

```json
{"question": "Jerian Grant's 3P% against Real Madrid away"}
```

The LLM only fills in a `QuerySpec` (OpenAI Structured Outputs). The spec is validated by
Pydantic, with one retry that sends the errors back to the model, and then runs exactly like
`POST /query`. The LLM never writes SQL. The response `status` is `answered`, `unanswerable`,
`needs_clarification` (unknown or ambiguous name, with candidates), `invalid`, or `error` (503).

Evaluate the parser on 50 questions with hand-written gold specs:

```powershell
python -m eval.run_eval --check-gold   # no LLM: every gold spec is valid and runs
python -m eval.run_eval                # accuracy overall and per category, plus failures
```

The database path defaults to `data/sports_analytics.db` in the project folder;
set the `SPORTS_DB_PATH` environment variable to use another file.

## Layout

| Path | What it holds |
| --- | --- |
| `database/` | SQLite connection, schema (`schema.py`), writes (`repositories.py`), reads (`queries.py`) |
| `analysis/` | Stat analysis over CSV and DB, plus the keyword question parser |
| `api/` | FastAPI app (`main.py`) and Pydantic models including `QuerySpec` (`models.py`) |
| `llm/` | Question → `QuerySpec` with an LLM (`question_parser.py`) |
| `eval/` | 50-question evaluation set (`questions.jsonl`) and scorer (`run_eval.py`) |
| `ingestion/` | Loads source data into the database (`euroleague.py`) |
| `tests/` | pytest suite |
| `ask.py`, `analysis_runner.py` | Older command-line entry points |
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
1. Euroleague ingestion ✅ 2024-25 (330 games), 2025-26 (402) and 2026-27 so far
2. Team/player alias tables and name resolution ✅ `database.aliases.resolve_team()` / `resolve_player()`
3. Query layer (opponent, home/away, last N, opponent over .500, player absent) ✅ `analysis.stat_query.query_stats()`
4. FastAPI + Pydantic `QuerySpec` API ✅ `api/` (`POST /query`, `GET /resolve/{kind}`, `GET /stats`)
5. LLM question → validated `QuerySpec`, with a 50-question evaluation set ✅ `POST /ask`, `eval/`: 50/50 with gpt-6-luna on 2026-11-14 (3 runs)
6. Greek league ingestion
7. Docker, demo, agent for multi-step questions, Azure deployment
