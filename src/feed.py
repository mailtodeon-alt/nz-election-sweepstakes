"""Electoral Commission feed, advance-voting totals, and headline refresh.

Results XML stays off until lock time (7pm NZDT on the configured lock_at,
default Sat 7 Nov 2026), then turns itself on at a 180s interval with backoff.
The 2026 media file identifies parties by p_no only; names are joined from
parties.xml on the same base URL and cached. A pre-count document (nothing
counted, every mapped figure zero) is not written as AUTO. Manual results
are never overwritten. Advance voting is polled about every 30 minutes until
lock. Headlines refresh about every 5 minutes. A failed fetch never clears
the last good value.
"""
from __future__ import annotations
import json
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Optional
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import advance as ADV
import db as D
import newsfeed as NEWS


# Live 2026 media feed. election.xml parties are p_no only; names are in parties.xml.
# The poller stays off until lock time (feed_enabled defaults to 0).
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
    # 2026 parties.xml short_name is "Opportunity Party" (abbrev OPP), not TOP.
    "opportunity": "TOP",
    "opportunity party": "TOP",
    "the opportunity party": "TOP",
}

# Ballot parties stay full-size on the TV. TOP is the Opportunity Party;
# the ballot choice is still the key "TOP".
BALLOT_PARTIES = ("National", "Labour", "Green", "ACT", "NZ First", "Te Pāti Māori")
PARTY_COLORS = {
    "National": "#00529F",
    "Labour": "#D82A20",
    "Green": "#098137",
    "ACT": "#F5C518",
    "NZ First": "#888888",
    "Te Pāti Māori": "#B2001D",
    "TOP": "#32b5e8",
}
_OTHER_COLORS = ("#c4b5fd", "#f9a8d4", "#fdba74", "#86efac", "#93c5fd", "#fca5a5", "#fde68a", "#a5b4fc")


def party_label(key: str) -> str:
    """Name on the party-vote board. Scoring still uses the key, including TOP."""
    if key == "TOP":
        return "Opportunity (TOP)"
    return key


def party_color(key: str) -> str:
    if key in PARTY_COLORS:
        return PARTY_COLORS[key]
    total = sum(ord(ch) for ch in (key or ""))
    return _OTHER_COLORS[total % len(_OTHER_COLORS)]


def party_is_compact(key: str, percent: Any) -> bool:
    """Smaller row for a long-tail party. Ballot parties and TOP stay full size."""
    if key in BALLOT_PARTIES or key == "TOP":
        return False
    try:
        return float(percent) < 5.0
    except (TypeError, ValueError):
        return True


