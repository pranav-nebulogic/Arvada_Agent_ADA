"""
api.py
======
FastAPI surface for the Arvada agent.

Endpoints:
  POST /chat/stream  — SSE stream (token / tool_call / citation / warning / escalate / done / error),
                       terminated by `data: [DONE]`. Mirrors the reference React chat contract.
  POST /chat         — non-streaming convenience endpoint (full graph).
  GET  /health       — model + KB + Redis readiness.
  GET  /kb-search    — lightweight article search (no LLM) for the keyword launchpad.

Security: every request must carry `X-Internal-Token` matching INTERNAL_SERVICE_TOKEN
(so only the trusted Spring proxy can call it). Per-session rate limiting.
"""
from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import db
from brand import scrub
from config import settings
from agent import llm
from agent.obs import log
from agent.graph import get_graph, run_prep
from agent.nodes.cache import write_cache
from agent.nodes.judge import judge
from agent.nodes.generate import astream_answer, citations_for
from agent.state import AgentState

app = FastAPI(title="Arvada RAG Agent", version="0.2.0")

# Permissive CORS for local dev/Postman; in production the Spring proxy calls server-side.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_BODY_BYTES = 256 * 1024  # 256 KB request cap (abuse guard)


@app.on_event("startup")
async def _on_startup() -> None:
    # Ensure feature tables exist (best-effort; never blocks boot).
    await db.ensure_views_table()
    await db.ensure_feedback_table()


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body too large"}, status_code=413)
    return await call_next(request)


# ── Dev console UI ─────────────────────────────────────────────────────────────
UI_DIR = Path(__file__).parent / "ui"
UI_INDEX = UI_DIR / "index.html"
UI_ARTICLE = UI_DIR / "article.html"
UI_ARTICLES = UI_DIR / "articles.html"
UI_FEEDBACK = UI_DIR / "feedback.html"


@app.get("/", include_in_schema=False)
async def ui_index():
    """Local dev chat console (animated mascot + streaming chat)."""
    if UI_INDEX.exists():
        return FileResponse(UI_INDEX, media_type="text/html")
    return JSONResponse({"detail": "UI not found"}, status_code=404)


@app.get("/article/{article_id}", include_in_schema=False)
async def ui_article(article_id: str):
    """In-app knowledge article page (rendered client-side from /api/article)."""
    if UI_ARTICLE.exists():
        return FileResponse(UI_ARTICLE, media_type="text/html")
    return JSONResponse({"detail": "article view not found"}, status_code=404)


@app.get("/articles", include_in_schema=False)
async def ui_articles():
    """Browsable knowledge-base index (rendered client-side from /api/articles)."""
    if UI_ARTICLES.exists():
        return FileResponse(UI_ARTICLES, media_type="text/html")
    return JSONResponse({"detail": "articles view not found"}, status_code=404)


@app.get("/feedback", include_in_schema=False)
async def ui_feedback():
    """Admin Feedback List (Feedback SR queue), rendered from /api/feedback."""
    if UI_FEEDBACK.exists():
        return FileResponse(UI_FEEDBACK, media_type="text/html")
    return JSONResponse({"detail": "feedback view not found"}, status_code=404)


# ── Request models ────────────────────────────────────────────────────────────
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list)
    lang: str = "en"
    city_id: str | None = None
    session_id: str | None = None
    permit_type: str | None = None


class FeedbackRequest(BaseModel):
    rating: str                       # 'good' | 'bad'
    reason: str | None = None
    comment: str | None = None


class TtsRequest(BaseModel):
    text: str
    voice: str = "nova"


class ArticleEditRequest(BaseModel):
    title: str | None = None
    summary: str | None = None
    content: dict | None = None
    publish: bool = True


