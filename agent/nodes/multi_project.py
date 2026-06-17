"""
multi_project node (Multi-Project Dependency Resolver)
======================================================
When a resident describes several projects at once ("deck + garage conversion +
solar"), this node assembles ONE coordinated plan: the order to pull permits,
what can run concurrently, shared inspections, and a combined fee picture.

Flow:
  1. Extract the distinct projects from the message (small/fast model).
  2. project_planner.build_plan() decides structure & sequencing (deterministic).
  3. Price each project's permit from the live fee_schedules (deterministic).
  4. Stash everything in `project_plan`; the generate node narrates it.

Like the fee engine, the structure and numbers are authoritative, so this path
skips the grounding judge and the semantic cache.
"""
from __future__ import annotations

import db
from agent import fee_tables, llm, project_planner
from agent.obs import log
from agent.prompts import MULTI_PROJECT_EXTRACT_SYSTEM, multi_project_extract_user
from config import settings
from agent.state import AgentState


async def _extract_projects(state: AgentState) -> list[dict]:
    """Pull the distinct projects (+ optional per-project valuation) from the query."""
    query = state.standalone_query or state.query
    try:
        result = await llm.complete_json(
            model=settings.intent_model,
            system=MULTI_PROJECT_EXTRACT_SYSTEM,
            user=multi_project_extract_user(query),
            reasoning_effort=settings.small_model_reasoning_effort,
        )
        projects = result.get("projects")
        if isinstance(projects, list) and projects:
            return [p for p in projects if isinstance(p, dict) and p.get("project")]
    except Exception as exc:  # noqa: BLE001
        log.warning("multi_project_extract_failed", error=str(exc), query=query[:200])

    # Fallback: the intent classifier's coarse permit_types list.
    pts = (state.entities or {}).get("permit_types") or []
    return [{"project": pt, "valuation": None} for pt in pts]


async def _price_project(state: AgentState, permit_type: str, valuation) -> dict:
    """Deterministic fee estimate for one project's permit (best-effort)."""
    try:
        fee_rows = await db.fetch_fee_schedule(state.city_id, permit_type)
    except Exception as exc:  # noqa: BLE001
        log.warning("multi_project_fee_lookup_failed", error=str(exc), permit_type=permit_type)
        return {"permit_type": permit_type, "unavailable": True}
    if not fee_rows:
        return {"permit_type": permit_type, "no_schedule": True}
    val = None
    if valuation is not None:
        try:
            val = float(valuation)
        except (TypeError, ValueError):
            val = None
    return fee_tables.compute_fees(permit_type, fee_rows, valuation=val)


async def multi_project(state: AgentState) -> dict:
    projects = await _extract_projects(state)
    plan = project_planner.build_plan([p["project"] for p in projects])

    # Map each project's valuation (if any) onto its resolved permit_type for pricing.
    valuation_for: dict[str, float] = {}
    for p in projects:
        key = project_planner.resolve_project_key(p.get("project"))
        if not key:
            continue
        pt = project_planner.permit_type_for_key(key)
        if pt and p.get("valuation") is not None:
            try:
                valuation_for[pt] = float(p["valuation"])
            except (TypeError, ValueError):
                pass

    # Price each distinct permit type once.
    fee_breakdown: list[dict] = []
    combined_total = 0.0
    any_needs_valuation = False
    for pt in dict.fromkeys(plan.get("permit_types", [])):  # dedupe, keep order
        fee = await _price_project(state, pt, valuation_for.get(pt))
        fee_breakdown.append(fee)
        if fee.get("needs_valuation") or fee.get("no_schedule") or fee.get("unavailable"):
            any_needs_valuation = any_needs_valuation or bool(fee.get("needs_valuation"))
        elif fee.get("total"):
            combined_total += float(fee["total"])

    plan["fees"] = {
        "breakdown": fee_breakdown,
        "combined_total": round(combined_total, 2) if combined_total else None,
        "needs_valuation": any_needs_valuation,
        "disclaimer": fee_tables.DISCLAIMER,
    }
    return {"intent": "MULTI_PROJECT", "project_plan": plan}
