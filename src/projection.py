"""Seat estimate. Not a result, and never written into the pot.

Sainte-Laguë follows the Electoral Act method: quotients of votes/(2n+1),
5% threshold or one electorate seat, 120 seats, then overhang is noted when
electorate wins exceed the entitlement. The 120-seat entitlement of the 2023
official party votes is National 48, Labour 34, Green 15, ACT 11, NZ First 8,
Te Pāti Māori 4. Te Pāti Māori won 6 electorates, so the official House was
122 before the Port Waikato by-election. That is what the unit test locks.

Special-vote defaults are the 2023 preliminary-to-official shift.
The preliminary count excluded 603,257 special votes (Electoral Commission,
via Wikipedia, "2023 New Zealand general election"). Official shares are the
archived result at
https://archive.electionresults.govt.nz/electionresults_2023/xml/election.xml
Preliminary shares are the ordinary-vote percentages at the end of election
night. The full shift is applied when the remaining special share equals
603,257 / 2,851,211. Any other share scales that shift in a straight line.
"""
from __future__ import annotations

import json
from typing import Optional

SPECIAL_VOTES_2023 = 603_257
VALID_PARTY_VOTES_2023 = 2_851_211
SPECIAL_FRACTION_2023 = SPECIAL_VOTES_2023 / VALID_PARTY_VOTES_2023

# Ordinary-vote percentages reported when the preliminary count closed,
# and the official shares from the archive XML above.
PRELIMINARY_2023 = {
    "National": 38.95,
    "Labour": 26.85,
    "Green": 10.77,
    "ACT": 8.96,
    "NZ First": 6.46,
    "Te Pāti Māori": 2.61,
    "TOP": 2.07,
}
OFFICIAL_PCT_2023 = {
    "National": 38.08,
    "Labour": 26.91,
    "Green": 11.60,
    "ACT": 8.64,
    "NZ First": 6.08,
    "Te Pāti Māori": 3.08,
    "TOP": 2.22,
}
SHIFT_2023 = {party: round(OFFICIAL_PCT_2023[party] - PRELIMINARY_2023[party], 2) for party in OFFICIAL_PCT_2023}

OFFICIAL_VOTES_2023 = {
    "National": 1_085_851,
    "Labour": 767_540,
    "Green": 330_907,
    "ACT": 246_473,
    "NZ First": 173_553,
    "Te Pāti Māori": 87_844,
    "TOP": 63_344,
}
OFFICIAL_ELECTORATES_2023 = {
    "National": 43,
    "Labour": 17,
    "Green": 3,
    "ACT": 2,
    "NZ First": 0,
    "Te Pāti Māori": 6,
    "TOP": 0,
}
# Official seat totals on 3 November 2023, before the Port Waikato by-election.
OFFICIAL_SEATS_2023 = {
    "National": 48,
    "Labour": 34,
    "Green": 15,
    "ACT": 11,
    "NZ First": 8,
    "Te Pāti Māori": 6,
    "TOP": 0,
}

TRACKED = ["National", "Labour", "Green", "ACT", "NZ First", "Te Pāti Māori", "TOP"]


def eligible(votes: dict[str, float], electorates: dict[str, int]) -> list[str]:
    total = sum(v for v in votes.values() if v and v > 0)
    if total <= 0:
        return []
    names = []
    for name, count in votes.items():
        share = (count or 0) / total
        if share >= 0.05 or int(electorates.get(name) or 0) > 0:
            names.append(name)
    return names


def sainte_lague(votes: dict[str, float], electorates: Optional[dict[str, int]] = None, seats: int = 120) -> dict:
    """Allocate `seats` by Sainte-Laguë, then note overhang.

    Returns entitlement (the 120), final (electorate wins kept if higher),
    and overhang counts.
    """
    electorates = electorates or {}
    clean = {name: float(count or 0) for name, count in votes.items() if count and float(count) > 0}
    names = eligible(clean, electorates)
    entitlement = {name: 0 for name in names}
    for _ in range(seats):
        if not names:
            break
        winner = max(names, key=lambda name: clean[name] / (2 * entitlement[name] + 1))
        entitlement[winner] += 1
    final = {}
    overhang = {}
    for name in names:
        won = int(electorates.get(name) or 0)
        got = entitlement[name]
        if won > got:
            overhang[name] = won - got
            final[name] = won
        else:
            final[name] = got
    for name in votes:
        entitlement.setdefault(name, 0)
        final.setdefault(name, 0)
    return {
        "entitlement": entitlement,
        "seats": final,
        "overhang": overhang,
        "eligible": names,
        "house": sum(final.values()),
    }


