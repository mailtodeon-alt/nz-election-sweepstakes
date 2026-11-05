"""Dual-backend persistence: Postgres if DATABASE_URL is set, else SQLite."""
from __future__ import annotations
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Iterator
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")
ROOT = Path(__file__).resolve().parent.parent

_use_pg = bool((os.environ.get("DATABASE_URL") or "").strip())
_lock = threading.RLock()  # serialize SQLite writers across threads

DEFAULT_QUESTIONS = [
    {"key": "nat_vote", "label": "National party vote", "scoring_type": "vote_band_mc", "max_points": 10, "sort_order": 1,
     "meta": {"party": "National", "choices": ["<30", "30–33.9", "34–37.9", "38–41.9", "42+"]}},
    {"key": "lab_vote", "label": "Labour party vote", "scoring_type": "vote_band_mc", "max_points": 10, "sort_order": 2,
     "meta": {"party": "Labour", "choices": ["<24", "24–27.9", "28–31.9", "32–35.9", "36+"]}},
    {"key": "grn_vote", "label": "Greens party vote", "scoring_type": "vote_band_mc", "max_points": 10, "sort_order": 3,
     "meta": {"party": "Green", "choices": ["<8", "8–10.9", "11–13.9", "14+"]}},
    {"key": "act_vote", "label": "ACT party vote", "scoring_type": "vote_band_mc", "max_points": 10, "sort_order": 4,
     "meta": {"party": "ACT", "choices": ["<6", "6–8.9", "9–11.9", "12+"]}},
    {"key": "nzf_vote", "label": "NZ First party vote", "scoring_type": "vote_band_mc", "max_points": 10, "sort_order": 5,
     "meta": {"party": "NZ First", "choices": ["under 5% (out)", "5–7.9", "8–10.9", "11+"]}},
    {"key": "tpm_vote", "label": "Te Pāti Māori party vote", "scoring_type": "vote_band_mc", "max_points": 10, "sort_order": 6,
     "meta": {"party": "Te Pāti Māori", "choices": ["<3", "3–4.9", "5–6.9", "7+"]}},
    {"key": "most_seats", "label": "Party with the most seats", "scoring_type": "exact_choice", "max_points": 5, "sort_order": 7,
     "meta": {"choices": ["National", "Labour", "Other"]}},
    {"key": "parties_in", "label": "How many parties make it into Parliament?", "scoring_type": "parties_band_mc", "max_points": 10, "sort_order": 8,
     "meta": {"choices": ["4", "5", "6", "7", "8+"]}},
    {"key": "bloc_61", "label": "Will National + ACT + NZ First reach 61+ seats?", "scoring_type": "yes_no", "max_points": 15, "sort_order": 9,
     "meta": {"choices": ["Yes", "No"]}},
    {"key": "minor_highest", "label": "Which minor gets the highest party vote?", "scoring_type": "exact_choice", "max_points": 10, "sort_order": 10,
     "meta": {"choices": ["ACT", "Green", "NZ First", "Te Pāti Māori", "TOP"]}},
]

DEFAULT_SETTINGS = {
    "lock_at": "2026-11-07T19:00:00+13:00",
    "entries_open": "1",
    "board_mode": "live",
    "demo_mode": "0",
    "feed_enabled": "0",
    "feed_url": "https://media.election.net.nz/xml/",
    "feed_poll_seconds": "90",
    "reporting_pct": "",
    "prize_first": "100",
    "prize_second": "20",
    "entry_fee": "20",
    "event_title": "NZ Election Night Sweepstakes 2026",
    "most_seats_tie_rule": "If parties tie on seats, higher party vote % wins. Leaderboard ties: closest National exact party-vote % guess wins; still tied = prizes split.",
    "seats_note": "Vote bands use one-decimal %; boundary values fall in the higher band. Exact band 10 pts, adjacent 4 (parties-in-Parliament adjacent = 5).",
}


def using_postgres() -> bool:
    return bool((os.environ.get("DATABASE_URL") or "").strip())


def _default_db_path() -> str:
    if os.environ.get("DB_PATH"):
        return os.environ["DB_PATH"]
    return str(ROOT / "data" / "sweepstakes.db")


