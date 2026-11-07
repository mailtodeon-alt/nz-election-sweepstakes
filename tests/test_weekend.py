"""Weekend views. Pot prizes, the frozen snapshot, and the pot CSV stay on the main ballot."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (os.environ.get("DATABASE_URL") or "").strip() and not (os.environ.get("DB_PATH") or "").strip():
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-weekend-")) / "sweep.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
# Default lock is 7pm NZDT on 7 Nov 2026. Freeze earlier so entries stay open.
os.environ.setdefault("SWEEP_NOW", "2026-11-07T18:30:00+13:00")
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402
import db as D  # noqa: E402
import weekend as W  # noqa: E402
from projection import (  # noqa: E402
    OFFICIAL_ELECTORATES_2023, OFFICIAL_SEATS_2023, OFFICIAL_VOTES_2023, sainte_lague,
)

CLIENT = app.test_client()


def expect(resp, status):
    if resp.status_code != status:
        raise AssertionError(f"{resp.status_code} != {status}: {resp.get_data(as_text=True)[:700]}")
    raw = resp.get_data(as_text=True)
    if raw[:1] in "{[":
        return json.loads(raw)
    return raw


def login():
    expect(CLIENT.post("/api/admin/login", json={"password": os.environ["ADMIN_PASSWORD"]}), 200)


def test_sainte_lague_matches_2023_official():
    allocated = sainte_lague(OFFICIAL_VOTES_2023, OFFICIAL_ELECTORATES_2023, seats=120)
    assert allocated["entitlement"]["National"] == 48
    assert allocated["entitlement"]["Labour"] == 34
    assert allocated["entitlement"]["Green"] == 15
    assert allocated["entitlement"]["ACT"] == 11
    assert allocated["entitlement"]["NZ First"] == 8
    assert allocated["entitlement"]["Te Pāti Māori"] == 4
    assert allocated["entitlement"]["TOP"] == 0
    assert allocated["overhang"] == {"Te Pāti Māori": 2}
    assert allocated["seats"]["Te Pāti Māori"] == 6
    assert allocated["house"] == 122
    for party, seats in OFFICIAL_SEATS_2023.items():
        assert allocated["seats"][party] == seats


def test_latest_count_does_not_move_frozen_pot():
    login()
    qs = expect(CLIENT.get("/api/state"), 200)["questions"]
    nat = next(q for q in qs if q["key"] == "nat_vote")
    pots = D.list_players(D.connect(False), pool=D.POOL_POT)
    token = pots[0]["token"]
    D.connect(False).close()
    conn = D.connect(False)
    try:
        pots = D.list_players(conn, pool=D.POOL_POT)
        token = pots[0]["token"]
    finally:
        conn.close()
    expect(CLIENT.post(f"/api/player/{token}/predictions", json={
        "predictions": {str(nat["id"]): nat["meta"]["choices"][2]},
        "tb_national_vote": "35.0",
    }), 200)
    expect(CLIENT.post("/api/admin/results", json={
        "results": {str(nat["id"]): "35.0"}, "source": "manual", "reporting_pct": "40",
    }), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)
    expect(CLIENT.post("/api/admin/snapshot", json={"kind": "election_night", "label": "night"}), 200)
    frozen = expect(CLIENT.get("/api/state"), 200)
    frozen_total = next(r["total"] for r in frozen["leaderboard"] if r["player_id"] == pots[0]["id"])
    expect(CLIENT.post("/api/admin/results", json={
        "results": {str(nat["id"]): "20.0"}, "source": "manual", "reporting_pct": "80",
    }), 200)
    still = expect(CLIENT.get("/api/state"), 200)
    assert still["settings"]["board_mode"] == "frozen"
    assert next(r["total"] for r in still["leaderboard"] if r["player_id"] == pots[0]["id"]) == frozen_total
    latest = still["latest_count"]
    assert latest["label"] == "Latest count (not the pot result)"
    me = next(p for p in latest["players"] if p["player_id"] == pots[0]["id"])
    assert me["wrong"] >= 1
    assert any(t["reporting_pct"] == "80" for t in latest["ticks"])
    csv_body = expect(CLIENT.get("/api/admin/export.csv"), 200)
    assert "Latest count" not in csv_body


def test_bonus_closest_is_public_only():
    login()
    expect(CLIENT.post("/api/admin/lock", json={"action": "reopen", "lock_at": "2099-01-01T00:00:00+13:00"}), 200)
    conn = D.connect(False)
    try:
        conn.execute("DELETE FROM side_bets")
        conn.execute("DELETE FROM side_bet_picks")
        conn.commit()
    finally:
        conn.close()
    created = expect(CLIENT.post("/api/open/signup", json={"name": "Bonus Fan"}), 200)
    fun = created["player"]["token"]
    expect(CLIENT.post("/api/admin/side-bets", json={
        "prompt": "Final turnout %", "kind": "closest", "points": 10,
        "open_until": "2099-01-01T00:00:00+13:00",
    }), 200)
    bet_id = expect(CLIENT.get("/api/state"), 200)["side_bets"][-1]["id"]
    conn = D.connect(False)
    try:
        pot = D.list_players(conn, pool=D.POOL_POT)[0]
    finally:
        conn.close()
    expect(CLIENT.post(f"/api/player/{pot['token']}/side-bets", json={"side_bet_id": bet_id, "value": "80"}), 200)
    expect(CLIENT.post(f"/api/player/{fun}/side-bets", json={"side_bet_id": bet_id, "value": "78.2"}), 200)
    expect(CLIENT.post(f"/api/admin/side-bets/{bet_id}", json={"action": "answer", "answer": "78"}), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)
    pot_state = expect(CLIENT.get("/api/state"), 200)
    pot_row = next(r for r in pot_state["leaderboard"] if r["player_id"] == pot["id"])
    assert pot_row.get("side_points", 0) in (0, None)
    assert pot_row["total"] != 10 or pot_row.get("question_total", pot_row["total"]) == pot_row["total"]
    scored = {r["player_id"]: r for r in pot_state["side_bet_scores"]}
    assert scored[pot["id"]]["points"] == 0
    pub = expect(CLIENT.get("/api/open/state"), 200)
    fan = next(r for r in pub["leaderboard"] if r["name"] == "Bonus Fan")
    assert fan["side_points"] == 10
    assert fan["total"] == fan["question_total"] + 10
    bad = expect(CLIENT.post("/api/admin/coalition", json={"headline": "Nope", "source_url": "javascript:alert(1)"}), 400)
    assert bad["error"] == "bad_url"
    expect(CLIENT.post("/api/admin/coalition", json={
        "headline": "Talks continue", "source_url": "https://www.rnz.co.nz/news", "parties": "National, ACT",
    }), 200)
    events = expect(CLIENT.get("/api/coalition"), 200)["events"]
    assert any(e["headline"] == "Talks continue" and e["source_url"].startswith("https://") for e in events)
    recap = expect(CLIENT.get("/api/recap"), 200)
    assert recap["awaiting"] is False
    assert recap["parliament"]["label"] == "New Parliament 2026 (provisional)"
    assert recap["parliament"]["projected"] is True
    assert len(recap["top"]) == 6
    png = CLIENT.get("/recap.png")
    assert png.status_code == 200
    assert png.data[:8] == b"\x89PNG\r\n\x1a\n"
    page = expect(CLIENT.get("/recap"), 200)
    assert "Download PNG" in page
    assert "Latest count" in expect(CLIENT.get("/live"), 200)


def test_demo_weekend_preview_leaves_real_rows():
    login()
    conn = D.connect(False)
    try:
        before = [(p["id"], p["token"], p["tb_national_vote"]) for p in D.list_players(conn, pool=D.POOL_POT)]
    finally:
        conn.close()
    expect(CLIENT.post("/api/admin/demo/reset"), 200)
    demo = expect(CLIENT.get("/api/state", headers={"X-Demo": "1"}), 200)
    assert demo["settings"]["weekend_mode"] == "1"
    assert any(b.get("kind") == "closest" for b in demo["side_bets"])
    notes = expect(CLIENT.get("/api/coalition", headers={"X-Demo": "1"}), 200)["events"]
    assert notes and notes[0]["source_url"].startswith("https://")
    page = expect(CLIENT.get("/demo/live"), 200)
    assert "Latest count (not the pot result)" in page
    assert "ESTIMATE" in page
    conn = D.connect(False)
    try:
        after = [(p["id"], p["token"], p["tb_national_vote"]) for p in D.list_players(conn, pool=D.POOL_POT)]
    finally:
        conn.close()
    assert after == before


def test_recap_standings_mark_ties():
    rows = [
        {"place": 1, "name": "Caleb", "total": 10},
        {"place": 1, "name": "Deon", "total": 10},
        {"place": 3, "name": "Jarrad", "total": 4},
        {"place": 4, "name": "Mark", "total": 3},
        {"place": 5, "name": "Nathan", "total": 2},
        {"place": 6, "name": "Pete", "total": 1},
    ]
    card = W.recap_from_state("pot", rows, None, "t")
    assert [row["place_label"] for row in card["top"]] == ["=1", "=1", "3", "4", "5", "6"]
    assert len(card["top"]) == 6


def main():
    test_sainte_lague_matches_2023_official()
    test_recap_standings_mark_ties()
    test_latest_count_does_not_move_frozen_pot()
    test_bonus_closest_is_public_only()
    test_demo_weekend_preview_leaves_real_rows()
    print("WEEKEND TESTS OK", "postgres" if D.using_postgres() else "sqlite")


if __name__ == "__main__":
    main()
