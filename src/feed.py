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


# 2023 archive election.xml identifies parties by p_no. These ids are the
# ones published for that file; parties.xml is preferred when we can fetch it.
BUILTIN_PNO = {
    "5": "ACT",
    "10": "Green",
    "13": "Labour",
    "14": "Te Pāti Māori",
    "16": "National",
    "17": "NZ First",
    "24": "TOP",
}

CANONICAL = {
    "national": "National",
    "national party": "National",
    "labour": "Labour",
    "labour party": "Labour",
    "green": "Green",
    "green party": "Green",
    "act": "ACT",
    "act new zealand": "ACT",
    "nz first": "NZ First",
    "new zealand first": "NZ First",
    "new zealand first party": "NZ First",
    "te pati maori": "Te Pāti Māori",
    "maori party": "Te Pāti Māori",
    "top": "TOP",
    "the opportunities party (top)": "TOP",
    "the opportunities party": "TOP",
}


class FeedPoller:
    def __init__(self, broadcast_fn=None, on_applied=None):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.broadcast = broadcast_fn or (lambda *_: None)
        self.on_applied = on_applied

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
                secs = int(D.get_setting(conn, "feed_poll_seconds", "180") or "180")
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

    def poll_once(self, demo: bool = False, *, body: Optional[bytes] = None, parties_body: Optional[bytes] = None, apply: bool = True) -> dict:
        """Fetch (or accept bytes) and optionally apply. Never clears manual values.

        A bad, empty, or failed fetch records an error and leaves results alone.
        Exceptions are caught so a preview or a poll cannot take down the process.
        """
        try:
            return self._poll_once(demo, body=body, parties_body=parties_body, apply=apply)
        except Exception as e:
            try:
                self._record_error(str(e))
            except Exception:
                pass
            return {"ok": False, "error": "feed_failed", "message": str(e)[:300]}

    def preview(self, demo: bool = False) -> dict:
        """Fetch and parse without writing results. Works while the poller is off."""
        result = self.poll_once(demo=demo, apply=False)
        result["applied"] = False
        result["preview"] = True
        return result

    def _poll_once(self, demo: bool, *, body: Optional[bytes], parties_body: Optional[bytes], apply: bool) -> dict:
        conn = D.connect(demo)
        try:
            enabled = D.get_setting(conn, "feed_enabled", "0") == "1"
            url = D.get_setting(conn, "feed_url", DEFAULT_FEED) or DEFAULT_FEED
            if apply:
                conn.execute(
                    "UPDATE feed_status SET last_attempt_at=?, enabled=? WHERE id=1",
                    (D.iso_now(), 1 if enabled else 0),
                )
                conn.commit()
                if body is None and not enabled:
                    return {"ok": False, "reason": "feed_disabled"}
            else:
                conn.execute(
                    "UPDATE feed_status SET last_attempt_at=? WHERE id=1",
                    (D.iso_now(),),
                )
                conn.commit()

            used = None
            if body is None:
                body, parties_body, used, err = fetch_feed(url)
                if body is None:
                    conn.execute("UPDATE feed_status SET last_error=? WHERE id=1", ((err or "no response")[:500],))
                    conn.commit()
                    return {"ok": False, "reason": err or "no response", "votes": {}, "seats": {}}

            parsed = parse_election_xml(body, parties_body)
            if not parsed.get("ok"):
                conn.execute(
                    "UPDATE feed_status SET last_error=? WHERE id=1",
                    ((parsed.get("error") or "parse failed")[:500],),
                )
                conn.commit()
                parsed["votes"] = parsed.get("votes") or {}
                parsed["seats"] = parsed.get("seats") or {}
                return parsed

            if not apply:
                return {
                    "ok": True,
                    "preview": True,
                    "applied": False,
                    "url": used,
                    "votes": parsed.get("votes") or {},
                    "seats": parsed.get("seats") or {},
                    "values": parsed.get("values") or {},
                    "reporting_pct": parsed.get("reporting_pct"),
                }

            questions = {q["key"]: q for q in D.list_questions(conn)}
            results = D.get_results(conn)
            applied = []
            for key, value in (parsed.get("values") or {}).items():
                if value is None or value == "":
                    continue
                q = questions.get(key)
                if not q:
                    continue
                existing = results.get(str(q["id"]))
                if existing and existing.get("source") == "manual" and existing.get("value") not in (None, ""):
                    continue
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
            if applied and self.on_applied:
                self.on_applied(conn)
            conn.commit()
            if applied:
                self.broadcast("results")
            return {
                "ok": True,
                "applied": applied,
                "url": used,
                "votes": parsed.get("votes") or {},
                "seats": parsed.get("seats") or {},
                "reporting_pct": parsed.get("reporting_pct"),
            }
        finally:
            conn.close()


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("ā", "a").replace("ī", "i").replace("ū", "u").split())