def db_path(demo: bool = False) -> Path:
    primary = Path(_default_db_path())
    if demo:
        return primary.parent / "demo.db"
    return primary


class Conn:
    """Thin wrapper: ? placeholders work on both SQLite and Postgres."""

    def __init__(self, raw, backend: str, demo: bool = False):
        self.raw = raw
        self.backend = backend  # 'sqlite' | 'postgres'
        self.demo = demo
        self._pg_schema = "demo" if demo else "public"

    def _sql(self, sql: str) -> str:
        if self.backend == "postgres":
            return sql.replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executescript(self, script: str):
        if self.backend == "sqlite":
            self.raw.executescript(script)
        else:
            cur = self.raw.cursor()
            cur.execute(script)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def begin_immediate(self):
        """Start a write transaction safe for concurrent submissions."""
        if self.backend == "sqlite":
            self.raw.execute("BEGIN IMMEDIATE")
        else:
            self.raw.execute("BEGIN")


def connect(demo: bool = False) -> Conn:
    if using_postgres():
        import psycopg
        from psycopg.rows import dict_row
        url = os.environ["DATABASE_URL"].strip()
        # Render sometimes provides postgres:// — psycopg accepts postgresql://
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        raw = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        conn = Conn(raw, "postgres", demo=demo)
        if demo:
            conn.execute("CREATE SCHEMA IF NOT EXISTS demo")
            conn.execute("SET search_path TO demo, public")
            conn.commit()
        else:
            conn.execute("SET search_path TO public")
            conn.commit()
        return conn

    path = db_path(demo)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    return Conn(raw, "sqlite", demo=demo)


def now_nz() -> datetime:
    return datetime.now(NZ)


def iso_now() -> str:
    return now_nz().isoformat(timespec="seconds")


def _schema_sql(backend: str) -> str:
    if backend == "postgres":
        return """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            slot INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            tb_national_vote TEXT
        );
        CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            scoring_type TEXT NOT NULL,
            max_points INTEGER NOT NULL DEFAULT 10,
            sort_order INTEGER NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS predictions (
            player_id INTEGER NOT NULL REFERENCES players(id),
            question_id INTEGER NOT NULL REFERENCES questions(id),
            value TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (player_id, question_id)
        );
        CREATE TABLE IF NOT EXISTS results (
            question_id INTEGER PRIMARY KEY REFERENCES questions(id),
            value TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id SERIAL PRIMARY KEY,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id SERIAL PRIMARY KEY,
            label TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feed_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_success_at TEXT,
            last_attempt_at TEXT,
            last_error TEXT,
            last_payload_summary TEXT,
            enabled INTEGER NOT NULL DEFAULT 0
        );
        """
    return """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            slot INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            tb_national_vote TEXT
        );
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            scoring_type TEXT NOT NULL,
            max_points INTEGER NOT NULL DEFAULT 10,
            sort_order INTEGER NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS predictions (
            player_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            value TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (player_id, question_id),
            FOREIGN KEY (player_id) REFERENCES players(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
        CREATE TABLE IF NOT EXISTS results (
            question_id INTEGER PRIMARY KEY,
            value TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feed_status (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_success_at TEXT,
            last_attempt_at TEXT,
            last_error TEXT,
            last_payload_summary TEXT,
            enabled INTEGER NOT NULL DEFAULT 0
        );
        """


