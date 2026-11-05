"""Public RSS/Atom headlines. Titles and short summaries only — never article bodies.

Verified 5 Nov 2026 (HTTP 200, real items):
- RNZ Politics: https://www.rnz.co.nz/rss/political.xml
- 1News politics: https://www.1news.co.nz/arc/outboundfeeds/rss/category/politics/?outputType=xml
- NZ Herald politics: https://www.nzherald.co.nz/arc/outboundfeeds/rss/section/nz/politics/?outputType=xml
- Stuff politics (Atom): https://www.stuff.co.nz/rss?section=/politics

Skipped: The Spinoff feed URLs 404; RNZ /rss/election.xml is a retired stub.
"""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA = "NZ-Election-Sweepstakes/1.0 (private party; headlines only)"
FEEDS = (
    ("RNZ", "https://www.rnz.co.nz/rss/political.xml"),
    ("1News", "https://www.1news.co.nz/arc/outboundfeeds/rss/category/politics/?outputType=xml"),
    ("NZ Herald", "https://www.nzherald.co.nz/arc/outboundfeeds/rss/section/nz/politics/?outputType=xml"),
    ("Stuff", "https://www.stuff.co.nz/rss?section=/politics"),
)


def _local(tag: str) -> str:
    return tag.split("}")[-1].lower()


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _strip(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def _when(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    if re.match(r"20\d{2}-\d{2}-\d{2}", raw):
        return raw
    return ""


def _link(el: ET.Element) -> str:
    link = el.find("link")
    if link is not None:
        href = link.attrib.get("href") or (link.text or "")
        if href.strip():
            return href.strip()
    for child in el:
        if _local(child.tag) == "link":
            href = child.attrib.get("href") or (child.text or "")
            if href.strip() and child.attrib.get("rel", "alternate") in ("alternate", ""):
                return href.strip()
    return ""


def parse_feed(body: bytes, source: str) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = ""
        summary = ""
        published = ""
        url = _link(el)
        for child in el:
            name = _local(child.tag)
            if name == "title" and not title:
                title = _strip(_text(child))
            elif name in ("description", "summary") and not summary:
                summary = _strip(_text(child))[:280]
            elif name in ("pubdate", "published", "updated", "date") and not published:
                published = _when(_text(child))
            elif name == "link" and not url:
                url = (child.attrib.get("href") or _text(child)).strip()
        if not title or not url:
            continue
        if "feed is no longer" in title.lower() or "this feed is no longer" in title.lower():
            continue
        items.append({
            "title": title[:220],
            "summary": summary,
            "url": url,
            "source": source,
            "published": published,
        })
    return items


def canonical_url(url: str) -> str:
    base = (url or "").split("#")[0].strip()
    if "?" in base:
        path, query = base.split("?", 1)
        kept = [part for part in query.split("&") if part and not part.lower().startswith("utm_")]
        base = path if not kept else path + "?" + "&".join(kept)
    return base.rstrip("/")


def merge_items(existing: list, fresh: list, limit: int = 40) -> list[dict]:
    by_url: dict[str, dict] = {}
    for item in list(existing or []) + list(fresh or []):
        url = canonical_url(item.get("url") or "")
        title = (item.get("title") or "").strip()
        if not url or not title:
            continue
        if "feed is no longer" in title.lower():
            continue
        row = {
            "title": title[:220],
            "summary": _strip(item.get("summary") or "")[:280],
            "url": item.get("url") or url,
            "source": item.get("source") or "",
            "published": item.get("published") or "",
        }
        prev = by_url.get(url)
        if prev is None or (row["published"] or "") >= (prev.get("published") or ""):
            by_url[url] = row
    rows = list(by_url.values())
    rows.sort(key=lambda r: r.get("published") or "", reverse=True)
    return rows[:limit]


def fetch_news() -> dict:
    """Fetch every feed. A failed source is skipped; others are kept."""
    fresh: list[dict] = []
    sources = []
    for name, url in FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                if getattr(resp, "status", 200) != 200:
                    sources.append({"source": name, "ok": False, "error": f"HTTP {resp.status}"})
                    continue
                items = parse_feed(resp.read(), name)
        except Exception as exc:
            sources.append({"source": name, "ok": False, "error": str(exc)[:180]})
            continue
        if not items:
            sources.append({"source": name, "ok": False, "error": "no items"})
            continue
        fresh.extend(items)
        sources.append({"source": name, "ok": True, "count": len(items)})
    return {"items": fresh, "sources": sources, "ok": any(s.get("ok") for s in sources)}
