"""Scoring for NZ Election Sweepstakes 2026 — mostly multiple-choice bands."""
from __future__ import annotations
from typing import Any, Optional

# Band definitions: list of (label, lo_inclusive, hi_exclusive_or_None_for_open)
# Boundary values fall in the HIGHER band: lo is inclusive, previous band's top is exclusive.
# For open low: (-inf, first_lo); for open high: [last_lo, +inf)

BANDS = {
    "nat_vote": [
        ("<30", None, 30.0),
        ("30–33.9", 30.0, 34.0),
        ("34–37.9", 34.0, 38.0),
        ("38–41.9", 38.0, 42.0),
        ("42+", 42.0, None),
    ],
    "lab_vote": [
        ("<24", None, 24.0),
        ("24–27.9", 24.0, 28.0),
        ("28–31.9", 28.0, 32.0),
        ("32–35.9", 32.0, 36.0),
        ("36+", 36.0, None),
    ],
    "grn_vote": [
        ("<8", None, 8.0),
        ("8–10.9", 8.0, 11.0),
        ("11–13.9", 11.0, 14.0),
        ("14+", 14.0, None),
    ],
    "act_vote": [
        ("<6", None, 6.0),
        ("6–8.9", 6.0, 9.0),
        ("9–11.9", 9.0, 12.0),
        ("12+", 12.0, None),
    ],
    "nzf_vote": [
        ("under 5% (out)", None, 5.0),
        ("5–7.9", 5.0, 8.0),
        ("8–10.9", 8.0, 11.0),
        ("11+", 11.0, None),
    ],
    "tpm_vote": [
        ("<3", None, 3.0),
        ("3–4.9", 3.0, 5.0),
        ("5–6.9", 5.0, 7.0),
        ("7+", 7.0, None),
    ],
    "parties_in": [
        ("4", 4, 5),
        ("5", 5, 6),
        ("6", 6, 7),
        ("7", 7, 8),
        ("8+", 8, None),
    ],
}

MOST_SEATS_CHOICES = ["National", "Labour", "Other"]
MINOR_CHOICES = ["ACT", "Green", "NZ First", "Te Pāti Māori", "TOP"]
BLOC_CHOICES = ["Yes", "No"]


def band_index_for_value(bands: list[tuple], value: float) -> Optional[int]:
    """Return index of band containing value. Boundaries fall in higher band (lo inclusive)."""
    v = float(value)
    for i, (_label, lo, hi) in enumerate(bands):
        if lo is None and hi is not None:
            if v < hi:
                return i
        elif lo is not None and hi is None:
            if v >= lo:
                return i
        elif lo is not None and hi is not None:
            if v >= lo and v < hi:
                return i
    return None


def band_index_for_label(bands: list[tuple], label: str) -> Optional[int]:
    lab = str(label).strip()
    for i, (l, _lo, _hi) in enumerate(bands):
        if l == lab:
            return i
    # also allow matching without special dashes
    norm = lab.replace("–", "-").replace("—", "-")
    for i, (l, _lo, _hi) in enumerate(bands):
        if l.replace("–", "-").replace("—", "-") == norm:
            return i
    return None


def score_band(predicted_label: Any, actual_value: Any, band_key: str, exact: int = 10, adjacent: int = 4) -> Optional[int]:
    if actual_value is None or actual_value == "" or predicted_label is None or predicted_label == "":
        return None
    bands = BANDS[band_key]
    try:
        actual_f = float(actual_value)
    except (TypeError, ValueError):
        # actual might already be a band label
        ai = band_index_for_label(bands, actual_value)
        if ai is None:
            return None
    else:
        ai = band_index_for_value(bands, actual_f)
    pi = band_index_for_label(bands, predicted_label)
    if ai is None or pi is None:
        return 0
    if pi == ai:
        return exact
    if abs(pi - ai) == 1:
        return adjacent
    return 0


def score_parties_in_band(predicted_label: Any, actual_count: Any) -> Optional[int]:
    """4/5/6/7/8+ : exact 10, adjacent 5."""
    if actual_count is None or actual_count == "" or predicted_label is None or predicted_label == "":
        return None
    bands = BANDS["parties_in"]
    try:
        # If actual is numeric count
        n = int(round(float(actual_count)))
        # Map count to band: 4,5,6,7,8+
        if n <= 4:
            # "4" band is exactly 4 for scoring actual; if somehow <4, treat as 4 band
            ai = 0 if n == 4 else (0 if n < 4 else None)
            if n < 4:
                ai = 0  # still use lowest band
        elif n == 5:
            ai = 1
        elif n == 6:
            ai = 2
        elif n == 7:
            ai = 3
        else:
            ai = 4
    except (TypeError, ValueError):
        ai = band_index_for_label(bands, actual_count)
    pi = band_index_for_label(bands, predicted_label)
    if ai is None or pi is None:
        return 0
    if pi == ai:
        return 10
    if abs(pi - ai) == 1:
        return 5
    return 0


def score_exact_choice(predicted: Any, actual: Any, max_points: int = 10) -> Optional[int]:
    if actual is None or predicted is None or actual == "" or predicted == "":
        return None
    if str(predicted).strip().lower() == str(actual).strip().lower():
        return int(max_points)
    return 0


def actual_band_label(band_key: str, value: float) -> Optional[str]:
    bands = BANDS[band_key]
    idx = band_index_for_value(bands, float(value))
    if idx is None:
        return None
    return bands[idx][0]


