"""Deterministic fee math — pure, no network."""
from agent.fee_tables import compute_table_18_1, compute_fees, normalize_permit_type

TIERS = [
    {"min": 1, "max": 500, "base": 34.0, "per_1000_rate": 0},
    {"min": 501, "max": 2000, "base": 34.0, "per_1000_rate": 30.50},
    {"min": 2001, "max": 25000, "base": 79.75, "per_1000_rate": 14.0},
    {"min": 25001, "max": 50000, "base": 401.75, "per_1000_rate": 10.10},
    {"min": 50001, "max": 100000, "base": 654.25, "per_1000_rate": 7.0},
]


def test_table_18_1_tier_boundaries_are_continuous():
    assert compute_table_18_1(TIERS, 500) == 34.0
    assert compute_table_18_1(TIERS, 2000) == 79.75
    assert compute_table_18_1(TIERS, 25000) == 401.75
    assert compute_table_18_1(TIERS, 50000) == 654.25


def test_table_18_1_above_top_tier_uses_last_tier():
    # Should not crash and should be >= top tier base.
    assert compute_table_18_1(TIERS, 250000) >= 654.25


def test_building_permit_total_matches_spec():
    rows = [
        {"fee_type": "permit_fee", "calc_method": "table_18_1", "value": None, "table_data": TIERS},
        {"fee_type": "plan_review", "calc_method": "flat", "value": 32.5, "table_data": None},
        {"fee_type": "use_tax", "calc_method": "percentage_of_valuation", "value": 0.0346, "table_data": None},
    ]
    r = compute_fees("building_permit", rows, valuation=25000)
    assert r["total"] == 1299.25
    assert r["needs_valuation"] is False
    labels = {li["fee_type"]: li["amount"] for li in r["line_items"]}
    assert labels["permit_fee"] == 401.75
    assert labels["use_tax"] == 865.0


def test_missing_valuation_flags_needs_valuation():
    rows = [{"fee_type": "permit_fee", "calc_method": "table_18_1", "value": None, "table_data": TIERS}]
    r = compute_fees("building_permit", rows, valuation=None)
    assert r["needs_valuation"] is True
    assert r["total"] is None


def test_flat_fee_only():
    rows = [{"fee_type": "permit_fee", "calc_method": "flat", "value": 60.0, "table_data": None}]
    r = compute_fees("food_truck_permit", rows)
    assert r["total"] == 60.0


def test_normalize_permit_type_aliases():
    assert normalize_permit_type("Solar") == "building_permit_solar"
    assert normalize_permit_type("short term rental") == "str_permit"
    assert normalize_permit_type("remodel") == "building_permit"
    assert normalize_permit_type(None) is None