def init_db(demo: bool = False, force_seed_players: bool = True) -> None:
    with _lock:
        conn = connect(demo)
        try:
            if conn.backend == "sqlite":
                conn.executescript(_schema_sql("sqlite"))
            else:
                # run statements one-by-one for psycopg
                for stmt in _schema_sql("postgres").split(";"):
                    s = stmt.strip()
                    if s:
                        conn.execute(s)
                conn.commit()

            for k, v in DEFAULT_SETTINGS.items():
                if conn.backend == "sqlite":
                    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
                else:
                    conn.execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                        (k, v),
                    )
            if demo:
                upsert_setting_sql = (
                    "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)"
                    if conn.backend == "sqlite"
                    else "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
                )
                conn.execute(upsert_setting_sql, ("demo_mode", "1"))
                conn.execute(upsert_setting_sql, ("event_title", "DEMO — NZ Election Sweepstakes"))

            nq = conn.execute("SELECT COUNT(*) AS c FROM questions").fetchone()
            nq = nq["c"] if isinstance(nq, dict) else nq[0]
            if nq == 0:
                for q in DEFAULT_QUESTIONS:
                    conn.execute(
                        "INSERT INTO questions(key, label, scoring_type, max_points, sort_order, meta_json) VALUES (?,?,?,?,?,?)",
                        (q["key"], q["label"], q["scoring_type"], q["max_points"], q["sort_order"], json.dumps(q.get("meta", {}))),
                    )

            np = conn.execute("SELECT COUNT(*) AS c FROM players").fetchone()
            np = np["c"] if isinstance(np, dict) else np[0]
            if np == 0 and force_seed_players:
                for i in range(1, 7):
                    token = secrets.token_urlsafe(18)
                    conn.execute(
                        "INSERT INTO players(name, token, slot, created_at) VALUES (?,?,?,?)",
                        (f"Player {i}", token, i, iso_now()),
                    )

            # migrate tb column if needed (sqlite older DBs)
            if conn.backend == "sqlite":
                cols = [r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()]
                if "tb_national_vote" not in cols:
                    conn.execute("ALTER TABLE players ADD COLUMN tb_national_vote TEXT")
            else:
                conn.execute(
                    """DO $$ BEGIN
                        ALTER TABLE players ADD COLUMN IF NOT EXISTS tb_national_vote TEXT;
                    EXCEPTION WHEN others THEN NULL; END $$;"""
                )

            if conn.backend == "sqlite":
                conn.execute("INSERT OR IGNORE INTO feed_status(id, enabled) VALUES (1, 0)")
            else:
                conn.execute(
                    "INSERT INTO feed_status(id, enabled) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"
                )
            conn.commit()
        finally:
            conn.close()


def get_setting(conn: Conn, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return row["value"] if isinstance(row, dict) else row[0]


def set_setting(conn: Conn, key: str, value: str) -> None:
    if conn.backend == "sqlite":
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
    else:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            (key, value),
        )


def audit(conn: Conn, actor: str, action: str, detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, detail) VALUES (?,?,?,?)",
        (iso_now(), actor, action, json.dumps(detail) if detail is not None else None),
    )


def all_settings(conn: Conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def _row(r) -> dict:
    return dict(r)


def list_questions(conn: Conn, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM questions"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY sort_order, id"
    rows = []
    for r in conn.execute(sql):
        d = _row(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        rows.append(d)
    return rows


def list_players(conn: Conn, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM players"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY slot"
    return [_row(r) for r in conn.execute(sql)]


def player_by_token(conn: Conn, token: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM players WHERE token=? AND active=1", (token,)).fetchone()
    return _row(r) if r else None


def get_predictions(conn: Conn, player_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT question_id, value FROM predictions WHERE player_id=?", (player_id,)
    ).fetchall()
    return {str(r["question_id"]): r["value"] for r in rows}


def get_all_predictions(conn: Conn) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for r in conn.execute("SELECT player_id, question_id, value FROM predictions"):
        out.setdefault(r["player_id"], {})[str(r["question_id"])] = r["value"]
    return out


def get_results(conn: Conn) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM results"):
        out[str(r["question_id"])] = {"value": r["value"], "source": r["source"], "updated_at": r["updated_at"]}
    return out


def entries_locked(conn: Conn) -> bool:
    open_flag = get_setting(conn, "entries_open", "1")
    if open_flag == "0":
        return True
    lock_at = get_setting(conn, "lock_at")
    if not lock_at:
        return False
    try:
        dt = datetime.fromisoformat(lock_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NZ)
        return now_nz() >= dt
    except Exception:
        return False


@contextmanager
def write_txn(demo: bool = False) -> Iterator[Conn]:
    """Exclusive write transaction for concurrent-safe submissions."""
    with _lock:
        conn = connect(demo)
        try:
            conn.begin_immediate()
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
