"""
agent/project_planner.py
========================
Deterministic multi-project sequencing — NO LLM. Mirrors fee_tables.py: a pure,
testable module that turns "I want to add a deck AND convert my garage AND put up
solar" into an authoritative plan the generator only has to narrate.

Given several projects, it works out:
  - the permit each needs (-> fee_schedules + KB, via the canonical permit_type)
  - the order they must happen in (phase + explicit dependencies)
  - which can run concurrently (same phase, no dependency between them)
  - shared inspections (so the resident schedules one visit, not three)
  - a critical-path review-time estimate

The fee numbers themselves are still computed by the fee engine / fee_tables from
the live schedule; this module only decides structure and sequencing. Keeping the
domain rules here (not in a prompt) means they're reviewable and unit-testable.
"""
from __future__ import annotations

from typing import Any

from agent.fee_tables import normalize_permit_type

# Coarse sequencing phases. Lower runs first; same number can run concurrently
# unless an explicit dependency says otherwise.
PHASE_ZONING = 1      # setbacks / planning / HOA / land-use review
PHASE_STRUCTURE = 2   # the building shell: decks, additions, conversions, walls
PHASE_SYSTEMS = 3     # trades mounted on the structure: solar, re-roof-then-solar
PHASE_FINISH = 4      # cosmetic / envelope: windows, siding

PHASE_LABELS = {
    PHASE_ZONING: "Planning & zoning review",
    PHASE_STRUCTURE: "Structural permits & build",
    PHASE_SYSTEMS: "Systems & trades",
    PHASE_FINISH: "Envelope & finish",
}


# Catalog of common residential projects. `key` is what we resolve user phrases to.
#   permit_type        -> canonical fee/KB key (None = no separate permit / advisory)
#   phase              -> default sequencing bucket
#   depends_on         -> project keys that must FINISH (or be permitted) first
#   needs_planning     -> triggers a shared upfront zoning-review phase
#   inspections        -> inspection names (used to find shared visits)
#   review_days        -> (min, max) typical plan-review turnaround
_CATALOG: dict[str, dict[str, Any]] = {
    "deck": {
        "label": "Deck",
        "permit_type": "building_permit",
        "phase": PHASE_STRUCTURE,
        "depends_on": [],
        "needs_planning": True,   # setback / size checks
        "inspections": ["footing", "framing", "final"],
        "review_days": (5, 10),
    },
    "addition": {
        "label": "Room addition",
        "permit_type": "building_permit",
        "phase": PHASE_STRUCTURE,
        "depends_on": [],
        "needs_planning": True,
        "inspections": ["footing", "framing", "rough_electrical", "insulation", "final"],
        "review_days": (10, 20),
    },
    "garage_conversion": {
        "label": "Garage conversion",
        "permit_type": "building_permit",
        "phase": PHASE_STRUCTURE,
        "depends_on": [],
        "needs_planning": True,   # change-of-use / egress / parking
        "inspections": ["framing", "rough_electrical", "rough_mechanical", "insulation", "final"],
        "review_days": (10, 20),
    },
    "adu": {
        "label": "Accessory dwelling unit (ADU)",
        "permit_type": "building_permit",
        "phase": PHASE_STRUCTURE,
        "depends_on": [],
        "needs_planning": True,
        "inspections": ["footing", "framing", "rough_electrical", "rough_plumbing", "insulation", "final"],
        "review_days": (15, 30),
    },
    "retaining_wall": {
        "label": "Retaining wall",
        "permit_type": "retaining_wall",
        "phase": PHASE_STRUCTURE,
        "depends_on": [],
        "needs_planning": True,   # height / drainage / setback
        "inspections": ["footing", "final"],
        "review_days": (5, 10),
    },
    "reroof": {
        "label": "Re-roof",
        "permit_type": "building_permit",
        "phase": PHASE_STRUCTURE,
        "depends_on": [],
        "needs_planning": False,
        "inspections": ["in_progress", "final"],
        "review_days": (1, 5),
    },
    "solar": {
        "label": "Rooftop solar",
        "permit_type": "building_permit_solar",
        "phase": PHASE_SYSTEMS,
        # A new roof must be on before panels are mounted; planner enforces this
        # only when the resident is ALSO doing a re-roof.
        "depends_on": ["reroof"],
        "needs_planning": False,
        "inspections": ["mounting", "electrical_final", "final"],
        "review_days": (5, 10),
    },
    "windows_siding": {
        "label": "Windows / siding",
        "permit_type": "building_permit_windows_siding",
        "phase": PHASE_FINISH,
        "depends_on": [],
        "needs_planning": False,
        "inspections": ["final"],
        "review_days": (1, 5),
    },
    "fence": {
        "label": "Fence",
        "permit_type": None,   # zoning clearance, not a building permit
        "phase": PHASE_ZONING,
        "depends_on": [],
        "needs_planning": True,
        "inspections": [],
        "review_days": (1, 5),
    },
}

# Map user phrases / canonical permit_types onto catalog keys.
_PROJECT_ALIASES: dict[str, str] = {
    "deck": "deck",
    "patio_cover": "deck",
    "addition": "addition",
    "room_addition": "addition",
    "home_addition": "addition",
    "garage_conversion": "garage_conversion",
    "garage": "garage_conversion",
    "convert_garage": "garage_conversion",
    "adu": "adu",
    "accessory_dwelling_unit": "adu",
    "mother_in_law": "adu",
    "retaining_wall": "retaining_wall",
    "reroof": "reroof",
    "re_roof": "reroof",
    "roof": "reroof",
    "roofing": "reroof",
    "solar": "solar",
    "solar_panels": "solar",
    "photovoltaic": "solar",
    "pv": "solar",
    "windows": "windows_siding",
    "siding": "windows_siding",
    "windows_siding": "windows_siding",
    "fence": "fence",
    # Canonical permit_type keys (from the intent extractor) -> best-guess project.
    "building_permit": "addition",
    "building_permit_solar": "solar",
    "building_permit_windows_siding": "windows_siding",
}

