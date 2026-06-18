"""
judge node
=========
Post-generation grounding check (small/fast model). Only judges GROUNDED answers
(RAG path) — deterministic fee answers, status fallbacks, escalations, and the
honest "I don't know" path are trusted by construction and skipped.

On a failed check it sets `judge_passed=False` and logs the unsupported claims for
telemetry. It deliberately does NOT surface a user-facing disclaimer (product
decision 2026-06-18): the grounding signal stays in logs/metrics, not in the UI.
"""
from __future__ import annotations

from config import settings
from agent import llm, prompts
from agent.nodes.generate import group_sources
from agent.obs import log
from agent.state import AgentState


def _should_judge(state: AgentState) -> bool:
    if not state.answer:
        return False
    if state.fee_result or state.status_result or state.changes_result or state.project_plan:
        return False
    if state.intent == "ESCALATE":
        return False
    if state.low_confidence or not state.reranked_chunks:
        return False
    return True


async def judge(state: AgentState) -> dict:
    if not _should_judge(state):
        return {"judge_passed": True}

    context = prompts.generation_context_block(group_sources(state.reranked_chunks))
    try:
        verdict = await llm.complete_json(
            model=settings.judge_model,
            system=prompts.JUDGE_SYSTEM,
            user=prompts.judge_user(state.answer or "", context),
            reasoning_effort=settings.small_model_reasoning_effort,
        )
        grounded = bool(verdict.get("grounded", True))
        if grounded:
            return {"judge_passed": True}
        # Record the grounding miss for telemetry, but do NOT show a user-facing
        # disclaimer (product decision 2026-06-18).
        log.warning(
            "judge_grounding_failed",
            reason=str(verdict.get("reason", ""))[:300],
            unsupported=verdict.get("unsupported"),
        )
        return {"judge_passed": False}
    except Exception as exc:
        # Never block an answer on judge failure — but log it: a silently disabled
        # grounding check means hallucinations ship unflagged.
        log.warning("judge_check_failed", error=str(exc))
        return {"judge_passed": True}
