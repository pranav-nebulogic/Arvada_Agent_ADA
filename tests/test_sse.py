"""SSE framing + cached-answer chunking."""
import json

from api import _sse, _chunk_text, _format_cards
from agent.nodes.generate import build_citations


def test_sse_framing():
    out = _sse({"type": "token", "content": "hi"})
    assert out.startswith("data: ")
    assert out.endswith("\n\n")
    payload = json.loads(out[len("data: "):].strip())
    assert payload == {"type": "token", "content": "hi"}


def test_sse_unicode_preserved():
    out = _sse({"type": "token", "content": "café ñ 日本"})
    payload = json.loads(out[len("data: "):].strip())
    assert payload["content"] == "café ñ 日本"


def test_chunk_text_reconstructs_words():
    text = "The City of Arvada food truck permit costs sixty dollars per year."
    pieces = list(_chunk_text(text, size=12))
    assert len(pieces) > 1
    assert "".join(pieces).split() == text.split()


def test_citations_payload_numbering():
    chunks = [
        {"article_title": "A", "source_url": "u1", "section_type": "fees", "permit_type": "x", "effective_date": "2026-01-01"},
        {"article_title": "B", "source_url": "u2", "section_type": "faq", "permit_type": None, "effective_date": None},
    ]
    cites = _format_cards(build_citations(chunks))
    assert [c["n"] for c in cites] == [1, 2]
    # No article_id -> href falls back to the external source url.
    assert cites[0]["href"] == "u1"


def test_format_cards_prefers_internal_article_link():
    chunks = [{"article_id": "abc-123", "article_title": "A", "source_url": "u1", "section_type": "fees"}]
    cites = _format_cards(build_citations(chunks))
    assert cites[0]["href"] == "/article/abc-123"
    assert cites[0]["article_id"] == "abc-123"