def canonical_party(name: str) -> Optional[str]:
    return CANONICAL.get(_norm(name))


def parties_from_xml(body: bytes) -> dict[str, str]:
    """Map p_no → canonical party name from a parties.xml document."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != "party":
            continue
        pno = el.attrib.get("p_no") or el.attrib.get("pno")
        label = el.findtext("short_name") or el.findtext("party_name") or el.attrib.get("name") or ""
        canon = canonical_party(label)
        if pno and canon:
            out[str(pno)] = canon
    return out


def fetch_feed(url: str) -> tuple[Optional[bytes], Optional[bytes], Optional[str], Optional[str]]:
    """Return (election_xml, parties_xml, url_used, error)."""
    base = (url or "").strip()
    if base.endswith(".xml"):
        candidates = [base]
        parties_url = base.rsplit("/", 1)[0] + "/parties.xml"
    else:
        root = base.rstrip("/")
        candidates = [root + "/election.xml", root + "/"]
        parties_url = root + "/parties.xml"
    last_err = None
    body = None
    used = None
    for u in candidates:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "NZ-Election-Sweepstakes/1.0 (private party; results read-only)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if getattr(resp, "status", 200) != 200:
                    last_err = f"HTTP {resp.status} for {u}"
                    continue
                body = resp.read()
                used = u
                break
        except Exception as e:
            last_err = f"{u}: {e}"
    parties = None
    try:
        req = urllib.request.Request(parties_url, headers={"User-Agent": "NZ-Election-Sweepstakes/1.0 (private party; results read-only)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            if getattr(resp, "status", 200) == 200:
                parties = resp.read()
    except Exception:
        parties = None
    return body, parties, used, last_err


def parse_election_xml(body: bytes, parties_body: Optional[bytes] = None) -> dict:
    """Parse EC election XML, including the 2023 archive election.xml shape."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        text = body.decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"not XML ({e}); head={text!r}", "votes": {}, "seats": {}}

    pno_map = dict(BUILTIN_PNO)
    if parties_body:
        pno_map.update(parties_from_xml(parties_body))

    votes: dict[str, float] = {}
    seats: dict[str, int] = {}
    reporting = None

    counted = root.find(".//percent_voting_places_counted")
    if counted is not None and (counted.text or "").strip():
        try:
            reporting = float(counted.text.strip())
        except ValueError:
            pass

    for el in root.iter():
        tag = el.tag.split("}")[-1].lower()
        if tag != "party":
            if tag in ("votingplacescounted", "reporting"):
                try:
                    reporting = float((el.text or "").strip())
                except ValueError:
                    pass
            continue
        pno = el.attrib.get("p_no") or el.attrib.get("pno")
        attrs = {k.lower(): v for k, v in el.attrib.items()}
        name = (
            (pno_map.get(str(pno)) if pno else None)
            or canonical_party(attrs.get("party") or attrs.get("name") or attrs.get("partyname") or el.findtext("Party") or el.findtext("Name") or el.findtext("short_name") or "")
        )
        if not name:
            continue
        vote = el.findtext("percent_votes") or attrs.get("partyvote") or attrs.get("percentage") or el.findtext("Percentage") or el.findtext("PartyVote")
        seat = el.findtext("total_seats") or attrs.get("totalseats") or attrs.get("seats") or el.findtext("TotalSeats") or el.findtext("Seats")
        if vote is not None and str(vote).strip() != "":
            try:
                votes[name] = float(str(vote).replace("%", "").strip())
            except ValueError:
                pass
        if seat is not None and str(seat).strip() != "":
            try:
                seats[name] = int(float(str(seat).strip()))
            except ValueError:
                pass

    if not votes and not seats:
        return {"ok": False, "error": "XML parsed but no recognised party vote or seat figures", "votes": {}, "seats": {}}

    from scoring import derive_from_party_table
    derived = derive_from_party_table(votes, seats)
    values: dict[str, Any] = {}
    for key in ("nat_vote", "lab_vote", "grn_vote", "act_vote", "nzf_vote", "tpm_vote"):
        if derived.get(key) is not None:
            values[key] = derived[key]
    for key in ("most_seats", "parties_in", "bloc_61", "minor_highest"):
        if derived.get(key) is not None:
            values[key] = derived[key]
    return {"ok": True, "values": values, "votes": votes, "seats": seats, "reporting_pct": reporting}
