"""
retrieve node
=============
Runs hybrid_search (RRF of tsvector FTS + pgvector ANN) for the standalone query,
scoped to the tenant via RLS. Returns the top-K candidate chunks.
"""
from __future__ import annotations

import db
from config import settings
from agent.obs import log
from agent.state import AgentState


async def retrieve(state: AgentState) -> dict:
    query = state.standalone_query or state.query
    embedding = state.query_embedding
    if embedding is None:
        from agent import llm

        embedding = await llm.embed(query)

    permit_type = state.entities.get("permit_type") or state.permit_type

    # Reuse the search that ran alongside intent -- but ONLY when no permit_type
    # was extracted, because the prefetch is unfiltered. The filter is not a
    # narrowing of the unfiltered ranking: measured, the filtered top-10 for
    # "what permits do I need for a deck" shares 0 of 10 chunks with the
    # unfiltered top-100. So with a permit_type we must run the real query, and
    # the prefetch is simply discarded.
    if state.prefetched_chunks is not None and not permit_type:
        log.info("retrieve_used_prefetch", chunks=len(state.prefetched_chunks))
        return {"retrieved_chunks": state.prefetched_chunks, "query_embedding": embedding}

    chunks = await db.hybrid_search(
        city_id=state.city_id,
        query=query,
        embedding=embedding,
        permit_type=permit_type,
        limit=settings.retrieval_top_k,
    )
    return {"retrieved_chunks": chunks, "query_embedding": embedding}
