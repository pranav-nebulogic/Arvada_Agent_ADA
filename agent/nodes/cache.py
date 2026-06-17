"""
cache_check / write_cache nodes
==============================
Semantic cache wrappers around agent/redis_store.py.

- `cache_check` runs right after `contextualize` (which produced the standalone
  query + its embedding). On a hit it short-circuits the graph to END.
- `write_cache` runs after a confident grounded answer. It never caches the
  low-confidence "I don't know" path or non-deterministic intents (fee/status).
"""
from __future__ import annotations

from agent import redis_store
from agent.state import AgentState

# Intents whose answers must NOT be cached (vary per request / are live /
# conversational). CONVERSE replies are personalized smalltalk — never cache them.
# WHATS_CHANGED is time-sensitive; MULTI_PROJECT is a bespoke per-request plan.
_NON_CACHEABLE_INTENTS = {
    "FEE_CALC", "STATUS_LOOKUP", "ESCALATE", "CONVERSE", "WHATS_CHANGED", "MULTI_PROJECT"
}


async def cache_check(state: AgentState) -> dict:
    query = state.standalone_query or state.query
    hit = await redis_store.check_cache(state.city_id, state.lang, query, state.query_embedding)
    if hit and hit.get("answer"):
        return {
            "cache_hit": True,
            "cached_answer": hit["answer"],
            "answer": hit["answer"],
            "citations": hit.get("citations", []),
            "intent": hit.get("intent") or state.intent,
        }
    return {"cache_hit": False}


def is_cacheable(state: AgentState) -> bool:
    if state.cache_hit:
        return False
    if state.low_confidence or not state.answer:
        return False
    if state.intent in _NON_CACHEABLE_INTENTS:
        return False
    return True


async def write_cache(state: AgentState) -> dict:
    if is_cacheable(state):
        query = state.standalone_query or state.query
        await redis_store.write_cache(
            state.city_id,
            state.lang,
            query,
            state.query_embedding,
            state.answer or "",
            state.citations,
            state.intent,
        )
    return {}
