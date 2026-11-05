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
