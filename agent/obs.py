"""
agent/obs.py
============
Tiny structured-logging helper shared across nodes.

The agent leans on many best-effort `except Exception` handlers that must never
break the request path (cache, judge, intent fallback, fee sources, ...). Those
are correct to swallow, but swallowing them *silently* makes production
debugging guesswork. Use `log` to record what was swallowed without changing the
control flow.

    from agent.obs import log
    log.warning("intent_fallback", error=str(exc), query=query)

structlog is already a runtime dependency (see requirements-agent.txt). If it is
somehow unavailable we degrade to the stdlib logger so importing this never
fails.
"""
from __future__ import annotations

import logging

try:
    import structlog

    log = structlog.get_logger("arvada_agent")
except Exception:  # pragma: no cover - structlog is a declared dependency
    log = logging.getLogger("arvada_agent")
