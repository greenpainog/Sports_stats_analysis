# Reference

The details behind the [README](../README.md): every endpoint and filter, the stats, the
database, and how each source is loaded and kept up to date.

## Commands

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

**The agent (`POST /agent`)** answers questions that need several lookups, and follow-ups:

```json
{"question": "Is Panathinaikos rebounding better in its last 6 games than over the season?"}
{"question": "And Zalgiris?", "conversation_id": "<from the first response>"}
```

The response has `answer`, `conversation_id`, `check` and `steps` (each tool call with its
arguments and result, nulls left out). `status` is `answered`, or `error` (503) when the OpenAI
call fails.
- **Tools** (`agent/tools.py`): `query_stats` (a `QuerySpec`, compare included), `team_rankings`,
  `matchup`, `player_rankings`, `player_profile`, `resolve_name` and `team_roster`. Each reuses
  the matching endpoint's Pydantic model, so a tool call is validated like a request. Problems come
  back to the LLM as text starting with `Error:`, so it can fix its call.
- **Graph** (`agent/graph.py`): agent → tools → agent while the LLM asks for tools, at most 6 tool
  calls per question. Past that, the LLM answers without tools from what it has.
- **Number check** (`agent/verify.py`):
  - Every number in the answer must be in a tool result or the question.
  - Seasons and dates are skipped. A difference counts only when both of its numbers are in the
    answer.
  - `check` is `passed`, `fixed` (passed after one retry) or `fallback` (the raw tool results are
    returned instead).
  - It doesn't check numbers written as words, or a real number attached to the wrong player.
- **Conversations:**
  - Kept in LangGraph's `InMemorySaver`, keyed by `conversation_id` (made for you when left out).
  - The LLM sees the last 5 questions.
  - They live in the server process: lost on restart, not shared between several servers, and not
    capped in number yet.
- **Compare sides must ask for different games** once the top-level filters are added. A `last_n`
  at the top level applies to every side, so a "season vs last 6" compare puts `last_n` only in
  its own side. This rule also applies to `POST /query` and `/ask`.

Evaluate the agent (real model, about 0.5M tokens per full run):

```powershell
python -m eval.run_agent_eval --check-gold   # no LLM: every reference query runs
python -m eval.run_agent_eval                # pass/fail per question, number check, tool calls, time
python -m eval.run_agent_eval --only a04,a12
```

Evaluate the parser on the question set with hand-written gold specs:

```powershell
python -m eval.run_eval --check-gold   # no LLM: every gold spec is valid and runs
python -m eval.run_eval                # accuracy overall and per category, plus failures
python -m eval.run_eval --file eval/heldout_questions.jsonl   # questions written separately from the prompt
```

To compare conditions for the same subject, add `compare` with 2-4 sides. Shared filters stay
at the top level, and each side lists only what differs:

```json
{"stats": ["turnovers"], "team": "Panathinaikos", "season": "2026-27",
 "compare": [{"label": "without Nunn", "without_player": "Nunn"},
             {"label": "with Nunn", "with_player": "Nunn"}]}
```

The response has the result for the shared filters plus `comparison`, one result per side.

`player_threshold` keeps only games where a player's own number in that game was in a range
(`min`/`max`, both inclusive). Games that player missed never count. It works for any per-game
stat and inside compare sides:

```json
{"stats": ["total_rebounds"], "team": "Panathinaikos",
 "player_threshold": {"player": "Lessort", "stat": "minutes", "min": 30}}
```
`against_player` keeps only games where that player played for the other team (`with_player`
counts either team, so teammates too). `seasons` takes several seasons at once, instead of `season`:

```json
{"stats": ["points"], "player": "Mitoglou", "against_player": "Milutinov",
 "seasons": ["2025-26", "2026-27"]}
```
`/ask` builds these from questions like "Panathinaikos turnovers when Nunn is out vs playing".

**Team rankings.** `POST /rankings` ranks every team on each stat (all 23 team stats when `stats`
is left out). `POST /matchup` puts two teams side by side with their league rank for every stat:

