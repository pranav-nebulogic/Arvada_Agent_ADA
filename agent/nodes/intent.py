"""
intent node
==========
Small/fast model classifies the contextualized query into one of six intents and
extracts entities (permit_type, fee_valuation, reference_number, form_field_name).
Falls back to FAQ on any error so the request still gets a grounded answer.
"""
from __future__ import annotations

import re

from config import settings
from agent import llm, request_types
from agent.fee_tables import normalize_permit_type
from agent.obs import log
from agent.prompts import INTENT_SYSTEM, intent_user
from agent.state import AgentState

VALID_INTENTS = {
    "FAQ", "FORM_HELP", "DOC_CHECKLIST", "FEE_CALC", "STATUS_LOOKUP", "ESCALATE",
    "CONVERSE", "WHATS_CHANGED", "MULTI_PROJECT"
}


def _coerce_valuation(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    digits = re.sub(r"[^0-9.]", "", str(v))
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


async def intent(state: AgentState) -> dict:
    query = state.standalone_query or state.query
    try:
        result = await llm.complete_json(
            model=settings.intent_model,
            system=INTENT_SYSTEM,
            user=intent_user(query, state.query),
            reasoning_effort=settings.small_model_reasoning_effort,
        )
        raw_intent = str(result.get("intent", "FAQ")).upper().strip()
        chosen = raw_intent if raw_intent in VALID_INTENTS else "FAQ"
        complexity = str(result.get("complexity", "simple")).lower().strip()
        complexity = complexity if complexity in {"simple", "complex"} else "simple"
        ents = result.get("entities") or {}
        raw_types = ents.get("permit_types")
        permit_types = None
        if isinstance(raw_types, list):
            norm = [normalize_permit_type(t) for t in raw_types]
            # Dedupe, drop blanks, preserve order.
            seen: set = set()
            permit_types = [t for t in norm if t and not (t in seen or seen.add(t))]
            permit_types = permit_types or None
        # Canonical Arvada request type (smart-lp code) for deep-link + routing.
        # Match the free-text phrase first; fall back to the fee permit_type word.
        request_type = (
            request_types.normalize(ents.get("request_type"))
            or request_types.normalize(ents.get("permit_type"))
        )
        entities = {
            "permit_type": normalize_permit_type(ents.get("permit_type")) or state.permit_type,
            "permit_types": permit_types,
            "request_type": request_type,
            "fee_valuation": _coerce_valuation(ents.get("fee_valuation")),
            "reference_number": ents.get("reference_number"),
            "form_field_name": ents.get("form_field_name") or state.form_field,
        }
        entities = {k: v for k, v in entities.items() if v is not None}
        return {"intent": chosen, "entities": entities, "complexity": complexity}
    except Exception as exc:
        # Best-effort fallback so the request still gets a grounded answer — but
        # log it: a silent misroute to FAQ is otherwise invisible in production.
        log.warning("intent_classification_failed", error=str(exc), query=query[:200])
        return {"intent": "FAQ", "entities": {}, "complexity": "simple"}
