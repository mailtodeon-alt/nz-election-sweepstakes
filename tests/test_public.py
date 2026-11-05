"""Public-pool feature tests. SQLite or Postgres, depending on the environment.

DATABASE_URL set → Postgres. Otherwise DB_PATH (or a temp SQLite file).
"""
from __future__ import annotations

import html
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (os.environ.get("DATABASE_URL") or "").strip() and not (os.environ.get("DB_PATH") or "").strip():
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-feat-")) / "sweep.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")

sys.path.insert(0, str(ROOT))
from app import app  # noqa: E402
import db as D  # noqa: E402
import sweepstakes_flask as S  # noqa: E402
from scoring import score_question  # noqa: E402

CLIENT = app.test_client()

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
    for demo in (False, True):
        with D.write_txn(demo) as conn:
            conn.execute("DELETE FROM predictions")
            conn.execute("DELETE FROM results")
            conn.execute("DELETE FROM snapshots")
            conn.execute("DELETE FROM players WHERE pool=?", (D.POOL_PUBLIC,))
            D.set_setting(conn, "entries_open", "1")
            D.set_setting(conn, "lock_at", "2099-01-01T00:00:00+13:00")
            D.set_setting(conn, "public_signups_open", "1")
            D.set_setting(conn, "public_player_cap", "200")
            D.set_setting(conn, "board_mode", "live")
            if not demo:
                D.set_setting(conn, "demo_mode", "0")
                D.set_setting(conn, "party_table_json", "")


def signup(name, headers=None):
    return CLIENT.post("/api/open/signup", json={"name": name}, headers=headers or {})


def login():
    expect(CLIENT.post("/api/admin/login", json={"password": os.environ["ADMIN_PASSWORD"]}), 200)


def pot_players():
    conn = D.connect(False)
    try:
        return D.list_players(conn, pool=D.POOL_POT)
    finally:
        conn.close()


def questions():
    return expect(CLIENT.get("/api/state"), 200)["questions"]


def winning_and_losing(qs):
    win, lose = {}, {}
    for q in qs:
        actual = ACTUAL[q["key"]]
        hit = miss = None
        for choice in q["meta"]["choices"]:
            pts = score_question(q, choice, actual)
            if pts == q["max_points"] and hit is None:
                hit = choice
            if pts == 0 and miss is None:
                miss = choice
        if hit is None or miss is None:
            raise AssertionError(f"could not score choices for {q['key']}")
        win[str(q["id"])] = hit
        lose[str(q["id"])] = miss
    return win, lose


def predict(token, mapping, name=None, tb="35.0"):
    body = {"predictions": mapping, "tb_national_vote": tb}
    if name is not None:
        body["name"] = name
    return CLIENT.post(f"/api/player/{token}/predictions", json=body)


def set_official_results():
    qs = questions()
    payload = {str(q["id"]): ACTUAL[q["key"]] for q in qs}
    expect(CLIENT.post("/api/admin/results", json={"results": payload, "source": "manual"}), 200)
    return qs


def test_defaults_and_repeat_init_keeps_pot():
    conn = D.connect(False)
    try:
        assert D.get_setting(conn, "public_signups_open") == "1"
        assert D.get_setting(conn, "public_player_cap") == "200"
        before = [(p["id"], p["token"], p["name"], p["slot"], p["pool"]) for p in D.list_players(conn, pool=None)]
    finally:
        conn.close()
    assert len(before) == 6
    assert [p[4] for p in before] == ["pot"] * 6
    assert [p[3] for p in before] == [1, 2, 3, 4, 5, 6]
    D.init_db(False)
    D.init_db(False)
    conn = D.connect(False)
    try:
        after = [(p["id"], p["token"], p["name"], p["slot"], p["pool"]) for p in D.list_players(conn, pool=None)]
    finally:
        conn.close()
    assert after == before


