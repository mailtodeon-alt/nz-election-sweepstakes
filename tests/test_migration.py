"""Migrate a pre-pool database that already has pot players.

Creates the previous schema (no players.pool), inserts six players plus a
prediction, then imports the app so startup migration runs. Works on SQLite
and Postgres. Does not import the app until the legacy tables exist.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LEGACY_PLAYERS = [
    (1, "Ada", "token-ada-legacy", 1, "2026-10-01T10:00:00+13:00", 1, "35.2"),
    (2, "Bea", "token-bea-legacy", 2, "2026-10-01T10:00:00+13:00", 1, None),
    (3, "Cai", "token-cai-legacy", 3, "2026-10-01T10:00:00+13:00", 1, None),
    (4, "Dee", "token-dee-legacy", 4, "2026-10-01T10:00:00+13:00", 1, None),
    (5, "Eve", "token-eve-legacy", 5, "2026-10-01T10:00:00+13:00", 1, None),
    (6, "Fay", "token-fay-legacy", 6, "2026-10-01T10:00:00+13:00", 1, None),
]
PRED = "34\u201337.9"


def _sqlite_legacy(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
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
        conn.execute("INSERT INTO settings(key, value) VALUES ('event_title', 'KeepMe')")
        conn.executemany(
            "INSERT INTO players(id, name, token, slot, created_at, active, tb_national_vote) VALUES (?,?,?,?,?,?,?)",
            LEGACY_PLAYERS,
        )
        conn.execute(
            """INSERT INTO questions(id, key, label, scoring_type, max_points, sort_order, meta_json, active)
               VALUES (1, 'nat_vote', 'National party vote', 'vote_band_mc', 10, 1, '{}', 1)"""
        )
        conn.execute(
            "INSERT INTO predictions(player_id, question_id, value, updated_at) VALUES (1, 1, ?, ?)",
            (PRED, "2026-10-01T10:05:00+13:00"),
        )
        conn.execute(
            "INSERT INTO results(question_id, value, source, updated_at) VALUES (1, '35.1', 'manual', '2026-10-01T11:00:00+13:00')"
        )
        conn.commit()
    finally:
        conn.close()


def _postgres_legacy(url: str) -> None:
    import psycopg

    conn = psycopg.connect(url, autocommit=True)
    try:
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
        conn.execute("INSERT INTO settings(key, value) VALUES ('event_title', 'KeepMe')")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO players(id, name, token, slot, created_at, active, tb_national_vote) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                LEGACY_PLAYERS,
            )
        conn.execute(
            """INSERT INTO questions(id, key, label, scoring_type, max_points, sort_order, meta_json, active)
               VALUES (1, 'nat_vote', 'National party vote', 'vote_band_mc', 10, 1, '{}', 1)"""
        )
        conn.execute(
            "INSERT INTO predictions(player_id, question_id, value, updated_at) VALUES (1, 1, %s, %s)",
            (PRED, "2026-10-01T10:05:00+13:00"),
        )
        conn.execute(
            "INSERT INTO results(question_id, value, source, updated_at) VALUES (1, '35.1', 'manual', '2026-10-01T11:00:00+13:00')"
        )
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


def _assert_migrated(D) -> None:
    def snapshot(conn):
        rows = D.list_players(conn, active_only=False, pool=None)
        rows = sorted(rows, key=lambda r: r["slot"])
        preds = D.get_predictions(conn, 1)
        result = D.get_results(conn).get("1", {})
        return {
            "rows": [(r["id"], r["name"], r["token"], r["slot"], r["pool"], r.get("tb_national_vote")) for r in rows],
            "pred": preds.get("1"),
            "result": result.get("value"),
            "title": D.get_setting(conn, "event_title"),
            "open": D.get_setting(conn, "public_signups_open"),
        }

    conn = D.connect(False)
    try:
        first = snapshot(conn)
    finally:
        conn.close()
    assert [r[1] for r in first["rows"]] == ["Ada", "Bea", "Cai", "Dee", "Eve", "Fay"]
    assert [r[2] for r in first["rows"]] == [p[2] for p in LEGACY_PLAYERS]
    assert [r[4] for r in first["rows"]] == ["pot"] * 6
    assert first["rows"][0][5] == "35.2"
    assert first["pred"] == PRED
    assert first["result"] == "35.1"
    assert first["title"] == "KeepMe"
    assert first["open"] == "1"
    assert len(first["rows"]) == 6

    D.init_db(False)
    conn = D.connect(False)
    try:
        second = snapshot(conn)
        conn.execute("UPDATE players SET pool='public' WHERE id=1")
        conn.commit()
    finally:
        conn.close()
    assert second == first

    D.init_db(False)
    conn = D.connect(False)
    try:
        third = snapshot(conn)
    finally:
        conn.close()
    assert third["rows"][0][4] == "public"
    assert [r[4] for r in third["rows"][1:]] == ["pot"] * 5
    assert [r[2] for r in third["rows"]] == [p[2] for p in LEGACY_PLAYERS]
    assert third["pred"] == PRED
    assert third["result"] == "35.1"
    assert third["title"] == "KeepMe"


def main():
    _prepare()
    sys.path.insert(0, str(ROOT))
    from app import app  # noqa: F401  startup migration
    import db as D

    _assert_migrated(D)
    backend = "postgres" if D.using_postgres() else "sqlite"
    print(f"MIGRATION OK ({backend})")


if __name__ == "__main__":
    main()
