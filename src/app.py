#!/usr/bin/env python3
"""NZ Election Night Sweepstakes 2026 — Flask server."""
from __future__ import annotations
import csv
import html
import io
import json
import os
import queue
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import (
    Flask, Response, jsonify, redirect, request, send_from_directory,
    session, stream_with_context, make_response,
)
import qrcode
from qrcode.image.svg import SvgImage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import db as D
import night as N
import projection as P
import weekend as W
from scoring import score_player, rank_players, derive_from_party_table, actual_band_label, BANDS
from feed import FeedPoller

PORT = int(os.environ.get("PORT", "8787"))

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
_sk = (os.environ.get("SECRET_KEY") or "").strip()
if not _sk:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.secret_key = _sk

# Stable for the life of this process. A Render deploy sets RENDER_GIT_COMMIT;
# without that, boot time still changes the cache name so the previous shell is dropped.
_APP_STARTED_AT = str(int(time.time()))
_SW_JS = ROOT / "static" / "sw.js"
_REVALIDATE_TYPES = {"text/css", "text/javascript", "application/javascript"}


def shell_cache_name() -> str:
    raw = (os.environ.get("RENDER_GIT_COMMIT") or "").strip() or _APP_STARTED_AT
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in "._-")[:80]
    return "sweep-shell-" + (safe or _APP_STARTED_AT)


@app.after_request
def _revalidate_shell(resp):
    """Browsers and the TV must not keep a previous deploy's documents or CSS/JS."""
    path = request.path or ""
    if resp.mimetype == "text/html" or path.endswith("manifest.webmanifest") or (
        path.startswith("/static/") and resp.mimetype in _REVALIDATE_TYPES
    ):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# --- SSE hub ---
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


def broadcast(event: str = "update", data: Any = None):
    payload = json.dumps({"event": event, "data": data, "at": D.iso_now()})
    with _sub_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


feed_poller = FeedPoller(broadcast_fn=broadcast)
reactions = N.ReactionHub(broadcast)

# One gunicorn worker, so an in-memory counter is the sign-up limit.
# Buckets are per client IP and split between the real DB and demo DB.
_signup_hits: dict[str, list[float]] = {}
_signup_lock = threading.Lock()
SIGNUP_LIMIT = 5
SIGNUP_WINDOW_SEC = 10 * 60


def reset_signup_rate_limits() -> None:
    with _signup_lock:
        _signup_hits.clear()


def client_ip() -> str:
    """Client address for the sign-up limit.

    Render's proxy sets X-Forwarded-For to the caller. When that header is
    absent (local gunicorn, tests), fall back to the socket address.
    """
    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        ip = xff.split(",")[0].strip()
    else:
        ip = (request.remote_addr or "").strip()
    return (ip or "unknown")[:80]


def signup_rate_limited(bucket: str) -> bool:
    now = time.monotonic()
    with _signup_lock:
        hits = [t for t in _signup_hits.get(bucket, []) if now - t < SIGNUP_WINDOW_SEC]
        if len(hits) >= SIGNUP_LIMIT:
            _signup_hits[bucket] = hits
            return True
        hits.append(now)
        _signup_hits[bucket] = hits
        return False


def public_cap(conn) -> int:
    raw = D.get_setting(conn, "public_player_cap", "200") or "200"
    try:
        cap = int(str(raw).strip())
    except ValueError:
        return 200
    if cap < 0:
        return 0
    return cap


def national_actual_from_results(conn, questions, results_values):
    """Numeric National party vote for TB; prefer raw setting, else result for nat_vote."""
    raw = D.get_setting(conn, "party_table_json")
    if raw:
        try:
            pt = json.loads(raw)
            v = (pt.get("votes") or {}).get("National")
            if v is not None and v != "":
                return float(v)
        except Exception:
            pass
    for q in questions:
        if q.get("key") == "nat_vote":
            val = results_values.get(str(q["id"]))
            if val is not None and val != "":
                try:
                    return float(val)
                except ValueError:
                    return None
    return None


def enrich_leaderboard(ranked, questions):
    """Add best/worst call, still-in-it, wooden-spoon flag for /live."""
    if not ranked:
        return ranked
    leader_score = max((r.get("total") or 0) for r in ranked)
    places = [r.get("place") or r.get("rank") or 0 for r in ranked]
    last_place = max(places) if places else 0
    qmap = {str(q["id"]): q for q in questions}
    out = []
    for r in ranked:
        row = dict(r)
        best = None
        worst = None
        for d in r.get("detail") or []:
            if d.get("status") != "scored" or d.get("points") is None:
                continue
            q = qmap.get(str(d["question_id"]), {})
            lost = int(d.get("max") or 0) - int(d["points"])
            info = {
                "question_id": d["question_id"],
                "label": q.get("label") or q.get("key") or str(d["question_id"]),
                "points": d["points"],
                "max": d.get("max"),
                "lost": lost,
            }
            if best is None or d["points"] > best["points"] or (
                d["points"] == best["points"] and lost < best.get("lost", 99)
            ):
                best = info
            if worst is None or lost > worst["lost"] or (
                lost == worst["lost"] and d["points"] < worst["points"]
            ):
                worst = info
        max_now = int(r.get("total") or 0) + int(r.get("pending_max") or 0)
        row["best_call"] = best
        row["worst_call"] = worst
        row["max_possible_now"] = max_now
        row["still_in_it"] = max_now >= leader_score
        row["is_last"] = (row.get("place") or row.get("rank")) == last_place and last_place > 1
        out.append(row)
    return out


def build_leaderboard(conn, pool=D.POOL_POT):
    """Score and rank one pool.

    The default is the paid pot. /live, /compare, frozen snapshots, the pot
    CSV, prizes, and the NEW LEADER takeover all read this and therefore
    never include public players.
    """
    questions = D.list_questions(conn)
    players = D.list_players(conn, pool=pool)
    preds = D.get_all_predictions(conn)
    results_raw = D.get_results(conn)
    results_values = {k: v["value"] for k, v in results_raw.items()}
    nat = national_actual_from_results(conn, questions, results_values)
    scored = []
    for p in players:
        s = score_player(questions, preds.get(p["id"], {}), results_values)
        scored.append({
            "player_id": p["id"],
            "name": p["name"],
            "slot": p["slot"],
            "submitted": any(v not in (None, "") for v in (preds.get(p["id"]) or {}).values()) or bool(p.get("tb_national_vote")),
            "tb_national_vote": p.get("tb_national_vote"),
            **s,
        })
    ranked = rank_players(scored, national_actual=nat)
    return enrich_leaderboard(ranked, questions), questions, players, results_raw


def record_results_history(conn) -> None:
    """Store the score each board is showing after a results change.

    Pot rows are question points only. Public rows include side-bet points.
    """
    pot, questions, _players, results_raw = build_leaderboard(conn, pool=D.POOL_POT)
    pub, _q2, _p2, _r2 = build_leaderboard(conn, pool=D.POOL_PUBLIC)
    nat = national_actual_from_results(conn, questions, {k: v["value"] for k, v in results_raw.items()})
    pub = enrich_leaderboard(N.public_rows_with_side_bets(pub, conn, nat), questions)
    N.record_score_history(conn, pot, D.POOL_POT)
    N.record_score_history(conn, pub, D.POOL_PUBLIC)
    W.record_count_tick(conn)


feed_poller.on_applied = record_results_history


def admin_password() -> str | None:
    """Admin password from ADMIN_PASSWORD env var only (no file in deploy)."""
    pw = (os.environ.get("ADMIN_PASSWORD") or "").strip()
    return pw or None


def require_admin():
    if not admin_password():
        return jsonify({"error": "ADMIN_PASSWORD not configured", "message": "Set the ADMIN_PASSWORD app setting before using admin."}), 503
    if session.get("admin"):
        return None
    return jsonify({"error": "unauthorized"}), 401


def use_demo() -> bool:
    """Request can force demo via ?demo=1 or X-Demo:1; else global setting on real DB."""
    if request.args.get("demo") == "1" or request.headers.get("X-Demo") == "1":
        return True
    if request.path.startswith("/demo"):
        return True
    conn = D.connect(False)
    try:
        return D.get_setting(conn, "demo_mode", "0") == "1"
    finally:
        conn.close()


def conn_for_request():
    return D.connect(use_demo())


# ---------- pages ----------