def test_pages():
    home = expect(CLIENT.get("/"), 200)
    join = expect(CLIENT.get("/join"), 200)
    assert "Play for fun (not in the pot)" in home
    assert 'href="/open"' in home
    assert "/demo/open" in home
    assert "Play for fun (not in the pot)" in join
    opened = expect(CLIENT.get("/open"), 200)
    assert "Save this link" in opened
    assert "home screen" in opened.lower()
    assert "localStorage" in opened
    assert "Copy link" in opened
    live = expect(CLIENT.get("/live"), 200)
    assert 'class="tv-head-main"' in live
    public_live = expect(CLIENT.get("/open/live"), 200)
    conn = D.connect(False)
    try:
        sample = D.list_players(conn, pool=D.POOL_POT)[0]["token"]
    finally:
        conn.close()
    ballot = expect(CLIENT.get(f"/p/{sample}"), 200)
    assert "This is me" in ballot
    assert "manifestLink" not in ballot
    assert "/api/player/" + sample + "/manifest.webmanifest" not in ballot
    common = CLIENT.get("/static/js/common.js").get_data(as_text=True)
    assert "bindIdentity" in common
    assert "sweep_public_entry" not in common
    recap = expect(CLIENT.get("/api/recap"), 200)
    assert recap["awaiting"] is True
    assert recap["message"] == "No results yet, recap fills in once counting starts"
    assert not recap.get("leader")
    assert not recap.get("top")
    assert "morning dishes" in live
    assert "$100" in live
    assert "morning dishes" not in public_live.lower()
    assert "$100" not in public_live
    assert "Prize pool" not in public_live
    assert expect(CLIENT.get("/demo/open"), 200)
    assert expect(CLIENT.get("/demo/open/live"), 200)
    pot = expect(CLIENT.get("/api/state"), 200)
    pub = expect(CLIENT.get("/api/open/state"), 200)
    assert pot["pool"] == "pot"
    assert pub["pool"] == "public"
    assert [q["key"] for q in pot["questions"]] == [q["key"] for q in pub["questions"]]
    assert len(pot["questions"]) == 10
    assert "prize_first" in pot["settings"]
    assert "prize_first" not in pub["settings"]


def test_export_requires_admin():
    assert CLIENT.get("/api/admin/export.csv").status_code == 401
    assert CLIENT.get("/api/admin/export-public.csv").status_code == 401


def test_signup_issues_unguessable_link():
    wipe()
    created = expect(signup("Fun Only"), 200)
    player = created["player"]
    token = player["token"]
    assert player["pool"] == "public"
    assert player["link"] == f"/p/{token}"
    assert len(token) >= 20
    assert token not in ("fun-only", "Fun Only")
    manifest = expect(CLIENT.get(f"/api/player/{token}/manifest.webmanifest"), 200)
    assert manifest["start_url"] == "/"
    assert manifest["short_name"] == "NZ Election"
    assert "Fun Only" not in json.dumps(manifest)
    assert CLIENT.get(f"/p/{token}").status_code == 200
    me = expect(CLIENT.get(f"/api/player/{token}"), 200)
    assert me["player"]["pool"] == "public"
    conn = D.connect(False)
    try:
        public = D.list_players(conn, pool=D.POOL_PUBLIC)
        pot = D.list_players(conn, pool=D.POOL_POT)
    finally:
        conn.close()
    assert len(public) == 1
    assert public[0]["slot"] >= 1001
    assert [p["slot"] for p in pot] == [1, 2, 3, 4, 5, 6]
    pot_state = expect(CLIENT.get("/api/state"), 200)
    assert len(pot_state["players"]) == 6
    assert all(p["name"] != "Fun Only" for p in pot_state["players"])
    assert all(r["name"] != "Fun Only" for r in pot_state["leaderboard"])
    # /live and /compare both read this pot payload, so the takeover and OUT
    # maths cannot see Fun Only.
    pub_state = expect(CLIENT.get("/api/open/state"), 200)
    assert [r["name"] for r in pub_state["leaderboard"]] == ["Fun Only"]
    assert pub_state["leaderboard"][0]["player_id"] not in {p["id"] for p in pot_state["players"]}


