"""House chart, advance-voting parse, headlines, and automatic feed enable."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
os.environ.setdefault("SWEEP_DISABLE_FETCH", "1")
if not (os.environ.get("DATABASE_URL") or "").strip() and not (os.environ.get("DB_PATH") or "").strip():
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-hub-")) / "sweep.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

import advance as ADV  # noqa: E402
import db as D  # noqa: E402
import newsfeed as NEWS  # noqa: E402
import projection as P  # noqa: E402
from feed import (  # noqa: E402
    build_electorate_rows,
    electorate_is_close,
    maybe_enable_results_feed,
    merge_electorate_rows,
    parse_election_xml,
    parse_electorates,
)
from app import app  # noqa: E402

CLIENT = app.test_client()
NZ = ZoneInfo("Pacific/Auckland")

ADVANCE_HTML = """
<table>
<tr><th>Date</th><th>2020 General Election</th><th>2023 General Election</th><th>2026 General Election</th></tr>
<tr><td>26 October</td><td>92,434</td><td>65,845</td><td>80,000</td></tr>
<tr><td>2 November</td><td>124,819</td><td>148,476</td><td>159,994</td></tr>
<tr><td>Totals</td><td>1,282,028</td><td>860,668</td><td>1,074,416</td></tr>
</table>
""".encode()

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<item>
  <title>Early votes pass one million</title>
  <link>https://www.rnz.co.nz/news/example</link>
  <pubDate>Tue, 03 Nov 2026 02:00:00 +0000</pubDate>
  <description>Cumulative advance votes. &lt;p&gt;Full story stays on RNZ.&lt;/p&gt;</description>
</item>
<item>
  <title>This feed is no longer available</title>
  <link>https://www.rnz.co.nz/rss</link>
  <description>dead</description>
</item>
</channel></rss>
"""

ELECTORATES = """<election>
  <electorate>
    <electorate_name>Tāmaki Makaurau</electorate_name>
    <statistics><percent_voting_places_counted>42.5</percent_voting_places_counted></statistics>
    <candidate><candidate_name>Aroha</candidate_name><party>Te Pāti Māori</party><votes>1200</votes></candidate>
    <candidate><candidate_name>Other</candidate_name><party>Labour</party><votes>900</votes></candidate>
  </electorate>
</election>
"""


def test_current_parliament_is_123():
    house = P.current_parliament(None)
    assert house["seats"]["National"] == 49
    assert house["seats"]["Labour"] == 34
    assert house["seats"]["Green"] == 15
    assert house["seats"]["ACT"] == 11
    assert house["seats"]["NZ First"] == 8
    assert house["seats"]["Te Pāti Māori"] == 6
    assert house["house"] == 123
    assert house["majority"] == 62
    assert house["label"] == "Current Parliament (2023 election)"
    assert house["right"]["seats"] == 49 + 11 + 8
    assert house["right"]["majority"] is True
    assert house["left"]["short"] == 62 - (34 + 15 + 6)


def test_hemicycle_puts_top_between_blocs():
    seats = {
        "TOP": 7,
        "Green": 12,
        "Te Pāti Māori": 7,
        "Labour": 30,
        "NZ First": 4,
        "National": 55,
        "ACT": 5,
    }
    house = P.pack_house(seats, "2026 seats (official feed)")
    assert house["order"][:7] == ["Green", "Te Pāti Māori", "Labour", "TOP", "NZ First", "National", "ACT"]
    assert house["left"]["names"] == ["Labour", "Green", "Te Pāti Māori"]
    assert house["right"]["names"] == ["National", "ACT", "NZ First"]
    assert house["left"]["seats"] == 49
    assert house["right"]["seats"] == 64
    assert house["majority"] == P.majority_line(sum(seats.values()))
    # TOP follows the left bloc, so these 7 seats end at index 55, left of seat 61.
    assert 12 + 7 + 30 + 7 <= house["majority"]
    assert house["swing_left"]["names"] == ["Labour", "Green", "Te Pāti Māori", "TOP"]
    assert house["swing_left"]["seats"] == 49 + 7
    assert house["swing_left"]["short"] == house["majority"] - 56
    assert house["swing_left"]["majority"] is False
    clear = {
        "Green": 15,
        "Te Pāti Māori": 6,
        "Labour": 35,
        "TOP": 6,
        "NZ First": 8,
        "National": 40,
        "ACT": 10,
    }
    swung = P.pack_house(clear, "2026 seats (official feed)")
    assert swung["house"] == 120
    assert swung["majority"] == 61
    assert swung["left"]["seats"] == 56
    assert swung["left"]["short"] == 5
    assert swung["right"]["seats"] == 58
    assert swung["swing_left"]["seats"] == 62
    assert swung["swing_left"]["short"] == 0
    assert swung["swing_left"]["majority"] is True
    js = (ROOT / "static" / "js" / "house.js").read_text(encoding="utf-8")
    assert "'Green', 'Te Pāti Māori', 'Labour', 'TOP', 'NZ First', 'National', 'ACT'" in js
    assert "uncommitted, leans left" in js
    assert "if TOP swings left" in js
    assert "fill-opacity" in js
    options = P.coalition_options(seats, house["majority"])
    assert [row["names"] for row in options[:5]] == [
        ["National", "ACT", "NZ First"],
        ["National", "ACT"],
        ["National", "NZ First"],
        ["Labour", "Green", "Te Pāti Māori"],
        ["Labour", "Green"],
    ]
    assert any("TOP" in row["names"] for row in options)
    for row in options:
        if "TOP" in row["names"]:
            assert row["names"][-1] == "TOP"