def score_question(q: dict, predicted: Any, actual: Any) -> Optional[int]:
    qtype = q["scoring_type"]
    key = q.get("key") or ""
    max_pts = int(q.get("max_points", 10))
    if qtype == "vote_band_mc":
        return score_band(predicted, actual, key, exact=10, adjacent=4)
    if qtype == "parties_band_mc":
        return score_parties_in_band(predicted, actual)
    if qtype in ("exact_choice", "yes_no"):
        return score_exact_choice(predicted, actual, max_points=max_pts)
    return None


def score_player(questions: list[dict], predictions: dict[str, Any], results: dict[str, Any]) -> dict:
    detail = []
    total = 0
    available = 0
    pending_max = 0

    for q in questions:
        qid = str(q["id"])
        max_pts = int(q.get("max_points", 10))
        pred = predictions.get(qid)
        if pred is None and str(qid).isdigit():
            pred = predictions.get(int(qid))
        actual = results.get(qid)
        if actual is None and str(qid).isdigit():
            actual = results.get(int(qid))

        pts = score_question(q, pred, actual)
        if pts is None:
            detail.append({
                "question_id": qid, "points": None, "status": "pending",
                "max": max_pts, "predicted": pred, "actual": None,
            })
            pending_max += max_pts
        else:
            detail.append({
                "question_id": qid, "points": pts, "status": "scored",
                "max": max_pts, "predicted": pred, "actual": actual,
            })
            total += pts
            available += max_pts

    return {
        "total": total,
        "available": available,
        "pending_max": pending_max,
        "max_possible": available + pending_max,
        "detail": detail,
    }


def rank_players(scored: list[dict], national_actual: Optional[float] = None) -> list[dict]:
    """
    Tie-break: highest total, then closest National exact party-vote % guess
    (tb_national_vote), then equal placing with prizes split.
    scored items may include tb_national_vote (float|None).
    """
    def tb_err(s):
        guess = s.get("tb_national_vote")
        if national_actual is None or guess is None or guess == "":
            return float("inf")
        try:
            return abs(float(guess) - float(national_actual))
        except (TypeError, ValueError):
            return float("inf")

    ordered = sorted(
        scored,
        key=lambda s: (-s["total"], tb_err(s), str(s.get("name", ""))),
    )

    places = []
    i = 0
    n = len(ordered)
    while i < n:
        j = i + 1
        while j < n and ordered[j]["total"] == ordered[i]["total"] and tb_err(ordered[j]) == tb_err(ordered[i]):
            j += 1
        place = i + 1
        for _ in range(i, j):
            places.append(place)
        i = j

    from collections import defaultdict
    prize_for_place = {1: 100.0, 2: 20.0}
    groups = defaultdict(list)
    for idx, place in enumerate(places):
        groups[place].append(idx)
    prizes = [0.0] * n
    for place in sorted(groups.keys()):
        idxs = groups[place]
        span = list(range(place, place + len(idxs)))
        pot = sum(prize_for_place.get(p, 0.0) for p in span)
        share = pot / len(idxs) if idxs else 0.0
        for idx in idxs:
            prizes[idx] = share

    out = []
    for idx, s in enumerate(ordered):
        row = dict(s)
        row["rank"] = idx + 1
        row["place"] = places[idx]
        row["prize"] = prizes[idx]
        row["tb_error"] = None if tb_err(s) == float("inf") else tb_err(s)
        out.append(row)
    return out


def most_seats_winner(party_seats: dict[str, int], party_votes: dict[str, float]) -> Optional[str]:
    if not party_seats:
        return None
    max_seats = max(party_seats.values())
    tied = [p for p, s in party_seats.items() if s == max_seats]
    if len(tied) == 1:
        return tied[0]
    tied.sort(key=lambda p: (-float(party_votes.get(p, 0) or 0), p))
    return tied[0]


def derive_from_party_table(votes: dict[str, float], seats: dict[str, int]) -> dict:
    """
    votes/seats keys: National, Labour, Green, ACT, NZ First, Te Pāti Māori, TOP/Opportunity
    Returns raw numerics + derived MC answers for questions.
    """
    out: dict[str, Any] = {}
    # Store raw vote % for band resolution + TB
    for party, key in [
        ("National", "nat_vote"), ("Labour", "lab_vote"), ("Green", "grn_vote"),
        ("ACT", "act_vote"), ("NZ First", "nzf_vote"), ("Te Pāti Māori", "tpm_vote"),
        ("TOP", "top_vote"), ("Opportunity", "top_vote"),
    ]:
        if party in votes and votes[party] is not None:
            out[key] = float(votes[party])
            out[key + "_band"] = actual_band_label(key, float(votes[party])) if key in BANDS else None

    seat_parties = {p: int(s) for p, s in seats.items() if s is not None}
    if seat_parties:
        winner = most_seats_winner(seat_parties, votes)
        if winner in ("National", "Labour"):
            out["most_seats"] = winner
        elif winner:
            out["most_seats"] = "Other"
        out["parties_in"] = sum(1 for s in seat_parties.values() if int(s) >= 1)
        bloc = (
            int(seat_parties.get("National", 0) or 0)
            + int(seat_parties.get("ACT", 0) or 0)
            + int(seat_parties.get("NZ First", 0) or 0)
        )
        out["bloc_61"] = "Yes" if bloc >= 61 else "No"
        out["bloc_seats"] = bloc

    minors = ["ACT", "Green", "NZ First", "Te Pāti Māori", "TOP"]
    # Accept Opportunity as TOP
    vcopy = dict(votes)
    if "Opportunity" in vcopy and "TOP" not in vcopy:
        vcopy["TOP"] = vcopy["Opportunity"]
    present = {p: float(vcopy[p]) for p in minors if p in vcopy and vcopy[p] is not None}
    if present:
        best = max(present.values())
        winners = sorted([p for p, v in present.items() if v == best])
        out["minor_highest"] = winners[0]
    return out
