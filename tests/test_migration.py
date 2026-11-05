"""Production-data migration test.

Builds a database in the live schema (no players.pool, no night tables):
6 named pot players with tokens and tie-breaks, the 10 real questions, and
60 predictions (every player, every question). Then starts the app.

Asserts those players, tokens, tie-breaks, question rows, and prediction
values are byte-identical afterwards. Startup may add columns and empty
tables. It must not reseed, rename, re-token, or rewrite stored answers.

The DROP SCHEMA below runs only in this throwaway test database so the
fixture can be loaded. Application startup never drops the live schema.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
import db as D  # noqa: E402  does not connect or seed

PLAYERS = [
    (1, "Alex", "tok-alex-keep-9f3a", 1, "2026-10-02T08:00:01+13:00", 1, "36.1"),
    (2, "Blair", "tok-blair-keep-9f3a", 2, "2026-10-02T08:00:02+13:00", 1, "35.4"),
    (3, "Casey", "tok-casey-keep-9f3a", 3, "2026-10-02T08:00:03+13:00", 1, "34.0"),
    (4, "Drew", "tok-drew-keep-9f3a", 4, "2026-10-02T08:00:04+13:00", 1, "37.8"),
    (5, "Eden", "tok-eden-keep-9f3a", 5, "2026-10-02T08:00:05+13:00", 1, "33.3"),
    (6, "Frankie", "tok-frankie-keep-9f3a", 6, "2026-10-02T08:00:06+13:00", 1, "38.6"),
]

SETTINGS = {
    "event_title": "Saturday night — keep this title",
    "lock_at": "2026-11-07T19:00:00+13:00",
    "entries_open": "1",
    "board_mode": "live",
    "demo_mode": "0",
    "feed_enabled": "0",
    "feed_url": "https://media.election.net.nz/xml/",
    "feed_poll_seconds": "90",
    "reporting_pct": "12.5",
    "prize_first": "100",
    "prize_second": "20",
    "entry_fee": "20",
}

QUESTIONS = []
PREDICTIONS = []
for qid, q in enumerate(D.DEFAULT_QUESTIONS, start=1):
    meta = json.dumps(q.get("meta") or {})
    QUESTIONS.append((qid, q["key"], q["label"], q["scoring_type"], q["max_points"], q["sort_order"], meta, 1))
for pid, _name, _token, _slot, _created, _active, _tb in PLAYERS:
    for qid, q in enumerate(D.DEFAULT_QUESTIONS, start=1):
        choices = q["meta"]["choices"]
        value = choices[(pid + qid) % len(choices)]
        updated = f"2026-10-20T18:{pid:02d}:{qid:02d}+13:00"
        PREDICTIONS.append((pid, qid, value, updated))
assert len(PREDICTIONS) == 60


def _insert(conn, placeholder: str) -> None:
    ph = placeholder
    cur = conn if hasattr(conn, "executemany") else conn.cursor()
    for key, value in SETTINGS.items():
        cur.execute(f"INSERT INTO settings(key, value) VALUES ({ph}, {ph})", (key, value))
    cur.executemany(
        f"INSERT INTO players(id, name, token, slot, created_at, active, tb_national_vote) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
        PLAYERS,
    )
    cur.executemany(
        f"""INSERT INTO questions(id, key, label, scoring_type, max_points, sort_order, meta_json, active)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})""",
        QUESTIONS,
    )
    cur.executemany(
        f"INSERT INTO predictions(player_id, question_id, value, updated_at) VALUES ({ph},{ph},{ph},{ph})",
        PREDICTIONS,
    )
    cur.execute(
        f"INSERT INTO results(question_id, value, source, updated_at) VALUES ({ph},{ph},{ph},{ph})",
        (1, "35.1", "manual", "2026-10-21T09:00:00+13:00"),
    )


def _sqlite_legacy(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if Path(path).exists():
        Path(path).unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                slot INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                tb_national_vote TEXT
            );
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                scoring_type TEXT NOT NULL,
                max_points INTEGER NOT NULL DEFAULT 10,
                sort_order INTEGER NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE predictions (
                player_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                value TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (player_id, question_id)
            );
            CREATE TABLE results (
                question_id INTEGER PRIMARY KEY,
                value TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                updated_at TEXT NOT NULL
            );
            """
        )
        _insert(conn, "?")
        conn.commit()
    finally:
        conn.close()


