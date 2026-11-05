"""Election-night features. SQLite or Postgres, depending on the environment.

Side-bet points must not move pot totals, prizes, the frozen snapshot, or the pot CSV.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (os.environ.get("DATABASE_URL") or "").strip() and not (os.environ.get("DB_PATH") or "").strip():
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-night-")) / "sweep.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402
import db as D  # noqa: E402
import sweepstakes_flask as S  # noqa: E402
from feed import parse_election_xml  # noqa: E402
from scoring import score_question  # noqa: E402

CLIENT = app.test_client()
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "election_2023.xml"

ACTUAL = {
    "nat_vote": "35.0",
    "lab_vote": "29.0",
    "grn_vote": "11.2",
    "act_vote": "8.5",
    "nzf_vote": "7.8",
    "tpm_vote": "3.2",
    "most_seats": "National",
    "parties_in": "6",
    "bloc_61": "Yes",
    "minor_highest": "Green",
}


def expect(resp, status):
    if resp.status_code != status:
        raise AssertionError(
            f"expected {status}, got {resp.status_code}: {resp.get_data(as_text=True)[:800]}"
        )
    raw = resp.get_data(as_text=True)
    if raw[:1] in "{[":
        return json.loads(raw)
    return raw


def wipe():
    S.reset_signup_rate_limits()
    S.reactions.reset()
    with D.write_txn(False) as conn:
        for table in ("side_bet_picks", "side_bets", "score_history", "ticker_messages", "predictions", "results", "snapshots"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM players WHERE pool=?", (D.POOL_PUBLIC,))
        conn.execute("UPDATE players SET tb_national_vote=NULL")
        D.set_setting(conn, "entries_open", "1")
        D.set_setting(conn, "lock_at", "2099-01-01T00:00:00+13:00")
        D.set_setting(conn, "board_mode", "live")
        D.set_setting(conn, "reactions_enabled", "1")
        D.set_setting(conn, "ticker_enabled", "1")
        D.set_setting(conn, "reporting_pct", "")
        D.set_setting(conn, "party_table_json", "")


def login():
    expect(CLIENT.post("/api/admin/login", json={"password": os.environ["ADMIN_PASSWORD"]}), 200)


def signup(name, headers=None):
    return expect(CLIENT.post("/api/open/signup", json={"name": name}, headers=headers or {}), 200)["player"]


def pot_by_slot():
    conn = D.connect(False)
    try:
        return {p["slot"]: p for p in D.list_players(conn, pool=D.POOL_POT)}
    finally:
        conn.close()


def questions():
    return expect(CLIENT.get("/api/state"), 200)["questions"]


def winning(qs):
    out = {}
    for q in qs:
        actual = ACTUAL[q["key"]]
        hit = next(c for c in q["meta"]["choices"] if score_question(q, c, actual) == q["max_points"])
        out[str(q["id"])] = hit
    return out


def predict(token, mapping, tb="35.0"):
    return CLIENT.post(
        f"/api/player/{token}/predictions",
        json={"predictions": mapping, "tb_national_vote": tb},
    )


def set_results():
    qs = questions()
    payload = {str(q["id"]): ACTUAL[q["key"]] for q in qs}
    expect(CLIENT.post("/api/admin/results", json={"results": payload, "source": "manual", "reporting_pct": "48"}), 200)
    return qs


def test_reactions_throttle_and_coalesce():
    wipe()
    captured = []
    previous = S.reactions.broadcast
    S.reactions.broadcast = lambda event, data=None: captured.append((event, data))
    try:
        S.reactions.reset()
        assert S.reactions.tap(False, "phone-a", "🎉") == (True, "ok")
        assert S.reactions.tap(False, "phone-b", "🎉") == (True, "ok")
        assert S.reactions.tap(False, "phone-c", "😱") == (True, "ok")
        assert S.reactions.tap(False, "phone-a", "🔥")[1] == "throttled"
        S.reactions.flush()
        bursts = {b["emoji"]: b["n"] for b in captured[-1][1]["bursts"]}
        assert bursts == {"🎉": 2, "😱": 1}
        assert captured[-1][1]["demo"] is False
        live_before = len(captured)
        assert S.reactions.tap(True, "phone-a", "👏")[0]
        S.reactions.flush()
        assert captured[-1][1]["demo"] is True
        assert len(captured) == live_before + 1
    finally:
        S.reactions.broadcast = previous
    first = expect(CLIENT.post("/api/reactions", json={"emoji": "😂"}), 200)
    assert first["ok"] is True
    blocked = expect(CLIENT.post("/api/reactions", json={"emoji": "😂"}), 429)
    assert blocked["error"] == "throttled"
    expect(CLIENT.post("/api/admin/settings", json={"reactions_enabled": "0"}), 200)
    off = expect(CLIENT.post("/api/reactions", json={"emoji": "😂"}), 403)
    assert off["error"] == "reactions_off"


def test_side_bets_do_not_move_the_pot():
    wipe()
    pots = pot_by_slot()
    fun = signup("SideFun")
    qs = set_results()
    win = winning(qs)
    expect(predict(pots[1]["token"], win, tb="35.0"), 200)
    expect(predict(fun["token"], win, tb="35.0"), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)
    before = expect(CLIENT.get("/api/state"), 200)
    leader = next(r for r in before["leaderboard"] if r["player_id"] == pots[1]["id"])
    assert leader["total"] == 100
    assert leader["prize"] == 100

    expect(CLIENT.post("/api/admin/side-bets", json={"prompt": "Specials flip a seat?", "choices": ["Yes", "No"], "points": 10}), 200)
    state = expect(CLIENT.get("/api/state"), 200)
    bet_id = state["side_bets"][0]["id"]
    expect(CLIENT.post(f"/api/player/{pots[1]['token']}/side-bets", json={"side_bet_id": bet_id, "value": "Yes"}), 200)
    expect(CLIENT.post(f"/api/player/{fun['token']}/side-bets", json={"side_bet_id": bet_id, "value": "Yes"}), 200)
    expect(CLIENT.post(f"/api/admin/side-bets/{bet_id}", json={"action": "answer", "answer": "Yes"}), 200)

    pot = expect(CLIENT.get("/api/state"), 200)
    leader = next(r for r in pot["leaderboard"] if r["player_id"] == pots[1]["id"])
    assert leader["total"] == 100
    assert leader["prize"] == 100
    assert fun["id"] not in {r["player_id"] for r in pot["leaderboard"]}
    scored = {r["name"]: r for r in pot["side_bet_scores"]}
    assert scored["Player 1"]["pool"] == "pot"
    assert scored["Player 1"]["points"] == 10
    assert scored["SideFun"]["points"] == 10

    pub = expect(CLIENT.get("/api/open/state"), 200)
    row = pub["leaderboard"][0]
    assert row["name"] == "SideFun"
    assert row["question_total"] == 100
    assert row["side_points"] == 10
    assert row["total"] == 110
    assert "prize" not in row
    assert all(r["name"] != "Player 1" for r in pub["leaderboard"])

    expect(CLIENT.post("/api/admin/snapshot", json={"kind": "election_night", "label": "night"}), 200)
    conn = D.connect(False)
    try:
        raw = conn.execute("SELECT payload_json FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    payload = json.loads(raw["payload_json"])
    snap_leader = next(r for r in payload["leaderboard"] if r["name"] == "Player 1")
    assert snap_leader["total"] == 100
    assert snap_leader["prize"] == 100
    assert all(r["name"] != "SideFun" for r in payload["leaderboard"])

    pot_csv = expect(CLIENT.get("/api/admin/export.csv"), 200)
    assert "SideFun" not in pot_csv
    # The pot total column is the question score, not 110.
    header = pot_csv.splitlines()[0].split(",")
    total_at = header.index("total")
    player_line = next(line for line in pot_csv.splitlines()[1:] if line.startswith("1,Player 1,"))
    assert player_line.split(",")[total_at] == "100"


def test_history_follows_results_only():
    wipe()
    pots = pot_by_slot()
    qs = set_results()
    win = winning(qs)
    expect(CLIENT.post("/api/admin/lock", json={"action": "reopen", "lock_at": "2099-01-01T00:00:00+13:00"}), 200)
    expect(predict(pots[1]["token"], win), 200)
    conn = D.connect(False)
    try:
        before = conn.execute("SELECT COUNT(*) AS c FROM score_history").fetchone()["c"]
    finally:
        conn.close()
    # Saving a ballot is not a results change.
    expect(predict(pots[2]["token"], win), 200)
    conn = D.connect(False)
    try:
        after_pred = conn.execute("SELECT COUNT(*) AS c FROM score_history").fetchone()["c"]
    finally:
        conn.close()
    assert after_pred == before
    expect(CLIENT.post("/api/admin/results", json={"results": {str(qs[0]["id"]): "35.0"}, "reporting_pct": "48", "source": "manual"}), 200)
    series = expect(CLIENT.get("/api/history?pool=pot"), 200)["series"]
    player = next(s for s in series if s["player_id"] == pots[1]["id"])
    assert player["points"][-1]["reporting_pct"] == "48"
    assert isinstance(player["points"][-1]["total"], int)
    public = expect(CLIENT.get("/api/history?pool=public"), 200)
    assert public["pool"] == "public"


def test_ticker_rules():
    wipe()
    pots = pot_by_slot()
    fun = signup("Talker")
    posted = expect(CLIENT.post(f"/api/player/{fun['token']}/ticker", json={"text": "  <b>hello</b> fuck  "}), 200)
    assert "<" not in posted["body"]
    assert "fuck" not in posted["body"].lower()
    assert "*" in posted["body"]
    assert "&lt;b&gt;" in posted["body"]
    again = expect(CLIENT.post(f"/api/player/{fun['token']}/ticker", json={"text": "too soon"}), 429)
    assert again["error"] == "throttled"
    long = expect(CLIENT.post(f"/api/player/{pots[1]['token']}/ticker", json={"text": "x" * 81}), 400)
    assert long["error"] == "bad_message"
    expect(CLIENT.post(f"/api/player/{pots[1]['token']}/ticker", json={"text": "pot line"}), 200)
    pot = expect(CLIENT.get("/api/state"), 200)
    pot_text = " ".join(m["body"] for m in pot["ticker"])
    assert "pot line" in pot_text
    assert "hello" not in pot_text
    pub = expect(CLIENT.get("/api/open/state"), 200)
    pub_text = " ".join(m["body"] for m in pub["ticker"])
    assert "hello" in pub_text
    assert "pot line" not in pub_text
    messages = expect(CLIENT.get("/api/admin/ticker"), 200)["messages"]
    fun_id = next(m["id"] for m in messages if "hello" in m["body"])
    expect(CLIENT.post(f"/api/admin/ticker/{fun_id}", json={"action": "feature"}), 200)
    pot = expect(CLIENT.get("/api/state"), 200)
    assert any("hello" in m["body"] for m in pot["ticker"])
    expect(CLIENT.post(f"/api/admin/ticker/{fun_id}", json={"action": "hide"}), 200)
    pot = expect(CLIENT.get("/api/state"), 200)
    assert all("hello" not in m["body"] for m in pot["ticker"])
    expect(CLIENT.post("/api/admin/settings", json={"ticker_enabled": "0"}), 200)
    assert expect(CLIENT.get("/api/state"), 200)["ticker"] == []
    closed = expect(CLIENT.post(f"/api/player/{pots[2]['token']}/ticker", json={"text": "nope"}), 403)
    assert closed["error"] == "ticker_off"


def test_averages_count_submitted_only():
    wipe()
    pots = pot_by_slot()
    fun = signup("AvgFan")
    qs = set_results()
    win = winning(qs)
    expect(predict(pots[1]["token"], win, tb="35.0"), 200)
    blank = {str(q["id"]): q["meta"]["choices"][0] for q in qs}
    expect(predict(fun["token"], blank, tb="20"), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)
    state = expect(CLIENT.get("/api/state"), 200)
    # Five pot players submitted nothing, so they are not in the average.
    assert state["compare_averages"]["pot"]["submitted"] == 1
    assert state["compare_averages"]["pot"]["average"] == 100
    assert state["compare_averages"]["public"]["submitted"] == 1
    assert state["compare_averages"]["public"]["average"] != 100
    assert all(r["name"] != "AvgFan" for r in state["leaderboard"])


def test_feed_fixture_and_manual_override():
    parsed = parse_election_xml(FIXTURE.read_bytes())
    assert parsed["ok"] is True
    assert parsed["reporting_pct"] == 100.0
    assert parsed["votes"]["National"] == 38.08
    assert parsed["seats"]["National"] == 48
    assert parsed["votes"]["Labour"] == 26.91
    assert parsed["seats"]["Labour"] == 34
    assert parsed["votes"]["Green"] == 11.6
    assert parsed["seats"]["Green"] == 15
    assert parsed["votes"]["ACT"] == 8.64
    assert parsed["seats"]["ACT"] == 11
    assert parsed["votes"]["NZ First"] == 6.08
    assert parsed["seats"]["NZ First"] == 8
    assert parsed["votes"]["Te Pāti Māori"] == 3.08
    assert parsed["seats"]["Te Pāti Māori"] == 6
    assert parsed["values"]["most_seats"] == "National"
    assert str(parsed["values"]["parties_in"]) == "6"
    assert parsed["values"]["bloc_61"] == "Yes"
    assert parsed["values"]["minor_highest"] == "Green"

    wipe()
    qs = {q["key"]: q for q in questions()}
    expect(CLIENT.post("/api/admin/results", json={"results": {str(qs["nat_vote"]["id"]): "1.0"}, "source": "manual"}), 200)
    preview = S.feed_poller.poll_once(body=FIXTURE.read_bytes(), apply=False)
    assert preview["ok"] is True
    assert preview["preview"] is True
    assert preview.get("applied") is False
    conn = D.connect(False)
    try:
        nat = conn.execute("SELECT value, source FROM results WHERE question_id=?", (qs["nat_vote"]["id"],)).fetchone()
        lab = conn.execute("SELECT value FROM results WHERE question_id=?", (qs["lab_vote"]["id"],)).fetchone()
    finally:
        conn.close()
    assert nat["value"] == "1.0" and nat["source"] == "manual"
    assert lab is None

    applied = S.feed_poller.poll_once(body=FIXTURE.read_bytes(), apply=True)
    assert applied["ok"] is True
    assert "nat_vote" not in applied["applied"]
    assert "lab_vote" in applied["applied"]
    conn = D.connect(False)
    try:
        nat = conn.execute("SELECT value, source FROM results WHERE question_id=?", (qs["nat_vote"]["id"],)).fetchone()
        lab = conn.execute("SELECT value, source FROM results WHERE question_id=?", (qs["lab_vote"]["id"],)).fetchone()
    finally:
        conn.close()
    assert nat["value"] == "1.0" and nat["source"] == "manual"
    assert lab["value"] == "26.91" and lab["source"] == "auto"

    bad = S.feed_poller.poll_once(body=b"<election></election>", apply=True)
    assert bad["ok"] is False
    conn = D.connect(False)
    try:
        nat = conn.execute("SELECT value, source FROM results WHERE question_id=?", (qs["nat_vote"]["id"],)).fetchone()
        lab = conn.execute("SELECT value FROM results WHERE question_id=?", (qs["lab_vote"]["id"],)).fetchone()
    finally:
        conn.close()
    assert nat["value"] == "1.0"
    assert lab["value"] == "26.91"

    crashed = S.feed_poller.poll_once(body=b"this is not xml", apply=True)
    assert crashed["ok"] is False
    conn = D.connect(False)
    try:
        nat = conn.execute("SELECT value FROM results WHERE question_id=?", (qs["nat_vote"]["id"],)).fetchone()
    finally:
        conn.close()
    assert nat["value"] == "1.0"


def test_demo_previews_features_without_touching_real_rows():
    wipe()
    before = [(p["id"], p["token"], p["name"], p["tb_national_vote"]) for p in pot_by_slot().values()]
    expect(CLIENT.post("/api/admin/demo/reset"), 200)
    demo = expect(CLIENT.get("/api/state", headers={"X-Demo": "1"}), 200)
    texts = " ".join(m["body"] for m in demo["ticker"])
    assert "Calling it early" in texts
    assert "big screen" in texts
    assert "couch" not in texts
    assert any(b["status"] == "open" for b in demo["side_bets"])
    assert any(r["points"] == 5 and r["pool"] == "pot" for r in demo["side_bet_scores"])
    demo_pub = expect(CLIENT.get("/api/open/state", headers={"X-Demo": "1"}), 200)
    pub_texts = " ".join(m["body"] for m in demo_pub["ticker"])
    assert "couch" in pub_texts
    assert "Calling it early" not in pub_texts
    expect(CLIENT.post("/api/admin/demo/step", json={"stage": 1}, headers={"X-Demo": "1"}), 200)
    history = expect(CLIENT.get("/api/history?pool=pot", headers={"X-Demo": "1"}), 200)
    assert history["series"]
    page = expect(CLIENT.get("/demo/live"), 200)
    assert "reactLayer" in page
    assert "historyChart" in page
    assert "ticker" in page
    after = [(p["id"], p["token"], p["name"], p["tb_national_vote"]) for p in pot_by_slot().values()]
    assert after == before
    real = expect(CLIENT.get("/api/state"), 200)
    assert real["side_bets"] == []
    assert real["ticker"] == []


def main():
    login()
    test_reactions_throttle_and_coalesce()
    test_side_bets_do_not_move_the_pot()
    test_history_follows_results_only()
    test_ticker_rules()
    test_averages_count_submitted_only()
    test_feed_fixture_and_manual_override()
    test_demo_previews_features_without_touching_real_rows()
    backend = "postgres" if D.using_postgres() else "sqlite"
    print(f"NIGHT TESTS OK ({backend})")


if __name__ == "__main__":
    main()
