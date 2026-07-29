"""
Tests for the rerank fast path (agent/nodes/rerank.py).

The LLM rerank sits in front of the first token on every grounded answer, so it
is skipped when hybrid search has already separated the winners. These tests pin
the two things that matter: the skip fires when the gap is genuinely decisive,
and it does NOT fire when the candidates are bunched (where reordering is the
whole point).
"""
from __future__ import annotations

import pytest

from config import settings
from agent.nodes.rerank import _retrieval_is_decisive, rerank
from agent.state import AgentState


def chunks(*scores: float) -> list[dict]:
    return [{"chunk_id": f"c{i}", "rrf_score": s, "content_text": "x"}
            for i, s in enumerate(scores)]


class TestDecisiveness:
    def test_clear_winner_is_decisive(self):
        # Top 1.0 vs first-dropped 0.2 -> gap 0.8 of top, well over 0.35.
        assert _retrieval_is_decisive(chunks(1.0, .9, .8, .7, .6, .2, .1)) is True

    def test_bunched_candidates_are_not_decisive(self):
        # Everything within a whisker: this is exactly what rerank is FOR.
        assert _retrieval_is_decisive(chunks(.52, .51, .50, .50, .49, .48, .47)) is False

    def test_fewer_candidates_than_the_cut_is_decisive(self):
        # Nothing would be dropped, so ordering cannot change the kept set.
        assert _retrieval_is_decisive(chunks(.4, .3)) is True

    def test_all_zero_scores_defer_to_the_llm(self):
        # No usable retrieval signal -> don't pretend it was decisive.
        assert _retrieval_is_decisive(chunks(*([0.0] * 8))) is False

    def test_missing_scores_do_not_crash(self):
        rows = [{"chunk_id": f"c{i}"} for i in range(8)]
        assert _retrieval_is_decisive(rows) is False


class TestRerankNode:
    @pytest.mark.asyncio
    async def test_decisive_retrieval_never_calls_the_llm(self, monkeypatch):
        called = False

        async def boom(*a, **k):
            nonlocal called
            called = True
            raise AssertionError("LLM rerank must not run on the fast path")

        monkeypatch.setattr("agent.nodes.rerank._llm_scores", boom)
        state = AgentState(query="deck permit", city_id="woodinville-wa",
                           session_id="t", retrieved_chunks=chunks(1.0, .9, .8, .7, .6, .1, .05))
        out = await rerank(state)
        assert called is False
        assert len(out["reranked_chunks"]) == settings.rerank_top_k
        assert out["low_confidence"] is False

    @pytest.mark.asyncio
    async def test_bunched_retrieval_still_uses_the_llm(self, monkeypatch):
        seen = {}

        async def fake(query, rows):
            seen["ran"] = True
            # Reverse the order to prove the LLM's ranking is what's honoured.
            return {r["chunk_id"]: float(i) for i, r in enumerate(rows)}

        monkeypatch.setattr("agent.nodes.rerank._llm_scores", fake)
        rows = chunks(.52, .51, .50, .50, .49, .48, .47)
        state = AgentState(query="deck permit", city_id="woodinville-wa",
                           session_id="t", retrieved_chunks=rows)
        out = await rerank(state)
        assert seen.get("ran") is True
        assert out["reranked_chunks"][0]["chunk_id"] == "c6"   # highest LLM score

    @pytest.mark.asyncio
    async def test_skip_can_be_disabled(self, monkeypatch):
        ran = {}

        async def fake(query, rows):
            ran["yes"] = True
            return {r["chunk_id"]: 5.0 for r in rows}

        monkeypatch.setattr("agent.nodes.rerank._llm_scores", fake)
        monkeypatch.setattr(settings, "rerank_skip_enabled", False)
        state = AgentState(query="q", city_id="woodinville-wa", session_id="t",
                           retrieved_chunks=chunks(1.0, .9, .8, .7, .6, .1, .05))
        await rerank(state)
        assert ran.get("yes") is True

    @pytest.mark.asyncio
    async def test_no_chunks_is_low_confidence(self):
        state = AgentState(query="q", city_id="woodinville-wa", session_id="t",
                           retrieved_chunks=[])
        out = await rerank(state)
        assert out["low_confidence"] is True
