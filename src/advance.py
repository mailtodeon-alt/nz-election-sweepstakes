"""Advance-voting totals from the Electoral Commission.

The 2023 statistics page is a daily table with a year column and a totals row
(https://elections.nz/democracy-in-nz/historical-events/2023-general-election/voting-statistics).
2026 figures are published the same way. We try the likely pages; a bot wall,
a 404, or a table we cannot read leaves the last good settings value in place.
"""
from __future__ import annotations

import re
import urllib.request
from html.parser import HTMLParser

UA = "NZ-Election-Sweepstakes/1.0 (private party; advance-voting read-only)"
ADVANCE_URLS = (
    "https://elections.nz/2026-general-election/voting-statistics/",
    "https://elections.nz/stats-and-research/2026-general-election/advance-voting-statistics/",
    "https://vote.nz/voting-statistics/",
)
SOURCE_LINK = "https://elections.nz"

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _number(text: str) -> int | None:
    raw = (text or "").strip().lower()
    if raw in ("", "-", "–", "—", "closed", "n/a", "na"):
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    if not digits:
        return None
    return int(digits)


def _iso_date(text: str, year: int = 2026) -> str | None:
    raw = (text or "").strip()
    iso = re.search(r"(20\d{2})-(\d{2})-(\d{2})", raw)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(20\d{2}))?", raw)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower()[:3]) or _MONTHS.get(m.group(2).lower())
    if not month:
        return None
    y = int(m.group(3)) if m.group(3) else year
    return f"{y:04d}-{month:02d}-{int(m.group(1)):02d}"


def _blocked(body: bytes) -> bool:
    head = body[:800].lower()
    return b"incapsula" in head or b"_incapsula_" in head or (
        b"noindex,nofollow" in head and b"<table" not in body.lower()
    )


def parse_advance_html(body: bytes) -> dict:
    """Return {ok, votes, as_of, error}. Never invents a total."""
    if not body:
        return {"ok": False, "error": "empty body"}
    if _blocked(body):
        return {"ok": False, "error": "elections.nz bot wall; kept last figure"}
    text = body.decode("utf-8", errors="replace")
    parser = _Tables()
    try:
        parser.feed(text)
    except Exception as exc:
        return {"ok": False, "error": f"html parse: {exc}"[:200]}
    for table in parser.tables:
        found = _from_table(table)
        if found:
            return found
    # A single sentence: "cumulative ... 1,074,416" and a date.
    flat = re.sub(r"<[^>]+>", " ", text)
    flat = re.sub(r"\s+", " ", flat)
    cum = re.search(
        r"(?:cumulative|running total|total(?:\s+advance)?(?:\s+votes)?)[^\d]{0,40}([\d,]{5,})",
        flat,
        re.I,
    )
    if cum:
        votes = _number(cum.group(1))
        when = _iso_date(flat)
        if votes and 1000 <= votes <= 5_000_000:
            return {"ok": True, "votes": votes, "as_of": when or ""}
    return {"ok": False, "error": "no advance-voting total in page"}


def _from_table(table: list[list[str]]) -> dict | None:
    header_i = None
    col_2026 = None
    for i, row in enumerate(table):
        for j, cell in enumerate(row):
            if re.search(r"\b2026\b", cell):
                header_i = i
                col_2026 = j
                break
        if header_i is not None:
            break
    if header_i is None or col_2026 is None:
        return None
    total = None
    last_date = None
    daily = 0
    for row in table[header_i + 1:]:
        if col_2026 >= len(row):
            continue
        label = row[0].strip().lower() if row else ""
        value = _number(row[col_2026])
        if value is None:
            continue
        if label in ("total", "totals", "cumulative"):
            total = value
            continue
        when = _iso_date(row[0])
        if when:
            last_date = when
        daily += value
    votes = total if total is not None else (daily or None)
    if votes is None or not (1000 <= votes <= 5_000_000):
        return None
    return {"ok": True, "votes": votes, "as_of": last_date or ""}


def fetch_advance() -> dict:
    """Try each published page. The first parseable total wins."""
    last = "no response"
    for url in ADVANCE_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if getattr(resp, "status", 200) != 200:
                    last = f"HTTP {resp.status} {url}"
                    continue
                parsed = parse_advance_html(resp.read())
        except Exception as exc:
            last = f"{url}: {exc}"
            continue
        if parsed.get("ok"):
            parsed["url"] = url
            return parsed
        last = parsed.get("error") or last
    return {"ok": False, "error": str(last)[:300]}
