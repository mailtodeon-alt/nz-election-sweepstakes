"""Weekend views after the frozen pot snapshot. Display only for the pot result."""
from __future__ import annotations

import html
import io
import json
import math
from datetime import datetime
from urllib.parse import urlparse

from PIL import Image, ImageDraw, ImageFont

import db as D
import projection as P
from scoring import derive_from_party_table, score_question

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def classify_call(q: dict, detail: dict) -> str:
    if not detail or detail.get("status") != "scored" or detail.get("points") is None:
        return "pending"
    points = int(detail["points"])
    maximum = int(detail.get("max") or q.get("max_points") or 0)
    kind = q.get("scoring_type")
    if kind in ("vote_band_mc", "parties_band_mc"):
        if points >= maximum and maximum:
            return "right"
        if points > 0:
            return "adjacent"
        return "wrong"
    if maximum and points >= maximum:
        return "right"
    return "wrong"


def latest_rows(ranked: list[dict], questions: list[dict]) -> list[dict]:
    qmap = {str(q["id"]): q for q in questions}
    out = []
    for row in ranked:
        counts = {"right": 0, "adjacent": 0, "wrong": 0, "pending": 0}
        for detail in row.get("detail") or []:
            q = qmap.get(str(detail.get("question_id")), {})
            counts[classify_call(q, detail)] += 1
        out.append({
            "player_id": row.get("player_id"),
            "name": row.get("name"),
            "right": counts["right"],
            "adjacent": counts["adjacent"],
            "wrong": counts["wrong"],
            "pending": counts["pending"],
            "latest_points": int(row.get("question_total", row.get("total")) or 0),
        })
    return out


def biggest_mover(series: list[dict]) -> dict | None:
    best = None
    for item in series:
        points = [p for p in item.get("points") or [] if p.get("place")]
        if len(points) < 2:
            continue
        delta = int(points[0]["place"]) - int(points[-1]["place"])
        candidate = {
            "name": item.get("name"),
            "from_place": int(points[0]["place"]),
            "to_place": int(points[-1]["place"]),
            "delta": delta,
        }
        if best is None or candidate["delta"] > best["delta"]:
            best = candidate
    return best


def clean_http_url(raw) -> str:
    if raw is None or str(raw).strip() == "":
        return ""
    url = str(raw).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Link must start with http:// or https://")
    return url


def record_count_tick(conn: D.Conn) -> None:
    raw = D.get_setting(conn, "party_table_json") or ""
    votes, seats = {}, {}
    if raw:
        try:
            parsed = json.loads(raw)
            votes = parsed.get("votes") or {}
            seats = parsed.get("seats") or {}
        except Exception:
            votes, seats = {}, {}
    if not votes:
        questions = {q["key"]: q for q in D.list_questions(conn)}
        results = D.get_results(conn)
        key_party = {
            "nat_vote": "National", "lab_vote": "Labour", "grn_vote": "Green",
            "act_vote": "ACT", "nzf_vote": "NZ First", "tpm_vote": "Te Pāti Māori",
        }
        for key, party in key_party.items():
            q = questions.get(key)
            if not q:
                continue
            cell = results.get(str(q["id"]))
            if not cell or cell.get("value") in (None, ""):
                continue
            try:
                votes[party] = float(cell["value"])
            except (TypeError, ValueError):
                pass
    conn.execute(
        "INSERT INTO count_ticks(at, reporting_pct, votes_json, seats_json) VALUES (?,?,?,?)",
        (D.iso_now(), D.get_setting(conn, "reporting_pct") or "", json.dumps(votes), json.dumps(seats)),
    )


def recent_ticks(conn: D.Conn, limit: int = 12) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM count_ticks ORDER BY id DESC LIMIT ?", (limit,))]
    rows.reverse()
    out = []
    for row in rows:
        try:
            votes = json.loads(row["votes_json"] or "{}")
        except Exception:
            votes = {}
        try:
            seats = json.loads(row["seats_json"] or "{}")
        except Exception:
            seats = {}
        out.append({
            "at": row["at"],
            "reporting_pct": row["reporting_pct"] or "",
            "votes": votes,
            "seats": seats,
        })
    return out


