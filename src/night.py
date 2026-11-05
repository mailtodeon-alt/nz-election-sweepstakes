"""Election-night extras: reactions, side bets, score history, ticker, averages.

Side-bet points are never written into the pot leaderboard. Callers add them
only when building the public board or the side-bet mini-table.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from datetime import datetime
from typing import Any, Optional

import db as D
from scoring import rank_players

EMOJIS = ("🎉", "😱", "🔥", "😂", "👏", "😬")
TICKER_MAX = 80
TICKER_GAP_SEC = 60
REACTION_GAP_SEC = 1.0
REACTION_FLUSH_SEC = 1.0

_PROFANITY = re.compile(
    r"\b(?:fuck\w*|shit\w*|cunt\w*|bitch\w*|asshole|bastard|dickhead|wanker|piss\w*)\b",
    re.IGNORECASE,
)


def mask_profanity(text: str) -> str:
    return _PROFANITY.sub(lambda m: "*" * len(m.group(0)), text)


def clean_ticker(raw: Any) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(raw, str):
        return None, "Enter a message."
    text = raw.strip()
    if not text:
        return None, "Enter a message."
    if len(text) > TICKER_MAX:
        return None, f"Messages are limited to {TICKER_MAX} characters."
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None, "That message contains unsupported characters."
    return html.escape(mask_profanity(text), quote=True), None


class ReactionHub:
    """In-memory taps. Bursts collapse into one SSE payload per second."""

    def __init__(self, broadcast):
        self.broadcast = broadcast
        self._lock = threading.Lock()
        self._buf: dict[tuple[bool, str], int] = {}
        self._totals: dict[tuple[bool, str], int] = {}
        self._last: dict[tuple[bool, str], float] = {}
        self._timer: Optional[threading.Timer] = None

    def reset(self) -> None:
        with self._lock:
            self._buf.clear()
            self._totals.clear()
            self._last.clear()

    def totals(self, demo: bool) -> dict[str, int]:
        with self._lock:
            return {emoji: n for (flag, emoji), n in self._totals.items() if flag is demo}

    def tap(self, demo: bool, client_key: str, emoji: str) -> tuple[bool, str]:
        if emoji not in EMOJIS:
            return False, "bad_emoji"
        now = time.monotonic()
        key = (bool(demo), client_key)
        with self._lock:
            prev = self._last.get(key, 0.0)
            if now - prev < REACTION_GAP_SEC:
                return False, "throttled"
            self._last[key] = now
            bucket = (bool(demo), emoji)
            self._buf[bucket] = self._buf.get(bucket, 0) + 1
            self._totals[bucket] = self._totals.get(bucket, 0) + 1
            if self._timer is None:
                self._timer = threading.Timer(REACTION_FLUSH_SEC, self.flush)
                self._timer.daemon = True
                self._timer.start()
        return True, "ok"

    def flush(self) -> None:
        with self._lock:
            buf, self._buf = self._buf, {}
            self._timer = None
        by_demo: dict[bool, list[dict]] = {}
        for (demo, emoji), n in buf.items():
            by_demo.setdefault(demo, []).append({"emoji": emoji, "n": n})
        for demo, bursts in by_demo.items():
            try:
                self.broadcast("reactions", {"demo": demo, "bursts": bursts})
            except Exception:
                pass


def list_side_bets(conn: D.Conn, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM side_bets"
    if active_only:
        sql += " WHERE active=1"
    sql += " ORDER BY id"
    rows = []
    for r in conn.execute(sql):
        d = dict(r)
        d["choices"] = json.loads(d.pop("choices_json") or "[]")
        d["kind"] = d.get("kind") or "choice"
        d["open_until"] = d.get("open_until") or ""
        rows.append(d)
    return rows


def side_bet_picks(conn: D.Conn) -> dict[int, dict[int, str]]:
    out: dict[int, dict[int, str]] = {}
    for r in conn.execute("SELECT side_bet_id, player_id, value FROM side_bet_picks"):
        out.setdefault(int(r["side_bet_id"]), {})[int(r["player_id"])] = r["value"]
    return out


def _bet_is_open(bet: dict) -> bool:
    if bet.get("status") != "open":
        return False
    until = (bet.get("open_until") or "").strip()
    if not until:
        return True
    try:
        deadline = datetime.fromisoformat(until)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=D.NZ)
    except Exception:
        return True
    return D.now_nz() < deadline


def side_bonus_map(conn: D.Conn) -> dict[int, dict[str, int]]:
    """Points already earned, plus points still available on unanswered bets.

    Choice bets pay an exact match. Closest-number bets pay everyone tied for
    the smallest error. These points are for the public board and the bonus
    table only; callers must not add them to pot totals.
    """
    bets = list_side_bets(conn)
    picks = side_bet_picks(conn)
    earned: dict[int, int] = {}
    pending_each = 0
    for bet in bets:
        pts = int(bet["points"] or 0)
        answer = (bet.get("answer") or "").strip()
        chosen = picks.get(int(bet["id"]), {})
        kind = bet.get("kind") or "choice"
        if not answer:
            if _bet_is_open(bet):
                pending_each += pts
            continue
        if kind == "closest":
            errors = []
            try:
                target = float(answer)
            except ValueError:
                continue
            for pid, value in chosen.items():
                try:
                    errors.append((abs(float(value) - target), pid))
                except (TypeError, ValueError):
                    continue
            if not errors:
                continue
            best = min(err for err, _pid in errors)
            for err, pid in errors:
                if err == best:
                    earned[pid] = earned.get(pid, 0) + pts
            continue
        for pid, value in chosen.items():
            if str(value).strip() == answer:
                earned[pid] = earned.get(pid, 0) + pts
    players = D.list_players(conn, active_only=True, pool=None)
    out: dict[int, dict[str, int]] = {}
    for p in players:
        out[p["id"]] = {"points": earned.get(p["id"], 0), "pending": pending_each}
    return out


def side_score_table(conn: D.Conn) -> list[dict]:
    """Every active player and their side-bet points. Not a pot ranking."""
    bonus = side_bonus_map(conn)
    rows = []
    for pool in (D.POOL_POT, D.POOL_PUBLIC):
        for p in D.list_players(conn, pool=pool):
            info = bonus.get(p["id"], {"points": 0, "pending": 0})
            raw = html.unescape(p["name"]) if pool == D.POOL_PUBLIC else p["name"]
            rows.append({
                "player_id": p["id"],
                "name": html.escape(raw, quote=True),
                "pool": pool,
                "points": info["points"],
                "pending": info["pending"],
            })
    rows.sort(key=lambda r: (-r["points"], r["pool"], r["name"]))
    return rows


def public_rows_with_side_bets(ranked: list[dict], conn: D.Conn, national_actual: Optional[float]) -> list[dict]:
    """Re-rank public rows with side-bet points included. Does not touch pot rows."""
    bonus = side_bonus_map(conn)
    adjusted = []
    for row in ranked:
        info = bonus.get(row["player_id"], {"points": 0, "pending": 0})
        item = dict(row)
        item["question_total"] = int(row.get("total") or 0)
        item["side_points"] = info["points"]
        item["total"] = item["question_total"] + info["points"]
        item["pending_max"] = int(row.get("pending_max") or 0) + info["pending"]
        adjusted.append(item)
    return rank_players(adjusted, national_actual=national_actual)


def record_score_history(conn: D.Conn, rows: list[dict], pool: str) -> None:
    reporting = D.get_setting(conn, "reporting_pct") or ""
    at = D.iso_now()
    for row in rows:
        conn.execute(
            """INSERT INTO score_history(at, reporting_pct, pool, player_id, name, total, place)
               VALUES (?,?,?,?,?,?,?)""",
            (
                at,
                reporting,
                pool,
                row["player_id"],
                row["name"],
                int(row.get("question_total", row.get("total")) or 0) if pool == D.POOL_POT else int(row.get("total") or 0),
                row.get("place") or row.get("rank"),
            ),
        )


def history_series(conn: D.Conn, pool: str) -> list[dict]:
    series: dict[int, dict] = {}
    for r in conn.execute(
        "SELECT * FROM score_history WHERE pool=? ORDER BY id",
        (pool,),
    ):
        pid = int(r["player_id"])
        bucket = series.setdefault(pid, {"player_id": pid, "name": r["name"], "points": []})
        bucket["name"] = r["name"]
        bucket["points"].append({
            "at": r["at"],
            "reporting_pct": r["reporting_pct"] or "",
            "total": int(r["total"] or 0),
            "place": r["place"],
        })
    return list(series.values())


def submitted_average(rows: list[dict]) -> dict:
    submitted = [r for r in rows if r.get("submitted")]
    if not submitted:
        return {"average": None, "submitted": 0}
    total = sum(int(r.get("question_total", r.get("total")) or 0) for r in submitted)
    return {"average": round(total / len(submitted), 1), "submitted": len(submitted)}


def ticker_rows(conn: D.Conn, *, pool: str, include_featured_public: bool) -> list[dict]:
    if (D.get_setting(conn, "ticker_enabled", "1") or "1") != "1":
        return []
    clauses = ["hidden=0"]
    params: list[Any] = []
    if include_featured_public:
        clauses.append("(pool=? OR (pool=? AND featured=1))")
        params.extend([pool, D.POOL_PUBLIC])
    else:
        clauses.append("pool=?")
        params.append(pool)
    sql = "SELECT * FROM ticker_messages WHERE " + " AND ".join(clauses) + " ORDER BY id DESC LIMIT 30"
    rows = [dict(r) for r in conn.execute(sql, tuple(params))]
    rows.reverse()
    return rows


def ticker_too_soon(conn: D.Conn, player_id: int) -> bool:
    row = conn.execute(
        "SELECT created_at FROM ticker_messages WHERE player_id=? ORDER BY id DESC LIMIT 1",
        (player_id,),
    ).fetchone()
    if not row or not row["created_at"]:
        return False
    try:
        prev = datetime.fromisoformat(row["created_at"])
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=D.NZ)
    except Exception:
        return False
    return (D.now_nz() - prev).total_seconds() < TICKER_GAP_SEC


def seed_demo_night(conn: D.Conn) -> None:
    """Sample side bet, ticker lines, and a public guest so /demo/live can preview the night."""
    now = D.iso_now()
    pots = D.list_players(conn, pool=D.POOL_POT)
    questions = D.list_questions(conn)
    for i, p in enumerate(pots):
        for q in questions:
            choices = (q.get("meta") or {}).get("choices") or []
            if not choices:
                continue
            conn.execute(
                """INSERT INTO predictions(player_id, question_id, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(player_id, question_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (p["id"], q["id"], choices[i % len(choices)], now),
            )
        conn.execute("UPDATE players SET tb_national_vote=? WHERE id=?", (str(34 + i), p["id"]))

    token = __import__("secrets").token_urlsafe(18)
    conn.execute(
        "INSERT INTO players(name, token, slot, created_at, pool) VALUES (?,?,?,?,?)",
        ("Preview Guest", token, 1001, now, D.POOL_PUBLIC),
    )
    guest = conn.execute("SELECT id FROM players WHERE token=?", (token,)).fetchone()
    guest_id = guest["id"]
    for q in questions:
        choices = (q.get("meta") or {}).get("choices") or []
        if not choices:
            continue
        conn.execute(
            """INSERT INTO predictions(player_id, question_id, value, updated_at) VALUES (?,?,?,?)""",
            (guest_id, q["id"], choices[min(1, len(choices) - 1)], now),
        )

    conn.execute(
        """INSERT INTO side_bets(prompt, choices_json, points, status, answer, created_at, active)
           VALUES (?,?,?,?,?,?,1)""",
        ("Will National still be ahead at 10pm?", json.dumps(["Yes", "No"]), 5, "open", None, now),
    )
    conn.execute(
        """INSERT INTO side_bets(prompt, choices_json, points, status, answer, created_at, active)
           VALUES (?,?,?,?,?,?,1)""",
        ("More than 50% counted by the first speech?", json.dumps(["Yes", "No"]), 5, "closed", "Yes", now),
    )
    closed = conn.execute("SELECT id FROM side_bets WHERE status='closed' ORDER BY id DESC LIMIT 1").fetchone()
    if pots and closed:
        conn.execute(
            "INSERT INTO side_bet_picks(side_bet_id, player_id, value, updated_at) VALUES (?,?,?,?)",
            (closed["id"], pots[0]["id"], "Yes", now),
        )
        conn.execute(
            "INSERT INTO side_bet_picks(side_bet_id, player_id, value, updated_at) VALUES (?,?,?,?)",
            (closed["id"], guest_id, "No", now),
        )

    samples = [
        (pots[0]["id"], pots[0]["name"], D.POOL_POT, "Calling it early.", 0),
        (guest_id, "Preview Guest", D.POOL_PUBLIC, "Watching from the couch.", 0),
        (guest_id, "Preview Guest", D.POOL_PUBLIC, "Put this one on the big screen.", 1),
    ]
    for pid, name, pool, text, featured in samples:
        body, _err = clean_ticker(text)
        conn.execute(
            """INSERT INTO ticker_messages(player_id, player_name, pool, body, created_at, hidden, featured)
               VALUES (?,?,?,?,?,0,?)""",
            (pid, html.escape(name, quote=True), pool, body, now, featured),
        )
    D.set_setting(conn, "weekend_mode", "1")
    conn.execute(
        """INSERT INTO side_bets(prompt, choices_json, points, status, answer, created_at, active, kind, open_until)
           VALUES (?,?,?,?,?,?,1,'closest',?)""",
        ("Final turnout %", "[]", 10, "open", None, now, "2026-11-27T12:00:00+13:00"),
    )
    conn.execute(
        """INSERT INTO coalition_events(at, headline, source_url, parties, created_at, active)
           VALUES (?,?,?,?,?,1)""",
        (
            now,
            html.escape("Talks open between the largest party and two possibles.", quote=True),
            "https://www.electionresults.govt.nz/",
            "National, ACT, NZ First",
            now,
        ),
    )