INSPECTION_LABELS = {
    "footing": "Footing",
    "framing": "Framing",
    "rough_electrical": "Rough electrical",
    "rough_mechanical": "Rough mechanical",
    "rough_plumbing": "Rough plumbing",
    "insulation": "Insulation",
    "in_progress": "In-progress roofing",
    "mounting": "Solar mounting",
    "electrical_final": "Electrical final",
    "final": "Final",
}


def resolve_project_key(raw: str | None) -> str | None:
    """Map a permit_type / free phrase onto a catalog key."""
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _CATALOG:
        return key
    if key in _PROJECT_ALIASES:
        return _PROJECT_ALIASES[key]
    # Try normalizing as a permit_type first (handles "Solar" -> building_permit_solar).
    pt = normalize_permit_type(raw)
    return _PROJECT_ALIASES.get(pt) if pt else None


def permit_type_for_key(key: str | None) -> str | None:
    """Canonical permit_type for a catalog key (None if no separate permit)."""
    if not key:
        return None
    return _CATALOG.get(key, {}).get("permit_type")


def build_plan(project_inputs: list[str]) -> dict[str, Any]:
    """Turn a list of project phrases into a structured, ordered plan.

    Returns a dict (see AGENT_STATE project_plan schema) with:
      projects, phases (ordered, concurrent within a phase), shared_inspections,
      permit_types (for the fee engine), critical_path_days, unrecognized.
    """
    resolved: list[dict[str, Any]] = []
    unrecognized: list[str] = []
    seen: set[str] = set()
    for raw in project_inputs:
        key = resolve_project_key(raw)
        if key is None:
            unrecognized.append(raw)
            continue
        if key in seen:
            continue
        seen.add(key)
        spec = _CATALOG[key]
        resolved.append({"key": key, **spec})

    # An upfront planning/zoning phase is shared by every project that needs it.
    needs_planning = [p for p in resolved if p.get("needs_planning")]

    # Effective phase: a project depending on another present project is pushed
    # to at least one phase after it (e.g. solar after a concurrent re-roof).
    present = {p["key"] for p in resolved}
    for p in resolved:
        eff = p["phase"]
        for dep in p.get("depends_on", []):
            if dep in present:
                dep_phase = _CATALOG[dep]["phase"]
                eff = max(eff, dep_phase + 1)
        p["effective_phase"] = eff

    # Build ordered phases. Phase 1 is the shared planning review (only if needed);
    # subsequent phases group projects by effective_phase, concurrent within each.
    phases: list[dict[str, Any]] = []
    order = 1
    if needs_planning:
        phases.append(
            {
                "order": order,
                "label": PHASE_LABELS[PHASE_ZONING],
                "concurrent": True,
                "projects": [p["label"] for p in needs_planning],
                "note": "Confirm setbacks, zoning, and any HOA approval before pulling building permits.",
            }
        )
        order += 1

    build_phases = sorted({p["effective_phase"] for p in resolved if p["permit_type"] or p["inspections"]})
    for ph in build_phases:
        group = [p for p in resolved if p["effective_phase"] == ph and (p["permit_type"] or p["inspections"])]
        if not group:
            continue
        phases.append(
            {
                "order": order,
                "label": PHASE_LABELS.get(ph, "Permits & build"),
                "concurrent": len(group) > 1,
                "projects": [p["label"] for p in group],
                "note": _phase_note(group),
            }
        )
        order += 1

    # Shared inspections: inspection types required by 2+ projects -> one visit.
    insp_to_projects: dict[str, list[str]] = {}
    for p in resolved:
        for insp in p.get("inspections", []):
            insp_to_projects.setdefault(insp, []).append(p["label"])
    shared = [
        {"inspection": INSPECTION_LABELS.get(k, k), "projects": v}
        for k, v in insp_to_projects.items()
        if len(v) > 1
    ]

    # Critical-path estimate: planning review (if any) + the longest single chain.
    # Projects in the same phase run concurrently, so a phase costs its slowest member.
    planning_days = (5, 15) if needs_planning else (0, 0)
    per_phase_max: dict[int, tuple[int, int]] = {}
    for p in resolved:
        lo, hi = p.get("review_days", (5, 10))
        ph = p["effective_phase"]
        cur = per_phase_max.get(ph, (0, 0))
        per_phase_max[ph] = (max(cur[0], lo), max(cur[1], hi))
    chain_lo = planning_days[0] + sum(v[0] for v in per_phase_max.values())
    chain_hi = planning_days[1] + sum(v[1] for v in per_phase_max.values())

    return {
        "projects": [
            {"key": p["key"], "label": p["label"], "permit_type": p["permit_type"]}
            for p in resolved
        ],
        "permit_types": [p["permit_type"] for p in resolved if p["permit_type"]],
        "phases": phases,
        "shared_inspections": shared,
        "needs_planning_review": bool(needs_planning),
        "critical_path_days": {"min": chain_lo, "max": chain_hi},
        "unrecognized": unrecognized,
    }


def _phase_note(group: list[dict]) -> str:
    if len(group) > 1:
        return "These can be applied for and built concurrently."
    return ""
