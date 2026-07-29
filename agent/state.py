"""
agent/state.py
==============
AgentState — the single object threaded through the LangGraph.

Conversation is CLIENT-OWNED: the client sends the full `messages` thread every
turn. Nodes read it (via `contextualize`) but do not persist server-side memory.
Redis is used only for the semantic cache.

Every node returns a PARTIAL dict of only the fields it changes.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Intent = Literal[
    "FAQ", "FORM_HELP", "DOC_CHECKLIST", "FEE_CALC", "STATUS_LOOKUP", "ESCALATE",
    "CONVERSE", "WHATS_CHANGED", "MULTI_PROJECT"
]


class AgentState(BaseModel):
    # ── Tenant / Session (injected at request start, never changed) ──────────
    city_id: str
    session_id: str
    user_type: Literal["homeowner", "contractor", "staff"] = "homeowner"
    lang: str = "en"

    # ── User input ────────────────────────────────────────────────────────────
    query: str
    permit_type: str | None = None
    form_field: str | None = None

    # ── Conversation (client-owned thread) ──────────────────────────────────────
    messages: list[dict] = Field(default_factory=list)

    # ── Query contextualization ─────────────────────────────────────────────────
    standalone_query: str | None = None

    # ── Pre-flight ─────────────────────────────────────────────────────────────
    query_embedding: list[float] | None = None
    cache_hit: bool = False
    cached_answer: str | None = None

    # ── Routing ────────────────────────────────────────────────────────────────
    intent: Intent | None = None
    entities: dict = Field(default_factory=dict)
    # Classifier hint for tiered generation: "simple" -> fast model, "complex" -> deep model.
    complexity: str | None = None

    # ── Retrieval ──────────────────────────────────────────────────────────────
    retrieved_chunks: list[dict] = Field(default_factory=list)
    reranked_chunks: list[dict] = Field(default_factory=list)
    low_confidence: bool = False
    # Unfiltered candidates fetched CONCURRENTLY with intent classification
    # (settings.parallel_intent_retrieve). `retrieve` uses these only when intent
    # produced no permit_type; otherwise it discards them and runs the filtered
    # query. None = no prefetch happened, which is distinct from "prefetch
    # returned nothing".
    prefetched_chunks: list[dict] | None = None

    # ── Fee calculation ──────────────────────────────────────────────────────────
    fee_result: dict | None = None

    # ── Status lookup ────────────────────────────────────────────────────────────
    status_result: dict | None = None

    # ── "What Changed?" fee & code alerts ─────────────────────────────────────────
    changes_result: dict | None = None

    # ── Multi-project dependency plan ─────────────────────────────────────────────
    project_plan: dict | None = None

    # ── Output ─────────────────────────────────────────────────────────────────
    answer: str | None = None
    citations: list[dict] = Field(default_factory=list)
    judge_passed: bool | None = None
    warning: str | None = None
    escalation_ticket_id: str | None = None

    # ── Telemetry ────────────────────────────────────────────────────────────────
    meta: dict[str, Any] = Field(default_factory=dict)
