"""
rerank node
===========
Scores the retrieved candidates with a small fast model (0-10 relevance) and
keeps the top-K above the minimum score. Also sets `low_confidence` for the
confidence gate when nothing clears the bar.

Falls back to RRF ordering if the LLM rerank fails for any reason.
"""
from __future__ import annotations

from config import settings
from agent import llm
from agent.obs import log
from agent.prompts import RERANK_SYSTEM, rerank_user
from agent.state import AgentState


def _retrieval_is_decisive(chunks: list[dict]) -> bool:
    """True when hybrid search already separated the winners clearly.

    The LLM rerank is an extra round trip on the critical path for EVERY grounded
    answer -- measured TTFT was 4-8s. When the top candidate's RRF score stands
    well clear of the one just past the cut, reordering changes nothing and the
    call is pure latency.

    Compares the best score against the first chunk that would be DROPPED
    (index rerank_top_k). If there is nothing to drop, ordering is moot.
    """
    k = settings.rerank_top_k
    if len(chunks) <= k:
        return True
    scores = [float(c.get("rrf_score") or 0.0) for c in chunks]
    top = max(scores)
    if top <= 0:
        return False                      # no usable signal -> let the LLM decide
    first_dropped = scores[k] if k < len(scores) else 0.0
    return (top - first_dropped) >= settings.rerank_skip_score_gap * top


async def rerank(state: AgentState) -> dict:
    chunks = state.retrieved_chunks
    if not chunks:
        return {"reranked_chunks": [], "low_confidence": True}

    # Fast path: skip the LLM entirely when retrieval already decided.
    if settings.rerank_skip_enabled and _retrieval_is_decisive(chunks):
        top = chunks[: settings.rerank_top_k]
        log.info("rerank_skipped", candidates=len(chunks), kept=len(top))
        return {"reranked_chunks": top, "low_confidence": len(top) == 0}

    query = state.standalone_query or state.query
    scored = await _llm_scores(query, chunks)

    if scored is None:
        # Fallback: keep RRF order, no confidence signal from rerank.
        top = chunks[: settings.rerank_top_k]
        return {"reranked_chunks": top, "low_confidence": len(top) == 0}

    by_id = {c["chunk_id"]: c for c in chunks}
    for cid, score in scored.items():
        if cid in by_id:
            by_id[cid]["rerank_score"] = score

    ranked = sorted(
        chunks, key=lambda c: c.get("rerank_score", 0.0), reverse=True
    )
    relevant = [c for c in ranked if c.get("rerank_score", 0.0) >= settings.rerank_min_score]
    top = (relevant or ranked)[: settings.rerank_top_k]

    low_confidence = len(relevant) == 0
    return {"reranked_chunks": top, "low_confidence": low_confidence}


async def _llm_scores(query: str, chunks: list[dict]) -> dict[str, float] | None:
    try:
        result = await llm.complete_json(
            model=settings.rerank_model,
            system=RERANK_SYSTEM,
            user=rerank_user(query, chunks),
            reasoning_effort=settings.small_model_reasoning_effort,
        )
        out: dict[str, float] = {}
        for item in result.get("scores", []):
            cid = str(item.get("id"))
            try:
                out[cid] = float(item.get("score", 0))
            except (TypeError, ValueError):
                continue
        return out or None
    except Exception as exc:
        # Fall back to RRF order, but log: silent rerank loss degrades answer quality.
        log.warning("rerank_scoring_failed", error=str(exc))
        return None
