"""
retrieve node
=============
Runs hybrid_search (RRF of tsvector FTS + pgvector ANN) for the standalone query,
scoped to the tenant via RLS. Returns the top-K candidate chunks.
"""
from __future__ import annotations

import db
from config import settings
from agent.state import AgentState


async def retrieve(state: AgentState) -> dict:
    query = state.standalone_query or state.query
    embedding = state.query_embedding
    if embedding is None:
        from agent import llm

        embedding = await llm.embed(query)

    permit_type = state.entities.get("permit_type") or state.permit_type

    chunks = await db.hybrid_search(
        city_id=state.city_id,
        query=query,
        embedding=embedding,
        permit_type=permit_type,
        limit=settings.retrieval_top_k,
    )
    return {"retrieved_chunks": chunks, "query_embedding": embedding}
