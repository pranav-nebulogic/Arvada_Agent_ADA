"""Deterministic multi-project sequencing — pure, no network."""
from agent.project_planner import build_plan, resolve_project_key


def test_resolve_aliases_and_permit_types():
    assert resolve_project_key("deck") == "deck"
    assert resolve_project_key("garage conversion") == "garage_conversion"
    assert resolve_project_key("Solar") == "solar"
    assert resolve_project_key("re-roof") == "reroof"
    assert resolve_project_key("building_permit_solar") == "solar"
    assert resolve_project_key("nonsense") is None
    assert resolve_project_key(None) is None


def test_plan_groups_concurrent_projects_in_one_phase():
    plan = build_plan(["deck", "retaining wall"])
    # Both are structural with no inter-dependency -> same build phase, concurrent.
    build_phases = [p for p in plan["phases"] if "Structural" in p["label"] or "Permits" in p["label"]]
    assert any(p["concurrent"] and len(p["projects"]) == 2 for p in build_phases)


def test_solar_waits_for_reroof_when_both_present():
    plan = build_plan(["re-roof", "solar"])
    # Solar depends on reroof, so it must land in a later phase than the re-roof.
    order_of = {}
    for ph in plan["phases"]:
        for proj in ph["projects"]:
            order_of[proj] = ph["order"]
    assert order_of["Rooftop solar"] > order_of["Re-roof"]


def test_solar_alone_does_not_invent_a_reroof_dependency():
    plan = build_plan(["solar"])
    labels = [p["label"] for p in plan["projects"]]
    assert labels == ["Rooftop solar"]
    # Only one build phase, no phantom re-roof.
    assert all("Re-roof" not in ph["projects"] for ph in plan["phases"])


def test_shared_inspections_detected():
    plan = build_plan(["deck", "room addition"])
    shared = {s["inspection"] for s in plan["shared_inspections"]}
    # Deck and addition both have footing, framing, final.
    assert "Framing" in shared
    assert "Final" in shared


def test_planning_review_phase_added_once():
    plan = build_plan(["deck", "garage conversion"])
    planning = [p for p in plan["phases"] if "Planning" in p["label"]]
    assert len(planning) == 1
    assert plan["needs_planning_review"] is True


def test_permit_types_collected_for_fee_engine():
    plan = build_plan(["deck", "solar", "windows"])
    assert "building_permit" in plan["permit_types"]
    assert "building_permit_solar" in plan["permit_types"]
    assert "building_permit_windows_siding" in plan["permit_types"]


def test_unrecognized_projects_surface():
    plan = build_plan(["deck", "swimming pool moat"])
    assert "swimming pool moat" in plan["unrecognized"]
    assert [p["label"] for p in plan["projects"]] == ["Deck"]


def test_critical_path_is_positive_and_ordered():
    plan = build_plan(["deck", "garage conversion", "solar", "re-roof"])
    cp = plan["critical_path_days"]
    assert cp["min"] >= 1
    assert cp["max"] >= cp["min"]
