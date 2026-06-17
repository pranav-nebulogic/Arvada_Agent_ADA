"""
run_agent.py
============
Uvicorn entrypoint for the Arvada agent API (reads AGENT_HOST / AGENT_PORT).

  python run_agent.py
"""
import sys

import uvicorn

from config import settings

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    uvicorn.run(
        "api:app",
        host=settings.agent_host,
        port=settings.agent_port,
        log_level=settings.log_level,
        reload=False,
    )