def test_live_projection_uses_sainte_lague():
    live = P.project_live(
        {"National": 40, "Labour": 30, "Green": 12, "ACT": 8, "NZ First": 6, "Te Pāti Māori": 3},
        {"Te Pāti Māori": 6},
    )
    assert live["label"] == "Projected 2026 seats (provisional count)"
    assert live["house"] >= 120
    assert live["seats"]["Te Pāti Māori"] >= 6
    assert P.poll_projection("") is None
    assert P.poll_projection('{"parties": {}}') is None


def test_advance_table_and_blocked_page():
    parsed = ADV.parse_advance_html(ADVANCE_HTML)
    assert parsed["ok"] is True
    assert parsed["votes"] == 1074416
    assert parsed["as_of"] == "2026-11-02"
    blocked = ADV.parse_advance_html(b"<html><script src='/_Incapsula_Resource'></script></html>")
    assert blocked["ok"] is False


def test_news_parse_dedupe_and_skip_stub():
    items = NEWS.parse_feed(RSS, "RNZ")
    assert len(items) == 1
    assert items[0]["title"] == "Early votes pass one million"
    assert "<p>" not in items[0]["summary"]
    again = NEWS.merge_items(items, items)
    assert len(again) == 1


def test_electorate_leader_and_maori_region():
    import xml.etree.ElementTree as ET
    rows = parse_electorates(ET.fromstring(ELECTORATES), {})
    assert rows[0]["name"] == "Tāmaki Makaurau"
    assert rows[0]["region"] == "Māori"
    assert rows[0]["leader"] == "Aroha"
    assert rows[0]["party"] == "Te Pāti Māori"
    assert rows[0]["margin"] == 300
    assert rows[0]["counted"] == 42.5


INDEX_XML = b"""<electorates>
  <electorate e_no="1"><electorate_name>Auckland Central</electorate_name></electorate>
  <electorate e_no="2"><electorate_name>Botany</electorate_name></electorate>
  <electorate e_no="3"><electorate_name>Epsom</electorate_name></electorate>
  <electorate e_no="4"><electorate_name>Ikaroa-R\xc4\x81whiti</electorate_name></electorate>
</electorates>"""

CANDIDATES_XML = b"""<candidates>
  <candidate c_no="243"><candidate_name>SWARBRICK, Chl\xc3\xb6e</candidate_name><electorate>1</electorate><party>10</party></candidate>
  <candidate c_no="192"><candidate_name>WHITE, Example</candidate_name><electorate>1</electorate><party>16</party></candidate>
  <candidate c_no="1"><candidate_name>BIG, Lead</candidate_name><electorate>2</electorate><party>16</party></candidate>
  <candidate c_no="2"><candidate_name>SMALL, Other</candidate_name><electorate>2</electorate><party>13</party></candidate>
  <candidate c_no="3"><candidate_name>NEAR, One</candidate_name><electorate>3</electorate><party>5</party></candidate>
  <candidate c_no="4"><candidate_name>NEAR, Two</candidate_name><electorate>3</electorate><party>16</party></candidate>
  <candidate c_no="5"><candidate_name>ZERO, A</candidate_name><electorate>4</electorate><party>14</party></candidate>
  <candidate c_no="6"><candidate_name>ZERO, B</candidate_name><electorate>4</electorate><party>13</party></candidate>
</candidates>"""

PNO = {"5": "ACT", "10": "Green", "13": "Labour", "14": "Te P\u0101ti M\u0101ori", "16": "National"}


