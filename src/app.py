#!/usr/bin/env python3
"""NZ Election Night Sweepstakes 2026 — Flask server."""
from __future__ import annotations
import csv
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
from scoring import score_player, rank_players, derive_from_party_table, actual_band_label, BANDS
from feed import FeedPoller

PORT = int(os.environ.get("PORT", "8787"))

app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
_sk = (os.environ.get("SECRET_KEY") or "").strip()
if not _sk:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.secret_key = _sk

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


def build_leaderboard(conn):
    questions = D.list_questions(conn)
    players = D.list_players(conn)
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
    resp = send_from_directory(ROOT / "static", "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/api/player/<token>/manifest.webmanifest")
def player_manifest(token):
    """Per-player manifest so Add to Home Screen opens their ballot."""
    demo = use_demo()
    conn = D.connect(demo)
    try:
        p = D.player_by_token(conn, token)
        if not p:
            return jsonify({"error": "not found"}), 404
        start = f"/p/{token}"
        if demo:
            start = f"/demo/p/{token}"
        body = {
            "name": f"Election Sweepstakes 2026 — {p['name']}",
            "short_name": "Sweepstakes",
            "description": "NZ Election Night Sweepstakes",
            "start_url": start,
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#070b14",
            "theme_color": "#070b14",
            "icons": [
                {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        }
        resp = jsonify(body)
        resp.headers["Content-Type"] = "application/manifest+json"
        return resp
    finally:
        conn.close()


@app.get("/")
def home():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/join")
def join_page():
    return send_from_directory(ROOT / "static", "join.html")


@app.get("/p/<token>")
def play_page(token):
    return send_from_directory(ROOT / "static", "play.html")


@app.get("/live")
def live_page():
    return send_from_directory(ROOT / "static", "live.html")


@app.get("/compare")
def compare_page():
    return send_from_directory(ROOT / "static", "compare.html")


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


# ---------- public state ----------
@app.get("/api/state")

def api_state():
    demo = use_demo()
    conn = D.connect(demo)
    try:
        settings = D.all_settings(conn)
        locked = D.entries_locked(conn)
        questions = D.list_questions(conn)
        players = D.list_players(conn)
        results_raw = D.get_results(conn)
        results_meta = results_raw
        if locked:
            ranked, questions, players, results_raw = build_leaderboard(conn)
            results_meta = results_raw
        else:
            ranked = []
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
                    "pending_max": sum(q["max_points"] for q in questions),
                    "max_possible": sum(q["max_points"] for q in questions),
                    "max_possible_now": sum(q["max_points"] for q in questions),
                    "still_in_it": True,
                    "is_last": False,
                    "best_call": None,
                    "worst_call": None,
                    "detail": [],
                    "tb_national_vote": None,
                })
        _f = conn.execute("SELECT * FROM feed_status WHERE id=1").fetchone()
        feed = dict(_f) if _f else {}

        public_players = [{"id": p["id"], "name": p["name"], "slot": p["slot"]} for p in players]

        return jsonify({
            "demo": demo,
            "settings": {
                "event_title": settings.get("event_title"),
                "lock_at": settings.get("lock_at"),
                "entries_open": settings.get("entries_open"),
                "board_mode": settings.get("board_mode"),
                "reporting_pct": settings.get("reporting_pct"),
                "prize_first": settings.get("prize_first"),
                "prize_second": settings.get("prize_second"),
                "entry_fee": settings.get("entry_fee"),
                "most_seats_tie_rule": settings.get("most_seats_tie_rule"),
                "seats_note": settings.get("seats_note"),
                "demo_mode": settings.get("demo_mode"),
                "feed_enabled": settings.get("feed_enabled"),
            },
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
            "players": public_players,
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
        })
    finally:
        conn.close()


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
            "player": {"id": p["id"], "name": p["name"], "slot": p["slot"], "token": p["token"], "tb_national_vote": p.get("tb_national_vote")},
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
            },
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
            name = (data.get("name") or "").strip()
            if name and name != p["name"]:
                conn.execute("UPDATE players SET name=? WHERE id=?", (name[:40], p["id"]))
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


