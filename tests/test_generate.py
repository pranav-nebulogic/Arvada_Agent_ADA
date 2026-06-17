"""generate.build_messages mode selection + citation dedupe + tiered model."""
from agent.nodes.generate import build_messages, build_citations, citations_for, pick_generation_model
from agent.state import AgentState
from config import settings


def _state(**kw) -> AgentState:
    base = dict(city_id="arvada-co", session_id="s1", query="q")
    base.update(kw)
    return AgentState(**base)


def test_mode_fee():
    _, mode = build_messages(_state(fee_result={"total": 60, "line_items": []}))
    assert mode == "fee"


def test_mode_status():
    _, mode = build_messages(_state(status_result={"available": False, "found": False}))
    assert mode == "status"


def test_mode_idk_when_low_confidence():
    _, mode = build_messages(_state(low_confidence=True))
    assert mode == "idk"


def test_mode_grounded():
    chunks = [{"content_text": "txt", "section_type": "fees", "article_title": "T", "source_url": "u"}]
    _, mode = build_messages(_state(reranked_chunks=chunks))
    assert mode == "grounded"


def test_fee_no_schedule_does_not_force_fee_mode():
    # no_schedule fee result should NOT be presented as a fee answer
    chunks = [{"content_text": "txt", "section_type": "fees", "article_title": "T", "source_url": "u"}]
    _, mode = build_messages(_state(fee_result={"no_schedule": True}, reranked_chunks=chunks))
    assert mode == "grounded"


def test_build_citations_dedupes_by_title_and_url():
    chunks = [
        {"article_title": "A", "source_url": "u1", "section_type": "fees"},
        {"article_title": "A", "source_url": "u1", "section_type": "steps"},  # dup
        {"article_title": "B", "source_url": "u2", "section_type": "faq"},
    ]
    cites = build_citations(chunks)
    assert len(cites) == 2
    assert {c["title"] for c in cites} == {"A", "B"}


def test_mode_converse_takes_priority():
    # Even with chunks present, a CONVERSE intent never goes to retrieval.
    chunks = [{"content_text": "txt", "section_type": "fees", "article_title": "T", "source_url": "u"}]
    _, mode = build_messages(_state(intent="CONVERSE", reranked_chunks=chunks))
    assert mode == "converse"


def test_converse_has_no_citations():
    chunks = [{"article_title": "A", "source_url": "u1", "section_type": "fees"}]
    assert citations_for(_state(intent="CONVERSE", reranked_chunks=chunks)) == []


def test_pick_model_complex_uses_deep():
    model, _ = pick_generation_model(_state(complexity="complex"))
    assert model == settings.generation_model_deep


def test_pick_model_simple_uses_fast():
    model, _ = pick_generation_model(_state(complexity="simple", query="hi"))
    assert model == settings.generation_model_fast


def test_pick_model_ordinance_top_chunk_uses_deep():
    chunks = [{"content_text": "t", "section_type": "ordinance", "article_title": "Code", "source_url": "u"}]
    model, _ = pick_generation_model(_state(complexity="simple", reranked_chunks=chunks))
    assert model == settings.generation_model_deep


def test_pick_model_long_multipart_uses_deep():
    q = "Do I need a permit and how much and what documents and how long? " * 5
    model, _ = pick_generation_model(_state(complexity="simple", query=q))
    assert model == settings.generation_model_deep
