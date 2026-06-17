"""
contextualize node
==================
Condenses the client-owned conversation thread + the latest message into a
single standalone English search query. This fixes multi-turn follow-ups like
"how much does it cost?" where "it" refers to an earlier permit.

Also embeds the standalone query (text-embedding-3-small) so downstream
retrieval / semantic-cache nodes can reuse it.
"""
from __future__ import annotations

from config import settings
from agent import llm
from agent.prompts import CONTEXTUALIZE_SYSTEM, contextualize_user
from agent.state import AgentState


def _history_text(messages: list[dict], turns: int) -> str:
    """Render the last N turns (excluding the current message) as plain text."""
    if not messages:
        return ""
    # The final message is the current user turn; use prior ones as history.
    prior = messages[:-1] if messages and messages[-1].get("role") == "user" else messages
    prior = prior[-(turns * 2):]
    lines = []
    for m in prior:
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        speaker = "User" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


async def resolve_standalone(state: AgentState) -> str:
    """Rewrite the latest message into a standalone English query (no embedding).

    Split out from the node so run_prep can compute the standalone query first,
    then fire the embedding and intent classification concurrently.
    """
    history = _history_text(state.messages, settings.conversation_history_turns)
    if not history:
        return state.query.strip()
    try:
        standalone = await llm.complete(
            model=settings.intent_model,
            system=CONTEXTUALIZE_SYSTEM,
            user=contextualize_user(history, state.query),
            reasoning_effort=settings.small_model_reasoning_effort,
            # GPT-5 reasoning models count reasoning toward max_completion_tokens;
            # keep generous headroom so the rewritten query isn't truncated to empty.
            max_tokens=512,
        )
        standalone = (standalone or state.query).strip().strip('"')
    except Exception:
        standalone = state.query.strip()
    return standalone or state.query.strip()


async def contextualize(state: AgentState) -> dict:
    standalone = await resolve_standalone(state)
    embedding = await llm.embed(standalone)
    return {"standalone_query": standalone, "query_embedding": embedding}
