"""
agent/llm.py
============
Thin async OpenAI helper (single provider, no abstraction layer).

Centralises:
  - the shared AsyncOpenAI client
  - GPT-5-family quirks (reasoning_effort, no custom temperature)
  - embeddings
  - a JSON-mode completion helper for the mechanical (intent/rerank/judge) tasks
  - a streaming completion helper for generation
"""
from __future__ import annotations

import json
from typing import AsyncIterator

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings

# Built-in SDK timeout + retries; tenacity adds backoff on top for transients.
client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=40.0, max_retries=2)

_RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)
_retry = retry(
    retry=retry_if_exception_type(_RETRYABLE),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)


def _is_reasoning_model(model: str) -> bool:
    """GPT-5 / o-series models take reasoning_effort and reject custom temperature."""
    return model.startswith("gpt-5") or model.startswith("o3") or model.startswith("o4")


@_retry
async def embed(text: str) -> list[float]:
    resp = await client.embeddings.create(model=settings.embedding_model, input=text)
    return resp.data[0].embedding


@_retry
async def complete(
    *,
    model: str,
    system: str,
    user: str,
    reasoning_effort: str | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """Non-streaming completion. Returns the message content string."""
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if _is_reasoning_model(model):
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        # GPT-5 family uses max_completion_tokens
        key = "max_completion_tokens" if _is_reasoning_model(model) else "max_tokens"
        kwargs[key] = max_tokens

    resp = await client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()


async def complete_json(
    *,
    model: str,
    system: str,
    user: str,
    reasoning_effort: str | None = None,
) -> dict:
    """Completion that must return a JSON object. Tolerant to fenced output."""
    raw = await complete(
        model=model,
        system=system,
        user=user,
        reasoning_effort=reasoning_effort,
        json_mode=True,
    )
    return _loads_tolerant(raw)


async def stream_chat(
    *,
    model: str,
    messages: list[dict],
    reasoning_effort: str | None = None,
) -> AsyncIterator[str]:
    """Stream a chat completion, yielding text deltas as they arrive."""
    kwargs: dict = {"model": model, "messages": messages, "stream": True}
    if _is_reasoning_model(model) and reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    @_retry
    async def _open_stream():
        # Retry only the initial connection; once tokens flow we don't replay.
        return await client.chat.completions.create(**kwargs)

    stream = await _open_stream()
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


def _loads_tolerant(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        # strip ```json ... ``` fences
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw.strip("`")
        if raw.lstrip().startswith("json"):
            raw = raw.lstrip()[4:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise
