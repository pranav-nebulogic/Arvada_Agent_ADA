"""
fee_engine node
==============
Deterministic fee calculation (no LLM math). Reads the permit_type + valuation
from extracted entities, looks up fee_schedules rows, and computes an itemised
estimate via agent/fee_tables.py. The downstream `generate` node turns the
numbers into a user-facing answer with the estimate disclaimer.
"""
from __future__ import annotations

import db
from agent import fee_tables
from agent.obs import log
from agent.state import AgentState


async def _fee_sources(state: AgentState, permit_type: str) -> list[dict]:
    """Attach the KB document(s) behind the fee numbers so the answer is sourced."""
    if not state.query_embedding:
        return []
    try:
        chunks = await db.hybrid_search(
            state.city_id,
            state.standalone_query or state.query,
            state.query_embedding,
            permit_type=permit_type,
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 — sources are best-effort, never block the estimate
        log.warning("fee_sources_lookup_failed", error=str(exc), permit_type=permit_type)
        return []

    seen: set = set()
    sources: list[dict] = []
    for c in chunks:
        key = (c.get("article_title"), c.get("source_url"))
        if key in seen or not c.get("source_url"):
            continue
        seen.add(key)
        sources.append(
            {
                "n": len(sources) + 1,
                "article_id": c.get("article_id"),
                "title": c.get("article_title") or "City of Arvada Fee Schedule",
                "url": c.get("source_url"),
                "effective_date": str(c.get("effective_date")) if c.get("effective_date") else None,
                "section_type": c.get("section_type"),
                "permit_type": c.get("permit_type"),
            }
        )
        if len(sources) >= 2:
            break
    return sources


def _extract_valuation(state: AgentState) -> float | None:
    ent = state.entities or {}
    for k in ("fee_valuation", "valuation", "project_valuation"):
        v = ent.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


async def fee_engine(state: AgentState) -> dict:
    raw_pt = (state.entities or {}).get("permit_type") or state.permit_type
    permit_type = fee_tables.normalize_permit_type(raw_pt)
    valuation = _extract_valuation(state)

    if not permit_type:
        return {
            "fee_result": {
                "permit_type": None,
                "needs_permit_type": True,
                "disclaimer": fee_tables.DISCLAIMER,
            }
        }

    fee_rows = await db.fetch_fee_schedule(state.city_id, permit_type)
    if not fee_rows:
        # No deterministic schedule -> let retrieval/generation answer from KB.
        return {"fee_result": {"permit_type": permit_type, "no_schedule": True}}

    result = fee_tables.compute_fees(permit_type, fee_rows, valuation=valuation)
    result["sources"] = await _fee_sources(state, permit_type)
    # Proactive "what changed" enrichment: if this permit's fees changed recently,
    # the fee answer can flag it instead of quoting a number with no context.
    try:
        changes = await db.fetch_recent_changes(
            state.city_id, permit_type=permit_type, change_type="fee", limit=3
        )
        if changes:
            result["recent_changes"] = changes
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort, never block the estimate
        log.warning("fee_change_enrichment_failed", error=str(exc), permit_type=permit_type)
    return {"fee_result": result, "entities": {**(state.entities or {}), "permit_type": permit_type}}
