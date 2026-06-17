# ARCHITECTURE.md — Cursor Context File
# Open this in Cursor alongside any file you're working on.
# This gives Cursor full context of the system so it generates correct code.

## What This Service Does
A standalone Python microservice that:
1. Answers any question about Arvada permits & licenses using RAG over scraped city data
2. Explains permit application steps and form fields in plain English
3. Calculates permit fees deterministically (no LLM)
4. Guides citizens through the permit process without filling forms for them
5. Escalates unresolved queries to city staff with full context

## How It Integrates With The Main Project
```
[React Widget / Standalone Chat]
        ↓  POST /api/agent/chat/stream
[Spring Boot — AgentProxyController.java]
  - Reads city_id from JWT
  - Adds X-City-ID, X-Session-ID headers
  - Proxies SSE stream to React
        ↓  POST http://localhost:8001/chat/stream
[THIS SERVICE — FastAPI + LangGraph]
        ↓
[Postgres + pgvector] + [Redis]
```

## Request Lifecycle (must know this cold)
```
Request arrives
    │
    ├─ asyncio.gather():
    │   ├─ Embed query  (text-embedding-3-small)
    │   └─ Load conversation history from Redis
    │
    ├─ Semantic cache check (Redis, cosine ≥ 0.92)
    │   └─ HIT → return instantly (3-8ms total) ──────────────────────► [DONE]
    │
    ├─ asyncio.gather():
    │   ├─ Intent classification (GPT-4.1 Nano, ~80ms)
    │   └─ Hybrid retrieval (pgvector+BM25 SQL, ~50ms)
    │
    ├─ Route by intent:
    │   ├─ FEE_CALC     → fee_engine (deterministic, no LLM)
    │   ├─ STATUS       → status_lookup (DB query, no LLM)
    │   ├─ ESCALATE     → escalate node
    │   └─ FAQ/FORM/DOC → reranker → generate
    │
    ├─ Reranker (GPT-4.1 Nano on top-10, ~60ms)
    │
    ├─ Stream generation (GPT-4.1 Mini, stream=True)
    │   └─ Emit SSE: token events + citation events
    │
    ├─ Judge (GPT-4.1 Nano, async post-stream)
    │   └─ If FAIL → emit warning SSE event
    │
    └─ Write-back: Redis semantic cache + Langfuse trace
```

## LangGraph Graph Shape
```python
START → cache_check
cache_check → [HIT: END] or [MISS: intent]
intent → [FEE_CALC: fee_engine] or [STATUS: status_lookup]
       → [FAQ/FORM/DOC: retrieve]
       → [ESCALATE: escalate]
retrieve → rerank
rerank → generate
fee_engine → generate
status_lookup → generate
generate → write_cache
write_cache → END
escalate → END
```

## SSE Event Types (contract with React frontend)
```
start      → {type, session_id, city_id, permit_type}
token      → {type, text}                          # one per LLM output token
citation   → {type, title, url, effective_date}    # after answer completes
tool_call  → {type, tool, status}                  # keeps user informed during tool waits
warning    → {type, msg}                           # judge detected unverified claim
escalate   → {type, ticket_id, dept, contact}      # escalation triggered
done       → literal string "[DONE]"
error      → {type, code, msg}
```

## Database Schema (key tables)
```sql
-- cities: one row per city tenant
cities (id uuid PK, name text, config jsonb, source_domains text[], active bool)

-- knowledge_articles: human-readable articles (source of truth for portal UI)
knowledge_articles (
  id uuid PK, city_id uuid FK, article_type text, permit_type text,
  category text, title text, slug text UNIQUE, content jsonb,
  tags text[], source_urls text[], ordinance_refs text[],
  effective_date date, version int, status text, last_verified date
)

-- article_chunks: embedded chunks for RAG (what gets retrieved)
article_chunks (
  id uuid PK, city_id uuid FK, article_id uuid FK,
  chunk_index int, section_type text,
  content_text text,                    -- plain text for BM25
  content_tsv tsvector GENERATED,       -- auto BM25 index
  embedding vector(1536),               -- dense vector
  metadata jsonb,                       -- permit_type, category, tags, effective_date
  token_count int
)
-- Indexes: HNSW on embedding, GIN on content_tsv, (city_id, permit_type)

-- fee_schedules: deterministic fee rules (fee calculator reads this, not LLM)
fee_schedules (
  id uuid PK, city_id uuid FK, permit_type text,
  fee_type text, calc_method text, value numeric,
  table_data jsonb,    -- tiered table rows: [{min, max, base, per_1000_rate}]
  effective_date date, expires_date date
)

-- article_sources: freshness monitoring
article_sources (
  id uuid PK, city_id uuid FK, article_id uuid FK,
  source_url text, last_fetched_at timestamptz,
  last_fetched_hash text,    -- SHA-256 of content body
  status text                -- fresh | stale | error
)
```

## Config (environment variables)
```
OPENAI_API_KEY=sk-...
POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/arvada_agent
REDIS_URL=redis://localhost:6379
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
LANGFUSE_HOST=https://cloud.langfuse.com
SEMANTIC_CACHE_THRESHOLD=0.92
SEMANTIC_CACHE_TTL_SECONDS=86400
CONVERSATION_MEMORY_TTL_SECONDS=3600
CONVERSATION_HISTORY_TURNS=6
INTENT_MODEL=gpt-4.1-nano-2025-04-14
GENERATION_MODEL=gpt-4.1-mini-2025-04-14
JUDGE_MODEL=gpt-4.1-nano-2025-04-14
EMBEDDING_MODEL=text-embedding-3-small
RETRIEVAL_TOP_K=10
RERANK_TOP_K=5
DEFAULT_CITY_ID=arvada-co
```

## Key External APIs
- OpenAI: embeddings + LLM calls (async client only)
- Langfuse: tracing (fire-and-forget, never on critical path)
- Internal: fee_schedules Postgres table (no external API)

## What This Service Does NOT Do
- Does NOT submit permit applications on behalf of users
- Does NOT fill form fields
- Does NOT process payments
- Does NOT access the incumbent permitting portal directly (status comes from our own permits table)
- Does NOT store PII beyond session-scoped conversation memory (TTL 1h)
