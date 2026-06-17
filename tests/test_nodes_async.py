"""Async node logic with the LLM monkeypatched (no live calls)."""
import pytest

from agent import llm
from agent.nodes.intent import intent
from agent.nodes.judge import judge, _should_judge
from agent.nodes.cache import is_cacheable
from agent.nodes.converse import converse
from agent.state import AgentState


def _state(**kw) -> AgentState:
    base = dict(city_id="arvada-co", session_id="s1", query="q")
    base.update(kw)
    return AgentState(**base)


async def test_intent_parses_and_normalizes(monkeypatch):
    async def fake_json(**kwargs):
        return {"intent": "FEE_CALC", "entities": {"permit_type": "solar", "fee_valuation": "$25,000"}}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    out = await intent(_state(standalone_query="how much is a solar permit for $25,000"))
    assert out["intent"] == "FEE_CALC"
    assert out["entities"]["permit_type"] == "building_permit_solar"
    assert out["entities"]["fee_valuation"] == 25000.0


async def test_intent_invalid_falls_back_to_faq(monkeypatch):
    async def fake_json(**kwargs):
        return {"intent": "NONSENSE", "entities": {}}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    out = await intent(_state())
    assert out["intent"] == "FAQ"
    assert out["complexity"] == "simple"


async def test_intent_classifies_converse_and_complexity(monkeypatch):
    async def fake_json(**kwargs):
        return {"intent": "CONVERSE", "complexity": "complex", "entities": {}}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    out = await intent(_state(query="what can you do?"))
    assert out["intent"] == "CONVERSE"
    assert out["complexity"] == "complex"


async def test_intent_bad_complexity_defaults_simple(monkeypatch):
    async def fake_json(**kwargs):
        return {"intent": "FAQ", "complexity": "weird", "entities": {}}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    out = await intent(_state())
    assert out["complexity"] == "simple"


async def test_converse_node_returns_answer_no_citations(monkeypatch):
    async def fake_stream(**kwargs):
        for tok in ["Hi! ", "I'm Ada."]:
            yield tok

    monkeypatch.setattr(llm, "stream_chat", fake_stream)
    out = await converse(_state(intent="CONVERSE", query="hi"))
    assert out["answer"] == "Hi! I'm Ada."
    assert out["citations"] == []


async def test_converse_node_falls_back_on_error(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("api down")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "stream_chat", boom)
    out = await converse(_state(intent="CONVERSE", query="hi"))
    assert "Ada" in out["answer"]
    assert out["citations"] == []


async def test_intent_exception_falls_back(monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(llm, "complete_json", boom)
    out = await intent(_state())
    assert out["intent"] == "FAQ"


def test_should_judge_only_grounded():
    chunks = [{"content_text": "t", "section_type": "fees", "article_title": "A", "source_url": "u"}]
    assert _should_judge(_state(answer="a", reranked_chunks=chunks)) is True
    assert _should_judge(_state(answer="a", fee_result={"total": 1})) is False
    assert _should_judge(_state(answer="a", status_result={})) is False
    assert _should_judge(_state(answer="a", intent="ESCALATE")) is False
    assert _should_judge(_state(answer="a", low_confidence=True)) is False
    assert _should_judge(_state(answer=None)) is False


async def test_judge_flags_ungrounded(monkeypatch):
    async def fake_json(**kwargs):
        return {"grounded": False, "reason": "fee not in context", "unsupported": ["$999"]}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    chunks = [{"content_text": "t", "section_type": "fees", "article_title": "A", "source_url": "u"}]
    out = await judge(_state(answer="It costs $999", reranked_chunks=chunks))
    assert out["judge_passed"] is False
    assert out["warning"]


async def test_judge_passes_when_grounded(monkeypatch):
    async def fake_json(**kwargs):
        return {"grounded": True}

    monkeypatch.setattr(llm, "complete_json", fake_json)
    chunks = [{"content_text": "t", "section_type": "fees", "article_title": "A", "source_url": "u"}]
    out = await judge(_state(answer="ok", reranked_chunks=chunks))
    assert out["judge_passed"] is True


def test_is_cacheable_rules():
    chunks = [{"content_text": "t"}]
    assert is_cacheable(_state(answer="a", reranked_chunks=chunks, intent="FAQ")) is True
    assert is_cacheable(_state(answer="a", intent="FEE_CALC")) is False
    assert is_cacheable(_state(answer="a", intent="STATUS_LOOKUP")) is False
    assert is_cacheable(_state(answer="a", low_confidence=True)) is False
    assert is_cacheable(_state(answer="", intent="FAQ")) is False
    assert is_cacheable(_state(answer="a", cache_hit=True)) is False
    assert is_cacheable(_state(answer="a", intent="CONVERSE")) is False