def default_assumptions() -> dict:
    return {
        "special_percent": round(SPECIAL_FRACTION_2023 * 100, 2),
        "shifts": dict(SHIFT_2023),
        "electorates": {party: 0 for party in TRACKED},
        "source": "2023 preliminary ordinary count vs official election.xml; 603,257 special votes",
    }


def project_shares(current: dict[str, float], special_percent: float, shifts: dict[str, float]) -> dict[str, float]:
    """Shift current percentages. special_percent is remaining specials as % of the final total."""
    fraction = max(0.0, float(special_percent)) / 100.0
    scale = 0.0 if SPECIAL_FRACTION_2023 == 0 else fraction / SPECIAL_FRACTION_2023
    raw = {}
    for party, pct in current.items():
        raw[party] = float(pct) + float(shifts.get(party) or 0) * scale
        if raw[party] < 0:
            raw[party] = 0.0
    base = sum(float(v) for v in current.values())
    got = sum(raw.values())
    if got <= 0 or base <= 0:
        return raw
    factor = base / got
    return {party: round(value * factor, 2) for party, value in raw.items()}


def project_seats(current_pct: dict[str, float], assumptions: dict) -> dict:
    shares = project_shares(
        current_pct,
        assumptions.get("special_percent") or 0,
        assumptions.get("shifts") or {},
    )
    votes = {party: pct * 1000 for party, pct in shares.items()}
    electorates = {party: int(assumptions.get("electorates", {}).get(party) or 0) for party in shares}
    allocated = sainte_lague(votes, electorates, seats=120)
    return {"shares": shares, "electorates": electorates, **allocated}


# Sitting Parliament after the 25 November 2023 Port Waikato by-election.
# Official 3 Nov 2023 result (elections.nz): National 48, Labour 34, Green 15,
# ACT 11, NZ First 8, Te Pāti Māori 6, House 122, TPM overhang of 2.
# Port Waikato added one National seat (House 123, majority 62).
PARLIAMENT_2023 = {
    "National": 49,
    "Labour": 34,
    "Green": 15,
    "ACT": 11,
    "NZ First": 8,
    "Te Pāti Māori": 6,
}
PARLIAMENT_2023_SOURCE = "https://elections.nz/media-and-news/2023/official-results-for-the-2023-general-election"
PARLIAMENT_2023_NOTE = (
    "Official results on 3 November 2023 were a 122-seat House "
    "(National 48, Labour 34, Green 15, ACT 11, NZ First 8, Te Pāti Māori 6, "
    "two-seat Te Pāti Māori overhang). The 25 November 2023 Port Waikato by-election "
    "added one National electorate seat, so the sitting House is 123 and a majority is 62."
)
# Left to right on the hemicycle. TOP sits between the blocs, so its seats
# are the last ones before the right bloc and fall just left of the majority
# line whenever Labour + Green + Te Pāti Māori + TOP is short of that line.
HOUSE_ORDER = ["Green", "Te Pāti Māori", "Labour", "TOP", "NZ First", "National", "ACT"]
# Pre-count party table and coalition search. Not the hemicycle.
STABLE_PARTY_ORDER = ("Green", "Te Pāti Māori", "Labour", "NZ First", "National", "ACT", "TOP")
LEFT_BLOC = ("Labour", "Green", "Te Pāti Māori")
RIGHT_BLOC = ("National", "ACT", "NZ First")
# Shown under the two committed blocs. Not a scored coalition.
SWING_LEFT = ("Labour", "Green", "Te Pāti Māori", "TOP")


def majority_line(house: int) -> int:
    return int(house) // 2 + 1


# The blocs people actually talk about, shown even when they are short.
# Any other minimal combination that reaches the majority line is added after these.
NAMED_BLOCS = (
    ("National", "ACT", "NZ First"),
    ("National", "ACT"),
    ("National", "NZ First"),
    ("Labour", "Green", "Te Pāti Māori"),
    ("Labour", "Green"),
)