def test_name_rules():
    wipe()
    for _ in range(6):
        body = expect(signup(""), 400)
        assert body["error"] == "bad_name"
    assert expect(signup("   "), 400)["error"] == "bad_name"
    assert expect(signup("a" * 25), 400)["error"] == "bad_name"
    assert expect(signup("bad\nname"), 400)["error"] == "bad_name"
    assert expect(CLIENT.post("/api/open/signup", json={}), 400)["error"] == "bad_name"
    # Invalid attempts do not burn the 5-per-10-minute budget.
    expect(signup("a" * 24), 200)
    escaped = expect(signup("<b>Ann</b>"), 200)
    assert escaped["player"]["name"] == html.escape("<b>Ann</b>", quote=True)
    assert "<" not in escaped["player"]["name"]
    expect(signup("  Sam  "), 200)
    assert expect(signup("sam"), 409)["error"] == "name_taken"
    assert expect(signup("SAM"), 409)["error"] == "name_taken"
    state = expect(CLIENT.get("/api/open/state"), 200)
    names = {r["name"] for r in state["leaderboard"]}
    assert "Sam" in names
    assert "&lt;b&gt;Ann&lt;/b&gt;" in names
    assert "<b>Ann</b>" not in names


def test_pot_name_does_not_block_public():
    wipe()
    pot = pot_players()
    first = next(p for p in pot if p["slot"] == 1)
    expect(CLIENT.post("/api/admin/players", json={"id": first["id"], "name": "Shared"}), 200)
    created = expect(signup("Shared"), 200)
    assert created["player"]["pool"] == "public"
    pot_state = expect(CLIENT.get("/api/state"), 200)
    assert sum(1 for p in pot_state["players"] if p["name"] == "Shared") == 1
    pub_state = expect(CLIENT.get("/api/open/state"), 200)
    assert [r["name"] for r in pub_state["leaderboard"]] == ["Shared"]
    expect(CLIENT.post("/api/admin/players", json={"id": first["id"], "name": "Player 1"}), 200)


def test_rate_limit_is_per_ip_and_ignores_demo():
    wipe()
    for i in range(5):
        expect(signup(f"A{i}", headers={"X-Forwarded-For": "198.51.100.10"}), 200)
    blocked = expect(signup("A5", headers={"X-Forwarded-For": "198.51.100.10"}), 429)
    assert blocked["error"] == "rate_limited"
    expect(signup("B0", headers={"X-Forwarded-For": "198.51.100.11"}), 200)
    # Same address, demo database: separate bucket and separate data.
    demo = expect(
        signup("DemoA", headers={"X-Demo": "1", "X-Forwarded-For": "198.51.100.10"}),
        200,
    )
    assert demo["player"]["link"].startswith("/demo/p/")
    real = expect(CLIENT.get("/api/open/state"), 200)
    demo_state = expect(CLIENT.get("/api/open/state", headers={"X-Demo": "1"}), 200)
    assert sorted(r["name"] for r in real["leaderboard"]) == ["A0", "A1", "A2", "A3", "A4", "B0"]
    assert [r["name"] for r in demo_state["leaderboard"]] == ["DemoA"]


