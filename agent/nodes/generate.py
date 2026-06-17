"""
generate / idk nodes
====================
`generate`  — grounded, cited, language-aware answer from the flagship model,
              built ONLY from reranked context.
`idk`       — polite "I don't have that information" + escalation offer, used by
              the confidence gate when retrieval found nothing relevant.

Both expose:
  - build_messages(state)         -> (messages, mode) for the LLM
  - astream_answer(state)         -> async iterator of token deltas (for SSE)
The node functions consume the stream fully for the non-stream / graph path.
"""
from __future__ import annotations

from typing import AsyncIterator

from config import settings
from agent import llm
from agent import prompts
from agent.state import AgentState


def group_sources(chunks: list[dict]) -> list[dict]:
    """Collapse reranked chunks into ordered, numbered, deduped sources.

    One entry per source document (article_title + source_url), in first-seen
    order, with the chunk texts merged. The assigned number `n` is what the model
    cites inline as [n] and what the UI renders as the matching source card, so
    the inline markers and the citation list always line up.
    """
    by_key: dict = {}
    order: list = []
    for c in chunks:
        title = c.get("article_title") or "City of Arvada"
        url = c.get("source_url")
        article_id = c.get("article_id")
        # Prefer the article id as the dedupe key so chunks from the same article
        # collapse into one source card that links to its in-app article page.
        key = article_id or (title, url)
        if key not in by_key:
            by_key[key] = {
                "article_id": article_id,
                "title": title,
                "url": url,
                "effective_date": str(c.get("effective_date")) if c.get("effective_date") else None,
                "section_type": c.get("section_type"),
                "permit_type": c.get("permit_type"),
                "texts": [],
            }
            order.append(key)
        text = (c.get("content_text") or "").strip()
        if text:
            by_key[key]["texts"].append(text)

    sources: list[dict] = []
    for i, key in enumerate(order, 1):
        s = by_key[key]
        sources.append(
            {
                "n": i,
                "article_id": s["article_id"],
                "title": s["title"],
                "url": s["url"],
                "effective_date": s["effective_date"],
                "section_type": s["section_type"],
                "permit_type": s["permit_type"],
                "text": "\n\n".join(s["texts"]),
            }
        )
    return sources


def build_citations(chunks: list[dict]) -> list[dict]:
    """Citation cards (one per source article)."""
    citations: list[dict] = []
    for s in group_sources(chunks):
        citations.append(
            {
                "n": s["n"],
                "article_id": s["article_id"],
                "title": s["title"],
                "url": s["url"],
                "effective_date": s["effective_date"],
                "section_type": s["section_type"],
                "permit_type": s["permit_type"],
            }
        )
    return citations


def build_messages(state: AgentState) -> tuple[list[dict], str]:
    """Return (messages, mode): 'converse' | 'fee' | 'status' | 'grounded' | 'idk'."""
    query = state.query

    # Conversational turn (greeting/meta/off-topic/abuse) — no retrieval, no citations.
    if state.intent == "CONVERSE":
        from agent.nodes.contextualize import _history_text

        history = _history_text(state.messages, settings.conversation_history_turns)
        messages = [
            {"role": "system", "content": prompts.converse_system(state.lang)},
            {"role": "user", "content": prompts.converse_user(query, history)},
        ]
        return messages, "converse"

    # Deterministic fee estimate -> present the engine's numbers exactly.
    if state.fee_result and not state.fee_result.get("no_schedule"):
        messages = [
            {"role": "system", "content": prompts.fee_system(state.lang)},
            {"role": "user", "content": prompts.fee_user(query, state.fee_result, state.lang)},
        ]
        return messages, "fee"

    # Permit/case status lookup result.
    if state.status_result:
        messages = [
            {"role": "system", "content": prompts.status_system(state.lang)},
            {"role": "user", "content": prompts.status_user(query, state.status_result, state.lang)},
        ]
        return messages, "status"

    # "What changed" fee/code alerts — present the change records exactly.
    if state.changes_result:
        messages = [
            {"role": "system", "content": prompts.changes_system(state.lang)},
            {"role": "user", "content": prompts.changes_user(query, state.changes_result, state.lang)},
        ]
        return messages, "changes"

    # Multi-project plan — present the deterministic sequencing/fees exactly.
    if state.project_plan:
        messages = [
            {"role": "system", "content": prompts.plan_system(state.lang)},
            {"role": "user", "content": prompts.plan_user(query, state.project_plan, state.lang)},
        ]
        return messages, "plan"

    if state.low_confidence or not state.reranked_chunks:
        messages = [
            {"role": "system", "content": prompts.idk_system(state.lang)},
            {"role": "user", "content": query},
        ]
        return messages, "idk"

    sources = group_sources(state.reranked_chunks)
    context = prompts.generation_context_block(sources)
    messages = [
        {"role": "system", "content": prompts.generation_system(state.lang)},
        {"role": "user", "content": prompts.generation_user(query, context, state.lang)},
    ]
    return messages, "grounded"


def pick_generation_model(state: AgentState) -> tuple[str, str]:
    """Tiered model selection: fast by default, deep for complex/legal answers.

    Returns (model, reasoning_effort). Escalates to the deep flagship when the
    classifier flagged the query complex, when the top source is an ordinance
    (legal precision matters), or when the question is long / multi-part.
    """
    deep = (settings.generation_model_deep, settings.generation_reasoning_effort_deep)
    fast = (settings.generation_model_fast, settings.generation_reasoning_effort_fast)

    if state.complexity == "complex":
        return deep

    for c in (state.reranked_chunks or [])[:1]:
        meta = c.get("metadata") or {}
        if meta.get("article_type") == "ordinance" or c.get("section_type") == "ordinance":
            return deep

    q = state.standalone_query or state.query or ""
    # Long or visibly multi-part questions benefit from the deeper model.
    if len(q) > 280 or q.count("?") >= 2:
        return deep

    return fast


async def astream_answer(state: AgentState) -> AsyncIterator[str]:
    """Stream the answer token-by-token (used by the SSE endpoint)."""
    messages, _mode = build_messages(state)
    model, effort = pick_generation_model(state)
    async for delta in llm.stream_chat(
        model=model,
        messages=messages,
        reasoning_effort=effort,
    ):
        yield delta


async def _collect(state: AgentState) -> str:
    parts: list[str] = []
    async for delta in astream_answer(state):
        parts.append(delta)
    return "".join(parts).strip()


def citations_for(state: AgentState) -> list[dict]:
    """Pick the source cards that match the answer the generator will produce."""
    if state.intent == "CONVERSE":
        return []
    if state.fee_result and not state.fee_result.get("no_schedule"):
        return state.fee_result.get("sources") or []
    if state.status_result:
        return []
    if state.changes_result:
        return state.changes_result.get("sources") or []
    if state.project_plan:
        return []
    if state.low_confidence or not state.reranked_chunks:
        return []
    return build_citations(state.reranked_chunks)


async def generate(state: AgentState) -> dict:
    """Grounded answer path (confident retrieval)."""
    try:
        answer = await _collect(state)
    except Exception:
        answer = prompts.idk_fallback_text(state.lang)
    if not answer:
        answer = prompts.idk_fallback_text(state.lang)
    return {"answer": answer, "citations": citations_for(state)}


async def idk(state: AgentState) -> dict:
    """Low-confidence path — no fabricated facts, offer escalation."""
    try:
        answer = await _collect(state)
    except Exception:
        answer = prompts.idk_fallback_text(state.lang)
    if not answer:
        answer = prompts.idk_fallback_text(state.lang)
    return {"answer": answer, "citations": []}