# ── Auth ──────────────────────────────────────────────────────────────────────
async def verify_token(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != settings.internal_service_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-Internal-Token")


@app.get("/api/article/{article_id}", dependencies=[Depends(verify_token)])
async def api_article(article_id: str, request: Request):
    """Structured article content for the in-app viewer."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    article = await db.fetch_article(city_id, article_id)
    if not article:
        raise HTTPException(status_code=404, detail="article not found")
    return article


@app.get("/api/article/{article_id}/related", dependencies=[Depends(verify_token)])
async def api_article_related(article_id: str, request: Request, k: int = 4):
    """Semantically-related articles for the in-app viewer (vector neighbors)."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    k = max(1, min(k, 12))
    articles = await db.related_articles(city_id, article_id, limit=k)
    return {"articles": articles}


@app.post("/api/article/{article_id}/view", dependencies=[Depends(verify_token)])
async def api_article_view(article_id: str, request: Request):
    """Record an article view (powers the Smart Knowledge 'Trending' list)."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    await db.record_view(city_id, article_id)
    return {"ok": True}


@app.post("/api/article/{article_id}/feedback", dependencies=[Depends(verify_token)])
async def api_article_feedback(article_id: str, req: FeedbackRequest, request: Request):
    """Record thumbs up/down. A thumbs-down becomes a Feedback SR for admin review."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    article = await db.fetch_article(city_id, article_id)
    title = article["title"] if article else None
    res = await db.record_feedback(
        city_id, article_id if article else None, title, req.rating, req.reason, req.comment
    )
    return res


@app.put("/api/article/{article_id}", dependencies=[Depends(verify_token)])
async def api_article_edit(article_id: str, req: ArticleEditRequest, request: Request):
    """Admin edit: patch the article and (by default) publish it."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    updated = await db.update_article(
        city_id, article_id, title=req.title, summary=req.summary,
        content=req.content, publish=req.publish,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="article not found")
    return updated


@app.get("/api/feedback", dependencies=[Depends(verify_token)])
async def api_feedback_list(request: Request, rating: str | None = None):
    """Feedback SR queue for the admin Feedback List."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    items = await db.list_feedback(city_id, rating=rating)
    return {"count": len(items), "items": items}


@app.get("/api/feedback/{feedback_id}", dependencies=[Depends(verify_token)])
async def api_feedback_get(feedback_id: int, request: Request):
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    fb = await db.get_feedback(city_id, feedback_id)
    if not fb:
        raise HTTPException(status_code=404, detail="feedback not found")
    return fb


@app.get("/api/nav", dependencies=[Depends(verify_token)])
async def api_nav(request: Request):
    """Lightweight article list for the article-page left nav (no ordinances)."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    return {"articles": await db.nav_articles(city_id)}


@app.get("/api/articles/trending", dependencies=[Depends(verify_token)])
async def api_articles_trending(request: Request, k: int = 12, days: int = 30):
    """Most-viewed articles over a recent window (Smart Knowledge 'Trending')."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    k = max(1, min(k, 60))
    days = max(1, min(days, 365))
    articles = await db.trending_articles(city_id, limit=k, days=days)
    return {"count": len(articles), "articles": articles}


@app.get("/api/articles", dependencies=[Depends(verify_token)])
async def api_articles(
    request: Request,
    q: str | None = None,
    category: str | None = None,
    article_type: str | None = None,
):
    """Lightweight, filterable article index for the knowledge-base browser."""
    city_id = request.headers.get("X-Tenant-Id") or settings.default_city_id
    articles = await db.list_articles(city_id, q=q, category=category, article_type=article_type)
    return {"count": len(articles), "articles": articles}


# ── Rate limiting (in-memory sliding window, per session/IP) ────────────────────
_hits: dict[str, deque[float]] = defaultdict(deque)


def _rate_limit(key: str) -> None:
    now = time.monotonic()
    window = 60.0
    bucket = _hits[key]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        raise HTTPException(status_code=429, detail="rate limit exceeded; slow down")
    bucket.append(now)


def _build_state(req: ChatRequest, request: Request) -> AgentState:
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages[] is required")

    # Trim (don't reject) overly long threads: keep the most recent messages so a
    # long-running conversation still works. contextualize only uses the last few
    # turns anyway, so dropping the oldest is safe and avoids a hard 413 mid-chat.
    messages = req.messages
    if len(messages) > settings.max_request_messages:
        messages = messages[-settings.max_request_messages:]

    last_user = next((m for m in reversed(messages) if m.role == "user"), None)
    if last_user is None:
        raise HTTPException(status_code=400, detail="no user message found")

    session_id = req.session_id or str(uuid.uuid4())
    rl_key = req.session_id or (request.client.host if request.client else "anon")
    _rate_limit(rl_key)

    return AgentState(
        city_id=req.city_id or settings.default_city_id,
        session_id=session_id,
        query=last_user.content,
        lang=req.lang or "en",
        permit_type=req.permit_type,
        messages=[m.model_dump() for m in messages],
    )


# ── SSE helpers ─────────────────────────────────────────────────────────────────
def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _chunk_text(text: str, size: int = 24):
    """Chunk a cached answer into word-ish pieces so the client still 'types' it."""
    words = text.split(" ")
    buf = ""
    for w in words:
        buf = f"{buf} {w}" if buf else w
        if len(buf) >= size:
            yield buf + " "
            buf = ""
    if buf:
        yield buf


async def _scrubbed_stream(agen):
    """Wrap a token stream so competitor names are scrubbed as it flows.

    Flushes only complete words (up to the last space) so a name can't be split
    across SSE deltas; the trailing partial word is held until the next delta.
    """
    buf = ""
    async for delta in agen:
        buf += delta
        idx = buf.rfind(" ")
        if idx >= 0:
            out = scrub(buf[: idx + 1])
            buf = buf[idx + 1 :]
            if out:
                yield out
    if buf:
        yield scrub(buf)


def _format_cards(cards: list[dict]) -> list[dict]:
    """Shape citation cards for the wire.

    `href` points at the in-app knowledge article page (rendered from the content
    we scraped) rather than redirecting to the external city website. The original
    source URL is still passed as `source` so the article page can link out to it.
    """
    out = []
    for i, c in enumerate(cards, 1):
        n = c.get("n") or i
        article_id = c.get("article_id")
        href = f"/article/{article_id}" if article_id else c.get("url")
        out.append(
            {
                "id": str(n),
                "n": n,
                "article_id": str(article_id) if article_id else None,
                "title": scrub(c.get("title")),
                "href": href,
                "source": c.get("url"),
                "effective_date": c.get("effective_date"),
                "section_type": c.get("section_type"),
                "permit_type": c.get("permit_type"),
            }
        )
    return out


# ── /chat/stream ────────────────────────────────────────────────────────────────
@app.post("/chat/stream", dependencies=[Depends(verify_token)])
async def chat_stream(req: ChatRequest, request: Request):
    state = _build_state(req, request)

    debug_timing = settings.expose_timing or request.query_params.get("debug") == "1"

    def _timing(prepped) -> dict | None:
        return prepped.meta.get("timing") if debug_timing else None

    async def event_gen():
        try:
            prepped = await run_prep(state)

            # ── Semantic cache hit: stream the cached answer, skip the LLM ──────
            if prepped.cache_hit and prepped.cached_answer:
                cached_answer = scrub(prepped.cached_answer)
                for piece in _chunk_text(cached_answer):
                    yield _sse({"type": "token", "content": piece})
                citations = _format_cards(prepped.citations or [])
                if citations:
                    yield _sse({"type": "citation", "citations": citations})
                yield _sse(
                    {
                        "type": "done",
                        "citations": citations,
                        "intent": prepped.intent or "kb",
                        "answer": cached_answer,
                        "cached": True,
                        "timing": _timing(prepped),
                    }
                )
                return

            # ── Conversational turn: persona answer, no retrieval/judge/cache ────
            if prepped.intent == "CONVERSE":
                answer_parts: list[str] = []
                async for delta in _scrubbed_stream(astream_answer(prepped)):
                    answer_parts.append(delta)
                    yield _sse({"type": "token", "content": delta})
                yield _sse(
                    {
                        "type": "done",
                        "citations": [],
                        "intent": "CONVERSE",
                        "answer": "".join(answer_parts),
                        "timing": _timing(prepped),
                    }
                )
                return

            # ── Escalation: deterministic answer, no LLM ────────────────────────
            if prepped.intent == "ESCALATE":
                yield _sse(
                    {
                        "type": "escalate",
                        "ticket_id": prepped.escalation_ticket_id,
                        "department": prepped.meta.get("escalation_dept"),
                        "contact": prepped.meta.get("escalation_contact"),
                    }
                )
                for piece in _chunk_text(scrub(prepped.answer or "")):
                    yield _sse({"type": "token", "content": piece})
                yield _sse(
                    {
                        "type": "done",
                        "citations": [],
                        "intent": "ESCALATE",
                        "answer": scrub(prepped.answer or ""),
                        "escalation_ticket_id": prepped.escalation_ticket_id,
                        "timing": _timing(prepped),
                    }
                )
                return

            # ── Tool-call signals for fee / status / changes / plan / weak retrieval ──
            if prepped.fee_result and not prepped.fee_result.get("no_schedule"):
                yield _sse({"type": "tool_call", "name": "fee_calculator", "status": "calculating"})
            elif prepped.status_result is not None:
                yield _sse({"type": "tool_call", "name": "status_lookup", "status": "checking"})
            elif prepped.changes_result is not None:
                yield _sse({"type": "tool_call", "name": "change_tracker", "status": "checking"})
            elif prepped.project_plan is not None:
                yield _sse({"type": "tool_call", "name": "project_planner", "status": "planning"})
            elif prepped.low_confidence or not prepped.reranked_chunks:
                yield _sse({"type": "tool_call", "name": "search", "status": "no_strong_match"})

            raw_cards = citations_for(prepped)
            citations = _format_cards(raw_cards)

            answer_parts: list[str] = []
            async for delta in _scrubbed_stream(astream_answer(prepped)):
                answer_parts.append(delta)
                yield _sse({"type": "token", "content": delta})

            if citations:
                yield _sse({"type": "citation", "citations": citations})

            # ── Grounding check (RAG answers only) -> optional warning event ────
            full_answer = "".join(answer_parts)
            # Cache the RAW cards (formatted at the edge), keeping cache/API consistent.
            final_state = prepped.model_copy(update={"answer": full_answer, "citations": raw_cards})
            verdict = await judge(final_state)
            final_state = final_state.model_copy(update=verdict)
            if verdict.get("warning"):
                yield _sse({"type": "warning", "message": verdict["warning"]})

            yield _sse(
                {
                    "type": "done",
                    "citations": citations,
                    "intent": prepped.intent or "kb",
                    "answer": full_answer,
                    "warning": final_state.warning,
                    "timing": _timing(prepped),
                }
            )

            # ── Write confident, cacheable answers to the semantic cache ────────
            await write_cache(final_state)
        except Exception as exc:  # noqa: BLE001
            log.error("chat_stream_failed", error=str(exc), session_id=state.session_id)
            yield _sse({"type": "error", "message": f"agent error: {exc}"})
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── /chat (non-streaming) ─────────────────────────────────────────────────────
@app.post("/chat", dependencies=[Depends(verify_token)])
async def chat(req: ChatRequest, request: Request):
    state = _build_state(req, request)
    graph = get_graph()
    result = await graph.ainvoke(state)
    get = (lambda f: result.get(f)) if isinstance(result, dict) else (lambda f: getattr(result, f, None))

    # The generate/idk/cache nodes all populate state.citations with raw cards;
    # we only shape them for the wire here.
    return {
        "reply": scrub(get("answer") or ""),
        "citations": _format_cards(get("citations") or []),
        "intent": get("intent") or "kb",
    }


# ── /health ────────────────────────────────────────────────────────────────────
@app.post("/tts", dependencies=[Depends(verify_token)])
async def tts(req: TtsRequest):
    """Text-to-speech (OpenAI) for the chat 'Listen' button — streamed audio/mpeg."""
    text = scrub((req.text or "").strip())[:4000]
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    voice = req.voice if req.voice in {"alloy", "echo", "fable", "onyx", "nova", "shimmer"} else "nova"

    async def audio_gen():
        async with llm.client.audio.speech.with_streaming_response.create(
            model="tts-1", voice=voice, input=text, response_format="mp3",
        ) as response:
            async for chunk in response.iter_bytes():
                yield chunk

    return StreamingResponse(
        audio_gen(), media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    db_status = await db.health_check()
    redis_status = {"ok": False}
    try:
        from agent.redis_store import ping_redis

        redis_status = await ping_redis()
    except Exception as exc:  # noqa: BLE001 — redis_store not present until Phase 2C
        redis_status = {"ok": False, "error": str(exc)}

    from agent import tracing

    return {
        "ok": db_status.get("ok", False),
        "hasKey": bool(settings.openai_api_key),
        "model": {"fast": settings.generation_model_fast, "deep": settings.generation_model_deep},
        "kbRetrieval": {"source": "postgres", "table": "article_chunks", "chunks": db_status.get("chunks")},
        "redis": redis_status,
        "tracing": tracing.status(),
    }


# ── /ready (readiness probe for orchestrators) ──────────────────────────────────
@app.get("/ready")
async def ready():
    db_status = await db.health_check()
    redis_ok = False
    try:
        from agent.redis_store import ping_redis

        redis_ok = (await ping_redis()).get("ok", False)
    except Exception:
        redis_ok = False

    checks = {
        "db": db_status.get("ok", False),
        "redis": redis_ok,
        "openai_key": bool(settings.openai_api_key),
    }
    ready = all(checks.values())
    return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)


# ── /kb-search ─────────────────────────────────────────────────────────────────
@app.get("/kb-search", dependencies=[Depends(verify_token)])
async def kb_search(q: str, k: int = 3, city_id: str | None = None):
    if not q.strip():
        return JSONResponse({"articles": []})
    try:
        embedding = await llm.embed(q)
        chunks = await db.hybrid_search(
            city_id=city_id or settings.default_city_id,
            query=q,
            embedding=embedding,
            limit=max(k * 3, 6),
        )
        seen: set = set()
        articles = []
        for c in chunks:
            aid = c["article_id"]
            if aid in seen:
                continue
            seen.add(aid)
            articles.append(
                {
                    "answerId": aid,
                    "title": c.get("article_title"),
                    "href": c.get("source_url"),
                }
            )
            if len(articles) >= k:
                break
        return JSONResponse({"articles": articles})
    except Exception:
        return JSONResponse({"articles": []})
