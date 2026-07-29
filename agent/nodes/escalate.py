"""
escalate node
============
Writes the conversation to the existing `escalation_queue` table and returns a
deterministic confirmation answer + ticket id. Routes to the right department
using phone numbers verified to exist in the ingested Arvada knowledge base.

Emits an `escalate` SSE event (handled in api.py).
"""
from __future__ import annotations

import db
from config import settings
from city_config import get_city
from agent import engine_fees, prompts, request_types
from agent.obs import log
from agent.state import AgentState

def dept_contact(dept_key: str | None, city_id: str | None = None) -> dict[str, str]:
    """
    Escalation contact for a department, in the caller's city.

    Falls back to the city's main line when that city has no verified direct
    number for the department. That fallback is deliberate: these numbers used to
    be a module-level Arvada dict, so any other city's residents would have been
    told to phone Arvada. A vaguer-but-correct number beats a precise wrong one.
    """
    city = get_city(city_id)
    dept = city.departments.get(dept_key or "")
    if dept:
        return dept
    return {"name": city.city_name, "phone": city.phone}

_BUILDING_PERMITS = {
    "building_permit",
    "building_permit_solar",
    "building_permit_windows_siding",
    "retaining_wall",
}


def _route_department(state: AgentState) -> str:
    # Prefer the canonical request type when we have one — it carries the owning
    # department, mapped to a contact bucket with a verified phone number.
    rt = (state.entities or {}).get("request_type")
    if rt:
        return request_types.contact_bucket(rt)

    pt = (state.entities or {}).get("permit_type") or state.permit_type
    text = f"{state.query} {state.standalone_query or ''}".lower()

    if pt in _BUILDING_PERMITS or "permit" in text and any(
        w in text for w in ("build", "remodel", "solar", "inspection", "roof")
    ):
        return "building"
    if pt == "special_event_permit" or "right of way" in text or "right-of-way" in text or "planning" in text:
        return "planning"
    if "license" in text or "liquor" in text or "clerk" in text or pt == "str_permit":
        return "city_clerk"
    return "general"


async def escalate(state: AgentState) -> dict:
    dept_key = _route_department(state)
    dept = dept_contact(dept_key, state.city_id)

    history = (state.messages or [])[-settings.conversation_history_turns * 2:]

    ticket_id = None
    try:
        ticket_id = await db.insert_escalation(
            city_id=state.city_id,
            session_id=state.session_id,
            query=state.query,
            conversation=history,
            intent="ESCALATE",
            permit_type=(state.entities or {}).get("permit_type") or state.permit_type,
            routed_to_dept=dept_key,
        )
    except Exception as exc:
        # Still give the user a routed answer even if the ticket write failed,
        # but log it — a dropped escalation row is a lost citizen request.
        log.error("escalation_insert_failed", error=str(exc), session_id=state.session_id)
        ticket_id = None

    # Use a 12-hex-char reference (formatted AB12-CD34-EF56). A UUID's first 8
    # hex chars collide far too easily across tickets; 12 keeps it readable while
    # giving ~2.8e14 of headroom, and it still maps back to the full ticket UUID.
    ref = ""
    if ticket_id:
        h = ticket_id.replace("-", "")[:12].upper()
        pretty = "-".join(h[i:i + 4] for i in range(0, len(h), 4))
        ref = f" Your reference number is {pretty}."
    answer = (
        f"I've routed your request to **{dept['name']}**.{ref}\n\n"
        f"For direct help, you can contact them at **{dept['phone']}** or visit "
        f"{get_city(state.city_id).website}. A staff member can assist you with this matter, "
        f"including appeals, complaints, or anything that needs a person to review."
    )

    # If we recognized a specific request type, offer a deep-link to start it.
    # Ada guides the citizen to the apply page — it does not create the record.
    meta = {**state.meta, "escalation_dept": dept_key, "escalation_contact": dept}
    # Resolve against THIS city's catalog, not the static Arvada registry: a
    # Woodinville user was being offered "Residential Interior", a type that city
    # does not have. No match -> no button, rather than a wrong one.
    surface = request_types.surface_for(state.user_type)
    text = " ".join(filter(None, [
        state.standalone_query or state.query,
        (state.entities or {}).get("request_type") or "",
        (state.entities or {}).get("permit_type") or state.permit_type or "",
    ]))
    applied = await engine_fees.resolve_apply(state.city_id, text, surface)
    if applied:
        # Point at the action, don't repeat its URL. `meta["apply"]` below is
        # already rendered by the UI as a proper button under "Manage a Service
        # Request"; pasting the same link into the prose showed it TWICE — once
        # as a real button and once as unclickable text.
        answer += (f"\n\nWhen you're ready, you can start the "
                   f"**{applied['label']}** application below.")
        meta["apply"] = applied

    return {
        "intent": "ESCALATE",
        "answer": answer,
        "escalation_ticket_id": ticket_id,
        "citations": [],
        "meta": meta,
    }
