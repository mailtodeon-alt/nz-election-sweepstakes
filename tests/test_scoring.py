import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from scoring import (
    score_band, score_parties_in_band, score_exact_choice, score_player,
    rank_players, band_index_for_value, actual_band_label, derive_from_party_table, BANDS,
)

def test_boundaries_fall_in_higher_band():
    # 30.0 is start of 30-33.9, not <30
    assert actual_band_label("nat_vote", 29.9) == "<30"
    assert actual_band_label("nat_vote", 30.0) == "30–33.9"
    assert actual_band_label("nat_vote", 33.9) == "30–33.9"
    assert actual_band_label("nat_vote", 34.0) == "34–37.9"
    assert actual_band_label("nat_vote", 42.0) == "42+"
    assert actual_band_label("nzf_vote", 4.9) == "under 5% (out)"
    assert actual_band_label("nzf_vote", 5.0) == "5–7.9"

def test_band_scoring_adjacent():
    assert score_band("34–37.9", 35.2, "nat_vote") == 10
    assert score_band("30–33.9", 35.2, "nat_vote") == 4  # adjacent
    assert score_band("<30", 35.2, "nat_vote") == 0
    assert score_band("34–37.9", None, "nat_vote") is None

def test_parties_in():
    assert score_parties_in_band("6", 6) == 10
    assert score_parties_in_band("5", 6) == 5
    assert score_parties_in_band("4", 6) == 0
    assert score_parties_in_band("8+", 9) == 10
    assert score_parties_in_band("8+", 8) == 10

def test_yes_no_and_most():
    assert score_exact_choice("Yes", "Yes", 15) == 15
    assert score_exact_choice("No", "Yes", 15) == 0
    assert score_exact_choice("National", "National", 5) == 5

def test_score_player_pending():
    qs = [
        {"id": 1, "key": "nat_vote", "scoring_type": "vote_band_mc", "max_points": 10},
        {"id": 2, "key": "bloc_61", "scoring_type": "yes_no", "max_points": 15},
    ]
    s = score_player(qs, {"1": "34–37.9", "2": "Yes"}, {"1": "35.1"})
    assert s["total"] == 10
    assert s["pending_max"] == 15
    assert s["detail"][1]["status"] == "pending"

def test_tiebreak_national_exact():
    scored = [
        {"player_id": 1, "name": "A", "total": 80, "tb_national_vote": "35.0"},
        {"player_id": 2, "name": "B", "total": 80, "tb_national_vote": "35.4"},
        {"player_id": 3, "name": "C", "total": 70, "tb_national_vote": "40.0"},
    ]
    ranked = rank_players(scored, national_actual=35.5)
    assert ranked[0]["name"] == "B"  # closer to 35.5
    assert ranked[0]["prize"] == 100
    assert ranked[1]["name"] == "A"
    assert ranked[1]["prize"] == 20

def test_equal_place_splits():
    scored = [
        {"player_id": 1, "name": "A", "total": 90, "tb_national_vote": "35.0"},
        {"player_id": 2, "name": "B", "total": 90, "tb_national_vote": "35.0"},
        {"player_id": 3, "name": "C", "total": 10, "tb_national_vote": "20"},
    ]
    ranked = rank_players(scored, national_actual=35.0)
    assert ranked[0]["place"] == 1 and ranked[1]["place"] == 1
    assert ranked[0]["prize"] == 60.0

def test_derive():
    d = derive_from_party_table(
        {"National": 35.1, "Labour": 29.0, "Green": 11.2, "ACT": 8.5, "NZ First": 7.8, "Te Pāti Māori": 3.2, "TOP": 2.1},
        {"National": 46, "Labour": 34, "Green": 14, "ACT": 11, "NZ First": 8, "Te Pāti Māori": 4, "TOP": 0},
    )
    assert d["most_seats"] == "National"
    assert d["parties_in"] == 6  # TOP 0 seats
    assert d["bloc_61"] == "Yes"  # 46+11+8=65
    assert d["minor_highest"] == "Green"

def test_minor_highest_counts_top():
    """TOP is a ballot choice. A higher Opportunity Party share still scores as TOP."""
    d = derive_from_party_table(
        {"National": 40.0, "Labour": 28.0, "Green": 4.0, "ACT": 3.5, "NZ First": 3.0, "Te Pāti Māori": 2.0, "TOP": 5.59},
        {"National": 48, "Labour": 34, "Green": 10, "ACT": 8, "NZ First": 7, "Te Pāti Māori": 4, "TOP": 7},
    )
    assert d["minor_highest"] == "TOP"
    assert d["bloc_61"] == "Yes"  # 48+8+7
    assert d["parties_in"] == 7
    alias = derive_from_party_table(
        {"Green": 4.0, "ACT": 3.0, "NZ First": 2.0, "Te Pāti Māori": 1.0, "Opportunity": 5.59},
        {"Green": 5, "ACT": 4, "NZ First": 3, "Te Pāti Māori": 2, "Opportunity": 7},
    )
    assert alias["minor_highest"] == "TOP"

if __name__ == "__main__":
    test_boundaries_fall_in_higher_band()
    test_band_scoring_adjacent()
    test_parties_in()
    test_yes_no_and_most()
    test_score_player_pending()
    test_tiebreak_national_exact()
    test_equal_place_splits()
    test_derive()
    test_minor_highest_counts_top()
    print("ALL SCORING TESTS PASSED")