def recap_from_state(pool: str, ranked: list[dict], mover: dict | None, when: str) -> dict:
    ordered = sorted(ranked, key=lambda row: (row.get("place") or row.get("rank") or 99, row.get("name") or ""))
    top = []
    for row in ordered[:3]:
        top.append({
            "place": row.get("place") or row.get("rank"),
            "name": row.get("name"),
            "total": row.get("total"),
        })
    leader = ordered[0] if ordered else {}
    best = leader.get("best_call") if leader else None
    worst = leader.get("worst_call") if leader else None
    return {
        "pool": pool,
        "title": "Latest count (not the pot result)" if pool == "pot" else "Public play",
        "at": when,
        "leader": leader.get("name") or "—",
        "leader_total": leader.get("total"),
        "top": top,
        "best_call": (best or {}).get("label"),
        "best_points": (best or {}).get("points"),
        "worst_call": (worst or {}).get("label"),
        "worst_lost": (worst or {}).get("lost"),
        "biggest_mover": mover,
    }


_SEAT_RGB = {
    "Green": (9, 129, 55),
    "Te Pāti Māori": (178, 0, 29),
    "Labour": (216, 42, 32),
    "NZ First": (107, 114, 128),
    "National": (0, 82, 159),
    "ACT": (245, 197, 24),
    "TOP": (50, 181, 232),
}


def _draw_recap_seats(draw: ImageDraw.ImageDraw, payload: dict, top: int) -> None:
    """Same left-to-right order and solid fills as the on-screen hemicycle."""
    seats = payload.get("house_seats") or dict(P.PARLIAMENT_2023)
    order = payload.get("house_order") or list(P.HOUSE_ORDER)
    seq = []
    for party in order:
        seq.extend([party] * int(seats.get(party) or 0))
    if not seq or top > 1180:
        return
    cx, cy, radius = 540, top + 150, 130
    n = len(seq)
    for i, party in enumerate(seq):
        t = 0.5 if n == 1 else i / (n - 1)
        ang = 3.14159 * (1 - t)
        row = i % 4
        rad = radius - row * 16
        x = cx + rad * math.cos(ang)
        y = cy - rad * math.sin(ang) * 0.72
        color = _SEAT_RGB.get(party, (143, 163, 200))
        r = 7
        outline = (255, 255, 255) if party == "NZ First" else None
        draw.ellipse((x - r, y - r, x + r, y + r), fill=color, outline=outline)


def render_recap_png(payload: dict) -> bytes:
    image = Image.new("RGB", (1080, 1350), "#070b14")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(FONT_BOLD, 54)
    big_font = ImageFont.truetype(FONT_BOLD, 92)
    body_font = ImageFont.truetype(FONT, 36)
    small_font = ImageFont.truetype(FONT, 28)
    draw.rectangle((0, 0, 1080, 16), fill="#5dd6ff")
    draw.text((64, 64), payload.get("title") or "Recap", font=title_font, fill="#8fa3c8")
    draw.text((64, 160), str(payload.get("leader") or "—"), font=big_font, fill="#ffd166")
    total = payload.get("leader_total")
    draw.text((64, 280), f"{total if total is not None else '—'} pts", font=body_font, fill="#eef3ff")
    y = 380
    for row in payload.get("top") or []:
        draw.text((64, y), f"{row.get('place')}. {row.get('name')}   {row.get('total')}", font=body_font, fill="#eef3ff")
        y += 64
    y += 24
    draw.text((64, y), f"Best call: {payload.get('best_call') or '—'}", font=body_font, fill="#3ddc97")
    y += 56
    draw.text((64, y), f"Worst call: {payload.get('worst_call') or '—'}", font=body_font, fill="#ff5d7a")
    y += 56
    mover = payload.get("biggest_mover") or {}
    if mover:
        line = f"Biggest mover: {mover.get('name')} {mover.get('from_place')} → {mover.get('to_place')}"
    else:
        line = "Biggest mover: —"
    draw.text((64, y), line, font=body_font, fill="#5dd6ff")
    _draw_recap_seats(draw, payload, y + 80)
    draw.text((64, 1240), str(payload.get("at") or ""), font=small_font, fill="#8fa3c8")
    draw.text((64, 1288), "Not the prize-pot result", font=small_font, fill="#8fa3c8")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def escape_text(raw: str) -> str:
    return html.escape(str(raw or "").strip(), quote=True)


