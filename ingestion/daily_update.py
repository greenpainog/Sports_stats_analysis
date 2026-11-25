"""Daily update: load new Euroleague, EuroCup and Greek league games for the current season.

    python -m ingestion.daily_update

Run once a day (Windows Task Scheduler, see the README). Steps:
1. Take a lock (data/update.lock), so two runs never overlap and the
   one-request-per-second rule holds across both sources.
2. Back up the database to data/backups/ (the last 7 copies are kept).
3. Euroleague, then EuroCup (same API): download the season's game list
   again if it is older than 20 hours, then load finished games that aren't
   saved yet.
4. Greek league: re-read the round pages (only stale ones are downloaded)
   and load finished games that aren't saved yet.
5. Write a log to data/logs/update-<date>.log, including new Greek players
   that may be the same person as a Euroleague player (to review by hand).

Exit code 0 = all fine, 1 = some games failed, 2 = another run holds the lock.
"""
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

from database import db
from database.schema import create_tables
from ingestion import esake, euroleague

PROJECT_ROOT = db.PROJECT_ROOT
LOCK_FILE = PROJECT_ROOT / "data" / "update.lock"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
LOG_DIR = PROJECT_ROOT / "data" / "logs"
BACKUPS_KEPT = 7
STALE_LOCK_HOURS = 6  # a lock older than this was left by a run that crashed

log = logging.getLogger("daily_update")


def current_season(today=None):
    """Start year and esake name of the season in progress: 2026, '2026-27'.

    A season runs from autumn to the next summer, so from July on it is the
    new one.
    """
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return start, f"{start}-{str(start + 1)[2:]}"


class Locked(Exception):
    pass


def acquire_lock(path=LOCK_FILE, stale_hours=STALE_LOCK_HOURS):
    """Create the lock file, or raise Locked if a recent one exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > stale_hours * 3600:
        log.warning("Removing a stale lock from %s", datetime.fromtimestamp(path.stat().st_mtime))
        path.unlink()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Locked(f"{path} exists: another update is running.") from None
    os.write(fd, f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}".encode())
    os.close(fd)


def backup_database(source=None, backup_dir=BACKUP_DIR, keep=BACKUPS_KEPT):
    """Copy the database with SQLite's backup API (safe while it is open); keep the newest `keep`."""
    source = Path(source or db.DATABASE_FILE)
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{source.stem}-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    for old in sorted(backup_dir.glob(f"{source.stem}-*.db"))[:-keep]:
        old.unlink()
    return target


def update_euroleague(season_start, competition="E"):
    name = euroleague.COMPETITION_NAMES[competition]
    conn = db.get_connection()
    try:
        report = euroleague.ingest_new_games(competition, season_start, conn)
    finally:
        conn.close()
    log.info("%s %s: %d new game(s)", name, season_start, len(report["saved"]))
    for code, teams in report["saved"]:
        log.info("  saved game %s: %s", code, teams)
    for code, note in report["notes"]:
        log.info("  note game %s: %s", code, note)
    for code, why in report["waiting"]:
        log.info("  waiting game %s: %s", code, why)
    for code, error in report["failed"]:
        log.error("  FAILED game %s: %s", code, error)
    return len(report["failed"])


def update_greek_league(season_name):
    if season_name not in esake.CHAMPIONSHIPS:
        log.error("Greek league %s: no championship id in esake.CHAMPIONSHIPS; add it "
                  "(from the season list on www.esake.gr results pages).", season_name)
        return 1
    report = esake.ingest_season(season_name, verbose=False, only_new=True)
    log.info("Greek league %s: %d new game(s)", season_name, len(report["new"]))
    for idgame in report["new"]:
        log.info("  saved game %s", idgame)
    for name, other in report["linked"]:
        log.info("  linked new player %s -> %s (exact name)", name, other)
    seen = set()
    for name, esake_id, candidates in report["review"]:
        if esake_id not in seen:
            seen.add(esake_id)
            log.warning("  REVIEW new player %s (%s): similar to %s. Same person? Add to "
                        "MANUAL_PLAYER_LINKS in ingestion/esake.py.", name, esake_id, ", ".join(candidates))
    for idgame, error in report["failed"]:
        log.error("  FAILED game %s: %s", idgame, error)
    return len(report["failed"])


def run(today=None):
    season_start, season_name = current_season(today)
    log.info("Daily update for %s", season_name)
    backup = backup_database()
    log.info("Backed up the database to %s", backup.name)
    create_tables()  # adds tables/columns a newer loader needs; never deletes data
    failures = 0
    steps = (("Euroleague", update_euroleague, (season_start, "E")),
             ("EuroCup", update_euroleague, (season_start, "U")),
             ("Greek league", update_greek_league, (season_name,)))
    for number, (name, step, args) in enumerate(steps):
        if number == len(steps) - 1:
            time.sleep(euroleague.SECONDS_BETWEEN_REQUESTS)  # each loader times only its own requests
        try:
            failures += step(*args)
        except Exception:  # one league failing shouldn't stop the other
            log.exception("%s update failed", name)
            failures += 1
    log.info("Done: %d failure(s)", failures)
    return failures


def main():
    os.chdir(PROJECT_ROOT)  # the loaders save raw pages under data/raw/ relative to here
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / f"update-{date.today()}.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    try:
        acquire_lock()
    except Locked as exc:
        log.error("%s", exc)
        sys.exit(2)
    try:
        failures = run()
    finally:
        LOCK_FILE.unlink(missing_ok=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
