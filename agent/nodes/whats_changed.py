"""
whats_changed node ("What Changed?" fee & code alerts)
======================================================
Answers "did anything change recently?" questions. Reads recent rows from
`change_log` (optionally scoped to the permit type the user mentioned) and stashes
them in `changes_result`. The downstream `generate` node turns the records into a
user-facing answer (prompts.changes_*), presenting only what the records say.

Like the fee engine, the data here is authoritative and deterministic — the LLM
only narrates it, so this path skips the grounding judge and the semantic cache
(changes are time-sensitive).
"""
from __future__ import annotations

import db
from agent import fee_tables
from agent.obs import log
from agent.state import AgentState


async def _change_sources(state: AgentState, changes: list[dict]) -> list[dict]:
    """Link each change back to its source knowledge article when one exists."""
    sources: list[dict] = []
    seen: set = set()
    for c in changes:
        aid = c.get("source_article_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        sources.append(
            {
                "n": len(sources) + 1,
                "article_id": aid,
                "title": c.get("title") or "City of Arvada update",
                "url": None,
                "effective_date": c.get("effective_date"),
                "section_type": c.get("change_type"),
                "permit_type": c.get("permit_type"),
            }
        )
    return sources


async def whats_changed(state: AgentState) -> dict:
    raw_pt = (state.entities or {}).get("permit_type") or state.permit_type
    permit_type = fee_tables.normalize_permit_type(raw_pt)

    try:
        changes = await db.fetch_recent_changes(state.city_id, permit_type=permit_type)
    except Exception as exc:  # noqa: BLE001 — degrade to "nothing on record", never 500
        log.warning("whats_changed_lookup_failed", error=str(exc), permit_type=permit_type)
        changes = []

    return {
        "intent": "WHATS_CHANGED",
        "changes_result": {
            "changes": changes,
            "permit_type": permit_type,
            "scope": "permit" if permit_type else "city",
            "sources": await _change_sources(state, changes),
        },
    }
