"""Daily update job: season, lock, backups, and which Euroleague games get loaded.

No requests are made: the Euroleague fetchers are replaced with fakes.
"""
import os
import sqlite3
import time
from datetime import date, datetime, timezone

import pytest

from ingestion import daily_update, euroleague


def test_current_season_turns_over_in_july():
    assert daily_update.current_season(date(2026, 11, 15)) == (2026, "2026-27")
    assert daily_update.current_season(date(2027, 5, 30)) == (2026, "2026-27")
    assert daily_update.current_season(date(2027, 7, 1)) == (2027, "2027-28")


def test_lock_blocks_a_second_run_but_not_after_a_crash(tmp_path):
    lock = tmp_path / "update.lock"
    daily_update.acquire_lock(lock)
    with pytest.raises(daily_update.Locked):
        daily_update.acquire_lock(lock)
    old = time.time() - 7 * 3600  # left behind by a run that crashed
    os.utime(lock, (old, old))
    daily_update.acquire_lock(lock)
    assert lock.exists()


def test_backup_keeps_the_newest_copies(tmp_path):
    source = tmp_path / "sports.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
    backups = tmp_path / "backups"
    for n in range(9):
        (backups / f"sports-20260101-00000{n}.db").parent.mkdir(exist_ok=True)
        (backups / f"sports-20260101-00000{n}.db").write_bytes(b"")
    target = daily_update.backup_database(source, backups, keep=7)
    assert len(list(backups.glob("sports-*.db"))) == 7
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == (1,)


NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _game(code, played, utc):
    return {"gameCode": code, "played": played, "utcDate": utc,
            "local": {"club": {"name": f"Home {code}"}}, "road": {"club": {"name": f"Away {code}"}}}


@pytest.fixture
def fake_euroleague(monkeypatch, tmp_path):
    games = {
        1: _game(1, True, "2026-11-13T19:00:00Z"),   # already saved
        2: _game(2, True, "2026-11-14T19:00:00Z"),   # new and finished
        3: _game(3, True, "2026-11-15T10:00:00Z"),   # started 2 hours ago
        4: _game(4, False, "2026-11-20T19:00:00Z"),  # not played
        5: _game(5, True, "2026-11-14T18:00:00Z"),   # header still live
    }
    loaded = []
    monkeypatch.setattr(euroleague, "RAW_DIR", tmp_path)
    monkeypatch.setattr(euroleague, "fetch_season_games", lambda *a, **k: games)
    monkeypatch.setattr(euroleague, "fetch_json", lambda kind, c, s, code, download=True: {"Live": code == 5})
    def fake_ingest_game(competition, season, gamecode):
        loaded.append(gamecode)
        return gamecode, ""

    monkeypatch.setattr(euroleague, "ingest_game", fake_ingest_game)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE games (source, source_game_id)")
    conn.execute("INSERT INTO games VALUES ('euroleague', 'E2026-1')")
    (tmp_path / "header_E2026_5.json").write_text("{}")
    return conn, loaded, tmp_path


def test_only_new_finished_games_are_loaded(fake_euroleague):
    conn, loaded, raw = fake_euroleague
    report = euroleague.ingest_new_games("E", 2026, conn, now=NOW)
    assert loaded == [2]
    assert [code for code, _ in report["saved"]] == [2]
    assert dict(report["waiting"]) == {3: "started less than 3 hours ago", 5: "header still says live"}
    assert not (raw / "header_E2026_5.json").exists()  # a live game's files are never kept
    assert report["failed"] == []


def test_game_list_is_downloaded_again_when_old(monkeypatch, tmp_path):
    path = tmp_path / "games.json"
    path.write_text('{"old": true}')
    calls = []

    class Response:
        text = '{"new": true}'

        def raise_for_status(self):
            pass

    monkeypatch.setattr(euroleague.requests, "get", lambda *a, **k: (calls.append(1), Response())[1])
    assert euroleague._cached_get(path, "url", max_age_hours=20) == {"old": True}  # fresh copy: no request
    old = time.time() - 21 * 3600
    os.utime(path, (old, old))
    assert euroleague._cached_get(path, "url", max_age_hours=20) == {"new": True}
    assert len(calls) == 1