```json
{"team_a": "Panathinaikos", "team_b": "Virtus", "season": "2026-27"}
```
Filters: `season`, `phase`, `venue`, `result` (`won`/`lost`), `max_margin` (5 = games decided by
5 or fewer), `last_n` (each team's own last N), `per` (`game`/`total`), and `side: "opponent"` for
what teams allowed. Rank 1 is always the highest value, also for turnovers, fouls and points
allowed; teams with the same shown value share a rank.

Advanced team stats (`analysis/advanced.py`), computed from summed box-score totals:
`possessions`, `pace` (per 40 minutes, so overtime doesn't inflate it), `off_rating`,
`def_rating`, `net_rating` (per 100 possessions), `efg_pct`, `ts_pct`, `tov_pct`, `oreb_pct`,
`dreb_pct`, `ft_rate` (FTA/FGA), `fg3a_rate`, `ast_pct` (assisted share of made shots), `ast_to`,
`stl_pct` and `blk_pct`. Possessions = FGA − OREB + TOV + 0.44 × FTA, averaged over both teams.
`forced_turnovers` and `forced_tov_pct` are the opponents' turnovers (per game) and turnover %,
so "forced turnovers and steals" can be ranked together with side `team`.
Other sites use slightly different possession formulas, so their numbers can differ a little.

**Shot-list stats** (teams and players, per game): `paint_points`, `paint_fg_made`,
`paint_fg_attempted`, `paint_fg_pct`, `fastbreak_points`, `second_chance_points`,
`points_off_turnovers`, and `pts_from_paint_pct`. They come from each game's shot list (the
Euroleague "Points" endpoint). The paint is worked out from the shot coordinates (the FIBA paint,
4.9 m × 5.8 m), not from the source's zone letters, which don't line up with it. A game's shot list
must have the box score's made shots and free throws. 3 of 842 games don't, and they have no
shot-list stats. 10 more are missing one missed attempt, which only affects `paint_fg_attempted`.

**Player rankings.** `POST /player-rankings` ranks players on 47 stats; `POST /player-profile`
shows one player's value and rank in all of them (`{"player": "Nunn", "season": "2026-27"}`).
- Traditional: every box-score stat plus fg2/fg3/ft/fg %, win_pct; `per` is `game`, `total` or `per40`.
- Advanced: `ts_pct`, `efg_pct`, `usage_pct`, `ast_pct`, `oreb_pct`, `dreb_pct`, `reb_pct`, `tov_pct`,
  `stl_pct`, `blk_pct`, `ast_to`. Shares of team plays count only the time he was on the floor.
- Scoring: `pts_from_2pt_pct`, `pts_from_3pt_pct`, `pts_from_ft_pct`, `points_per_fga`, `fg3a_rate`,
  `ft_rate`, `team_pts_share`.
- Misc: `double_doubles`, `triple_doubles`, `starts`, `games_20_points`, `plus_minus` (for a team: its point margin, from the score), `fouls_received`, `pir`.

Only players with enough games are ranked: by default half the games of the most active player,
rounded up (`min_games` overrides it). Use `min_minutes` for shooting stats, or low-volume players
top them. Filters as for teams, plus `team`; `limit` (default 50) trims rows, not ranks.
`order: "lowest"` (teams and players) makes rank 1 the lowest value, e.g. fewest turnovers.

**Ranking questions in `/ask`.** "Where does Panathinaikos rank in FT made per game?", "who has the
best defense?", "top 5 scorers", "Panathinaikos vs Virtus full stats" and "where does Vezenkov rank
in every stat?" are turned into one of the four ranking requests above (`ranking_kind`,
`ranking_spec` and `ranking_result` in the response). "The most assists" means season totals;
"leads in"/"top scorers" means per game.

The database path defaults to `data/sports_analytics.db` in the project folder;
set the `SPORTS_DB_PATH` environment variable to use another file.

## Layout

