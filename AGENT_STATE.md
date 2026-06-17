# AGENT_STATE.md — Cursor Context File
# Reference this whenever writing a LangGraph node.
# Every node receives AgentState and returns a PARTIAL dict (not full state).

## AgentState Schema (complete)

Conversation is **client-owned**: the client sends the full `messages` thread every
turn. Nodes read it (via `contextualize`) but the server keeps **no** conversation
memory. Redis is used **only** for the semantic cache.

```python
from typing import Any, Literal
from pydantic import BaseModel, Field

Intent = Literal[
    "FAQ", "FORM_HELP", "DOC_CHECKLIST",
    "FEE_CALC", "STATUS_LOOKUP", "ESCALATE", "CONVERSE",
]

class AgentState(BaseModel):
    # ── Tenant / Session (injected at request start, never changed) ─────────
    city_id:       str           # e.g. "arvada-co" — from JWT header, not user input
    session_id:    str           # UUID — request correlation only (no server memory)
    user_type:     Literal["homeowner", "contractor", "staff"] = "homeowner"
    lang:          str = "en"

    # ── User Input ──────────────────────────────────────────────────────────
    query:         str           # raw user message
    permit_type:   str | None = None
    form_field:    str | None = None

    # ── Conversation (client-owned thread) ───────────────────────────────────
    messages:      list[dict] = Field(default_factory=list)  # [{role, content}, ...]

    # ── Query contextualization ───────────────────────────────────────────────
    standalone_query: str | None = None   # latest msg rewritten to a standalone query

    # ── Pre-flight ───────────────────────────────────────────────────────────
    query_embedding:    list[float] | None = None  # text-embedding-3-small
    cache_hit:          bool = False
    cached_answer:      str | None = None

    # ── Routing ─────────────────────────────────────────────────────────────
    intent:     Intent | None = None
    entities:   dict = Field(default_factory=dict)
    complexity: str | None = None   # "simple" | "complex" — drives tiered generation
    # entities: {permit_type, fee_valuation, reference_number, form_field_name}

    # ── Retrieval ────────────────────────────────────────────────────────────
    retrieved_chunks: list[dict] = Field(default_factory=list)
    # chunk schema: {chunk_id, content_text, section_type, article_title, article_id,
    #                source_url, effective_date, permit_type, score, metadata}
    reranked_chunks:  list[dict] = Field(default_factory=list)
    low_confidence:   bool = False

    # ── Fee calculation ────────────────────────────────────────────────────────
    fee_result:    dict | None = None   # deterministic engine output (+ "sources" cards)

    # ── Status lookup ────────────────────────────────────────────────────────────
    status_result: dict | None = None   # external engine case lookup

    # ── Output ───────────────────────────────────────────────────────────────
    answer:      str | None = None
    citations:   list[dict] = Field(default_factory=list)
    # raw citation card schema (formatted to wire form at the API edge):
    #   {n, article_id, title, url, effective_date, section_type, permit_type}
    judge_passed:  bool | None = None
    warning:       str | None = None
    escalation_ticket_id: str | None = None

    # ── Telemetry ────────────────────────────────────────────────────────────────
    meta: dict[str, Any] = Field(default_factory=dict)  # incl. meta["timing"] per node
```

## Node Return Patterns

Every node returns a **partial dict** — only the fields it modifies:

```python
# cache_check node — returns on hit
async def cache_check(state: AgentState, config: RunnableConfig) -> dict:
    # ...
    if hit:
        return {"cache_hit": True, "cached_answer": answer, "citations": citations}
    return {"cache_hit": False, "query_embedding": embedding}

# intent node
async def intent_router(state: AgentState, config: RunnableConfig) -> dict:
    # ...
    return {"intent": "FAQ", "entities": {"permit_type": "building_permit"}}

# retrieval node
async def hybrid_retrieve(state: AgentState, config: RunnableConfig) -> dict:
    # ...
    return {"retrieved_chunks": chunks}

# reranker node
async def reranker(state: AgentState, config: RunnableConfig) -> dict:
    return {"reranked_chunks": top_chunks}

# generator node — answer built from SSE stream, stored after completion
async def generate_stream(state: AgentState, config: RunnableConfig) -> dict:
    return {"answer": full_answer, "citations": citations}

# judge node
async def judge_async(state: AgentState, config: RunnableConfig) -> dict:
    return {"judge_passed": True}  # or {"judge_passed": False, "warning": "..."}

# fee engine — deterministic, no LLM
async def fee_calculator(state: AgentState, config: RunnableConfig) -> dict:
    return {"fee_result": {...}}

# escalation node
async def escalate(state: AgentState, config: RunnableConfig) -> dict:
    return {"escalation_ticket_id": ticket_id}
```

## Routing Functions (conditional edges)

```python
def route_cache(state: AgentState) -> str:
    return "hit" if state.cache_hit else "miss"

def route_intent(state: AgentState) -> str:
    intent_map = {
        "FEE_CALC": "fee_engine",
        "STATUS_LOOKUP": "status_lookup",
        "ESCALATE": "escalate",
        "CONVERSE": "converse",         # smalltalk/meta/off-topic/abuse — no retrieval
    }
    return intent_map.get(state.intent, "retrieve")  # FAQ/FORM_HELP/DOC_CHECKLIST -> retrieve
```

## Important: Conversation Memory (client-owned)
- There is **no** server-side conversation memory. The client sends the full
  `messages` thread on every request; `contextualize` rewrites the latest turn into
  a standalone query using that thread.
- Redis holds **only** the semantic cache (keyed by city/lang + embedding). CONVERSE,
  FEE_CALC, STATUS_LOOKUP, and ESCALATE answers are never cached.

## CONVERSE path (conversational robustness)
- The `intent` classifier emits `CONVERSE` for greetings, thanks, "what can you do",
  smalltalk, off-topic chatter, and profanity/venting. The raw user message is passed
  to the classifier alongside the standalone query so tone isn't lost in the rewrite.
- `converse` node answers from the shared persona (Ada) + a capabilities manifest —
  **no retrieval, no citations, no judge, no cache**. The SSE endpoint streams it
  directly (no "search" tool pill).

## Tiered generation (latency)
- `intent` also emits `complexity` ("simple" | "complex").
- `generate.pick_generation_model(state)` returns `(model, reasoning_effort)`:
  deep flagship (`generation_model_deep`) when complexity is complex, the top reranked
  chunk is an ordinance, or the query is long/multi-part; otherwise the fast model
  (`generation_model_fast`).
- `run_prep` parallelizes the embedding and intent classification with `asyncio.gather`
  once the standalone query is known. Per-node timing is recorded in `state.meta["timing"]`
  and surfaced on the SSE `done` event when `EXPOSE_TIMING=true` or `?debug=1`.