def _current_party_numbers(conn: D.Conn) -> tuple[dict, dict]:
    votes, seats = {}, {}
    raw = D.get_setting(conn, "party_table_json") or ""
    if raw:
        try:
            parsed = json.loads(raw)
            votes = {k: float(v) for k, v in (parsed.get("votes") or {}).items() if v not in (None, "")}
            seats = {k: int(v) for k, v in (parsed.get("seats") or {}).items() if v not in (None, "")}
        except Exception:
            votes, seats = {}, {}
    if votes:
        return votes, seats
    questions = {q["key"]: q for q in D.list_questions(conn)}
    results = D.get_results(conn)
    key_party = {
        "nat_vote": "National", "lab_vote": "Labour", "grn_vote": "Green",
        "act_vote": "ACT", "nzf_vote": "NZ First", "tpm_vote": "Te Pāti Māori",
    }
    for key, party in key_party.items():
        q = questions.get(key)
        if not q:
            continue
        cell = results.get(str(q["id"]))
        if not cell or cell.get("value") in (None, ""):
            continue
        try:
            votes[party] = float(cell["value"])
        except (TypeError, ValueError):
            pass
    return votes, seats


def projection_view(conn: D.Conn, ranked: list[dict], questions: list[dict]) -> dict:
    assumptions = P.default_assumptions()
    saved = D.get_setting(conn, "projection_json") or ""
    if saved:
        try:
            assumptions.update(json.loads(saved))
        except Exception:
            pass
    current, current_seats = _current_party_numbers(conn)
    if not current:
        return {"label": "ESTIMATE", "ready": False, "assumptions": assumptions, "note": "Enter a party table or vote percentages first."}
    projected = P.project_seats(current, assumptions)
    current_derived = derive_from_party_table(current, current_seats)
    proj_seats = {party: projected["seats"].get(party, 0) for party in projected["shares"]}
    proj_derived = derive_from_party_table(projected["shares"], proj_seats)
    q_by_key = {q["key"]: q for q in questions}
    flips = []
    for row in ranked:
        changed = []
        details = {str(d.get("question_id")): d for d in (row.get("detail") or [])}
        for key in ("nat_vote", "lab_vote", "grn_vote", "act_vote", "nzf_vote", "tpm_vote", "most_seats", "parties_in", "bloc_61", "minor_highest"):
            q = q_by_key.get(key)
            if not q:
                continue
            pred = (details.get(str(q["id"])) or {}).get("predicted")
            now_pts = score_question(q, pred, current_derived.get(key))
            next_pts = score_question(q, pred, proj_derived.get(key))
            if now_pts != next_pts:
                changed.append(q.get("label") or key)
        if changed:
            flips.append({"name": row.get("name"), "calls": changed})
    return {
        "label": "ESTIMATE",
        "ready": True,
        "assumptions": assumptions,
        "current_shares": current,
        "projected_shares": projected["shares"],
        "entitlement": projected["entitlement"],
        "seats": projected["seats"],
        "overhang": projected["overhang"],
        "house": projected["house"],
        "flips": flips,
        "note": "Special-vote estimate. Not the pot result. Overhang is noted and is not added to the 120-seat entitlement.",
    }
