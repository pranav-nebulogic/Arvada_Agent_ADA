"""
converse node
=============
Handles conversational turns that are NOT knowledge-base questions — greetings,
thanks, smalltalk, "what can you do", off-topic chatter, and profanity/venting.

No retrieval, no citations, no grounding judge. The persona prompt does the work
(see prompts.converse_system). Used by the non-streaming graph path; the SSE
endpoint streams the same messages directly via generate.astream_answer.
"""
from __future__ import annotations

from agent import prompts
from agent.nodes.generate import _collect
from agent.state import AgentState


async def converse(state: AgentState) -> dict:
    try:
        answer = await _collect(state)
    except Exception:
        answer = (
            "I'm Ada, the City of Arvada's virtual agent. I can help with permits, "
            "licenses, fees, document checklists, application status, and city "
            "ordinances — what can I do for you?"
        )
    if not answer:
        answer = (
            "I'm Ada, the City of Arvada's virtual agent. Ask me about permits, "
            "licenses, fees, or city processes and I'll take it from there."
        )
    return {"answer": answer, "citations": []}