class FeedPoller:
    def __init__(self, broadcast_fn=None, on_applied=None):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.broadcast = broadcast_fn or (lambda *_: None)
        self.on_applied = on_applied
        self._failures = 0
        self._results_next = 0.0
        self._advance_next = 0.0
        self._news_next = 0.0
        # parties.xml is stable on the night; keyed by its URL.
        self._parties_cache: dict[str, bytes] = {}

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
                self.tick()
            except Exception as e:
                self._record_error(str(e))
            for _ in range(15):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def tick(self) -> None:
        """One scheduler pass. Safe to call from tests with the network avoided."""
        now = time.time()
        conn = D.connect(False)
        try:
            started = maybe_enable_results_feed(conn)
            if started:
                conn.commit()
                self.broadcast("settings")
            if now >= self._advance_next and not past_lock(conn):
                self._advance_next = now + 30 * 60
                self._refresh_advance(conn)
            if now >= self._news_next:
                self._news_next = now + 5 * 60
                self._refresh_news(conn)
            enabled = D.get_setting(conn, "feed_enabled", "0") == "1"
        finally:
            conn.close()
        if enabled and now >= self._results_next:
            result = self.poll_once()
            interval = 180
            conn = D.connect(False)
            try:
                interval = int(D.get_setting(conn, "feed_poll_seconds", "180") or "180")
            except (TypeError, ValueError):
                interval = 180
            finally:
                conn.close()
            interval = max(30, min(interval, 600))
            if result.get("ok"):
                self._failures = 0
                self._results_next = time.time() + interval
            else:
                self._failures = min(self._failures + 1, 4)
                self._results_next = time.time() + min(interval * (2 ** self._failures), 900)

    def _refresh_advance(self, conn) -> None:
        try:
            parsed = ADV.fetch_advance()
        except Exception as exc:
            D.set_setting(conn, "advance_fetch_error", str(exc)[:300])
            conn.commit()
            return
        if not parsed.get("ok"):
            D.set_setting(conn, "advance_fetch_error", str(parsed.get("error") or "advance fetch failed")[:300])
            conn.commit()
            return
        D.set_setting(conn, "advance_votes_2026", str(int(parsed["votes"])))
        if parsed.get("as_of"):
            D.set_setting(conn, "advance_as_of", parsed["as_of"])
        D.set_setting(conn, "advance_fetch_ok_at", D.iso_now())
        D.set_setting(conn, "advance_fetch_error", "")
        conn.commit()
        self.broadcast("advance")

    def _refresh_news(self, conn) -> None:
        try:
            fetched = NEWS.fetch_news()
        except Exception as exc:
            D.set_setting(conn, "news_fetch_error", str(exc)[:300])
            conn.commit()
            return
        D.set_setting(conn, "news_sources_json", json.dumps(fetched.get("sources") or []))
        if not fetched.get("ok"):
            D.set_setting(conn, "news_fetch_error", "every headline source failed")
            conn.commit()
            return
        try:
            existing = json.loads(D.get_setting(conn, "news_json", "[]") or "[]")
        except Exception:
            existing = []
        merged = NEWS.merge_items(existing if isinstance(existing, list) else [], fetched.get("items") or [])
        D.set_setting(conn, "news_json", json.dumps(merged))
        D.set_setting(conn, "news_fetch_ok_at", D.iso_now())
        D.set_setting(conn, "news_fetch_error", "")
        conn.commit()
        self.broadcast("news")

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
            electorates_body = None
            if body is None:
                body, used, err = fetch_election(url)
                if body is None:
                    conn.execute("UPDATE feed_status SET last_error=? WHERE id=1", ((err or "no response")[:500],))
                    conn.commit()
                    return {"ok": False, "reason": err or "no response", "votes": {}, "seats": {}}
                if used:
                    electorates_body = fetch_url(used.rsplit("/", 1)[0] + "/electorates.xml")

            if parties_body is None and body and election_uses_pno(body):
                # Same directory as feed_url / the election document. Cached after a good fetch.
                parties_body = self._parties_body(used or url, allow_network=used is not None)

            parsed = parse_election_xml(body, parties_body, electorates_body)
            if not parsed.get("ok"):
                conn.execute(
                    "UPDATE feed_status SET last_error=? WHERE id=1",
                    ((parsed.get("error") or "parse failed")[:500],),
                )
                conn.commit()
                parsed["votes"] = parsed.get("votes") or {}
                parsed["seats"] = parsed.get("seats") or {}
                return parsed

            placeholder = is_precount_placeholder(parsed)
            if not apply:
                return {
                    "ok": True,
                    "preview": True,
                    "applied": False,
                    "placeholder": placeholder,
                    "url": used,
                    "votes": parsed.get("votes") or {},
                    "seats": parsed.get("seats") or {},
                    "values": parsed.get("values") or {},
                    "reporting_pct": parsed.get("reporting_pct"),
                }

            if placeholder:
                # Pre-count file: 0% counted and every mapped figure is zero.
                # Do not write AUTO results, reporting %, or the house JSON.
                summary = json.dumps({
                    "url": used,
                    "applied": [],
                    "skipped": "precount_placeholder",
                    "reporting": parsed.get("reporting_pct"),
                })
                conn.execute(
                    "UPDATE feed_status SET last_success_at=?, last_error=NULL, last_payload_summary=? WHERE id=1",
                    (D.iso_now(), summary),
                )
                conn.commit()
                return {
                    "ok": True,
                    "applied": [],
                    "skipped": "precount_placeholder",
                    "placeholder": True,
                    "url": used,
                    "votes": parsed.get("votes") or {},
                    "seats": parsed.get("seats") or {},
                    "reporting_pct": parsed.get("reporting_pct"),
                }

            # Same 180s cycle as party results. A failed or slow electorate
            # fetch must not stop the party write below.
            if used:
                try:
                    fresh = load_electorate_rows(used, parties_body)
                except Exception:
                    fresh = []
                if fresh:
                    try:
                        previous = json.loads(D.get_setting(conn, "electorates_json", "[]") or "[]")
                    except Exception:
                        previous = []
                    if not isinstance(previous, list):
                        previous = []
                    parsed["electorates"] = merge_electorate_rows(previous, fresh)

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
            if parsed.get("candidate_seats"):
                D.set_setting(conn, "candidate_seats_json", json.dumps(parsed["candidate_seats"]))
            if parsed.get("vote_counts"):
                D.set_setting(conn, "vote_counts_json", json.dumps(parsed["vote_counts"]))
            # Display snapshot. Question rows above stay the scoring source,
            # so a manual ballot result is not replaced by this table.
            D.set_setting(conn, "feed_party_json", json.dumps({
                "votes": parsed.get("votes") or {},
                "seats": parsed.get("seats") or {},
                "vote_counts": parsed.get("vote_counts") or {},
            }))
            counted_electorates = []
            for row in parsed.get("electorates") or []:
                if not isinstance(row, dict) or not row.get("leader"):
                    continue
                try:
                    if int(row.get("votes") or 0) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                counted_electorates.append(row)
            lead_events = []
            if counted_electorates:
                D.set_setting(conn, "electorates_json", json.dumps(counted_electorates))
                try:
                    lead_events = apply_lead_changes(conn, counted_electorates)
                except Exception:
                    lead_events = []

            summary = json.dumps({"url": used, "applied": applied, "reporting": parsed.get("reporting_pct")})
            conn.execute(
                "UPDATE feed_status SET last_success_at=?, last_error=NULL, last_payload_summary=? WHERE id=1",
                (D.iso_now(), summary),
            )
            D.audit(conn, "feed", "auto_update", {"applied": applied, "url": used})
            if applied and self.on_applied:
                self.on_applied(conn)
            conn.commit()
            if applied or parsed.get("electorates"):
                self.broadcast("results")
            if lead_events:
                self.broadcast("electorates", {"lead_changes": lead_events})
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

    def _parties_body(self, base_url: str, *, allow_network: bool) -> Optional[bytes]:
        """parties.xml for this feed base. Reuse a cached copy that named at least one party."""
        parties_url = parties_url_for(base_url)
        cached = self._parties_cache.get(parties_url)
        if cached is not None:
            return cached
        if not allow_network:
            return None
        fetched = fetch_url(parties_url)
        if fetched and parties_from_xml(fetched):
            self._parties_cache[parties_url] = fetched
        return fetched


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("ā", "a").replace("ī", "i").replace("ū", "u").split())