def test_cap_toggle_and_remove():
    wipe()
    expect(CLIENT.post("/api/admin/settings", json={"public_player_cap": 1}), 200)
    first = expect(signup("CapOne"), 200)
    capped = expect(signup("CapTwo"), 403)
    assert capped["error"] == "public_cap"
    expect(CLIENT.post(f"/api/admin/public-players/{first['player']['id']}/remove"), 200)
    assert CLIENT.get(f"/api/player/{first['player']['token']}").status_code == 404
    # Name and cap slot are free again. The removed row is kept, just inactive.
    expect(signup("CapOne"), 200)
    conn = D.connect(False)
    try:
        inactive = conn.execute(
            "SELECT COUNT(*) AS c FROM players WHERE pool=? AND active=0",
            (D.POOL_PUBLIC,),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert int(inactive) == 1
    expect(CLIENT.post("/api/admin/settings", json={"public_signups_open": "0", "public_player_cap": 200}), 200)
    closed = expect(signup("Closed"), 403)
    assert closed["error"] == "public_closed"
    assert expect(CLIENT.post("/api/admin/settings", json={"public_player_cap": "nope"}), 400)["error"] == "bad_cap"
    full = expect(CLIENT.get("/api/admin/full"), 200)
    assert full["settings"]["public_player_cap"] == "200"
    assert full["settings"]["public_signups_open"] == "0"
    pot_id = next(p["id"] for p in pot_players() if p["slot"] == 1)
    assert CLIENT.post(f"/api/admin/public-players/{pot_id}/remove").status_code == 404
    assert len(expect(CLIENT.get("/api/state"), 200)["players"]) == 6


def test_lock_refuses_signup_and_edits():
    wipe()
    created = expect(signup("Locker"), 200)
    token = created["player"]["token"]
    pid = created["player"]["id"]
    qs = questions()
    qid = str(qs[0]["id"])
    first_choice = qs[0]["meta"]["choices"][0]
    other_choice = qs[0]["meta"]["choices"][-1]
    expect(predict(token, {qid: first_choice}, tb="33.0"), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)
    locked_edit = expect(predict(token, {qid: other_choice}, tb="99"), 403)
    assert locked_edit["error"] == "entries_locked"
    locked_join = expect(signup("TooLate"), 403)
    assert locked_join["error"] == "entries_locked"
    conn = D.connect(False)
    try:
        assert D.get_predictions(conn, pid)[qid] == first_choice
        row = conn.execute("SELECT tb_national_vote FROM players WHERE id=?", (pid,)).fetchone()
        assert row["tb_national_vote"] == "33.0"
        assert D.count_players(conn, D.POOL_PUBLIC) == 1
    finally:
        conn.close()
    expect(
        CLIENT.post("/api/admin/lock", json={"action": "reopen", "lock_at": "2000-01-01T00:00:00+13:00"}),
        200,
    )
    past = expect(signup("PastLock"), 403)
    assert past["error"] == "entries_locked"


def test_pot_and_public_stay_separated():
    """/live, /compare, prizes, OUT, snapshots, and the pot CSV are pot-only."""
    wipe()
    pots = {p["slot"]: p for p in pot_players()}
    fun = expect(signup("FunOnlyBob"), 200)["player"]
    qs = set_official_results()
    win, lose = winning_and_losing(qs)
    expect(predict(pots[1]["token"], win, tb="35.0"), 200)
    expect(predict(pots[2]["token"], lose, tb="10"), 200)
    expect(predict(fun["token"], win, tb="35.0"), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)

    pot = expect(CLIENT.get("/api/state"), 200)
    assert pot["pool"] == "pot"
    assert len(pot["leaderboard"]) == 6
    assert all(r["name"] != "FunOnlyBob" for r in pot["leaderboard"])
    leader = next(r for r in pot["leaderboard"] if r["player_id"] == pots[1]["id"])
    loser = next(r for r in pot["leaderboard"] if r["player_id"] == pots[2]["id"])
    # If FunOnlyBob (also on 100 with the same tie-break) were in this list,
    # first prize would split to 60.
    assert leader["total"] == 100
    assert leader["prize"] == 100
    assert leader["place"] == 1
    assert leader["still_in_it"] is True
    assert loser["total"] == 0
    assert loser["still_in_it"] is False
    assert loser["max_possible_now"] == 0
    assert any(r["is_last"] for r in pot["leaderboard"])
    assert fun["id"] not in {r["player_id"] for r in pot["leaderboard"]}

    pub = expect(CLIENT.get("/api/open/state"), 200)
    assert [r["name"] for r in pub["leaderboard"]] == ["FunOnlyBob"]
    assert pub["leaderboard"][0]["total"] == 100
    assert "prize" not in pub["leaderboard"][0]
    assert pub["leaderboard"][0]["is_last"] is False
    assert all(not r.get("is_last") for r in pub["leaderboard"])
    assert "prize_first" not in pub["settings"]
    assert pub["settings"]["board_mode"] == "live"

    expect(CLIENT.post("/api/admin/snapshot", json={"kind": "election_night", "label": "night"}), 200)
    conn = D.connect(False)
    try:
        raw = conn.execute("SELECT payload_json, kind FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    payload = json.loads(raw["payload_json"])
    assert payload["pool"] == "pot"
    snap_names = [r["name"] for r in payload["leaderboard"]]
    assert snap_names.count("FunOnlyBob") == 0
    assert len(snap_names) == 6
    assert any(r["prize"] == 100 and r["name"] == "Player 1" for r in payload["leaderboard"])

    frozen = expect(CLIENT.get("/api/state"), 200)
    frozen_pub = expect(CLIENT.get("/api/open/state"), 200)
    assert frozen["settings"]["board_mode"] == "frozen"
    assert frozen_pub["settings"]["board_mode"] == "frozen"
    assert len(frozen["leaderboard"]) == 6
    assert [r["name"] for r in frozen_pub["leaderboard"]] == ["FunOnlyBob"]

    pot_csv = expect(CLIENT.get("/api/admin/export.csv"), 200)
    pub_csv = expect(CLIENT.get("/api/admin/export-public.csv"), 200)
    assert "FunOnlyBob" not in pot_csv
    assert "Player 1" in pot_csv
    assert "prize" in pot_csv.splitlines()[0].split(",")
    assert "FunOnlyBob" in pub_csv
    assert "Player 1" not in pub_csv
    assert "prize" not in pub_csv.splitlines()[0].split(",")
    assert "pred:nat_vote" in pot_csv.splitlines()[0]
    assert "pred:nat_vote" in pub_csv.splitlines()[0]

    full = expect(CLIENT.get("/api/admin/full"), 200)
    assert all(p["pool"] == "pot" for p in full["players"])
    assert len(full["players"]) == 6
    shown = next(p for p in full["public_players"] if p["name"] == "FunOnlyBob")
    assert shown["submitted"] is True
    assert shown["total"] == 100
    assert shown["pool"] == "public"


def test_public_tiebreak():
    wipe()
    closer = expect(signup("Near"), 200)["player"]
    farther = expect(signup("Far"), 200)["player"]
    qs = set_official_results()
    win, _lose = winning_and_losing(qs)
    expect(predict(closer["token"], win, tb="35.1"), 200)
    expect(predict(farther["token"], win, tb="40"), 200)
    expect(CLIENT.post("/api/admin/lock", json={"action": "lock"}), 200)
    board = {r["name"]: r for r in expect(CLIENT.get("/api/open/state"), 200)["leaderboard"]}
    assert board["Near"]["place"] == 1
    assert board["Far"]["place"] == 2
    assert board["Near"]["total"] == board["Far"]["total"] == 100
    assert board["Near"]["is_last"] is False
    assert board["Far"]["is_last"] is False
    pot = expect(CLIENT.get("/api/state"), 200)
    assert all(r["name"] not in ("Near", "Far") for r in pot["leaderboard"])


def test_admin_rename_and_ballot_rules():
    wipe()
    created = expect(signup("OldName"), 200)["player"]
    other = expect(signup("Other"), 200)["player"]
    expect(CLIENT.post("/api/admin/players", json={"id": created["id"], "name": "New Name"}), 200)
    conflict = expect(CLIENT.post("/api/admin/players", json={"id": other["id"], "name": "new name"}), 409)
    assert conflict["error"] == "name_taken"
    assert expect(
        CLIENT.post("/api/admin/players", json={"id": created["id"], "name": "x" * 25}),
        400,
    )["error"] == "bad_name"
    me = expect(CLIENT.get(f"/api/player/{created['token']}"), 200)
    assert html.unescape(me["player"]["name"]) == "New Name"
    expect(predict(created["token"], {}, name="Ballot Name", tb="35.0"), 200)
    bad = expect(predict(created["token"], {"1": "nope"}, name="y" * 30, tb="99"), 400)
    assert bad["error"] == "bad_name"
    conn = D.connect(False)
    try:
        row = conn.execute("SELECT name, tb_national_vote FROM players WHERE id=?", (created["id"],)).fetchone()
        assert html.unescape(row["name"]) == "Ballot Name"
        assert row["tb_national_vote"] == "35.0"
    finally:
        conn.close()
    full = expect(CLIENT.get("/api/admin/full"), 200)
    shown = next(p for p in full["public_players"] if p["id"] == created["id"])
    assert shown["display_name"] == "Ballot Name"
    assert shown["submitted"] is True


def test_demo_does_not_leak():
    wipe()
    live = expect(signup("LiveLana"), 200)["player"]
    demo = expect(signup("DemoDana", headers={"X-Demo": "1"}), 200)["player"]
    real_pub = expect(CLIENT.get("/api/open/state"), 200)
    demo_pub = expect(CLIENT.get("/api/open/state", headers={"X-Demo": "1"}), 200)
    assert [r["name"] for r in real_pub["leaderboard"]] == ["LiveLana"]
    assert [r["name"] for r in demo_pub["leaderboard"]] == ["DemoDana"]
    for url in (
        f"/api/player/{demo['token']}/manifest.webmanifest?demo=1",
        f"/api/player/{demo['token']}/manifest.webmanifest",
        f"/api/player/{live['token']}/manifest.webmanifest?demo=1",
    ):
        body = expect(CLIENT.get(url), 200)
        assert body["start_url"] == "/"
        assert body["short_name"] == "NZ Election"
        dumped = json.dumps(body)
        assert "DemoDana" not in dumped
        assert "LiveLana" not in dumped
    pot = expect(CLIENT.get("/api/state"), 200)
    assert all(r["name"] not in ("LiveLana", "DemoDana") for r in pot["leaderboard"])
    expect(CLIENT.post("/api/admin/demo/reset"), 200)
    after = expect(CLIENT.get("/api/open/state", headers={"X-Demo": "1"}), 200)
    assert [r["name"] for r in after["leaderboard"]] == ["Preview Guest"]
    still = expect(CLIENT.get("/api/open/state"), 200)
    assert [r["name"] for r in still["leaderboard"]] == ["LiveLana"]
    assert len(expect(CLIENT.get("/api/state"), 200)["players"]) == 6
    conn = D.connect(False)
    try:
        tokens = [p["token"] for p in D.list_players(conn, pool=D.POOL_POT)]
    finally:
        conn.close()
    assert live["token"] not in tokens
    assert len(tokens) == 6


def main():
    test_defaults_and_repeat_init_keeps_pot()
    test_pages()
    test_export_requires_admin()
    login()
    test_signup_issues_unguessable_link()
    test_name_rules()
    test_pot_name_does_not_block_public()
    test_rate_limit_is_per_ip_and_ignores_demo()
    test_cap_toggle_and_remove()
    test_lock_refuses_signup_and_edits()
    test_pot_and_public_stay_separated()
    test_public_tiebreak()
    test_admin_rename_and_ballot_rules()
    test_demo_does_not_leak()
    backend = "postgres" if D.using_postgres() else "sqlite"
    print(f"PUBLIC TESTS OK ({backend})")


if __name__ == "__main__":
    main()