def _efile(counted, cast, pairs):
    cands = "".join(f'<candidate c_no="{cno}"><votes>{votes}</votes></candidate>' for cno, votes in pairs)
    return (
        f'<electorate><statistics><percent_voting_places_counted>{counted}</percent_voting_places_counted>'
        f'<total_votes_cast>{cast}</total_votes_cast></statistics><candidatevotes>{cands}</candidatevotes></electorate>'
    ).encode()


def test_electorate_bundle_close_seats_and_api():
    rows = build_electorate_rows(INDEX_XML, CANDIDATES_XML, {
        "1": _efile("30.8", 19007, [("243", 8902), ("192", 8782)]),
        "2": _efile("40", 20000, [("1", 14000), ("2", 6000)]),
        "3": _efile("10", 100000, [("3", 50800), ("4", 50000)]),
        "4": _efile("0", 0, [("5", 0), ("6", 0)]),
    }, PNO)
    by = {row["name"]: row for row in rows}
    assert "Ikaroa-Rāwhiti" not in by
    central = by["Auckland Central"]
    assert central["leader"] == "Chlöe Swarbrick"
    assert central["party"] == "Green"
    assert central["votes"] == 8902
    assert central["second"] == "Example White"
    assert central["second_party"] == "National"
    assert central["margin"] == 120
    assert central["counted"] == 30.8
    assert central["close"] is True
    assert by["Botany"]["margin"] == 8000
    assert by["Botany"]["close"] is False
    assert by["Epsom"]["margin"] == 800
    assert by["Epsom"]["close"] is True
    assert electorate_is_close(500, 25000) is False
    assert electorate_is_close(499, 100000) is True
    kept = merge_electorate_rows(rows, [by["Botany"]])
    assert {row["name"] for row in kept} == {"Auckland Central", "Botany", "Epsom"}
    conn = D.connect(False)
    try:
        D.set_setting(conn, "electorates_json", __import__("json").dumps(rows))
        conn.commit()
    finally:
        conn.close()
    body = CLIENT.get("/api/electorates").get_json()
    assert [row["name"] for row in body["close"]] == ["Auckland Central", "Epsom"]
    assert body["electorates"][0]["leader"]
    page = CLIENT.get("/live").get_data(as_text=True)
    assert "electoratesPanel" in page and "Close seats" in page
    assert "tv-board" in CLIENT.get("/tv").get_data(as_text=True)


def test_auto_enable_waits_for_lock_instant():
    conn = D.connect(False)
    try:
        D.set_setting(conn, "feed_enabled", "0")
        D.set_setting(conn, "feed_manual_off", "0")
        D.set_setting(conn, "entries_open", "0")
        D.set_setting(conn, "lock_at", "2099-01-01T00:00:00+13:00")
        conn.commit()
        assert maybe_enable_results_feed(conn) is False
        assert D.get_setting(conn, "feed_enabled") == "0"
        D.set_setting(conn, "lock_at", "2020-01-01T00:00:00+13:00")
        conn.commit()
        assert maybe_enable_results_feed(conn) is True
        assert D.get_setting(conn, "feed_enabled") == "1"
        assert D.get_setting(conn, "feed_poll_seconds") == "180"
        D.set_setting(conn, "feed_enabled", "0")
        D.set_setting(conn, "feed_manual_off", "1")
        conn.commit()
        assert maybe_enable_results_feed(conn) is False
    finally:
        conn.close()


def test_pages_and_status():
    home = CLIENT.get("/")
    assert home.status_code == 200
    assert b"Play for fun (not in the pot)" in home.data
    assert b'href="/open"' in home.data
    for path in ("/results", "/news", "/seats", "/live", "/api/status", "/api/news", "/api/results"):
        resp = CLIENT.get(path)
        assert resp.status_code == 200, path
    status = CLIENT.get("/api/status").get_json()
    assert "results_feed" in status["sources"]
    assert "advance_voting" in status["sources"]
    assert "news" in status["sources"]
    results = CLIENT.get("/api/results").get_json()
    assert results["house"]["current"]["house"] == 123
    assert results["house"]["has_results"] is False
    assert results["advance"]["votes_2026"] == "1074416"
    parsed = parse_election_xml(Path(ROOT / "tests/fixtures/election_2023.xml").read_bytes())
    assert parsed["candidate_seats"]["National"] == 43
    assert datetime.now(NZ)


if __name__ == "__main__":
    test_current_parliament_is_123()
    test_hemicycle_puts_top_between_blocs()
    test_live_projection_uses_sainte_lague()
    test_advance_table_and_blocked_page()
    test_news_parse_dedupe_and_skip_stub()
    test_electorate_leader_and_maori_region()
    test_electorate_bundle_close_seats_and_api()
    test_auto_enable_waits_for_lock_instant()
    test_pages_and_status()
    print("HUB TESTS PASSED")