@app.get("/manifest.webmanifest")
def manifest_root():
    return send_from_directory(ROOT / "static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    script = _SW_JS.read_text(encoding="utf-8").replace("__SW_CACHE_NAME__", shell_cache_name())
    resp = Response(script, mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/player/<token>/manifest.webmanifest")
def player_manifest(token):
    """Old installs requested a per-player manifest. Always serve the site one.

    start_url stays /, so a home-screen icon cannot open someone else's ballot.
    The token is ignored and no player row is read.
    """
    return send_from_directory(ROOT / "static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/")
def home():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/join")
def join_page():
    return send_from_directory(ROOT / "static", "join.html")


@app.get("/open")
def open_signup_page():
    return send_from_directory(ROOT / "static", "open.html")


@app.get("/open/live")
def open_live_page():
    return send_from_directory(ROOT / "static", "open-live.html")


@app.get("/p/<token>")
def play_page(token):
    return send_from_directory(ROOT / "static", "play.html")


@app.get("/live")
def live_page():
    return send_from_directory(ROOT / "static", "live.html")


@app.get("/compare")
def compare_page():
    return send_from_directory(ROOT / "static", "compare.html")


@app.get("/results")
def results_page():
    return send_from_directory(ROOT / "static", "results.html")


@app.get("/news")
def news_page():
    return send_from_directory(ROOT / "static", "news.html")


@app.get("/seats")
def seats_page():
    return send_from_directory(ROOT / "static", "seats.html")


@app.get("/admin")
def admin_page():
    return send_from_directory(ROOT / "static", "admin.html")


@app.get("/demo")
@app.get("/demo/")
def demo_home():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/demo/<path:rest>")
def demo_pages(rest):
    # Map /demo/live → live.html etc.
    mapping = {
        "live": "live.html",
        "compare": "compare.html",
        "join": "join.html",
        "admin": "admin.html",
        "open": "open.html",
        "open/live": "open-live.html",
        "recap": "recap.html",
        "open/recap": "recap.html",
        "coalition": "coalition.html",
        "results": "results.html",
        "news": "news.html",
        "seats": "seats.html",
    }
    if rest in mapping:
        return send_from_directory(ROOT / "static", mapping[rest])
    if rest.startswith("p/"):
        return send_from_directory(ROOT / "static", "play.html")
    return send_from_directory(ROOT / "static", "index.html")


# ---------- QR ----------
@app.get("/api/qr")
def api_qr():
    url = request.args.get("url") or ""
    if not url:
        return jsonify({"error": "url required"}), 400
    img = qrcode.make(url, image_factory=SvgImage)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml")


# ---------- auth ----------
@app.post("/api/admin/login")
def admin_login():
    expected = admin_password()
    if not expected:
        return jsonify({"ok": False, "error": "ADMIN_PASSWORD not configured on server"}), 503
    data = request.get_json(force=True, silent=True) or {}
    if (data.get("password") or "") == expected:
        session["admin"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "bad password"}), 403


@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return jsonify({"ok": True})


@app.get("/api/admin/me")
def admin_me():
    return jsonify({"admin": bool(session.get("admin"))})


# ---------- SSE ----------
@app.get("/api/events")
def api_events():
    q: queue.Queue = queue.Queue(maxsize=64)
    with _sub_lock:
        _subscribers.append(q)

    def gen():
        try:
            yield f"data: {json.dumps({'event': 'hello', 'at': D.iso_now()})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    # Data-frame heartbeat every ~20s (keeps free-tier proxies / Render awake-ish
                    # and lets clients detect a dead stream so they reconnect + resync).
                    yield f"data: {json.dumps({'event': 'heartbeat', 'at': D.iso_now()})}\n\n"
        finally:
            with _sub_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------- health ----------
@app.get("/healthz")
def healthz():
    try:
        conn = D.connect(False)
        try:
            conn.execute("SELECT 1 AS ok")
        finally:
            conn.close()
        return jsonify({"ok": True, "backend": "postgres" if D.using_postgres() else "sqlite", "at": D.iso_now()}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def assemble_state(conn, demo: bool, pool: str) -> dict:
    """Board payload for one pool. Pot and public never share a leaderboard."""
    settings = D.all_settings(conn)
    locked = D.entries_locked(conn)
    questions = D.list_questions(conn)
    players = D.list_players(conn, pool=pool)
    results_raw = D.get_results(conn)
    results_meta = results_raw
    if locked:
        ranked, questions, players, results_raw = build_leaderboard(conn, pool=pool)
        results_meta = results_raw
    else:
        ranked = []
        pending = sum(q["max_points"] for q in questions)
        for p in players:
            own = D.get_predictions(conn, p["id"])
            submitted = any(v not in (None, "") for v in own.values()) or bool(p.get("tb_national_vote"))
            ranked.append({
                "player_id": p["id"],
                "name": p["name"],
                "slot": p["slot"],
                "submitted": submitted,
                "total": 0,
                "available": 0,
                "pending_max": pending,
                "max_possible": pending,
                "max_possible_now": pending,
                "still_in_it": True,
                "is_last": False,
                "best_call": None,
                "worst_call": None,
                "detail": [],
                "tb_national_vote": None,
            })
    for row in ranked:
        row["question_total"] = int(row.get("total") or 0)
    if locked:
        other_pool = D.POOL_PUBLIC if pool == D.POOL_POT else D.POOL_POT
        other_ranked, _, _, _ = build_leaderboard(conn, pool=other_pool)
        for row in other_ranked:
            row["question_total"] = int(row.get("total") or 0)
        pot_rows = ranked if pool == D.POOL_POT else other_ranked
        pub_rows = other_ranked if pool == D.POOL_POT else ranked
    else:
        other_rows = []
        other_pool = D.POOL_PUBLIC if pool == D.POOL_POT else D.POOL_POT
        for p in D.list_players(conn, pool=other_pool):
            own = D.get_predictions(conn, p["id"])
            other_rows.append({
                "submitted": any(v not in (None, "") for v in own.values()) or bool(p.get("tb_national_vote")),
                "total": 0,
                "question_total": 0,
            })
        pot_rows = ranked if pool == D.POOL_POT else other_rows
        pub_rows = other_rows if pool == D.POOL_POT else ranked
    if pool == D.POOL_PUBLIC and locked:
        nat = national_actual_from_results(conn, questions, {k: v["value"] for k, v in results_meta.items()})
        ranked = enrich_leaderboard(N.public_rows_with_side_bets(pub_rows, conn, nat), questions)
    if pool == D.POOL_PUBLIC:
        for row in ranked:
            row.pop("prize", None)
            row["is_last"] = False
    compare = {
        "pot": N.submitted_average(pot_rows) if locked else {
            "average": None,
            "submitted": sum(1 for r in pot_rows if r.get("submitted")),
        },
        "public": N.submitted_average(pub_rows) if locked else {
            "average": None,
            "submitted": sum(1 for r in pub_rows if r.get("submitted")),
        },
    }
    side_bets = []
    for bet in N.list_side_bets(conn):
        side_bets.append({
            "id": bet["id"],
            "prompt": bet["prompt"],
            "choices": bet["choices"],
            "points": bet["points"],
            "status": bet["status"],
            "answer": bet.get("answer") or "",
            "kind": bet.get("kind") or "choice",
            "open_until": bet.get("open_until") or "",
        })
    _f = conn.execute("SELECT * FROM feed_status WHERE id=1").fetchone()
    feed = dict(_f) if _f else {}
    settings_out = {
        "event_title": settings.get("event_title"),
        "lock_at": settings.get("lock_at"),
        "entries_open": settings.get("entries_open"),
        "board_mode": settings.get("board_mode"),
        "reporting_pct": settings.get("reporting_pct"),
        "most_seats_tie_rule": settings.get("most_seats_tie_rule"),
        "seats_note": settings.get("seats_note"),
        "demo_mode": settings.get("demo_mode"),
        "feed_enabled": settings.get("feed_enabled"),
        "feed_poll_seconds": settings.get("feed_poll_seconds") or "180",
        "reactions_enabled": settings.get("reactions_enabled") or "1",
        "ticker_enabled": settings.get("ticker_enabled") or "1",
        "weekend_mode": settings.get("weekend_mode") or "0",
    }
    if pool == D.POOL_POT:
        settings_out["prize_first"] = settings.get("prize_first")
        settings_out["prize_second"] = settings.get("prize_second")
        settings_out["entry_fee"] = settings.get("entry_fee")
    else:
        settings_out["public_signups_open"] = settings.get("public_signups_open") or "1"
    live_ranked = ranked
    mode = settings.get("board_mode") or "live"
    if pool == D.POOL_POT and locked and mode in ("frozen", "official"):
        kind = "official" if mode == "official" else "election_night"
        snap_row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if snap_row:
            try:
                snapped = json.loads(snap_row["payload_json"]).get("leaderboard") or []
            except Exception:
                snapped = []
            if snapped:
                ranked = snapped
                compare["pot"] = N.submitted_average(ranked)
    series = N.history_series(conn, pool)
    return {
        "demo": demo,
        "pool": pool,
        "settings": settings_out,
        "locked": locked,
        "server_now": D.iso_now(),
        "questions": [
            {
                "id": q["id"],
                "key": q["key"],
                "label": q["label"],
                "scoring_type": q["scoring_type"],
                "max_points": q["max_points"],
                "sort_order": q["sort_order"],
                "meta": q["meta"],
            }
            for q in questions
        ],
        "players": [{"id": p["id"], "name": p["name"], "slot": p["slot"]} for p in players],
        "leaderboard": ranked,
        "results": {
            k: {"value": v["value"], "source": v["source"], "updated_at": v["updated_at"]}
            for k, v in results_meta.items()
        },
        "feed": {
            "enabled": bool(feed.get("enabled")),
            "last_success_at": feed.get("last_success_at"),
            "last_attempt_at": feed.get("last_attempt_at"),
            "last_error": feed.get("last_error"),
            "last_payload_summary": feed.get("last_payload_summary"),
        },
        "compare_averages": compare,
        "side_bets": side_bets,
        "side_bet_scores": N.side_score_table(conn),
        "ticker": N.ticker_rows(conn, pool=pool, include_featured_public=(pool == D.POOL_POT)),
        "reactions": reactions.totals(demo),
        "latest_count": {
            "label": "Latest count (not the pot result)",
            "at": D.iso_now(),
            "reporting_pct": settings.get("reporting_pct") or "",
            "players": W.latest_rows(live_ranked, questions),
            "ticks": W.recent_ticks(conn),
        },
        "projection": W.projection_view(conn, live_ranked, questions),
        "biggest_mover": W.biggest_mover(series),
        "pot_result_frozen": pool == D.POOL_POT and mode in ("frozen", "official") and ranked is not live_ranked,
        "house": house_payload(conn, settings),
        "advance": advance_public(settings),
        "headlines": headlines_public(settings, limit=4),
    }


def _json_setting(settings: dict, key: str, fallback):
    raw = settings.get(key) or ""
    if not str(raw).strip():
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def advance_public(settings: dict) -> dict:
    return {
        "votes_2026": settings.get("advance_votes_2026") or "1074416",
        "as_of": settings.get("advance_as_of") or "2026-11-02",
        "votes_2023_same": settings.get("advance_votes_2023_same") or "860668",
        "votes_2020_same": settings.get("advance_votes_2020_same") or "1282028",
        "votes_2023_total": settings.get("advance_votes_2023_total") or "1368830",
        "source": "https://elections.nz",
        "updated_at": settings.get("advance_fetch_ok_at") or "",
    }


def headlines_public(settings: dict, limit: int = 4) -> list:
    rows = _json_setting(settings, "news_json", [])
    if not isinstance(rows, list):
        return []
    return rows[:limit]


def house_payload(conn, settings: dict) -> dict:
    votes, _seat_totals = W._current_party_numbers(conn)
    candidate = _json_setting(settings, "candidate_seats_json", {})
    if not isinstance(candidate, dict):
        candidate = {}
    live = P.project_live(votes, candidate)
    return {
        "has_results": live is not None,
        "current": P.current_parliament(settings.get("parliament_2023_json")),
        "live": live,
        "poll": P.poll_projection(settings.get("latest_poll_json")),
    }


# ---------- public state ----------
@app.get("/api/state")
def api_state():
    demo = use_demo()
    conn = D.connect(demo)
    try:
        return jsonify(assemble_state(conn, demo, D.POOL_POT))
    finally:
        conn.close()


@app.get("/api/news")
def api_news():
    demo = use_demo()
    conn = D.connect(demo)
    try:
        settings = D.all_settings(conn)
        rows = _json_setting(settings, "news_json", [])
        return jsonify({
            "demo": demo,
            "items": rows if isinstance(rows, list) else [],
            "updated_at": settings.get("news_fetch_ok_at") or "",
        })
    finally:
        conn.close()


@app.get("/api/results")
def api_results():
    demo = use_demo()
    conn = D.connect(demo)
    try:
        settings = D.all_settings(conn)
        votes, seats = W._current_party_numbers(conn)
        counts = _json_setting(settings, "vote_counts_json", {})
        if not isinstance(counts, dict):
            counts = {}
        electorates = _json_setting(settings, "electorates_json", [])
        if not isinstance(electorates, list):
            electorates = []
        seen = []
        for party in list(P.HOUSE_ORDER) + list(votes.keys()) + list(P.OFFICIAL_PCT_2023.keys()):
            if party in seen:
                continue
            if party not in votes and party not in seats and party not in P.OFFICIAL_PCT_2023:
                continue
            seen.append(party)
        rows = []
        for party in seen:
            now = votes.get(party)
            base = P.OFFICIAL_PCT_2023.get(party)
            change = None
            if now is not None and base is not None:
                change = round(float(now) - float(base), 2)
            rows.append({
                "party": party,
                "votes": counts.get(party),
                "percent": now,
                "pct_2023": base,
                "change_2023": change,
                "seats": seats.get(party),
            })
        _f = conn.execute("SELECT last_success_at, last_error FROM feed_status WHERE id=1").fetchone()
        feed = dict(_f) if _f else {}
        return jsonify({
            "demo": demo,
            "has_results": bool(votes),
            "reporting_pct": settings.get("reporting_pct") or "",
            "updated_at": feed.get("last_success_at") or "",
            "parties": rows,
            "electorates": electorates,
            "house": house_payload(conn, settings),
            "advance": advance_public(settings),
        })
    finally:
        conn.close()


@app.get("/api/status")
def api_status():
    """Last successful fetch per source. No secrets and no feed URL."""
    conn = D.connect(False)
    try:
        settings = D.all_settings(conn)
        _f = conn.execute("SELECT * FROM feed_status WHERE id=1").fetchone()
        feed = dict(_f) if _f else {}
        news = _json_setting(settings, "news_json", [])
        sources = _json_setting(settings, "news_sources_json", [])
        return jsonify({
            "ok": True,
            "at": D.iso_now(),
            "sources": {
                "results_feed": {
                    "enabled": D.get_setting(conn, "feed_enabled", "0") == "1",
                    "interval_seconds": D.get_setting(conn, "feed_poll_seconds", "180") or "180",
                    "last_success_at": feed.get("last_success_at"),
                    "last_attempt_at": feed.get("last_attempt_at"),
                    "last_error": feed.get("last_error") or "",
                },
                "advance_voting": {
                    "total": settings.get("advance_votes_2026") or "",
                    "as_of": settings.get("advance_as_of") or "",
                    "last_success_at": settings.get("advance_fetch_ok_at") or "",
                    "last_error": settings.get("advance_fetch_error") or "",
                    "source": "https://elections.nz",
                },
                "news": {
                    "count": len(news) if isinstance(news, list) else 0,
                    "last_success_at": settings.get("news_fetch_ok_at") or "",
                    "last_error": settings.get("news_fetch_error") or "",
                    "feeds": sources if isinstance(sources, list) else [],
                },
            },
        })
    finally:
        conn.close()


@app.get("/api/open/state")
def api_open_state():
    demo = use_demo()
    conn = D.connect(demo)
    try:
        return jsonify(assemble_state(conn, demo, D.POOL_PUBLIC))
    finally:
        conn.close()


@app.post("/api/open/signup")
def api_open_signup():
    """Issue an unguessable personal link for a play-for-fun player."""
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    cleaned, name_err = D.clean_public_name(data.get("name"))
    if name_err:
        return jsonify({"error": "bad_name", "message": name_err}), 400
    bucket = ("demo:" if demo else "live:") + client_ip()
    if signup_rate_limited(bucket):
        return jsonify({
            "error": "rate_limited",
            "message": "Too many sign-ups from this network. Try again in a few minutes.",
        }), 429
    try:
        with D.write_txn(demo) as conn:
            if D.entries_locked(conn):
                return jsonify({
                    "error": "entries_locked",
                    "message": "Entries are locked. Public sign-ups are closed.",
                }), 403
            if (D.get_setting(conn, "public_signups_open", "1") or "1") != "1":
                return jsonify({
                    "error": "public_closed",
                    "message": "Public sign-ups are closed.",
                }), 403
            if D.public_name_taken(conn, data.get("name")):
                return jsonify({
                    "error": "name_taken",
                    "message": "That name is already taken.",
                }), 409
            cap = public_cap(conn)
            if D.count_players(conn, D.POOL_PUBLIC) >= cap:
                return jsonify({
                    "error": "public_cap",
                    "message": "Public sign-ups are full.",
                }), 403
            token = secrets.token_urlsafe(18)
            slot = D.next_public_slot(conn)
            conn.execute(
                "INSERT INTO players(name, token, slot, created_at, pool) VALUES (?,?,?,?,?)",
                (cleaned, token, slot, D.iso_now(), D.POOL_PUBLIC),
            )
            row = conn.execute("SELECT id FROM players WHERE token=?", (token,)).fetchone()
            pid = row["id"]
            D.audit(conn, "public", "signup", {"id": pid, "name": cleaned})
        broadcast("players")
        prefix = "/demo" if demo else ""
        return jsonify({
            "ok": True,
            "player": {
                "id": pid,
                "name": cleaned,
                "token": token,
                "pool": D.POOL_PUBLIC,
                "link": f"{prefix}/p/{token}",
            },
        })
    except Exception as e:
        return jsonify({"error": "signup_failed", "message": str(e)}), 500


@app.get("/api/player/<token>")
def api_player(token):
    demo = use_demo()
    conn = D.connect(demo)
    try:
        p = D.player_by_token(conn, token)
        if not p:
            return jsonify({"error": "not found"}), 404
        locked = D.entries_locked(conn)
        questions = D.list_questions(conn)
        preds = D.get_predictions(conn, p["id"])
        results_raw = D.get_results(conn)
        results_values = {k: v["value"] for k, v in results_raw.items()}
        scored = score_player(questions, preds, results_values)
        settings = D.all_settings(conn)
        return jsonify({
            "demo": demo,
            "player": {
                "id": p["id"],
                "name": p["name"],
                "slot": p["slot"],
                "token": p["token"],
                "pool": p.get("pool") or D.POOL_POT,
                "tb_national_vote": p.get("tb_national_vote"),
            },
            "locked": locked,
            "lock_at": settings.get("lock_at"),
            "server_now": D.iso_now(),
            "questions": questions,
            "predictions": preds,
            "results": results_raw if locked else {},
            "score": scored if locked else None,
            "settings": {
                "most_seats_tie_rule": settings.get("most_seats_tie_rule"),
                "seats_note": settings.get("seats_note"),
                "event_title": settings.get("event_title"),
                "reactions_enabled": settings.get("reactions_enabled") or "1",
                "ticker_enabled": settings.get("ticker_enabled") or "1",
            },
            "side_bets": _player_side_bets(conn, p["id"]),
            "reactions": reactions.totals(demo),
        })
    finally:
        conn.close()


@app.post("/api/player/<token>/predictions")
def api_save_predictions(token):
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    preds = data.get("predictions") or {}
    try:
        with D.write_txn(demo) as conn:
            p = D.player_by_token(conn, token)
            if not p:
                return jsonify({"error": "not found"}), 404
            if D.entries_locked(conn):
                return jsonify({"error": "entries_locked", "message": "Entries are locked. Predictions cannot be changed."}), 403
            raw_name = data.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                if (p.get("pool") or D.POOL_POT) == D.POOL_PUBLIC:
                    current = html.unescape(p["name"])
                    if raw_name.strip() != current.strip():
                        cleaned, name_err = D.clean_public_name(raw_name)
                        if name_err:
                            return jsonify({"error": "bad_name", "message": name_err}), 400
                        if D.public_name_taken(conn, raw_name, exclude_id=p["id"]):
                            return jsonify({"error": "name_taken", "message": "That name is already taken."}), 409
                        conn.execute("UPDATE players SET name=? WHERE id=?", (cleaned, p["id"]))
                else:
                    renamed = raw_name.strip()
                    if renamed != p["name"]:
                        conn.execute("UPDATE players SET name=? WHERE id=?", (renamed[:40], p["id"]))
            if "tb_national_vote" in data:
                tb = data.get("tb_national_vote")
                tb_s = "" if tb is None or tb == "" else str(tb).strip()
                conn.execute("UPDATE players SET tb_national_vote=? WHERE id=?", (tb_s, p["id"]))
            questions = {str(q["id"]): q for q in D.list_questions(conn)}
            now = D.iso_now()
            upsert = (
                """INSERT INTO predictions(player_id, question_id, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(player_id, question_id) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at"""
                if conn.backend == "sqlite"
                else """INSERT INTO predictions(player_id, question_id, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(player_id, question_id) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at"""
            )
            for qid, val in preds.items():
                if str(qid) not in questions:
                    continue
                v = "" if val is None else str(val).strip()
                conn.execute(upsert, (p["id"], int(qid), v, now))
            D.audit(conn, f"player:{p['id']}", "save_predictions", {"keys": list(preds.keys())})
        broadcast("predictions")
        return jsonify({"ok": True, "locked": False})
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500


def _player_side_bets(conn, player_id: int) -> list[dict]:
    chosen = N.side_bet_picks(conn)
    out = []
    for bet in N.list_side_bets(conn):
        out.append({
            "id": bet["id"],
            "prompt": bet["prompt"],
            "choices": bet["choices"],
            "points": bet["points"],
            "status": bet["status"],
            "answer": bet.get("answer") or "",
            "kind": bet.get("kind") or "choice",
            "open_until": bet.get("open_until") or "",
            "pick": chosen.get(int(bet["id"]), {}).get(int(player_id), ""),
        })
    return out


@app.post("/api/reactions")
def api_reactions():
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    emoji = data.get("emoji") or ""
    conn = D.connect(demo)
    try:
        if (D.get_setting(conn, "reactions_enabled", "1") or "1") != "1":
            return jsonify({"error": "reactions_off", "message": "Reactions are off."}), 403
    finally:
        conn.close()
    token = (data.get("token") or "").strip()
    client_key = token or client_ip()
    ok, reason = reactions.tap(demo, client_key, emoji)
    if reason == "bad_emoji":
        return jsonify({"error": "bad_emoji", "message": "Pick one of the reaction buttons."}), 400
    if reason == "throttled":
        return jsonify({"error": "throttled", "message": "One reaction per second."}), 429
    return jsonify({"ok": True})


@app.get("/api/history")
def api_history():
    demo = use_demo()
    pool = request.args.get("pool") or D.POOL_POT
    if pool not in (D.POOL_POT, D.POOL_PUBLIC):
        pool = D.POOL_POT
    conn = D.connect(demo)
    try:
        return jsonify({"demo": demo, "pool": pool, "series": N.history_series(conn, pool)})
    finally:
        conn.close()


@app.post("/api/player/<token>/side-bets")
def api_player_side_bet(token):
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    try:
        with D.write_txn(demo) as conn:
            p = D.player_by_token(conn, token)
            if not p:
                return jsonify({"error": "not found"}), 404
            try:
                bet_id = int(data.get("side_bet_id"))
            except (TypeError, ValueError):
                return jsonify({"error": "bad_bet", "message": "Unknown side bet."}), 400
            bet = conn.execute("SELECT * FROM side_bets WHERE id=? AND active=1", (bet_id,)).fetchone()
            if not bet:
                return jsonify({"error": "bad_bet", "message": "Unknown side bet."}), 404
            row = dict(bet)
            if not N._bet_is_open(row):
                return jsonify({"error": "side_bet_closed", "message": "That side bet is closed."}), 403
            choices = json.loads(row["choices_json"] or "[]")
            value = str(data.get("value") or "").strip()
            kind = row.get("kind") or "choice"
            if kind == "closest":
                try:
                    float(value)
                except ValueError:
                    return jsonify({"error": "bad_choice", "message": "Enter a number."}), 400
            elif value not in choices:
                return jsonify({"error": "bad_choice", "message": "Pick one of the options."}), 400
            conn.execute(
                """INSERT INTO side_bet_picks(side_bet_id, player_id, value, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(side_bet_id, player_id) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (bet_id, p["id"], value, D.iso_now()),
            )
            D.audit(conn, f"player:{p['id']}", "side_bet", {"id": bet_id})
        broadcast("side_bets")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500


@app.post("/api/player/<token>/ticker")
def api_player_ticker(token):
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    body, err = N.clean_ticker(data.get("text"))
    if err:
        return jsonify({"error": "bad_message", "message": err}), 400
    try:
        with D.write_txn(demo) as conn:
            if (D.get_setting(conn, "ticker_enabled", "1") or "1") != "1":
                return jsonify({"error": "ticker_off", "message": "The ticker is off."}), 403
            p = D.player_by_token(conn, token)
            if not p:
                return jsonify({"error": "not found"}), 404
            if N.ticker_too_soon(conn, p["id"]):
                return jsonify({"error": "throttled", "message": "One message per minute."}), 429
            raw_name = html.unescape(p["name"]) if (p.get("pool") == D.POOL_PUBLIC) else p["name"]
            conn.execute(
                """INSERT INTO ticker_messages(player_id, player_name, pool, body, created_at, hidden, featured)
                   VALUES (?,?,?,?,?,0,0)""",
                (p["id"], html.escape(raw_name, quote=True), p.get("pool") or D.POOL_POT, body, D.iso_now()),
            )
        broadcast("ticker")
        return jsonify({"ok": True, "body": body})
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500


@app.post("/api/admin/side-bets")
def admin_side_bet_create():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    prompt = str(data.get("prompt") or "").strip()
    if not prompt or len(prompt) > 140:
        return jsonify({"error": "bad_prompt", "message": "Enter a question up to 140 characters."}), 400
    choices = data.get("choices") or []
    if isinstance(choices, str):
        choices = [c.strip() for c in choices.split("\n") if c.strip()]
    choices = [str(c).strip() for c in choices if str(c).strip()]
    kind = "closest" if str(data.get("kind") or "choice") == "closest" else "choice"
    if kind == "choice":
        if len(choices) < 2 or len(choices) > 8:
            return jsonify({"error": "bad_choices", "message": "Give 2 to 8 options."}), 400
        if len(set(choices)) != len(choices):
            return jsonify({"error": "bad_choices", "message": "Options must be different."}), 400
    open_until = str(data.get("open_until") or "").strip() or None
    try:
        points = int(data.get("points") or 5)
    except (TypeError, ValueError):
        return jsonify({"error": "bad_points", "message": "Points must be a whole number."}), 400
    if points < 1 or points > 50:
        return jsonify({"error": "bad_points", "message": "Points must be between 1 and 50."}), 400
    conn = D.connect(demo)
    try:
        conn.execute(
            """INSERT INTO side_bets(prompt, choices_json, points, status, created_at, active, kind, open_until)
               VALUES (?,?,?, 'open', ?, 1, ?, ?)""",
            (html.escape(prompt, quote=True), json.dumps(choices), points, D.iso_now(), kind, open_until),
        )
        D.audit(conn, "admin", "side_bet_create", {"prompt": prompt})
        conn.commit()
        broadcast("side_bets")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/side-bets/<int:bet_id>")
def admin_side_bet_update(bet_id):
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action") or ""
    conn = D.connect(demo)
    try:
        bet = conn.execute("SELECT * FROM side_bets WHERE id=?", (bet_id,)).fetchone()
        if not bet:
            return jsonify({"error": "not_found"}), 404
        if action == "open":
            conn.execute("UPDATE side_bets SET status='open' WHERE id=?", (bet_id,))
        elif action == "close":
            conn.execute("UPDATE side_bets SET status='closed' WHERE id=?", (bet_id,))
        elif action == "delete":
            conn.execute("UPDATE side_bets SET active=0 WHERE id=?", (bet_id,))
        elif action == "answer":
            answer = str(data.get("answer") or "").strip()
            kind = dict(bet).get("kind") or "choice"
            if kind == "closest":
                try:
                    float(answer)
                except ValueError:
                    return jsonify({"error": "bad_choice", "message": "Enter the numeric result."}), 400
            else:
                choices = json.loads(bet["choices_json"] or "[]")
                if answer not in choices:
                    return jsonify({"error": "bad_choice", "message": "Answer must be one of the options."}), 400
            conn.execute("UPDATE side_bets SET answer=?, status='closed' WHERE id=?", (answer, bet_id))
            record_results_history(conn)
        else:
            return jsonify({"error": "bad_action"}), 400
        D.audit(conn, "admin", "side_bet_update", {"id": bet_id, "action": action})
        conn.commit()
        broadcast("side_bets")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/ticker/<int:msg_id>")
def admin_ticker_update(msg_id):
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action") or ""
    conn = D.connect(demo)
    try:
        row = conn.execute("SELECT id FROM ticker_messages WHERE id=?", (msg_id,)).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        if action == "hide":
            conn.execute("UPDATE ticker_messages SET hidden=1 WHERE id=?", (msg_id,))
        elif action == "delete":
            conn.execute("DELETE FROM ticker_messages WHERE id=?", (msg_id,))
        elif action == "feature":
            conn.execute("UPDATE ticker_messages SET featured=1, hidden=0 WHERE id=?", (msg_id,))
        elif action == "unfeature":
            conn.execute("UPDATE ticker_messages SET featured=0 WHERE id=?", (msg_id,))
        else:
            return jsonify({"error": "bad_action"}), 400
        D.audit(conn, "admin", "ticker_update", {"id": msg_id, "action": action})
        conn.commit()
        broadcast("ticker")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/admin/ticker")
def admin_ticker_list():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM ticker_messages ORDER BY id DESC LIMIT 80")]
        return jsonify({"messages": rows})
    finally:
        conn.close()


# ---------- admin APIs ----------
@app.get("/api/admin/full")
def admin_full():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        players = D.list_players(conn, active_only=False, pool=D.POOL_POT)
        preds_all = D.get_all_predictions(conn)
        for p in players:
            preds = preds_all.get(p["id"], {})
            p["predictions"] = preds
            p["submitted"] = any(v not in (None, "") for v in preds.values()) or bool(p.get("tb_national_vote"))
            p["link_path"] = f"/p/{p['token']}"
        public_players = D.list_players(conn, active_only=True, pool=D.POOL_PUBLIC)
        ranked_pub, _, _, _ = build_leaderboard(conn, pool=D.POOL_PUBLIC)
        by_pub = {r["player_id"]: r for r in ranked_pub}
        for p in public_players:
            preds = preds_all.get(p["id"], {})
            info = by_pub.get(p["id"], {})
            p["predictions"] = preds
            p["submitted"] = any(v not in (None, "") for v in preds.values()) or bool(p.get("tb_national_vote"))
            p["total"] = info.get("total", 0)
            p["place"] = info.get("place")
            p["link_path"] = f"/p/{p['token']}"
            p["display_name"] = html.unescape(p["name"])
        questions = D.list_questions(conn, active_only=False)
        results = D.get_results(conn)
        settings = D.all_settings(conn)
        audit = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")]
        snaps = [dict(r) for r in conn.execute("SELECT id, label, kind, created_at FROM snapshots ORDER BY id DESC LIMIT 50")]
        _f = conn.execute("SELECT * FROM feed_status WHERE id=1").fetchone()
        feed = dict(_f) if _f else {}
        return jsonify({
            "demo": demo,
            "players": players,
            "public_players": public_players,
            "public_player_count": len(public_players),
            "questions": questions,
            "results": results,
            "settings": settings,
            "locked": D.entries_locked(conn),
            "audit": audit,
            "snapshots": snaps,
            "feed": feed,
            "side_bets": [
                {
                    "id": b["id"], "prompt": b["prompt"], "choices": b["choices"],
                    "points": b["points"], "status": b["status"], "answer": b.get("answer") or "",
                }
                for b in N.list_side_bets(conn)
            ],
            "server_now": D.iso_now(),
        })
    finally:
        conn.close()


@app.post("/api/admin/players")
def admin_upsert_player():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    try:
        with D.write_txn(demo) as conn:
            pid = data.get("id")
            if pid:
                row = conn.execute("SELECT id, pool FROM players WHERE id=?", (pid,)).fetchone()
                if not row:
                    return jsonify({"error": "not_found", "message": "Player not found."}), 404
                if row["pool"] == D.POOL_PUBLIC:
                    cleaned, name_err = D.clean_public_name(data.get("name"))
                    if name_err:
                        return jsonify({"error": "bad_name", "message": name_err}), 400
                    if D.public_name_taken(conn, data.get("name"), exclude_id=pid):
                        return jsonify({"error": "name_taken", "message": "That name is already taken."}), 409
                    conn.execute("UPDATE players SET name=? WHERE id=?", (cleaned, pid))
                    D.audit(conn, "admin", "rename_player", {"id": pid, "name": cleaned, "pool": "public"})
                else:
                    name = (data.get("name") or "").strip() or "Player"
                    conn.execute("UPDATE players SET name=? WHERE id=?", (name[:40], pid))
                    D.audit(conn, "admin", "rename_player", {"id": pid, "name": name, "pool": "pot"})
            else:
                # Pot only: slots 1–6. Public players sign up themselves.
                name = (data.get("name") or "").strip() or "Player"
                used = {r["slot"] for r in conn.execute(
                    "SELECT slot FROM players WHERE active=1 AND pool=?", (D.POOL_POT,)
                )}
                slot = next((i for i in range(1, 7) if i not in used), None)
                if slot is None:
                    return jsonify({"error": "max 6 players"}), 400
                token = secrets.token_urlsafe(18)
                conn.execute(
                    "INSERT INTO players(name, token, slot, created_at, pool) VALUES (?,?,?,?,?)",
                    (name[:40], token, slot, D.iso_now(), D.POOL_POT),
                )
                D.audit(conn, "admin", "add_player", {"name": name, "slot": slot})
        broadcast("players")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500


@app.post("/api/admin/public-players/<int:pid>/remove")
def admin_remove_public(pid):
    """Hide a public player. The row and predictions stay in the database."""
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    try:
        with D.write_txn(demo) as conn:
            row = conn.execute("SELECT id, pool, name FROM players WHERE id=?", (pid,)).fetchone()
            if not row or row["pool"] != D.POOL_PUBLIC:
                return jsonify({"error": "not_public", "message": "That is not a public player."}), 404
            conn.execute("UPDATE players SET active=0 WHERE id=?", (pid,))
            D.audit(conn, "admin", "remove_public_player", {"id": pid, "name": row["name"]})
        broadcast("players")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500


@app.post("/api/admin/players/<int:pid>/regenerate-token")
def admin_regen_token(pid):
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        token = secrets.token_urlsafe(18)
        conn.execute("UPDATE players SET token=? WHERE id=?", (token, pid))
        D.audit(conn, "admin", "regen_token", {"id": pid})
        conn.commit()
        broadcast("players")
        return jsonify({"ok": True, "token": token})
    finally:
        conn.close()


@app.post("/api/admin/lock")
def admin_lock():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        data = request.get_json(force=True, silent=True) or {}
        action = data.get("action", "lock")  # lock | reopen
        if action == "lock":
            D.set_setting(conn, "entries_open", "0")
            D.audit(conn, "admin", "lock_entries", {})
        else:
            D.set_setting(conn, "entries_open", "1")
            # optionally push lock_at into the future
            if data.get("lock_at"):
                D.set_setting(conn, "lock_at", data["lock_at"])
            D.audit(conn, "admin", "reopen_entries", {"lock_at": data.get("lock_at")})
        conn.commit()
        broadcast("lock")
        return jsonify({"ok": True, "locked": D.entries_locked(conn)})
    finally:
        conn.close()


@app.post("/api/admin/settings")
def admin_settings():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        data = request.get_json(force=True, silent=True) or {}
        if "public_player_cap" in data:
            try:
                cap = int(str(data["public_player_cap"]).strip())
            except (TypeError, ValueError):
                return jsonify({"error": "bad_cap", "message": "Cap must be a whole number."}), 400
            if cap < 0 or cap > 10000:
                return jsonify({"error": "bad_cap", "message": "Cap must be between 0 and 10000."}), 400
            data["public_player_cap"] = str(cap)
        if "public_signups_open" in data:
            flag = str(data["public_signups_open"]).strip().lower()
            data["public_signups_open"] = "1" if flag in ("1", "true", "yes", "on") else "0"
        allowed = {
            "lock_at", "event_title", "board_mode", "reporting_pct",
            "feed_enabled", "feed_url", "feed_poll_seconds", "demo_mode",
            "prize_first", "prize_second", "entry_fee",
            "public_signups_open", "public_player_cap",
            "reactions_enabled", "ticker_enabled", "weekend_mode",
            "feed_manual_off",
            "advance_votes_2026", "advance_as_of", "advance_votes_2023_same",
            "advance_votes_2020_same", "advance_votes_2023_total",
            "parliament_2023_json",
        }
        if "weekend_mode" in data:
            data["weekend_mode"] = "1" if str(data["weekend_mode"]).strip().lower() in ("1", "true", "yes", "on") else "0"
        for flag in ("reactions_enabled", "ticker_enabled"):
            if flag in data:
                data[flag] = "1" if str(data[flag]).strip().lower() in ("1", "true", "yes", "on") else "0"
        if "feed_enabled" in data:
            flag = str(data.get("feed_enabled") or "").strip().lower()
            data["feed_enabled"] = "1" if flag in ("1", "true", "yes", "on") else "0"
            # An admin switch is deliberate. Auto-enable at lock time will not undo it.
            data["feed_manual_off"] = "0" if data["feed_enabled"] == "1" else "1"
        if "feed_poll_seconds" in data:
            try:
                secs = int(str(data["feed_poll_seconds"]).strip())
            except (TypeError, ValueError):
                return jsonify({"error": "bad_interval", "message": "Interval must be a whole number of seconds."}), 400
            if secs < 30 or secs > 600:
                return jsonify({"error": "bad_interval", "message": "Interval must be between 30 and 600 seconds."}), 400
            data["feed_poll_seconds"] = str(secs)
        for k, v in data.items():
            if k in allowed:
                D.set_setting(conn, k, str(v))
        D.audit(conn, "admin", "update_settings", data)
        conn.commit()
        if data.get("feed_enabled") == "1":
            feed_poller.start()
        broadcast("settings")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/questions")
def admin_questions():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        if D.entries_locked(conn) and not (request.get_json(force=True, silent=True) or {}).get("force"):
            # allow edits only before lock by default
            pass
        data = request.get_json(force=True, silent=True) or {}
        qid = data.get("id")
        if data.get("delete") and qid:
            conn.execute("UPDATE questions SET active=0 WHERE id=?", (qid,))
            D.audit(conn, "admin", "deactivate_question", {"id": qid})
        elif qid:
            conn.execute(
                """UPDATE questions SET label=?, scoring_type=?, max_points=?, sort_order=?, meta_json=?, key=COALESCE(?, key)
                   WHERE id=?""",
                (
                    data.get("label"),
                    data.get("scoring_type"),
                    int(data.get("max_points", 10)),
                    int(data.get("sort_order", 99)),
                    json.dumps(data.get("meta") or {}),
                    data.get("key"),
                    qid,
                ),
            )
            D.audit(conn, "admin", "update_question", {"id": qid})
        else:
            key = data.get("key") or f"q_{secrets.token_hex(4)}"
            conn.execute(
                """INSERT INTO questions(key, label, scoring_type, max_points, sort_order, meta_json)
                   VALUES (?,?,?,?,?,?)""",
                (
                    key,
                    data.get("label") or "New question",
                    data.get("scoring_type") or "vote_band",
                    int(data.get("max_points", 10)),
                    int(data.get("sort_order", 99)),
                    json.dumps(data.get("meta") or {}),
                ),
            )
            D.audit(conn, "admin", "add_question", {"key": key})
        conn.commit()
        broadcast("questions")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/results")
def admin_results():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        data = request.get_json(force=True, silent=True) or {}
        items = data.get("results") or {}
        source = data.get("source") or "manual"
        reporting = data.get("reporting_pct")
        now = D.iso_now()
        for qid, val in items.items():
            v = None if val is None or val == "" else str(val)
            if v is None:
                conn.execute("DELETE FROM results WHERE question_id=?", (int(qid),))
            else:
                conn.execute(
                    """INSERT INTO results(question_id, value, source, updated_at) VALUES (?,?,?,?)
                       ON CONFLICT(question_id) DO UPDATE SET value=EXCLUDED.value, source=EXCLUDED.source, updated_at=EXCLUDED.updated_at""",
                    (int(qid), v, source, now),
                )
        if reporting is not None:
            D.set_setting(conn, "reporting_pct", str(reporting))
        D.audit(conn, "admin", "set_results", {"keys": list(items.keys()), "source": source})
        record_results_history(conn)
        conn.commit()
        broadcast("results")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/snapshot")
def admin_snapshot():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        data = request.get_json(force=True, silent=True) or {}
        kind = data.get("kind") or "election_night"
        label = data.get("label") or f"{kind} {D.iso_now()}"
        # Build payload from current state
        with app.test_request_context("/api/state" + ("?demo=1" if demo else "")):
            # manually build
            pass
        ranked, questions, players, results_raw = build_leaderboard(conn, pool=D.POOL_POT)
        payload = {
            "pool": D.POOL_POT,
            "leaderboard": ranked,
            "results": results_raw,
            "reporting_pct": D.get_setting(conn, "reporting_pct"),
            "at": D.iso_now(),
        }
        conn.execute(
            "INSERT INTO snapshots(label, kind, created_at, payload_json) VALUES (?,?,?,?)",
            (label, kind, D.iso_now(), json.dumps(payload)),
        )
        if kind == "election_night":
            D.set_setting(conn, "board_mode", "frozen")
        if kind == "official":
            D.set_setting(conn, "board_mode", "official")
        D.audit(conn, "admin", "snapshot", {"kind": kind, "label": label})
        conn.commit()
        broadcast("snapshot")
        return jsonify({"ok": True, "leaderboard": ranked})
    finally:
        conn.close()


def _csv_for_pool(conn, pool: str) -> tuple[str, str]:
    ranked, questions, players, _results_raw = build_leaderboard(conn, pool=pool)
    preds = D.get_all_predictions(conn)
    buf = io.StringIO()
    w = csv.writer(buf)
    if pool == D.POOL_POT:
        header = ["slot", "name", "total", "tb_national_vote", "tb_error", "place", "prize", "still_in_it"]
        filename = "sweepstakes_export.csv"
    else:
        header = ["name", "total", "tb_national_vote", "tb_error", "place", "still_in_it"]
        filename = "public_results.csv"
    for q in questions:
        header.append(f"pred:{q['key']}")
        header.append(f"pts:{q['key']}")
    w.writerow(header)
    by_id = {r["player_id"]: r for r in ranked}
    for p in players:
        r = by_id[p["id"]]
        if pool == D.POOL_POT:
            row = [p["slot"], p["name"], r["total"], r.get("tb_national_vote"), r.get("tb_error"), r.get("place"), r.get("prize"), r.get("still_in_it")]
        else:
            row = [html.unescape(p["name"]), r["total"], r.get("tb_national_vote"), r.get("tb_error"), r.get("place"), r.get("still_in_it")]
        detail = {str(d["question_id"]): d for d in r["detail"]}
        predmap = preds.get(p["id"], {})
        for q in questions:
            row.append(predmap.get(str(q["id"]), ""))
            d = detail.get(str(q["id"]), {})
            row.append(d.get("points") if d.get("points") is not None else "pending")
        w.writerow(row)
    return buf.getvalue(), filename


def _export_response(demo: bool, pool: str):
    err = require_admin()
    if err:
        return err
    conn = D.connect(demo)
    try:
        body, filename = _csv_for_pool(conn, pool)
        resp = make_response(body)
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return resp
    finally:
        conn.close()


@app.get("/api/admin/export.csv")
def admin_export():
    return _export_response(use_demo(), D.POOL_POT)


@app.get("/api/admin/export-public.csv")
def admin_export_public():
    return _export_response(use_demo(), D.POOL_PUBLIC)


@app.post("/api/admin/demo/reset")
def admin_demo_reset():
    err = require_admin()
    if err:
        return err
    if D.using_postgres():
        conn = D.connect(False)
        try:
            conn.execute("DROP SCHEMA IF EXISTS demo CASCADE")
            conn.commit()
        finally:
            conn.close()
    else:
        path = D.db_path(True)
        if path.exists():
            path.unlink()
        for s in (str(path) + "-wal", str(path) + "-shm"):
            try:
                Path(s).unlink(missing_ok=True)
            except Exception:
                pass
    D.init_db(demo=True)
    conn = D.connect(True)
    try:
        D.set_setting(conn, "entries_open", "1")
        D.set_setting(conn, "lock_at", "2099-01-01T00:00:00+13:00")
        N.seed_demo_night(conn)
        conn.commit()
    finally:
        conn.close()
    broadcast("demo_reset")
    return jsonify({"ok": True})


@app.post("/api/admin/demo/step")
def admin_demo_step():
    """Advance demo results through staged counting."""
    err = require_admin()
    if err:
        return err
    # Always operate on demo DB
    conn = D.connect(True)
    try:
        data = request.get_json(force=True, silent=True) or {}
        stage = int(data.get("stage", 1))
        # Stages: 0 empty, 1 early, 2 mid, 3 late with tie possibility, 4 final
        stages = {
            1: {
                "reporting": 12,
                "votes": {"nat_vote": 36.2, "lab_vote": 28.1, "nzf_vote": 7.4, "act_vote": 8.9, "grn_vote": 12.5, "tpm_vote": 2.8},
                "seats": {"nat_seats": 42, "lab_seats": 32},
                "most": "National",
                "bloc": 58,
            },
            2: {
                "reporting": 48,
                "votes": {"nat_vote": 35.4, "lab_vote": 28.8, "nzf_vote": 7.9, "act_vote": 8.5, "grn_vote": 12.1, "tpm_vote": 3.4},
                "seats": {"nat_seats": 44, "lab_seats": 34},
                "most": "National",
                "bloc": 61,
            },
            3: {
                "reporting": 82,
                "votes": {"nat_vote": 35.1, "lab_vote": 29.2, "nzf_vote": 8.1, "act_vote": 8.2, "grn_vote": 11.8, "tpm_vote": 3.6},
                "seats": {"nat_seats": 45, "lab_seats": 35},
                "most": "National",
                "bloc": 62,
            },
            4: {
                "reporting": 100,
                "votes": {"nat_vote": 34.8, "lab_vote": 29.5, "nzf_vote": 8.3, "act_vote": 8.0, "grn_vote": 11.6, "tpm_vote": 3.9},
                "seats": {"nat_seats": 46, "lab_seats": 36},
                "most": "National",
                "bloc": 63,
            },
        }
        st = stages.get(stage, stages[1])
        qmap = {q["key"]: q["id"] for q in D.list_questions(conn)}
        items = {}
        for k, v in {**st["votes"], **st["seats"]}.items():
            if k in qmap:
                items[str(qmap[k])] = v
        if "most_seats" in qmap:
            items[str(qmap["most_seats"])] = st["most"]
        if "right_bloc" in qmap:
            items[str(qmap["right_bloc"])] = st["bloc"]
        # Leave tpm seats etc missing intentionally at early stages — pending
        now = D.iso_now()
        for qid, val in items.items():
            conn.execute(
                """INSERT INTO results(question_id, value, source, updated_at) VALUES (?,?, 'manual', ?)
                   ON CONFLICT(question_id) DO UPDATE SET value=EXCLUDED.value, source='manual', updated_at=EXCLUDED.updated_at""",
                (int(qid), str(val), now),
            )
        D.set_setting(conn, "reporting_pct", str(st["reporting"]))
        D.set_setting(conn, "board_mode", "live")
        # Lock entries so leaderboard scores show
        D.set_setting(conn, "entries_open", "0")
        D.audit(conn, "admin", "demo_step", {"stage": stage})
        record_results_history(conn)
        conn.commit()
        broadcast("results")
        return jsonify({"ok": True, "stage": stage, "reporting": st["reporting"]})
    finally:
        conn.close()


@app.post("/api/admin/feed/poll")
def admin_feed_poll():
    err = require_admin()
    if err:
        return err
    result = feed_poller.poll_once(demo=use_demo())
    return jsonify(result)


@app.post("/api/admin/feed/preview")
def admin_feed_preview():
    """Fetch and parse the feed without writing results. Safe while the poller is off."""
    err = require_admin()
    if err:
        return err
    return jsonify(feed_poller.preview(demo=use_demo()))


@app.post("/api/admin/party-table")
def admin_party_table():
    """Save raw party vote% + seats; derive Q answers (overrideable)."""
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        data = request.get_json(force=True, silent=True) or {}
        votes = data.get("votes") or {}
        seats = data.get("seats") or {}
        # coerce
        votes_f, seats_i = {}, {}
        for k, v in votes.items():
            if v is None or v == "":
                continue
            votes_f[k] = float(v)
        for k, v in seats.items():
            if v is None or v == "":
                continue
            seats_i[k] = int(round(float(v)))
        derived = derive_from_party_table(votes_f, seats_i)
        D.set_setting(conn, "party_table_json", json.dumps({"votes": votes_f, "seats": seats_i, "derived": derived}))
        if data.get("reporting_pct") is not None:
            D.set_setting(conn, "reporting_pct", str(data.get("reporting_pct")))
        # Map derived + raw votes onto question results unless manual_overrides provided
        overrides = data.get("overrides") or {}  # question key -> value
        qmap = {q["key"]: q for q in D.list_questions(conn)}
        now = D.iso_now()
        # vote questions store numeric % (bands resolved at score time)
        for key in ("nat_vote", "lab_vote", "grn_vote", "act_vote", "nzf_vote", "tpm_vote"):
            party = {"nat_vote":"National","lab_vote":"Labour","grn_vote":"Green","act_vote":"ACT","nzf_vote":"NZ First","tpm_vote":"Te Pāti Māori"}[key]
            if key in overrides:
                val = overrides[key]
            elif party in votes_f:
                val = votes_f[party]
            else:
                continue
            q = qmap.get(key)
            if not q:
                continue
            conn.execute(
                """INSERT INTO results(question_id, value, source, updated_at) VALUES (?,?, 'manual', ?)
                   ON CONFLICT(question_id) DO UPDATE SET value=EXCLUDED.value, source='manual', updated_at=EXCLUDED.updated_at""",
                (q["id"], str(val), now),
            )
        for key in ("most_seats", "parties_in", "bloc_61", "minor_highest"):
            val = overrides[key] if key in overrides else derived.get(key)
            if val is None:
                continue
            q = qmap.get(key)
            if not q:
                continue
            conn.execute(
                """INSERT INTO results(question_id, value, source, updated_at) VALUES (?,?, 'manual', ?)
                   ON CONFLICT(question_id) DO UPDATE SET value=EXCLUDED.value, source='manual', updated_at=EXCLUDED.updated_at""",
                (q["id"], str(val), now),
            )
        D.audit(conn, "admin", "party_table", {"votes": votes_f, "seats": seats_i, "derived": derived})
        record_results_history(conn)
        conn.commit()
        broadcast("results")
        return jsonify({"ok": True, "derived": derived})
    finally:
        conn.close()



def _pinned_recap(conn, pool: str):
    row = conn.execute(
        "SELECT * FROM recap_cards WHERE pool=? AND pinned=1 ORDER BY id DESC LIMIT 1",
        (pool,),
    ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM recap_cards WHERE pool=? ORDER BY id DESC LIMIT 1",
            (pool,),
        ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload_json"])
    payload["id"] = row["id"]
    payload["day"] = row["day"]
    return payload


def _parliament_view(house: dict, mode: str) -> dict:
    """Current House before a count. After results, the 2026 allocation, provisional until official."""
    if house.get("has_results") and house.get("live"):
        view = dict(house["live"])
        final = (mode or "") == "official"
        view["label"] = "New Parliament 2026" if final else "New Parliament 2026 (provisional)"
        view["projected"] = True
        view["provisional"] = not final
        return view
    view = dict(house.get("current") or {})
    view["projected"] = False
    view["provisional"] = False
    return view


_COALITION_WORDS = ("coalition", "deal", "negotiat", "kingmaker", "confidence and supply")


def coalition_headlines(items) -> list:
    matched = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        blob = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
        if not any(word in blob for word in _COALITION_WORDS):
            continue
        matched.append({
            "title": item.get("title") or "",
            "url": item.get("url") or "",
            "source": item.get("source") or "",
            "published": item.get("published") or "",
        })
    matched.sort(key=lambda row: row.get("published") or "", reverse=True)
    return matched[:10]


def _awaiting_recap(pool: str) -> dict:
    return {
        "pool": pool,
        "title": "Recap",
        "at": D.iso_now(),
        "awaiting": True,
        "message": "No results yet, recap fills in once counting starts",
        "leader": None,
        "leader_total": None,
        "top": [],
        "best_call": None,
        "best_points": None,
        "worst_call": None,
        "worst_lost": None,
        "biggest_mover": None,
    }


def _attach_parliament(payload: dict, house: dict, mode: str) -> dict:
    view = _parliament_view(house, mode)
    payload["parliament"] = view
    payload["house_seats"] = view.get("seats") or {}
    payload["house_order"] = view.get("order") or []
    return payload


def _build_recap(conn, pool: str) -> dict:
    settings = D.all_settings(conn)
    house = house_payload(conn, settings)
    mode = settings.get("board_mode") or "live"
    if not house.get("has_results"):
        return _attach_parliament(_awaiting_recap(pool), house, mode)
    ranked, questions, _players, _results = build_leaderboard(conn, pool=pool)
    if pool == D.POOL_PUBLIC:
        nat = national_actual_from_results(conn, questions, {k: v["value"] for k, v in D.get_results(conn).items()})
        ranked = enrich_leaderboard(N.public_rows_with_side_bets(ranked, conn, nat), questions)
    mover = W.biggest_mover(N.history_series(conn, pool))
    payload = W.recap_from_state(pool, ranked, mover, D.iso_now())
    payload["awaiting"] = False
    return _attach_parliament(payload, house, mode)


def _serve_recap(conn, pool: str) -> dict:
    """Before the count, never crown a leader, including a recap pinned too early."""
    settings = D.all_settings(conn)
    house = house_payload(conn, settings)
    mode = settings.get("board_mode") or "live"
    if not house.get("has_results"):
        payload = _attach_parliament(_awaiting_recap(pool), house, mode)
        payload["pinned"] = False
        return payload
    pinned = _pinned_recap(conn, pool)
    payload = pinned or _build_recap(conn, pool)
    if pinned:
        payload = _attach_parliament(payload, house, mode)
        if not payload.get("top") or len(payload.get("top") or []) < 4:
            fresh = _build_recap(conn, pool)
            payload["top"] = fresh.get("top") or payload.get("top")
            payload["leader"] = fresh.get("leader", payload.get("leader"))
            payload["leader_total"] = fresh.get("leader_total", payload.get("leader_total"))
    payload["pinned"] = bool(pinned)
    payload.setdefault("awaiting", False)
    return payload


@app.get("/recap")
def recap_page():
    return send_from_directory(ROOT / "static", "recap.html")


@app.get("/open/recap")
def open_recap_page():
    return send_from_directory(ROOT / "static", "recap.html")


@app.get("/coalition")
def coalition_page():
    return send_from_directory(ROOT / "static", "coalition.html")


@app.get("/api/recap")
def api_recap():
    demo = use_demo()
    pool = D.POOL_PUBLIC if request.args.get("pool") == "public" else D.POOL_POT
    conn = D.connect(demo)
    try:
        return jsonify(_serve_recap(conn, pool))
    finally:
        conn.close()


@app.get("/recap.png")
@app.get("/open/recap.png")
def recap_png():
    demo = use_demo()
    pool = D.POOL_PUBLIC if request.path.startswith("/open") or request.args.get("pool") == "public" else D.POOL_POT
    conn = D.connect(demo)
    try:
        payload = _serve_recap(conn, pool)
    finally:
        conn.close()
    body = W.render_recap_png(payload)
    return Response(body, mimetype="image/png", headers={"Content-Disposition": "attachment; filename=recap.png"})


@app.post("/api/admin/recap")
def admin_recap():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    pool = D.POOL_PUBLIC if data.get("pool") == "public" else D.POOL_POT
    conn = D.connect(demo)
    try:
        payload = _build_recap(conn, pool)
        day = D.now_nz().date().isoformat()
        conn.execute("UPDATE recap_cards SET pinned=0 WHERE pool=?", (pool,))
        conn.execute(
            "INSERT INTO recap_cards(pool, day, created_at, pinned, payload_json) VALUES (?,?,?,?,?)",
            (pool, day, D.iso_now(), 1, json.dumps(payload)),
        )
        conn.commit()
        broadcast("recap")
        return jsonify({"ok": True, "recap": payload})
    finally:
        conn.close()


@app.get("/api/coalition")
def api_coalition():
    demo = use_demo()
    conn = D.connect(demo)
    try:
        settings = D.all_settings(conn)
        house = house_payload(conn, settings)
        mode = settings.get("board_mode") or "live"
        view = _parliament_view(house, mode)
        rows = []
        for r in conn.execute("SELECT * FROM coalition_events WHERE active=1 ORDER BY id DESC"):
            rows.append({
                "id": r["id"],
                "at": r["at"],
                "headline": r["headline"],
                "source_url": r["source_url"] or "",
                "parties": r["parties"] or "",
            })
        return jsonify({
            "label": view.get("label") or "",
            "source": "projected" if view.get("projected") else "current",
            "provisional": bool(view.get("provisional")),
            "house": view.get("house") or 0,
            "majority": view.get("majority") or 0,
            "blocs": P.coalition_options(view.get("seats") or {}, view.get("majority") or 62),
            "news": coalition_headlines(_json_setting(settings, "news_json", [])),
            "events": rows,
        })
    finally:
        conn.close()


@app.post("/api/admin/coalition")
def admin_coalition():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    headline = str(data.get("headline") or "").strip()
    if not headline or len(headline) > 180:
        return jsonify({"error": "bad_headline", "message": "Enter a headline up to 180 characters."}), 400
    try:
        source = W.clean_http_url(data.get("source_url"))
    except ValueError as exc:
        return jsonify({"error": "bad_url", "message": str(exc)}), 400
    conn = D.connect(demo)
    try:
        conn.execute(
            "INSERT INTO coalition_events(at, headline, source_url, parties, created_at, active) VALUES (?,?,?,?,?,1)",
            (D.iso_now(), headline, source, str(data.get("parties") or "").strip()[:80], D.iso_now()),
        )
        conn.commit()
        broadcast("coalition")
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/projection")
def admin_projection():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    data = request.get_json(force=True, silent=True) or {}
    conn = D.connect(demo)
    try:
        D.set_setting(conn, "projection_json", json.dumps({
            "special_percent": data.get("special_percent"),
            "shifts": data.get("shifts") or {},
            "electorates": data.get("electorates") or {},
        }))
        conn.commit()
        broadcast("projection")
        return jsonify({"ok": True})
    finally:
        conn.close()


def create_app():
    D.init_db(False)
    D.init_db(True)
    # Tests set SWEEP_DISABLE_FETCH=1 so they do not hit elections.nz or RSS.
    if os.environ.get("SWEEP_DISABLE_FETCH") != "1":
        feed_poller.start()
    return app


create_app()

if __name__ == "__main__":
    # create_app() already ran on import
    conn = D.connect(False)
    try:
        if D.get_setting(conn, "feed_enabled", "0") == "1":
            feed_poller.start()
    finally:
        conn.close()
    if not admin_password():
        print("WARNING: ADMIN_PASSWORD env var is not set — admin login will be refused.", flush=True)
    print(f"Sweepstakes listening on http://127.0.0.1:{PORT}", flush=True)
    print(f"DB: {D.db_path(False)}", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