def canonical_party(name: str) -> Optional[str]:
    return CANONICAL.get(_norm(name))


def parties_from_xml(body: bytes) -> dict[str, str]:
    """Map p_no → party key from a parties.xml document.

    Known parties use the scoring key (Opportunity Party → TOP). Every other
    party keeps its short name so a party with votes is not dropped.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    out: dict[str, str] = {}
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != "party":
            continue
        pno = el.attrib.get("p_no") or el.attrib.get("pno")
        label = (el.findtext("short_name") or el.findtext("party_name") or el.attrib.get("name") or "").strip()
        canon = canonical_party(label)
        if pno and (canon or label):
            out[str(pno)] = canon or label
    return out


def parties_url_for(feed_url: str) -> str:
    """parties.xml beside election.xml, or under a directory feed base."""
    base = (feed_url or "").strip() or DEFAULT_FEED
    if base.endswith(".xml"):
        return base.rsplit("/", 1)[0] + "/parties.xml"
    return base.rstrip("/") + "/parties.xml"


def election_uses_pno(body: bytes) -> bool:
    """True when a party node is identified only by p_no (the 2026 media shape)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return False
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != "party":
            continue
        if not (el.attrib.get("p_no") or el.attrib.get("pno")):
            continue
        attrs = {k.lower(): v for k, v in el.attrib.items()}
        named = (
            attrs.get("party") or attrs.get("name") or attrs.get("partyname")
            or el.findtext("Party") or el.findtext("Name")
            or el.findtext("short_name") or el.findtext("party_name")
        )
        if not (named or "").strip():
            return True
    return False


def is_precount_placeholder(parsed: dict) -> bool:
    """True when nothing is counted and every mapped vote and seat figure is zero.

    The live file before 7pm is this shape. Writing it would score pending
    questions as zero. A counted file with a real percentage is not a placeholder
    even if some party is still on zero.
    """
    reporting = parsed.get("reporting_pct")
    try:
        reporting_zero = reporting is None or float(reporting) == 0.0
    except (TypeError, ValueError):
        reporting_zero = True
    if not reporting_zero:
        return False
    numbers: list[float] = []
    for bucket in ("votes", "seats"):
        for value in (parsed.get(bucket) or {}).values():
            try:
                numbers.append(float(value))
            except (TypeError, ValueError):
                return False
    return bool(numbers) and all(n == 0.0 for n in numbers)


def fetch_election(url: str) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Return (election_xml, url_used, error). Does not fetch parties.xml."""
    base = (url or "").strip()
    if base.endswith(".xml"):
        candidates = [base]
    else:
        root = base.rstrip("/")
        candidates = [root + "/election.xml", root + "/"]
    last_err = None
    for u in candidates:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "NZ-Election-Sweepstakes/1.0 (private party; results read-only)"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if getattr(resp, "status", 200) != 200:
                    last_err = f"HTTP {resp.status} for {u}"
                    continue
                return resp.read(), u, None
        except Exception as e:
            last_err = f"{u}: {e}"
    return None, None, last_err


def fetch_feed(url: str) -> tuple[Optional[bytes], Optional[bytes], Optional[str], Optional[str]]:
    """Return (election_xml, parties_xml, url_used, error).

    parties.xml is included when election.xml identifies parties by p_no.
    FeedPoller.poll_once caches that file; this one-shot helper does not.
    """
    body, used, err = fetch_election(url)
    parties = None
    if body is not None and election_uses_pno(body):
        parties = fetch_url(parties_url_for(used or url))
    return body, parties, used, err


def fetch_url(url: str, timeout: int = 20) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "NZ-Election-Sweepstakes/1.0 (private party; results read-only)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return resp.read()
    except Exception:
        return None


def past_lock(conn) -> bool:
    """True once the configured lock instant has passed. Does not use the early-lock flag."""
    lock_at = D.get_setting(conn, "lock_at") or ""
    try:
        dt = datetime.fromisoformat(lock_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=D.NZ)
        return D.now_nz() >= dt
    except Exception:
        return False


def maybe_enable_results_feed(conn) -> bool:
    """Turn the results feed on at lock time unless an admin has switched it off."""
    if D.get_setting(conn, "feed_manual_off", "0") == "1":
        return False
    if D.get_setting(conn, "feed_enabled", "0") == "1":
        return False
    if not past_lock(conn):
        return False
    D.set_setting(conn, "feed_enabled", "1")
    D.set_setting(conn, "feed_poll_seconds", "180")
    D.audit(conn, "feed", "auto_enable", {"at": D.iso_now()})
    return True


def parse_election_xml(body: bytes, parties_body: Optional[bytes] = None, electorates_body: Optional[bytes] = None) -> dict:
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
    candidate_seats: dict[str, int] = {}
    vote_counts: dict[str, int] = {}
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
        raw = (
            attrs.get("party") or attrs.get("name") or attrs.get("partyname")
            or el.findtext("Party") or el.findtext("Name") or el.findtext("short_name")
            or ""
        ).strip()
        name = (pno_map.get(str(pno)) if pno else None) or canonical_party(raw) or raw
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
        cand = el.findtext("candidate_seats")
        if cand is not None and str(cand).strip() != "":
            try:
                candidate_seats[name] = int(float(str(cand).strip()))
            except ValueError:
                pass
        raw_votes = el.findtext("votes")
        if raw_votes is not None and str(raw_votes).strip() != "":
            try:
                vote_counts[name] = int(float(str(raw_votes).replace(",", "").strip()))
            except ValueError:
                pass

    electorates = parse_electorates(root, pno_map)
    if electorates_body:
        try:
            extra = parse_electorates(ET.fromstring(electorates_body), pno_map)
        except ET.ParseError:
            extra = []
        if extra:
            electorates = extra

    if not votes and not seats:
        return {"ok": False, "error": "XML parsed but no recognised party vote or seat figures", "votes": {}, "seats": {}}

    from scoring import derive_from_party_table
    # Full party table, including Opportunity/TOP, so parties-in-parliament
    # counts a party that has seats even though TOP has no vote-band question.
    derived = derive_from_party_table(votes, seats)
    values: dict[str, Any] = {}
    for party_name, pct in votes.items():
        key = PARTY_VOTE_KEYS.get(_norm(party_name))
        if key is not None and pct is not None:
            values[key] = pct
    for party_name, n in seats.items():
        key = PARTY_SEAT_KEYS.get(_norm(party_name))
        if key is not None and n is not None:
            values[key] = n
    for key in ("nat_vote", "lab_vote", "grn_vote", "act_vote", "nzf_vote", "tpm_vote"):
        if key not in values and derived.get(key) is not None:
            values[key] = derived[key]
    for key in ("most_seats", "parties_in", "bloc_61", "minor_highest"):
        if derived.get(key) is not None:
            values[key] = derived[key]
    return {
        "ok": True,
        "values": values,
        "votes": votes,
        "seats": seats,
        "candidate_seats": candidate_seats,
        "vote_counts": vote_counts,
        "electorates": electorates,
        "reporting_pct": reporting,
    }


_MAORI = {
    "te tai tokerau", "tamaki makaurau", "tāmaki makaurau", "hauraki-waikato",
    "waiariki", "ikaroa-rawhiti", "ikaroa-rāwhiti", "te tai hauauru", "te tai hauāuru",
    "te tai tonga",
}


def _region_for(name: str) -> str:
    key = _norm(name)
    if key in _MAORI or key.startswith("te tai ") or "māori" in (name or "").lower() or "maori" in key:
        return "Māori"
    return "General"


def parse_electorates(root: ET.Element, pno_map: dict[str, str]) -> list[dict]:
    """Leading candidate, party, margin and percent counted for each electorate."""
    found = []
    for el in root.iter():
        if el.tag.split("}")[-1].lower() != "electorate":
            continue
        name = (
            el.findtext("electorate_name")
            or el.findtext("name")
            or el.attrib.get("name")
            or el.attrib.get("electorate_name")
            or ""
        ).strip()
        if not name:
            continue
        counted = None
        for node in el.iter():
            if node.tag.split("}")[-1].lower() == "percent_voting_places_counted" and (node.text or "").strip():
                try:
                    counted = float(node.text.strip())
                except ValueError:
                    counted = None
        cands = []
        for node in el.iter():
            if node.tag.split("}")[-1].lower() != "candidate":
                continue
            if node.find("candidate") is not None and node is not el:
                pass
            cname = (node.findtext("candidate_name") or node.findtext("name") or node.attrib.get("name") or "").strip()
            party_raw = (node.findtext("party") or node.findtext("party_name") or node.attrib.get("party") or "").strip()
            pno = node.attrib.get("p_no") or node.attrib.get("pno")
            party = (pno_map.get(str(pno)) if pno else None) or canonical_party(party_raw) or party_raw
            votes_text = node.findtext("votes") or node.attrib.get("votes") or ""
            try:
                votes = int(float(str(votes_text).replace(",", "").strip() or "0"))
            except ValueError:
                votes = 0
            if cname:
                cands.append({"name": cname, "party": party, "votes": votes})
        cands.sort(key=lambda c: c["votes"], reverse=True)
        leader = cands[0] if cands else {"name": "", "party": "", "votes": 0}
        second = cands[1]["votes"] if len(cands) > 1 else 0
        found.append({
            "name": name,
            "region": _region_for(name),
            "leader": leader["name"],
            "party": leader["party"],
            "votes": leader["votes"],
            "margin": max(0, leader["votes"] - second) if cands else None,
            "counted": counted,
        })
    found.sort(key=lambda row: row["name"])
    return found


def electorate_is_close(margin, votes_cast) -> bool:
    """True when the lead is under 500 votes or under 2% of votes cast."""
    try:
        margin_n = int(margin)
    except (TypeError, ValueError):
        return False
    if margin_n < 0:
        return False
    try:
        cast_n = int(votes_cast or 0)
    except (TypeError, ValueError):
        cast_n = 0
    return margin_n < 500 or (cast_n > 0 and margin_n * 50 < cast_n)


def _name_part(part: str) -> str:
    part = (part or "").strip()
    letters = [ch for ch in part if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        return part.title()
    return part


def candidate_display_name(raw: str) -> str:
    """Turn 'SWARBRICK, Chlöe' into 'Chlöe Swarbrick'."""
    text = (raw or "").strip()
    if "," not in text:
        return _name_part(text)
    last, first = text.split(",", 1)
    flipped = f"{_name_part(first)} {_name_part(last)}".strip()
    return flipped or text


def _local_tag(el: ET.Element) -> str:
    return el.tag.split("}")[-1].lower()


def _int_text(text) -> int:
    try:
        return int(float(str(text or "0").replace(",", "").strip() or "0"))
    except (TypeError, ValueError):
        return 0


def parse_electorate_index(body: bytes) -> list[dict]:
    """electorates.xml: e_no and electorate_name. No vote figures."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    found = []
    for el in root.iter():
        if _local_tag(el) != "electorate":
            continue
        eno = (el.attrib.get("e_no") or el.attrib.get("eno") or el.findtext("e_no") or "").strip()
        name = (el.findtext("electorate_name") or el.findtext("name") or "").strip()
        if not eno or not name:
            continue
        found.append({"e_no": str(int(eno)) if eno.isdigit() else eno, "name": name})
    return found


def parse_candidate_directory(body: bytes) -> dict[str, dict]:
    """candidates.xml: c_no → name, electorate number, party number."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    out = {}
    for el in root.iter():
        if _local_tag(el) != "candidate":
            continue
        cno = (el.attrib.get("c_no") or el.attrib.get("cno") or "").strip()
        if not cno:
            continue
        out[str(cno)] = {
            "name": (el.findtext("candidate_name") or el.findtext("name") or "").strip(),
            "electorate": (el.findtext("electorate") or "").strip(),
            "party_no": (el.findtext("party") or "").strip(),
        }
    return out


def parse_electorate_votes(body: bytes) -> dict:
    """One eNN/eNN.xml file: percent counted, votes cast, candidate votes."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    counted = None
    counted_el = root.find(".//percent_voting_places_counted")
    if counted_el is not None and (counted_el.text or "").strip():
        try:
            counted = float(counted_el.text.strip())
        except ValueError:
            counted = None
    cast = _int_text(root.findtext(".//total_votes_cast"))
    by_cno = {}
    for el in root.iter():
        if _local_tag(el) != "candidate":
            continue
        cno = (el.attrib.get("c_no") or el.attrib.get("cno") or "").strip()
        if not cno:
            continue
        by_cno[str(cno)] = _int_text(el.findtext("votes"))
    return {"counted": counted, "votes_cast": cast, "by_cno": by_cno}


