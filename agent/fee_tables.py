"""
agent/fee_tables.py
==================
Deterministic fee math — NO LLM. The source of truth is the `fee_schedules`
table (seeded in init.sql from Arvada's 2026 schedule). This module turns those
rows + a project valuation into an itemised fee estimate.

Table 18-1 (tiered building permit fee) semantics, verified against the seed
data and the golden query ($25,000 -> $401.75):

    fee = base + rate_per_1000 * (valuation - (tier_min - 1)) / 1000

where `base` is the fee at the floor of the tier and `rate_per_1000` accrues
per $1,000 above that floor (exact division keeps the tiers continuous).
"""
from __future__ import annotations

from typing import Any

# Canonical permit_type keys used in fee_schedules, plus common aliases the
# intent extractor might emit. Keys are matched case-insensitively.
PERMIT_TYPE_ALIASES: dict[str, str] = {
    "building": "building_permit",
    "building_permit": "building_permit",
    "remodel": "building_permit",
    "renovation": "building_permit",
    "addition": "building_permit",
    "solar": "building_permit_solar",
    "solar_permit": "building_permit_solar",
    "photovoltaic": "building_permit_solar",
    "pv": "building_permit_solar",
    "building_permit_solar": "building_permit_solar",
    "windows": "building_permit_windows_siding",
    "siding": "building_permit_windows_siding",
    "windows_siding": "building_permit_windows_siding",
    "building_permit_windows_siding": "building_permit_windows_siding",
    "food_truck": "food_truck_permit",
    "food_truck_permit": "food_truck_permit",
    "mobile_food": "food_truck_permit",
    "str": "str_permit",
    "str_permit": "str_permit",
    "short_term_rental": "str_permit",
    "short-term-rental": "str_permit",
    "special_event": "special_event_permit",
    "special_event_permit": "special_event_permit",
    "event": "special_event_permit",
    "retaining_wall": "retaining_wall",
    "retaining_wall_permit": "retaining_wall",
}

VALUATION_REQUIRED = {"building_permit"}

FEE_LABELS = {
    "permit_fee": "Permit fee",
    "plan_review": "Plan review fee",
    "use_tax": "Use tax (estimate)",
    "flat": "Fee",
    "per_trade": "Per-trade fee",
}

DISCLAIMER = (
    "This is an estimate based on the City of Arvada's published fee schedule. "
    "Final fees are confirmed by the City at plan review and may include additional "
    "trade, inspection, or impact fees."
)


def normalize_permit_type(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return PERMIT_TYPE_ALIASES.get(key, key)


def compute_table_18_1(tiers: list[dict], valuation: float) -> float:
    if valuation <= 0:
        return 0.0
    tier = None
    for t in tiers:
        mn = float(t["min"])
        mx = float(t["max"])
        if mn <= valuation <= mx:
            tier = t
            break
    if tier is None:
        # Above the top tier: use the last tier.
        tier = max(tiers, key=lambda t: float(t["max"]))
    base = float(tier["base"])
    rate = float(tier.get("per_1000_rate") or 0)
    floor = float(tier["min"]) - 1
    fee = base + rate * (valuation - floor) / 1000.0
    return round(fee, 2)


def compute_fees(
    permit_type: str,
    fee_rows: list[dict[str, Any]],
    valuation: float | None = None,
    units: float | None = None,
) -> dict[str, Any]:
    """Return an itemised fee estimate dict (see AGENT_STATE fee_result schema)."""
    line_items: list[dict[str, Any]] = []
    needs_valuation = False

    for row in fee_rows:
        method = row["calc_method"]
        fee_type = row["fee_type"]
        label = FEE_LABELS.get(fee_type, FEE_LABELS.get(method, fee_type))

        if method == "flat":
            amount = float(row["value"] or 0)
        elif method == "table_18_1":
            if valuation is None:
                needs_valuation = True
                continue
            amount = compute_table_18_1(row.get("table_data") or [], valuation)
        elif method == "percentage_of_valuation":
            if valuation is None:
                needs_valuation = True
                continue
            amount = round(float(row["value"] or 0) * valuation, 2)
        elif method == "per_unit":
            if units is None:
                continue
            amount = round(float(row["value"] or 0) * units, 2)
        else:
            continue

        line_items.append(
            {"label": label, "fee_type": fee_type, "method": method, "amount": round(amount, 2)}
        )

    total = round(sum(li["amount"] for li in line_items), 2)
    return {
        "permit_type": permit_type,
        "valuation": valuation,
        "line_items": line_items,
        "total": total if line_items else None,
        "needs_valuation": needs_valuation,
        "disclaimer": DISCLAIMER,
    }