| Path | What it holds |
| --- | --- |
| `database/` | SQLite connection, schema (`schema.py`), writes (`repositories.py`), reads (`queries.py`) |
| `analysis/` | Stat analysis over CSV and DB, plus the keyword question parser |
| `api/` | FastAPI app (`main.py`) and Pydantic models including `QuerySpec` (`models.py`) |
| `llm/` | Question → `QuerySpec` with an LLM (`question_parser.py`) |
| `agent/` | The `/agent` LangGraph agent: tools (`tools.py`), graph (`graph.py`), number check (`verify.py`) |
| `eval/` | Evaluation sets and scorers: `/ask` (`questions.jsonl`, `run_eval.py`) and the agent (`agent_questions.jsonl`, `run_agent_eval.py`) |
| `ingestion/` | Loads source data into the database (`euroleague.py`) |
| `tests/` | pytest suite |
| `ask.py`, `analysis_runner.py` | Older command-line entry points |
| `legacy/` | The original Flashscore scraper, no longer used |
| `Dockerfile`, `docker-compose.yml` | The API as a container (see the README's Quick start) |
| `data/` | Local database and CSV exports (git-ignored); raw API/HTML responses go in `data/raw/` |

## Database

The competition-aware tables (`competitions`, `seasons`, `teams`, `players`, `games`,
`game_appearances`, `game_stats`) are the source of truth going forward.
`database.repositories.save_game_bundle()` saves one game with all of its stats in a single
transaction, and re-saving the same game updates rows instead of duplicating them.
The older `matches` / `team_match_stats` tables remain so the current CLI keeps working.

**One player, two leagues.** `source_links` sends another source's ID to an existing row, so
Kendrick Nunn is one player with both his Euroleague and Greek league games. The Greek loader
links a player automatically only on an exact name match; spelling differences (Kostas /
Konstantinos Mitoglou) are listed for review and linked by hand in `ingestion/esake.py`
(`MANUAL_PLAYER_LINKS`). Panathinaikos and Olympiacos are linked by hand in `TEAMS`.

**Greek-letter names.** The 2024-25 and 2025-26 pages write most players only in Greek letters
("ΓΚΡΙΓΚΟΝΙΣ ΜΑΡΙΟΥΣ"). These are transliterated with ELOT 743, the Greek passport standard that
the Euroleague also uses for Greek players (`to_latin()`), so Greek players come out right. Foreign
players linked to the Euroleague (25 reviewed by club and dates) show their Euroleague name; 87
more have their real spelling in `MANUAL_NAMES`, worked out by hand. About 40 foreign players I
couldn't identify keep a phonetic spelling ("Tzoouns"). The phonetic spelling stays an alias, so
"Gaountlok" still finds Andrew Goudelock. A transliterated name never replaces a saved Latin spelling.

**Playoffs.** `--playoffs` also loads the second phase ("Β Φάση"), one page per series code
(QF1 = 201 … F5 = 405). Only quarterfinals, semifinals, the third-place game and the finals are
loaded, with `phase: "playoffs"`; the 2024-25 play-out (relegation round) and placement games are
left out.

**Which league.** Every query, ranking and `/ask` question has `competition`: `"euroleague"`
(the default), `"eurocup"` or `"greek_league"`, so Euroleague answers never include other games.
The Greek league has no plus-minus or shot-list stats, and asking for them is an error, not a 0.
Rest days count games in every league (a Sunday Greek game makes Tuesday's Euroleague game short
rest).

**EuroCup** (2025-26 with playoffs, and 2026-27): same API and same club and player codes as the
Euroleague, so Besiktas, or a player who moves up, is one row across both. Its knockout rounds
(8F, 4F, 2F, Final) are `phase: "playoffs"`. Aris, Panionios and PAOK also play in the Greek
league and are linked to their EuroCup rows like Panathinaikos and Olympiacos; loading the EuroCup
meant rebuilding the Greek rows from the saved pages so their players link too. The source gave
two players a second code mid-season; `PLAYER_CODE_FIXES` in `ingestion/euroleague.py` maps them
back.

## Data sources

- **Euroleague / EuroCup:** the [`euroleague-api`](https://github.com/giasemidis/euroleague_api) package.
- **Greek league (Stoiximan GBL):** esake.gr game pages, parsed with the standard library's
  `html.parser` (`python -m ingestion.esake --season 2026-27`). esake.gr has no robots.txt; its
  terms (read 2026-11-15) allow copies **strictly for personal use** with the source cited, and
  need ESAKE's written permission for any reproduction or transmission. So the Greek data stays
  local; a public demo needs permission or Euroleague data only. A round page is downloaded again
  only while it lists a game that should have finished but has no score; a game counts as
  finished 3 hours after its start.

**Daily update.** `python -m ingestion.daily_update` loads the current season's new games from all three
leagues (Euroleague, EuroCup, Greek league) in one run: it takes a lock (`data/update.lock`, so two runs never overlap), backs up the
database to `data/backups/` (last 7 kept), downloads the Euroleague game list again if the saved
copy is over 20 hours old, and loads only games that are finished and not saved yet. A game counts
as finished 3 hours after its start; a Euroleague game whose header still says live is not cached
and is tried the next day. A quiet day costs 2-3 requests. Each run logs to
`data/logs/update-<date>.log`, including new Greek players that look like a Euroleague player
(`REVIEW` lines: link them in `MANUAL_PLAYER_LINKS` if they are the same person). Exit code 1 means
some games failed, 2 that another run was in progress. To run it every day at 10:00 (or as soon as
the PC is on), run `.\scripts\register_daily_update.ps1` once in PowerShell. The Greek playoffs and
a new season's esake championship id still need adding by hand.

Raw responses are saved under `data/raw/`, each page is fetched once, and requests are at
least one second apart. A response is only cached if it is valid JSON, and it is written to a
temporary file first. Tests load only cached files (`download=False`) and never make requests.
Data files, API keys and `.env` are never committed.