# ---------- admin APIs ----------
@app.get("/api/admin/full")
def admin_full():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        players = D.list_players(conn, active_only=False)
        for p in players:
            preds = D.get_predictions(conn, p["id"])
            p["predictions"] = preds
            p["submitted"] = any(v not in (None, "") for v in preds.values())
            p["link_path"] = f"/p/{p['token']}"
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
            "questions": questions,
            "results": results,
            "settings": settings,
            "locked": D.entries_locked(conn),
            "audit": audit,
            "snapshots": snaps,
            "feed": feed,
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
    conn = D.connect(demo)
    try:
        data = request.get_json(force=True, silent=True) or {}
        pid = data.get("id")
        name = (data.get("name") or "").strip() or "Player"
        if pid:
            conn.execute("UPDATE players SET name=? WHERE id=?", (name[:40], pid))
            D.audit(conn, "admin", "rename_player", {"id": pid, "name": name})
        else:
            # find free slot 1-6
            used = {r["slot"] for r in conn.execute("SELECT slot FROM players WHERE active=1")}
            slot = next((i for i in range(1, 7) if i not in used), None)
            if slot is None:
                return jsonify({"error": "max 6 players"}), 400
            token = secrets.token_urlsafe(18)
            conn.execute(
                "INSERT INTO players(name, token, slot, created_at) VALUES (?,?,?,?)",
                (name[:40], token, slot, D.iso_now()),
            )
            D.audit(conn, "admin", "add_player", {"name": name, "slot": slot})
        conn.commit()
        broadcast("players")
        return jsonify({"ok": True})
    finally:
        conn.close()


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
        allowed = {
            "lock_at", "event_title", "board_mode", "reporting_pct",
            "feed_enabled", "feed_url", "feed_poll_seconds", "demo_mode",
            "prize_first", "prize_second", "entry_fee",
        }
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
        ranked, questions, players, results_raw = build_leaderboard(conn)
        payload = {
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


@app.get("/api/admin/export.csv")
def admin_export():
    err = require_admin()
    if err:
        return err
    demo = use_demo()
    conn = D.connect(demo)
    try:
        ranked, questions, players, results_raw = build_leaderboard(conn)
        preds = D.get_all_predictions(conn)
        buf = io.StringIO()
        w = csv.writer(buf)
        header = ["slot", "name", "total", "tb_national_vote", "tb_error", "place", "prize", "still_in_it"]
        for q in questions:
            header.append(f"pred:{q['key']}")
            header.append(f"pts:{q['key']}")
        w.writerow(header)
        by_id = {r["player_id"]: r for r in ranked}
        for p in players:
            r = by_id[p["id"]]
            row = [p["slot"], p["name"], r["total"], r.get("tb_national_vote"), r.get("tb_error"), r.get("place"), r.get("prize"), r.get("still_in_it")]
            detail = {d["question_id"]: d for d in r["detail"]}
            predmap = preds.get(p["id"], {})
            for q in questions:
                row.append(predmap.get(str(q["id"]), ""))
                d = detail.get(str(q["id"]), {})
                row.append(d.get("points") if d.get("points") is not None else "pending")
            w.writerow(row)
        resp = make_response(buf.getvalue())
        resp.headers["Content-Type"] = "text/csv"
        resp.headers["Content-Disposition"] = "attachment; filename=sweepstakes_export.csv"
        return resp
    finally:
        conn.close()


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
    result = feed_poller.poll_once(demo=False)
    return jsonify(result)


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
        conn.commit()
        broadcast("results")
        return jsonify({"ok": True, "derived": derived})
    finally:
        conn.close()



def create_app():
    D.init_db(False)
    D.init_db(True)
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