def coalition_options(seats: dict, majority: int) -> list[dict]:
    """Named blocs, plus any other minimal combo that clears the majority line."""
    clean = {}
    for name, value in (seats or {}).items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            clean[str(name)] = count
    parties = [name for name in STABLE_PARTY_ORDER if clean.get(name)]
    for name in clean:
        if name not in parties:
            parties.append(name)

    def pack(names: tuple) -> dict:
        total = sum(clean.get(name, 0) for name in names)
        short = max(0, int(majority) - total)
        return {
            "names": list(names),
            "seats": total,
            "short": short,
            "majority": short == 0 and total > 0,
        }

    named = [pack(names) for names in NAMED_BLOCS]
    seen = {frozenset(row["names"]) for row in named}
    extras = []
    from itertools import combinations
    for size in range(2, min(4, len(parties)) + 1):
        for combo in combinations(parties, size):
            key = frozenset(combo)
            if key in seen:
                continue
            total = sum(clean.get(name, 0) for name in combo)
            if total < int(majority):
                continue
            smaller = False
            for drop in combo:
                rest = [name for name in combo if name != drop]
                if rest and sum(clean.get(name, 0) for name in rest) >= int(majority):
                    smaller = True
                    break
            if smaller:
                continue
            extras.append(pack(combo))
            seen.add(key)
    extras.sort(key=lambda row: (-row["seats"], row["names"]))
    return named + extras


def _bloc(seats: dict, names: tuple, majority: int) -> dict:
    total = sum(int(seats.get(name) or 0) for name in names)
    short = max(0, majority - total)
    return {"names": list(names), "seats": total, "short": short, "majority": short == 0}


def pack_house(seats: dict, label: str, source: str = "", note: str = "") -> dict:
    clean = {}
    for name, value in (seats or {}).items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            clean[str(name)] = count
    house = sum(clean.values())
    majority = majority_line(house) if house else 61
    order = list(HOUSE_ORDER)
    for name in sorted(clean, key=lambda n: (-clean[n], n)):
        if name not in order:
            order.append(name)
    return {
        "label": label,
        "seats": clean,
        "house": house or 120,
        "majority": majority if house else 61,
        "left": _bloc(clean, LEFT_BLOC, majority if house else 61),
        "right": _bloc(clean, RIGHT_BLOC, majority if house else 61),
        "swing_left": _bloc(clean, SWING_LEFT, majority if house else 61),
        "source": source,
        "note": note,
        "order": order,
    }


def current_parliament(raw: str | None) -> dict:
    seats = dict(PARLIAMENT_2023)
    source = PARLIAMENT_2023_SOURCE
    note = PARLIAMENT_2023_NOTE
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            blob = parsed.get("seats") if isinstance(parsed.get("seats"), dict) else parsed
            picked = {}
            for name, value in blob.items():
                if name in ("source", "note", "seats"):
                    continue
                try:
                    picked[name] = int(value)
                except (TypeError, ValueError):
                    continue
            if picked:
                seats = picked
            source = parsed.get("source") or source
            note = parsed.get("note") or note
    return pack_house(seats, "Current Parliament (2023 election)", source, note)


def project_live(votes: dict, candidate_seats: dict | None) -> dict | None:
    """Sainte-Laguë on the provisional party vote. Electorate wins drive overhang."""
    clean = {}
    for name, value in (votes or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            clean[name] = number
    if not clean:
        return None
    electorates = {}
    for name, value in (candidate_seats or {}).items():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            electorates[name] = count
    allocated = sainte_lague(clean, electorates, seats=120)
    packed = pack_house(allocated["seats"], "Projected 2026 seats (provisional count)")
    packed["overhang"] = allocated["overhang"]
    packed["entitlement"] = allocated["entitlement"]
    return packed


def poll_projection(raw: str | None) -> dict | None:
    if not raw or not str(raw).strip():
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    parties = parsed.get("parties") if isinstance(parsed, dict) else None
    if not isinstance(parties, dict):
        return None
    votes = {}
    for name, value in parties.items():
        if value in (None, ""):
            continue
        try:
            votes[name] = float(value)
        except (TypeError, ValueError):
            continue
    if not votes:
        return None
    packed = project_live(votes, {})
    if not packed:
        return None
    packed["label"] = "Projected from latest poll"
    packed["pollster"] = str(parsed.get("pollster") or "")
    packed["as_of"] = str(parsed.get("date") or "")
    return packed
