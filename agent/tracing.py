"""
agent/tracing.py
===============
Optional Langfuse tracing. Fully NO-OP when keys are placeholders (the default),
so it never adds latency or errors in dev. When real `pk-lf-`/`sk-lf-` keys are
present in .env it lazily initialises a Langfuse client.

Usage:
    async with tracing.observe("chat", input=query, metadata={...}) as span:
        ...
        if span: span.update(output=answer)
"""
from __future__ import annotations

import contextlib

from config import settings

_client = None
_init_tried = False


def enabled() -> bool:
    return settings.langfuse_enabled


def _get_client():
    global _client, _init_tried
    if not enabled():
        return None
    if _init_tried:
        return _client
    _init_tried = True
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception:
        _client = None
    return _client


@contextlib.asynccontextmanager
async def observe(name: str, **kwargs):
    """Yield a Langfuse span when enabled, else None (no-op)."""
    client = _get_client()
    if client is None:
        yield None
        return
    span = None
    try:
        # Support both langfuse v2 (.trace) and v3 (.start_span) gracefully.
        if hasattr(client, "trace"):
            span = client.trace(name=name, **kwargs)
        elif hasattr(client, "start_span"):
            span = client.start_span(name=name, **kwargs)
        yield span
    except Exception:
        yield None
    finally:
        try:
            if span is not None and hasattr(span, "end"):
                span.end()
        except Exception:
            pass


def status() -> dict:
    return {"enabled": enabled()}
