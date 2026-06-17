"""
agent/graph.py
==============
LangGraph wiring — full request lifecycle.

    START -> contextualize -> cache_check
        cache hit  -> END
        cache miss -> intent
            FEE_CALC                       -> fee_engine -> (no schedule? retrieve : generate)
            STATUS_LOOKUP                  -> status_lookup -> generate
            ESCALATE                       -> escalate -> END
            FAQ / FORM_HELP / DOC_CHECKLIST-> retrieve -> rerank -> [gate] generate | idk
        generate -> write_cache -> END
        idk      -> END

`run_prep` mirrors this (minus live generation) so the SSE endpoint can stream
the answer token-by-token afterwards.
"""
from __future__ import annotations

import asyncio
import time
from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from agent.state import AgentState
from agent.nodes.contextualize import contextualize, resolve_standalone
from agent.nodes.cache import cache_check, write_cache
from agent.nodes.intent import intent
from agent.nodes.retrieve import retrieve
from agent.nodes.rerank import rerank
from agent.nodes.fee_engine import fee_engine
from agent.nodes.status_lookup import status_lookup
from agent.nodes.whats_changed import whats_changed
from agent.nodes.multi_project import multi_project
from agent.nodes.escalate import escalate
from agent.nodes.generate import generate, idk
from agent.nodes.converse import converse
from agent.nodes.judge import judge
from agent import llm


def _route_cache(state: AgentState) -> str:
    return "hit" if state.cache_hit else "miss"


def _route_intent(state: AgentState) -> str:
    return {
        "FEE_CALC": "fee_engine",
        "STATUS_LOOKUP": "status_lookup",
        "WHATS_CHANGED": "whats_changed",
        "MULTI_PROJECT": "multi_project",
        "ESCALATE": "escalate",
        "CONVERSE": "converse",
    }.get(state.intent or "FAQ", "retrieve")


def _route_fee(state: AgentState) -> str:
    """If no deterministic schedule exists, fall back to retrieval/grounding."""
    if state.fee_result and state.fee_result.get("no_schedule"):
        return "retrieve"
    return "generate"


def _route_confidence(state: AgentState) -> str:
    return "idk" if state.low_confidence or not state.reranked_chunks else "generate"


def build_graph():
    g = StateGraph(AgentState)

    g.add_node("contextualize", contextualize)
    g.add_node("cache_check", cache_check)
    g.add_node("intent", intent)
    g.add_node("retrieve", retrieve)
    g.add_node("rerank", rerank)
    g.add_node("fee_engine", fee_engine)
    g.add_node("status_lookup", status_lookup)
    g.add_node("whats_changed", whats_changed)
    g.add_node("multi_project", multi_project)
    g.add_node("escalate", escalate)
    g.add_node("generate", generate)
    g.add_node("idk", idk)
    g.add_node("converse", converse)
    g.add_node("judge", judge)
    g.add_node("write_cache", write_cache)

    g.add_edge(START, "contextualize")
    g.add_edge("contextualize", "cache_check")
    g.add_conditional_edges("cache_check", _route_cache, {"hit": END, "miss": "intent"})
    g.add_conditional_edges(
        "intent",
        _route_intent,
        {
            "fee_engine": "fee_engine",
            "status_lookup": "status_lookup",
            "whats_changed": "whats_changed",
            "multi_project": "multi_project",
            "escalate": "escalate",
            "converse": "converse",
            "retrieve": "retrieve",
        },
    )
    g.add_conditional_edges("fee_engine", _route_fee, {"retrieve": "retrieve", "generate": "generate"})
    g.add_edge("status_lookup", "generate")
    g.add_edge("whats_changed", "generate")
    g.add_edge("multi_project", "generate")
    g.add_edge("retrieve", "rerank")
    g.add_conditional_edges("rerank", _route_confidence, {"generate": "generate", "idk": "idk"})
    g.add_edge("generate", "judge")
    g.add_edge("judge", "write_cache")
    g.add_edge("write_cache", END)
    g.add_edge("idk", END)
    g.add_edge("converse", END)
    g.add_edge("escalate", END)

    return g.compile()


@lru_cache
def get_graph():
    return build_graph()


def _record(state: AgentState, name: str, t0: float) -> None:
    """Stash per-node elapsed time (ms) under state.meta['timing']."""
    timing = state.meta.setdefault("timing", {})
    timing[name] = round((time.perf_counter() - t0) * 1000, 1)


async def run_prep(state: AgentState) -> AgentState:
    """Run everything up to (but not including) live generation, returning the
    updated state for the SSE endpoint to stream.

    Mirrors build_graph()'s routing, but parallelizes the pre-flight: once the
    standalone query is known we fire the embedding and intent classification
    concurrently (the embedding feeds the cache check; intent is speculative and
    simply discarded on a cache hit — it's a cheap nano call).

    Routing: -> {converse | fee_engine | status_lookup | escalate | retrieve+rerank}.
    """
    t0 = time.perf_counter()
    standalone = await resolve_standalone(state)
    _record(state, "contextualize", t0)

    # Embedding + intent run together — neither needs the other's output.
    interim = state.model_copy(update={"standalone_query": standalone})
    t0 = time.perf_counter()
    embedding, intent_update = await asyncio.gather(
        llm.embed(standalone),
        intent(interim),
    )
    _record(state, "preflight", t0)

    state = state.model_copy(
        update={"standalone_query": standalone, "query_embedding": embedding, **intent_update}
    )

    t0 = time.perf_counter()
    state = state.model_copy(update=await cache_check(state))
    _record(state, "cache_check", t0)
    if state.cache_hit:
        return state

    route = _route_intent(state)

    if route == "converse":
        # No retrieval / cache / judge — the SSE endpoint streams the persona
        # answer directly via astream_answer (build_messages 'converse' mode).
        return state

    if route == "escalate":
        t0 = time.perf_counter()
        out = state.model_copy(update=await escalate(state))
        _record(out, "escalate", t0)
        return out

    if route == "fee_engine":
        t0 = time.perf_counter()
        state = state.model_copy(update=await fee_engine(state))
        _record(state, "fee_engine", t0)
        if _route_fee(state) == "generate":
            return state
        # no schedule -> fall through to retrieval

    elif route == "status_lookup":
        t0 = time.perf_counter()
        out = state.model_copy(update=await status_lookup(state))
        _record(out, "status_lookup", t0)
        return out

    elif route == "whats_changed":
        t0 = time.perf_counter()
        out = state.model_copy(update=await whats_changed(state))
        _record(out, "whats_changed", t0)
        return out

    elif route == "multi_project":
        t0 = time.perf_counter()
        out = state.model_copy(update=await multi_project(state))
        _record(out, "multi_project", t0)
        return out

    t0 = time.perf_counter()
    state = state.model_copy(update=await retrieve(state))
    _record(state, "retrieve", t0)
    t0 = time.perf_counter()
    state = state.model_copy(update=await rerank(state))
    _record(state, "rerank", t0)
    return state
