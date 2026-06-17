"""
status_lookup node
=================
Looks up an existing permit/case by reference number against the main engine's
case API (`GET /api/cases/by-reference/{ref}`), authenticated with the s2s token.

Until the Spring proxy + s2s auth is wired (Phase 3) — i.e. ENGINE_CASE_API_BASE
is empty — this gracefully falls back to directing the user to the permit portal
/ phone. The downstream `generate` node renders the result; api.py emits a
`tool_call` SSE event for this node.
"""
from __future__ import annotations

import httpx

from config import settings
from agent.state import AgentState


async def status_lookup(state: AgentState) -> dict:
    ref = (state.entities or {}).get("reference_number")

    if not ref:
        return {
            "status_result": {
                "available": False,
                "reason": "no_reference",
                "found": False,
            }
        }

    if not settings.engine_case_api_base:
        # Phase 3 not wired yet -> graceful fallback to portal/phone.
        return {
            "status_result": {
                "available": False,
                "reason": "lookup_not_connected",
                "reference_number": ref,
                "found": False,
            }
        }

    url = f"{settings.engine_case_api_base.rstrip('/')}/api/cases/by-reference/{ref}"
    headers = {"X-Internal-Token": settings.engine_s2s_token, "X-Tenant-Id": state.city_id}
    try:
        async with httpx.AsyncClient(timeout=settings.engine_timeout_seconds) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            return {"status_result": {"available": True, "found": False, "reference_number": ref}}
        resp.raise_for_status()
        data = resp.json()
        # Engine SearchService.byReference() returns {"match": bool, ...}.
        if not data.get("match"):
            return {"status_result": {"available": True, "found": False, "reference_number": ref}}
        return {
            "status_result": {
                "available": True,
                "found": True,
                "reference_number": data.get("reference") or ref,
                "status": data.get("status"),
                "priority": data.get("priority"),
                "created_at": data.get("createdAt"),
                "updated_at": data.get("updatedAt"),
                "closed_at": data.get("closedAt"),
            }
        }
    except Exception as exc:  # noqa: BLE001 — never fail the request on a tool error
        return {
            "status_result": {
                "available": False,
                "reason": "lookup_error",
                "error": str(exc),
                "reference_number": ref,
                "found": False,
            }
        }