def build_electorate_rows(index_xml: bytes, candidates_xml: bytes, files: dict[str, bytes], pno_map: dict[str, str]) -> list[dict]:
    """Join the electorate list, candidate names, and per-electorate vote files.

    All-zero files are omitted. Missing files are omitted so the caller can
    keep the previous row for that electorate.
    """
    directory = parse_electorate_index(index_xml)
    candidates = parse_candidate_directory(candidates_xml)
    rows = []
    for item in directory:
        eno = str(item["e_no"])
        body = None
        for key in (eno, f"{int(eno):02d}" if eno.isdigit() else eno):
            if key in files:
                body = files[key]
                break
        if not body:
            continue
        parsed = parse_electorate_votes(body)
        if not parsed:
            continue
        tally = []
        for cno, votes in (parsed.get("by_cno") or {}).items():
            meta = candidates.get(str(cno)) or {}
            name = candidate_display_name(meta.get("name") or "")
            if not name:
                continue
            party_no = str(meta.get("party_no") or "")
            party = pno_map.get(party_no) or ""
            tally.append({"name": name, "party": party, "votes": int(votes or 0)})
        if not tally or sum(c["votes"] for c in tally) <= 0:
            continue
        tally.sort(key=lambda c: c["votes"], reverse=True)
        leader = tally[0]
        second = tally[1] if len(tally) > 1 else {"name": "", "party": "", "votes": 0}
        margin = max(0, leader["votes"] - second["votes"])
        cast = int(parsed.get("votes_cast") or 0) or sum(c["votes"] for c in tally)
        rows.append({
            "name": item["name"],
            "region": _region_for(item["name"]),
            "e_no": eno,
            "leader": leader["name"],
            "party": leader["party"],
            "votes": leader["votes"],
            "second": second["name"],
            "second_party": second["party"],
            "second_votes": second["votes"],
            "margin": margin,
            "counted": parsed.get("counted"),
            "votes_cast": cast,
            "close": electorate_is_close(margin, cast),
        })
    rows.sort(key=lambda row: row["name"])
    return rows


