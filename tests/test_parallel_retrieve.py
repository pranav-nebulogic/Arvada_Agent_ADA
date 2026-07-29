"""
Tests for the speculative intent||retrieve overlap.

Measured on production before building this:
  * intent ~1.3s and retrieve ~1.5s run back-to-back, so the classifier's
    latency is paid twice over.
  * only 3 of 12 sample queries produce a permit_type at all.
  * BUT where one exists the filter is NOT a narrowing of the unfiltered
    ranking -- the filtered top-10 for "what permits do I need for a deck"
    shares 0 of 10 chunks with the unfiltered top-100.

That last point is why the prefetch is discarded whenever a permit_type exists,
and it is the property these tests defend. Getting it wrong silently changes
which sources the answer cites.
"""
from __future__ import annotations

import pytest

from config import settings
from agent.nodes.retrieve import retrieve
from agent.state import AgentState

FILTERED = [{"chunk_id": f"f{i}", "content_text": "filtered"} for i in range(10)]
UNFILTERED = [{"chunk_id": f"u{i}", "content_text": "unfiltered"} for i in range(10)]


@pytest.fixture
def spy(monkeypatch):
    """Stub hybrid_search; records whether it ran and with what filter."""
    calls = []

    async def fake(city_id, query, embedding, permit_type=None, limit=10):
        calls.append(permit_type)
        return FILTERED if permit_type else UNFILTERED

    monkeypatch.setattr("agent.nodes.retrieve.db.hybrid_search", fake)
    return calls


def state(**kw) -> AgentState:
    base = dict(query="q", city_id="woodinville-wa", session_id="t",
                query_embedding=[0.0] * 8)
    base.update(kw)
    return AgentState(**base)


class TestPrefetchReuse:
    @pytest.mark.asyncio
    async def test_prefetch_used_when_no_permit_type(self, spy):
        out = await retrieve(state(prefetched_chunks=UNFILTERED, entities={}))
        assert out["retrieved_chunks"] == UNFILTERED
        assert spy == [], "should not hit the DB again -- that's the whole saving"

    @pytest.mark.asyncio
    async def test_prefetch_discarded_when_permit_type_present(self, spy):
        # The filtered result is NOT a subset of the unfiltered one, so reusing
        # the prefetch here would silently change the cited sources.
        out = await retrieve(state(prefetched_chunks=UNFILTERED,
                                   entities={"permit_type": "building_permit"}))
        assert out["retrieved_chunks"] == FILTERED
        assert spy == ["building_permit"]

    @pytest.mark.asyncio
    async def test_permit_type_from_state_also_discards(self, spy):
        out = await retrieve(state(prefetched_chunks=UNFILTERED,
                                   permit_type="special_event_permit", entities={}))
        assert out["retrieved_chunks"] == FILTERED

    @pytest.mark.asyncio
    async def test_no_prefetch_behaves_exactly_as_before(self, spy):
        out = await retrieve(state(entities={}))
        assert out["retrieved_chunks"] == UNFILTERED
        assert spy == [None]

    @pytest.mark.asyncio
    async def test_empty_prefetch_is_not_treated_as_absent(self, spy):
        # [] means "the prefetch ran and found nothing", which is a real answer.
        # None means "no prefetch happened". Conflating them re-queries needlessly.
        out = await retrieve(state(prefetched_chunks=[], entities={}))
        assert out["retrieved_chunks"] == []
        assert spy == []


class TestIntentNode:
    @pytest.mark.asyncio
    async def test_flag_off_does_not_prefetch(self, monkeypatch):
        from agent.nodes import intent as intent_mod
        ran = {"prefetch": False}

        async def no_prefetch(_s):
            ran["prefetch"] = True
            return UNFILTERED

        async def classify(_s):
            return {"intent": "FAQ", "entities": {}, "complexity": "simple"}

        monkeypatch.setattr(settings, "parallel_intent_retrieve", False)
        monkeypatch.setattr(intent_mod, "_prefetch_unfiltered", no_prefetch)
        monkeypatch.setattr(intent_mod, "_classify", classify)
        out = await intent_mod.intent(state())
        assert ran["prefetch"] is False
        assert "prefetched_chunks" not in out

    @pytest.mark.asyncio
    async def test_flag_on_attaches_prefetch(self, monkeypatch):
        from agent.nodes import intent as intent_mod

        async def prefetch(_s):
            return UNFILTERED

        async def classify(_s):
            return {"intent": "FAQ", "entities": {}, "complexity": "simple"}

        monkeypatch.setattr(settings, "parallel_intent_retrieve", True)
        monkeypatch.setattr(intent_mod, "_prefetch_unfiltered", prefetch)
        monkeypatch.setattr(intent_mod, "_classify", classify)
        out = await intent_mod.intent(state())
        assert out["prefetched_chunks"] == UNFILTERED

    @pytest.mark.asyncio
    async def test_prefetch_failure_never_breaks_the_turn(self, monkeypatch):
        from agent.nodes import intent as intent_mod

        async def boom(_s):
            raise RuntimeError("db down")

        async def classify(_s):
            return {"intent": "FAQ", "entities": {}, "complexity": "simple"}

        monkeypatch.setattr(settings, "parallel_intent_retrieve", True)
        monkeypatch.setattr(intent_mod, "db", type("D", (), {"hybrid_search": boom})())
        monkeypatch.setattr(intent_mod, "_classify", classify)
        out = await intent_mod.intent(state())
        assert out["intent"] == "FAQ"
        assert "prefetched_chunks" not in out    # falls back to a normal retrieve
