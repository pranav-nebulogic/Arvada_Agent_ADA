"""
The semantic cache must never hold a turn hostage.

`cache_check` is the FIRST node in the graph. A Redis that REFUSES a connection
was always handled (logged, treated as a miss), but one that ACCEPTS the socket
and never replies hung the call forever -- observed locally against a half-open
listener on 6379, where the graph stalled with no log line at all and every
local eval run appeared to "never finish".

These tests pin the guarantee: whatever Redis does, the turn continues.
"""
from __future__ import annotations

import asyncio

import pytest

from config import settings
from agent import redis_store


@pytest.mark.asyncio
async def test_hung_redis_does_not_block_the_turn(monkeypatch):
    """A call that never returns must be abandoned, not awaited forever."""
    def never_returns():
        import time
        time.sleep(30)          # simulates the half-open socket
        return {"answer": "should never be used"}

    monkeypatch.setattr(settings, "redis_timeout_seconds", 0.2)
    monkeypatch.setattr(redis_store, "_get_cache", lambda: None)
    monkeypatch.setattr(asyncio, "to_thread",
                        lambda fn, *a, **k: asyncio.sleep(30, result=None))

    got = await asyncio.wait_for(
        redis_store.check_cache("woodinville-wa", "en", "q", [0.0] * 8),
        timeout=5,               # the test itself fails if the guard is missing
    )
    assert got is None           # degraded to a cache miss


@pytest.mark.asyncio
async def test_refused_redis_is_still_a_plain_miss(monkeypatch):
    async def boom(*_a, **_k):
        raise ConnectionError("Error 10061 connecting to 127.0.0.1:6379")

    monkeypatch.setattr(asyncio, "to_thread", boom)
    assert await redis_store.check_cache("woodinville-wa", "en", "q", [0.0] * 8) is None


@pytest.mark.asyncio
async def test_write_timeout_is_swallowed(monkeypatch):
    monkeypatch.setattr(settings, "redis_timeout_seconds", 0.2)
    monkeypatch.setattr(asyncio, "to_thread",
                        lambda fn, *a, **k: asyncio.sleep(30, result=None))
    # Must return, not raise -- the answer has already been streamed by now.
    await asyncio.wait_for(
        redis_store.write_cache("woodinville-wa", "en", "q", [0.0] * 8,
                                "an answer", [], "FAQ"),
        timeout=5,
    )


def test_timeout_is_configurable_and_sane():
    assert 0 < settings.redis_timeout_seconds <= 5, \
        "a long cache timeout defeats the purpose -- it is on the critical path"