def apply_lead_changes(conn, rows: list) -> list[dict]:
    """Record a change when the leading candidate in an electorate changes.

    The first counted leader is stored and is not an event. All-zero rows
    are ignored. Times are New Zealand local time.
    """
    current = {}
    for row in conn.execute("SELECT electorate, leader, party FROM electorate_leaders").fetchall():
        current[row["electorate"]] = (row["leader"], row["party"] or "")
    events = []
    now = D.iso_now()
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("leader"):
            continue
        try:
            if int(row.get("votes") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        name = str(row["name"])
        leader = str(row["leader"])
        party = str(row.get("party") or "")
        prev = current.get(name)
        if prev and prev[0] and (prev[0] != leader or prev[1] != party):
            try:
                margin = int(row["margin"]) if row.get("margin") is not None else None
            except (TypeError, ValueError):
                margin = None
            if conn.backend == "sqlite":
                cur = conn.execute(
                    """INSERT INTO electorate_lead_events
                       (at, electorate, old_leader, old_party, new_leader, new_party, margin, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (now, name, prev[0], prev[1], leader, party, margin, now),
                )
                event_id = cur.lastrowid
            else:
                cur = conn.execute(
                    """INSERT INTO electorate_lead_events
                       (at, electorate, old_leader, old_party, new_leader, new_party, margin, created_at)
                       VALUES (?,?,?,?,?,?,?,?) RETURNING id""",
                    (now, name, prev[0], prev[1], leader, party, margin, now),
                )
                got = cur.fetchone()
                event_id = got["id"] if isinstance(got, dict) else got[0]
            events.append({
                "id": event_id,
                "at": now,
                "electorate": name,
                "old_leader": prev[0],
                "old_party": prev[1],
                "new_leader": leader,
                "new_party": party,
                "margin": margin,
            })
        if conn.backend == "sqlite":
            conn.execute(
                """INSERT INTO electorate_leaders(electorate, leader, party, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(electorate) DO UPDATE SET
                     leader=excluded.leader, party=excluded.party, updated_at=excluded.updated_at""",
                (name, leader, party, now),
            )
        else:
            conn.execute(
                """INSERT INTO electorate_leaders(electorate, leader, party, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(electorate) DO UPDATE SET
                     leader=EXCLUDED.leader, party=EXCLUDED.party, updated_at=EXCLUDED.updated_at""",
                (name, leader, party, now),
            )
        current[name] = (leader, party)
    return events


def merge_electorate_rows(previous: list, fresh: list) -> list[dict]:
    """Keep the last good row when this cycle's file was missing or all zeros."""
    by = {}
    for row in previous or []:
        if not isinstance(row, dict) or not row.get("name") or not row.get("leader"):
            continue
        try:
            if int(row.get("votes") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        by[row["name"]] = row
    for row in fresh or []:
        if isinstance(row, dict) and row.get("name"):
            by[row["name"]] = row
    return sorted(by.values(), key=lambda row: row["name"])


def fetch_electorate_files(base: str, e_numbers: list[str]) -> dict[str, bytes]:
    """Fetch eNN/eNN.xml together. Short timeouts; a miss returns no body."""
    numbers = []
    for eno in e_numbers:
        try:
            numbers.append(int(eno))
        except (TypeError, ValueError):
            continue

    def one(number: int):
        slug = f"e{number:02d}"
        url = base.rstrip("/") + f"/{slug}/{slug}.xml"
        return str(number), fetch_url(url, timeout=6)

    found: dict[str, bytes] = {}
    if not numbers:
        return found
    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(one, number) for number in numbers]
    try:
        for fut in as_completed(futures, timeout=20):
            try:
                eno, body = fut.result()
            except Exception:
                continue
            if body:
                found[eno] = body
    except TimeoutError:
        for fut in futures:
            fut.cancel()
    finally:
        # Don't hold the party-result write for a slow file. In-flight
        # requests time out on their own.
        pool.shutdown(wait=False, cancel_futures=True)
    return found


def load_electorate_rows(election_url: str, parties_body: Optional[bytes]) -> list[dict]:
    """Download the electorate list, candidates, and per-electorate files.

    Returns [] on failure so the caller leaves the last good snapshot alone.
    """
    base = election_url.rsplit("/", 1)[0] if (election_url or "").endswith(".xml") else (election_url or "").rstrip("/")
    if not base:
        return []
    index = fetch_url(base + "/electorates.xml", timeout=8)
    candidates = fetch_url(base + "/candidates.xml", timeout=8)
    if not index or not candidates:
        return []
    directory = parse_electorate_index(index)
    pno = dict(BUILTIN_PNO)
    if parties_body:
        pno.update(parties_from_xml(parties_body))
    files = fetch_electorate_files(base, [row["e_no"] for row in directory])
    if not files:
        return []
    return build_electorate_rows(index, candidates, files, pno)
