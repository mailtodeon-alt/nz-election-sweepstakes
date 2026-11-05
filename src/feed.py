"""Optional Electoral Commission XML feed poller. Default OFF until verified live."""
from __future__ import annotations
import json
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as D


# Known 2026 live media feed (community-documented; 404 until election night).
DEFAULT_FEED = "https://media.election.net.nz/xml/"
ARCHIVE_2023 = "https://archive.electionresults.govt.nz/electionresults_2023/xml/"

# Map party names in feed → our question keys (votes / seats)
PARTY_VOTE_KEYS = {
    "national": "nat_vote",
    "national party": "nat_vote",
    "labour": "lab_vote",
    "labour party": "lab_vote",
    "nz first": "nzf_vote",
    "new zealand first": "nzf_vote",
    "act": "act_vote",
    "act new zealand": "act_vote",
    "green": "grn_vote",
    "green party": "grn_vote",
    "te pati maori": "tpm_vote",
    "te pāti māori": "tpm_vote",
    "maori party": "tpm_vote",
}
PARTY_SEAT_KEYS = {
    "national": "nat_seats",
    "national party": "nat_seats",
    "labour": "lab_seats",
    "labour party": "lab_seats",
}


class FeedPoller:
    def __init__(self, broadcast_fn=None):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.broadcast = broadcast_fn or (lambda *_: None)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="feed-poller", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:
                self._record_error(str(e))
            # sleep in chunks so stop is responsive
            conn = D.connect(False)
            try:
                secs = int(D.get_setting(conn, "feed_poll_seconds", "90") or "90")
            finally:
                conn.close()
            secs = max(30, min(secs, 600))
            for _ in range(secs):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def _record_error(self, err: str):
        conn = D.connect(False)
        try:
            conn.execute(
                "UPDATE feed_status SET last_attempt_at=?, last_error=? WHERE id=1",
                (D.iso_now(), err[:500]),
            )
            conn.commit()
        finally:
            conn.close()

    def poll_once(self, demo: bool = False) -> dict:
        """Attempt one poll. Returns status dict. Does not overwrite MANUAL results."""
        conn = D.connect(demo)
        try:
            enabled = D.get_setting(conn, "feed_enabled", "0") == "1"
            url = D.get_setting(conn, "feed_url", DEFAULT_FEED) or DEFAULT_FEED
            conn.execute(
                "UPDATE feed_status SET last_attempt_at=?, enabled=? WHERE id=1",
                (D.iso_now(), 1 if enabled else 0),
            )
            conn.commit()
            if not enabled:
                return {"ok": False, "reason": "feed_disabled"}

            # Try election.xml first (common in media feed), then directory listing fallbacks
            candidates = [
                url.rstrip("/") + "/election.xml",
                url.rstrip("/") + "/parties.xml",
                url.rstrip("/") + "/",
            ]
            last_err = None
            body = None
            used = None
            for u in candidates:
                try:
                    req = urllib.request.Request(u, headers={"User-Agent": "NZ-Election-Sweepstakes/1.0 (private party; results read-only)"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        if resp.status != 200:
                            last_err = f"HTTP {resp.status} for {u}"
                            continue
                        body = resp.read()
                        used = u
                        break
                except Exception as e:
                    last_err = f"{u}: {e}"
            if body is None:
                conn.execute(
                    "UPDATE feed_status SET last_error=? WHERE id=1",
                    (last_err or "no response",),
                )
                conn.commit()
                return {"ok": False, "reason": last_err}

            parsed = parse_election_xml(body)
            if not parsed.get("ok"):
                conn.execute(
                    "UPDATE feed_status SET last_error=? WHERE id=1",
                    (parsed.get("error", "parse failed"),),
                )
                conn.commit()
                return parsed

            # Apply AUTO results only where question has no MANUAL override
            questions = {q["key"]: q for q in D.list_questions(conn)}
            results = D.get_results(conn)
            applied = []
            for key, value in parsed.get("values", {}).items():
                q = questions.get(key)
                if not q:
                    continue
                qid = str(q["id"])
                existing = results.get(qid)
                if existing and existing.get("source") == "manual" and existing.get("value") not in (None, ""):
                    continue  # admin override wins
                conn.execute(
                    """INSERT INTO results(question_id, value, source, updated_at)
                       VALUES (?,?, 'auto', ?)
                       ON CONFLICT(question_id) DO UPDATE SET
                         value=excluded.value, source='auto', updated_at=excluded.updated_at
                       WHERE results.source != 'manual' OR results.value IS NULL OR results.value = ''
                    """,
                    (q["id"], str(value), D.iso_now()),
                )
                applied.append(key)

            if parsed.get("reporting_pct") is not None:
                D.set_setting(conn, "reporting_pct", str(parsed["reporting_pct"]))

            summary = json.dumps({"url": used, "applied": applied, "reporting": parsed.get("reporting_pct")})
            conn.execute(
                "UPDATE feed_status SET last_success_at=?, last_error=NULL, last_payload_summary=? WHERE id=1",
                (D.iso_now(), summary),
            )
            D.audit(conn, "feed", "auto_update", {"applied": applied, "url": used})
            conn.commit()
            self.broadcast("results")
            return {"ok": True, "applied": applied, "url": used}
        finally:
            conn.close()


def parse_election_xml(body: bytes) -> dict:
    """Best-effort parser for EC election XML. Structure may vary; unverified for 2026."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        # Maybe HTML directory listing
        text = body.decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"not XML ({e}); head={text!r}"}

    values = {}
    reporting = None

    # Collect any element that looks like party vote / seats
    def norm(s: str) -> str:
        return " ".join((s or "").lower().replace("ā", "a").replace("ī", "i").replace("ū", "u").split())

    # Walk all elements; look for common attribute/tag patterns from past feeds
    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        attrs = {k.lower(): v for k, v in el.attrib.items()}
        name = attrs.get("party") or attrs.get("name") or attrs.get("partyname") or el.findtext("Party") or el.findtext("Name")
        if not name:
            continue
        n = norm(name)
        vote = attrs.get("partyvote") or attrs.get("vote") or attrs.get("percentage") or el.findtext("PartyVote") or el.findtext("Percentage")
        seats = attrs.get("totalseats") or attrs.get("seats") or el.findtext("TotalSeats") or el.findtext("Seats")
        if vote is not None and n in PARTY_VOTE_KEYS:
            try:
                values[PARTY_VOTE_KEYS[n]] = float(str(vote).replace("%", "").strip())
            except ValueError:
                pass
        if seats is not None and n in PARTY_SEAT_KEYS:
            try:
                values[PARTY_SEAT_KEYS[n]] = int(float(str(seats).strip()))
            except ValueError:
                pass

        if tag in ("votingplacescounted", "reporting") or "reporting" in attrs:
            try:
                reporting = float(attrs.get("percent") or attrs.get("percentage") or (el.text or ""))
            except ValueError:
                pass

    # Derive most_seats and right_bloc if we have enough
    seats_map = {}
    votes_map = {}
    for n, key in PARTY_SEAT_KEYS.items():
        if key in values:
            # reverse to party display name
            pass
    # Build from known keys
    party_seats = {}
    party_votes = {}
    key_to_party = {
        "nat_seats": "National", "lab_seats": "Labour",
        "nat_vote": "National", "lab_vote": "Labour",
        "nzf_vote": "NZ First", "act_vote": "ACT",
        "grn_vote": "Green", "tpm_vote": "Te Pāti Māori",
    }
    for k, v in values.items():
        party = key_to_party.get(k)
        if not party:
            continue
        if k.endswith("_seats"):
            party_seats[party] = int(v)
        if k.endswith("_vote"):
            party_votes[party] = float(v)

    # We may only have National/Labour seats from mapping; still compute most seats among known
    if party_seats:
        from scoring import most_seats_winner
        w = most_seats_winner(party_seats, party_votes)
        if w:
            values["most_seats"] = w
    if all(k in values for k in ("nat_seats",)) and "act_seats" not in values:
        # right_bloc needs ACT + NZF seats — leave pending unless present
        pass

    if not values:
        return {"ok": False, "error": "XML parsed but no recognised party fields (feed format unverified for 2026)"}
    return {"ok": True, "values": values, "reporting_pct": reporting}
