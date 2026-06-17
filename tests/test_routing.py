"""Graph routing functions + confidence gate — pure logic."""
from agent.graph import _route_cache, _route_intent, _route_fee, _route_confidence
from agent.state import AgentState


def _state(**kw) -> AgentState:
    base = dict(city_id="arvada-co", session_id="s1", query="q")
    base.update(kw)
    return AgentState(**base)


def test_route_cache():
    assert _route_cache(_state(cache_hit=True)) == "hit"
    assert _route_cache(_state(cache_hit=False)) == "miss"


def test_route_intent_mapping():
    assert _route_intent(_state(intent="FEE_CALC")) == "fee_engine"
    assert _route_intent(_state(intent="STATUS_LOOKUP")) == "status_lookup"
    assert _route_intent(_state(intent="WHATS_CHANGED")) == "whats_changed"
    assert _route_intent(_state(intent="MULTI_PROJECT")) == "multi_project"
    assert _route_intent(_state(intent="ESCALATE")) == "escalate"
    assert _route_intent(_state(intent="CONVERSE")) == "converse"
    assert _route_intent(_state(intent="FAQ")) == "retrieve"
    assert _route_intent(_state(intent="DOC_CHECKLIST")) == "retrieve"
    assert _route_intent(_state(intent=None)) == "retrieve"


def test_route_fee_falls_back_when_no_schedule():
    assert _route_fee(_state(fee_result={"no_schedule": True})) == "retrieve"
    assert _route_fee(_state(fee_result={"total": 60})) == "generate"


def test_confidence_gate():
    assert _route_confidence(_state(low_confidence=True, reranked_chunks=[{"x": 1}])) == "idk"
    assert _route_confidence(_state(low_confidence=False, reranked_chunks=[])) == "idk"
    assert _route_confidence(_state(low_confidence=False, reranked_chunks=[{"x": 1}])) == "generate"
