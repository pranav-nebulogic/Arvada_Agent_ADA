# Arvada Permits AI Agent — Standalone Microservice

A production-grade AI agent for the City of Arvada Permits & Licenses platform.
Runs independently from the main Java/Spring Boot project and integrates via HTTP/SSE.

## Architecture
```
React Frontend  →  Spring Boot Gateway  →  This Python Microservice
                        (proxies SSE)         FastAPI + LangGraph
                                                    ↓
                                         Postgres (pgvector) + Redis
```

## Phases

### Phase 1 — Scrape & Ingest
Get real Arvada data into Postgres. These scripts live at the project root.
```bash
python run_scraper.py        # scrape all Arvada sources → raw JSON
python run_transform.py      # raw HTML → structured articles
python run_ingest.py         # chunk → embed → upsert to pgvector
python run_agent_eval.py     # verify retrieval quality / end-to-end eval
```

### Phase 2 — Run the Agent
```bash
python run_agent.py          # FastAPI + LangGraph agent on :8001 (Ctrl+C to stop)
```

### Phase 3 — Integrate
Spring Boot calls `POST http://localhost:8001/chat/stream` with an SSE proxy.

## Quick Start (local)
> Windows/PowerShell paths shown; on macOS/Linux use `source .venv/bin/activate`
> and `python` instead of `.venv\Scripts\python.exe`.

```powershell
# 1. Create the database schema
#    Run init.sql against your Postgres (DBeaver: open a SQL editor, paste, F5).

# 2. (Optional) Start Redis for the semantic cache
docker-compose up -d

# 3. Install Python deps
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-agent.txt     # runtime only
# pip install -r requirements.txt         # add this for the scraper/ingest tools

# 4. Set env vars
copy .env.example .env
#    Fill in OPENAI_API_KEY and POSTGRES_URL (Redis/Langfuse are optional).

# 5. Run the agent
.venv\Scripts\python.exe run_agent.py     # serves http://localhost:8001
```

Once it's up:
- Open **http://localhost:8001/** for the local dev chat console.
- Health check: **http://localhost:8001/health**
- The chat API (`/chat`, `/chat/stream`) requires the `X-Internal-Token` header
  matching `INTERNAL_SERVICE_TOKEN` in your `.env`.

## Stack
- **Agent framework:** LangGraph 0.3+
- **API:** FastAPI + Uvicorn (SSE streaming)
- **Vector DB:** pgvector on Postgres (hybrid tsvector FTS + dense vector, RRF)
- **Cache:** Redis (semantic cache only — conversation is client-owned)
- **LLMs:** GPT-5.x family — nano for routing/rerank/judge, fast/deep for generation (see `config.py`)
- **Embeddings:** text-embedding-3-small (1536-dim)
- **Observability:** Langfuse (optional) + structlog

## Project Structure
The project is a flat layout — entry-point scripts live at the root, agent code
under `agent/`.
```
arvada-agent/
├── run_agent.py          # ▶ start the agent API (Uvicorn on :8001)
├── run_scraper.py        # Phase 1: scrape Arvada sources
├── run_transform.py      # Phase 1: raw → structured articles
├── run_ingest.py         # Phase 1: chunk → embed → pgvector upsert
├── run_agent_eval.py     # end-to-end / retrieval eval
├── api.py                # FastAPI app + SSE endpoints (/chat, /chat/stream, /health)
├── config.py             # settings via pydantic-settings (reads .env)
├── db.py                 # asyncpg pool + hybrid_search + RLS (tenant_conn)
├── sources.py            # all Arvada URLs to scrape
├── crawler.py            # Playwright + httpx crawlers
├── transformer.py        # raw HTML → article schema
├── ingestor.py           # chunk → embed → pgvector upsert
├── init.sql              # Postgres schema + RLS + indexes + seed data
├── docker-compose.yml    # local Redis (+ optional Langfuse)
├── Dockerfile            # production agent image
├── agent/                # the agent itself
│   ├── graph.py          # LangGraph wiring + run_prep (SSE pre-flight)
│   ├── state.py          # AgentState typed schema
│   ├── llm.py            # async OpenAI client (embed / complete / stream)
│   ├── prompts.py        # system prompts + persona, in one place
│   ├── fee_tables.py     # deterministic fee math (no LLM)
│   ├── project_planner.py# deterministic multi-project sequencing (no LLM)
│   ├── redis_store.py    # semantic cache wrapper
│   ├── obs.py            # structlog logger shared by nodes
│   ├── tracing.py        # optional Langfuse hooks
│   └── nodes/            # one file per graph node
│       ├── contextualize.py   intent.py        cache.py
│       ├── retrieve.py        rerank.py        generate.py
│       ├── judge.py           converse.py      escalate.py
│       ├── fee_engine.py      status_lookup.py
│       ├── whats_changed.py   # "What Changed?" fee & code alerts
│       └── multi_project.py   # multi-project dependency resolver
├── tests/                # pytest (pure-logic unit tests)
├── ui/                   # local dev chat console (served at /)
├── requirements.txt      # full deps (incl. scraper/ingest)
├── requirements-agent.txt# runtime-only deps (production image)
└── .env.example
```