def _postgres_legacy(url: str) -> None:
    import psycopg

    conn = psycopg.connect(url, autocommit=True)
    try:
        # Throwaway test database only. The app itself never drops public.
        conn.execute("DROP SCHEMA IF EXISTS demo CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE players (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                slot INTEGER NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                tb_national_vote TEXT
            );
            CREATE TABLE questions (
                id SERIAL PRIMARY KEY,
                key TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                scoring_type TEXT NOT NULL,
                max_points INTEGER NOT NULL DEFAULT 10,
                sort_order INTEGER NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE predictions (
                player_id INTEGER NOT NULL REFERENCES players(id),
                question_id INTEGER NOT NULL REFERENCES questions(id),
                value TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (player_id, question_id)
            );
            CREATE TABLE results (
                question_id INTEGER PRIMARY KEY REFERENCES questions(id),
                value TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                updated_at TEXT NOT NULL
            );
            """
        )
        _insert(conn, "%s")
        conn.execute("SELECT setval(pg_get_serial_sequence('players','id'), (SELECT MAX(id) FROM players))")
        conn.execute("SELECT setval(pg_get_serial_sequence('questions','id'), (SELECT MAX(id) FROM questions))")
    finally:
        conn.close()


def _prepare() -> None:
    os.environ.setdefault("SECRET_KEY", "test-secret")
    os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        _postgres_legacy(url)
        return
    if not (os.environ.get("DB_PATH") or "").strip():
        os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-mig-")) / "legacy.db")
    _sqlite_legacy(os.environ["DB_PATH"])


def _snapshot(conn) -> dict:
    players = [
        (r["id"], r["name"], r["token"], r["slot"], r["created_at"], r["active"], r["tb_national_vote"])
        for r in conn.execute(
            "SELECT id, name, token, slot, created_at, active, tb_national_vote FROM players ORDER BY id"
        )
    ]
    pools = [r["pool"] for r in conn.execute("SELECT pool FROM players ORDER BY id")]
    questions = [
        (r["id"], r["key"], r["label"], r["scoring_type"], r["max_points"], r["sort_order"], r["meta_json"], r["active"])
        for r in conn.execute(
            "SELECT id, key, label, scoring_type, max_points, sort_order, meta_json, active FROM questions ORDER BY id"
        )
    ]
    preds = [
        (r["player_id"], r["question_id"], r["value"], r["updated_at"])
        for r in conn.execute(
            "SELECT player_id, question_id, value, updated_at FROM predictions ORDER BY player_id, question_id"
        )
    ]
    result = conn.execute("SELECT question_id, value, source, updated_at FROM results WHERE question_id=1").fetchone()
    settings = {key: D.get_setting(conn, key) for key in SETTINGS}
    return {
        "players": players,
        "pools": pools,
        "questions": questions,
        "preds": preds,
        "result": (result["question_id"], result["value"], result["source"], result["updated_at"]),
        "settings": settings,
    }


def _assert_prod_bytes(snap: dict) -> None:
    assert snap["players"] == [
        (pid, name, token, slot, created, active, tb)
        for pid, name, token, slot, created, active, tb in PLAYERS
    ]
    assert snap["questions"] == QUESTIONS
    assert snap["preds"] == PREDICTIONS
    assert len(snap["preds"]) == 60
    assert snap["result"] == (1, "35.1", "manual", "2026-10-21T09:00:00+13:00")
    assert snap["settings"] == SETTINGS
    # A stored band string is still the exact option text, not a rewritten label.
    assert any(value == "30\u201333.9" for (_p, _q, value, _u) in snap["preds"])
    assert any(value == "under 5% (out)" for (_p, _q, value, _u) in snap["preds"])


def _assert_migrated() -> None:
    conn = D.connect(False)
    try:
        first = _snapshot(conn)
    finally:
        conn.close()
    _assert_prod_bytes(first)
    assert first["pools"] == ["pot"] * 6
    assert len(first["players"]) == 6

    D.init_db(False)
    conn = D.connect(False)
    try:
        second = _snapshot(conn)
    finally:
        conn.close()
    assert second == first

    conn = D.connect(False)
    try:
        conn.execute("UPDATE players SET pool='public' WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    D.init_db(False)
    conn = D.connect(False)
    try:
        third = _snapshot(conn)
    finally:
        conn.close()
    assert third["pools"] == ["public", "pot", "pot", "pot", "pot", "pot"]
    assert third["players"] == first["players"]
    assert third["preds"] == first["preds"]
    assert third["questions"] == first["questions"]
    assert third["settings"] == first["settings"]


def main():
    _prepare()
    sys.path.insert(0, str(ROOT))
    from app import app  # noqa: F401  startup migration

    _assert_migrated()
    backend = "postgres" if D.using_postgres() else "sqlite"
    print(f"PROD DATA MIGRATION OK ({backend})")


if __name__ == "__main__":
    main()
