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
from feed import maybe_enable_results_feed, parse_election_xml, parse_electorates  # noqa: E402
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


def test_hemicycle_puts_top_left_of_green():
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
    assert house["order"][:7] == ["TOP", "Green", "Te Pāti Māori", "Labour", "NZ First", "National", "ACT"]
    assert house["left"]["names"] == ["Labour", "Green", "Te Pāti Māori"]
    assert house["right"]["names"] == ["National", "ACT", "NZ First"]
    assert house["left"]["seats"] == 49
    assert house["right"]["seats"] == 64
    assert house["majority"] == P.majority_line(sum(seats.values()))
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
    test_live_projection_uses_sainte_lague()
    test_advance_table_and_blocked_page()
    test_news_parse_dedupe_and_skip_stub()
    test_electorate_leader_and_maori_region()
    test_auto_enable_waits_for_lock_instant()
    test_pages_and_status()
    print("HUB TESTS PASSED")
