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
from agent import prompts
from agent.obs import log
from agent.state import AgentState

# Department contacts — phone numbers verified present in the scraped KB.
DEPT_CONTACTS = {
    "building": {"name": "Building Services / Permits", "phone": "720-898-7620"},
    "planning": {"name": "Planning & Development (Right-of-Way)", "phone": "720-898-7640"},
    "city_clerk": {"name": "City Clerk's Office", "phone": "720-898-7544"},
    "general": {"name": "City of Arvada", "phone": prompts.CITY_PHONE},
}

_BUILDING_PERMITS = {
    "building_permit",
    "building_permit_solar",
    "building_permit_windows_siding",
    "retaining_wall",
}


def _route_department(state: AgentState) -> str:
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
    dept = DEPT_CONTACTS.get(dept_key, DEPT_CONTACTS["general"])

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
        f"{prompts.CITY_WEBSITE}. A staff member can assist you with this matter, "
        f"including appeals, complaints, or anything that needs a person to review."
    )

    return {
        "intent": "ESCALATE",
        "answer": answer,
        "escalation_ticket_id": ticket_id,
        "citations": [],
        "meta": {**state.meta, "escalation_dept": dept_key, "escalation_contact": dept},
    }
