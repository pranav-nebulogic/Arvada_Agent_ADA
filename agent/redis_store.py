"""
agent/redis_store.py
====================
Semantic cache ONLY (no server-side conversation memory — the client owns the
thread). Backed by redisvl's SemanticCache.

- Reuses the query embedding computed in `contextualize` (passed as `vector=`),
  so we never re-embed for cache lookups.
- Isolated per tenant + language via filterable tag fields, so Arvada's cache
  never serves another city's answer and an English answer never serves a
  Spanish turn.
- Skips caching low-confidence ("I don't know") and non-deterministic answers.

redisvl is synchronous; all calls are wrapped in asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
from typing import Any

import redis.asyncio as aioredis
from redisvl.extensions.cache.llm import SemanticCache
from redisvl.query.filter import Tag
from redisvl.utils.vectorize import OpenAITextVectorizer

from config import settings
from agent.obs import log

_cache: SemanticCache | None = None


def _get_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        # OpenAITextVectorizer fixes the index dims to 1536 (text-embedding-3-small)
        # and the cosine metric. We always pass `vector=` so it is not used to embed.
        vectorizer = OpenAITextVectorizer(
            model=settings.embedding_model,
            api_config={"api_key": settings.openai_api_key},
        )
        _cache = SemanticCache(
            name="arvada_semantic_cache",
            redis_url=settings.redis_url,
            distance_threshold=round(1.0 - settings.semantic_cache_threshold, 4),
            ttl=settings.semantic_cache_ttl_seconds,
            vectorizer=vectorizer,
            # Without these a Redis that ACCEPTS the socket and then never
            # answers hangs the call forever -- and since the cache is the FIRST
            # node in the graph, every turn hangs with it. A refused connection
            # already degrades gracefully; an unresponsive one did not. Observed
            # locally against a half-open listener on 6379: the graph stalled
            # indefinitely with no log line.
            connection_kwargs={
                "socket_connect_timeout": settings.redis_timeout_seconds,
                "socket_timeout": settings.redis_timeout_seconds,
            },
            filterable_fields=[
                {"name": "city_id", "type": "tag"},
                {"name": "lang", "type": "tag"},
            ],
        )
    return _cache


def _tenant_filter(city_id: str, lang: str):
    return (Tag("city_id") == city_id) & (Tag("lang") == lang)


async def check_cache(
    city_id: str, lang: str, query: str, embedding: list[float] | None
) -> dict[str, Any] | None:
    """Return a cached entry {answer, citations, intent} or None on miss."""
    if embedding is None:
        return None

    def _do() -> dict[str, Any] | None:
        cache = _get_cache()
        hits = cache.check(
            prompt=query,
            vector=embedding,
            num_results=1,
            return_fields=["response", "metadata"],
            filter_expression=_tenant_filter(city_id, lang),
        )
        if not hits:
            return None
        hit = hits[0]
        meta = hit.get("metadata") or {}
        return {
            "answer": hit.get("response", ""),
            "citations": meta.get("citations", []),
            "intent": meta.get("intent"),
        }

    try:
        # Belt AND braces: socket timeouts should make _do() fail fast, but this
        # is the FIRST node in the graph -- if the client ever wedges anyway, the
        # whole turn hangs. wait_for guarantees we move on. The worker thread may
        # linger; the request does not.
        return await asyncio.wait_for(
            asyncio.to_thread(_do), timeout=settings.redis_timeout_seconds
        )
    except asyncio.TimeoutError:
        log.warning("semantic_cache_check_timeout",
                    timeout=settings.redis_timeout_seconds)
        return None
    except Exception as exc:
        # Cache must never break the request path — degrade to a miss, but log so a
        # silently-down Redis (every query a miss) is visible.
        log.warning("semantic_cache_check_failed", error=str(exc))
        return None


async def write_cache(
    city_id: str,
    lang: str,
    query: str,
    embedding: list[float] | None,
    answer: str,
    citations: list[dict],
    intent: str | None,
) -> None:
    if embedding is None or not answer.strip():
        return

    def _do() -> None:
        cache = _get_cache()
        cache.store(
            prompt=query,
            response=answer,
            vector=embedding,
            metadata={"citations": citations, "intent": intent},
            filters={"city_id": city_id, "lang": lang},
        )

    try:
        # Same guard as check_cache: the write happens after the answer streams,
        # but a wedged client would still hold the turn open.
        await asyncio.wait_for(
            asyncio.to_thread(_do), timeout=settings.redis_timeout_seconds
        )
    except asyncio.TimeoutError:
        log.warning("semantic_cache_write_timeout",
                    timeout=settings.redis_timeout_seconds)
    except Exception as exc:
        log.warning("semantic_cache_write_failed", error=str(exc))


async def ping_redis() -> dict[str, Any]:
    try:
        client = aioredis.from_url(settings.redis_url)
        pong = await client.ping()
        await client.aclose()
        return {"ok": bool(pong)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
