# CURSOR_RULES.md — What to Say to Cursor at Each Step
# This file gives you the exact prompts to paste into Cursor for each build task.
# Always open ARCHITECTURE.md + AGENT_STATE.md as context alongside your current file.

## HOW TO SET UP CURSOR FOR THIS PROJECT

1. Open the `arvada-agent/` folder in Cursor
2. The `.cursorrules` file is auto-loaded — Cursor reads it for every chat
3. Before each task below, also open (Ctrl+Shift+P → "Add to Context"):
   - `cursor_context/ARCHITECTURE.md`
   - `cursor_context/AGENT_STATE.md`
4. Pin these files as persistent context

---

## PHASE 1: SCRAPER TASKS

### Task 1.1 — Run the scraper
```
I need to run the scraper for the first time.
The entry point is scraper/run_scraper.py.
Create this file. It should:
1. Import ARVADA_SOURCES from sources.py and scrape_all from crawler.py
2. Set up structlog JSON logging
3. Load .env with python-dotenv
4. Run asyncio.run(scrape_all(ARVADA_SOURCES))
5. Print a summary table using rich: URL | Status | Chars | Hash
6. Save a scrape_manifest.json with all results and timestamps

Make sure it handles the case where Playwright is not installed
(print clear install instructions: playwright install chromium).
```

### Task 1.2 — Run the transformer
```
Create scraper/run_transform.py.
It should:
1. Import transform_all from transformer.py
2. Check that OPENAI_API_KEY is set in .env, error clearly if not
3. Run asyncio.run(transform_all())
4. After running, open articles/ and print a review summary table using rich:
   - Article title | permit_type | fee count | step count | status
5. Print a clear message: "Set status to 'published' in each article JSON
   after reviewing fees and phone numbers, then run run_ingest.py"

Note: The transformer uses Batch API mode for bulk runs to save 50% cost.
Add a --batch flag that switches to OpenAI Batch API mode.
```

### Task 1.3 — Evaluator
```
Create scraper/evaluator.py and scraper/run_eval.py.

The evaluator should:
1. Import GOLDEN_QUERIES from sources.py (20 test queries)
2. For each query:
   a. Embed the query text (text-embedding-3-small)
   b. Run hybrid_search() SQL function against Postgres
   c. Check if the expected_permit_type appears in top-3 results
   d. Check if expected_section appears in top-3 results
3. Calculate Recall@3 and Recall@5
4. Print a table: query | top-1 section_type | match? | rrf_score
5. Print overall metrics: Recall@3=X.XX, Recall@5=X.XX
6. PASS if Recall@3 >= 0.85, FAIL otherwise

Target: Recall@3 >= 0.85 before building the agent.
If it fails, print specific guidance: which queries failed and why.
```

---

## PHASE 2: AGENT TASKS

### Task 2.1 — Config and DB setup
```
Create agent/config.py using pydantic-settings.
It should expose a Settings class with all env vars from .env.example.
Use @lru_cache to create a singleton get_settings() function.

Then create agent/db/postgres.py with:
1. An asyncpg connection pool (create_pool)
2. A context manager get_conn() that:
   - Acquires a connection from the pool
   - Sets: SET LOCAL app.tenant_id = $city_id
   - Yields the connection
   - Returns it to the pool
3. A run_hybrid_search() function that calls the hybrid_search SQL function
   and returns a list of chunk dicts

And agent/db/redis_client.py with:
1. A Redis connection singleton
2. A SemanticCache instance (from redisvl) with threshold from settings
3. cache_lookup(query_embedding, city_id) → str | None
4. cache_store(query_embedding, answer, city_id, permit_type, citations) → None
5. get_conversation(session_id) → list[dict]
6. save_conversation(session_id, messages) → None (trim to last 6 turns)
```

### Task 2.2 — Intent node
```
Create agent/nodes/intent.py.

The intent_router node should:
1. Take AgentState (see cursor_context/AGENT_STATE.md for full schema)
2. Call GPT-4.1 Nano with a few-shot classification prompt
3. Return: {"intent": one of FAQ|FORM_HELP|DOC_CHECKLIST|FEE_CALC|STATUS_LOOKUP|ESCALATE,
             "entities": {permit_type, fee_valuation, form_field_name}}

Few-shot examples to include in the system prompt:
- "how much is a solar permit?" → FEE_CALC, permit_type: building_permit_solar
- "what does project valuation mean?" → FORM_HELP, form_field_name: project_valuation
- "what documents do I need for an STR?" → DOC_CHECKLIST, permit_type: str_permit
- "I want to talk to someone" → ESCALATE
- "how long does plan review take?" → FAQ, permit_type: building_permit
- "status of my permit 2024-0123" → STATUS_LOOKUP

Use prompt caching: keep system prompt stable (put examples first, query last).
Model: settings.INTENT_MODEL (gpt-4.1-nano-2025-04-14).
Temperature: 0.
Parse JSON from response — handle parse errors gracefully (default to FAQ).
```

### Task 2.3 — Retrieval node
```
Create agent/nodes/retrieval.py.

The hybrid_retrieve node should:
1. Take AgentState — use state.query_embedding (already computed) and state.entities
2. Call run_hybrid_search() from db/postgres.py
3. Pass: query text, query_embedding, city_id, permit_type (from entities if present)
4. Return: {"retrieved_chunks": list of chunk dicts}

Also use asyncio.gather to run retrieval concurrently with intent classification.
The graph should call these two in parallel — but this node just does retrieval.

Add metadata filtering: if state.form_field is set, boost chunks
with section_type="form_fields" by adding them to the top of results.
```

### Task 2.4 — Generator node with SSE streaming
```
Create agent/nodes/generator.py.

This is the most important node. It should:
1. Build the system prompt with:
   - City name and current date
   - Instruction: "cite sources by title and URL for every factual claim"
   - Instruction: "NEVER state a fee amount not in the provided context — say 'use the fee calculator' instead"
   - Instruction: "if you don't know, say so clearly and provide the department contact"
2. Build context from state.reranked_chunks (already sorted best-first)
3. Include last 6 conversation turns from state.messages
4. Stream with openai AsyncOpenAI, stream=True
5. As tokens arrive, emit them as SSE events via LangGraph's get_stream_writer()
6. After streaming completes:
   - Parse citations from the complete answer
   - Emit citation SSE events
   - Return: {"answer": full_text, "citations": list_of_citations}

For SSE events, use LangGraph's built-in astream_events streaming.
The FastAPI layer (main.py) will filter on_chat_model_stream events.

Model: settings.GENERATION_MODEL
Temperature: 0.2 (slight creativity for natural language, not for facts)
```

### Task 2.5 — Judge node
```
Create agent/nodes/judge.py.

The judge node runs AFTER streaming. It validates the answer against retrieved chunks.
It should:
1. Take state.answer and state.reranked_chunks
2. Call GPT-4.1 Nano with a strict validation prompt:
   "Does the answer contain any specific fee amount, phone number, date, or deadline
    that is NOT supported by the provided context chunks? Answer PASS or FAIL.
    If FAIL, quote the unsupported claim."
3. If FAIL: return {"judge_passed": False, "warning": "Please verify [claim] directly with the city at [contact]"}
4. If PASS: return {"judge_passed": True}

This node runs asynchronously — it does NOT block the SSE stream.
The FastAPI layer emits a "warning" SSE event if judge returns FAIL.
This is post-stream validation, not pre-stream blocking.

Temperature: 0. Model: settings.JUDGE_MODEL.
```

### Task 2.6 — Fee calculator tool
```
Create agent/tools/fee_calculator.py.

This is DETERMINISTIC — no LLM. Pure Python rule engine.
It should:
1. Accept: city_id, permit_type, valuation (float)
2. Query fee_schedules table from Postgres (not LLM)
3. Calculate each fee type:
   - table_18_1: find the right tier, apply formula
   - flat: return the fixed amount
   - percentage_of_valuation: multiply by valuation
4. Return: FeeResult dataclass with permit_fee, plan_review_fee, use_tax, total, notes

Table 18-1 logic example:
  valuation=$25,000 → tier: $2,001-$25,000
  fee = $79.75 + (($25,000 - $2,000) / $1,000 × $14.00) = $79.75 + $322.00 = $401.75
  plan_review = $32.50 (flat)
  use_tax = $25,000 × 3.46% = $865.00
  total = $401.75 + $32.50 + $865.00 = $1,299.25

Write unit tests in tests/tools/test_fee_calculator.py covering:
- Solar permit (flat fee)
- $25,000 residential remodel (Table 18-1)
- $250,000 commercial project (Table 18-1)
- STR permit (flat)
- Edge case: valuation = 0
```

### Task 2.7 — FastAPI streaming endpoint
```
Create agent/main.py — the FastAPI application.

It should have one main endpoint:
  POST /chat/stream

Requirements:
1. Read city_id from X-City-ID header (injected by Spring Boot from JWT)
   If missing, return 401
2. Read session_id from X-Session-ID header (UUID)
   If missing, generate one
3. Read user_type from X-User-Type header (default: homeowner)
4. Validate X-Internal-Token matches settings.INTERNAL_SERVICE_TOKEN
5. Build AgentState from headers + request body
6. Run graph.astream_events() and yield SSE events:
   - on_chat_model_stream → emit "token" events
   - on_custom_event (citations, warnings) → emit those events
7. Always emit "data: [DONE]\n\n" in a finally block
8. Set response headers: Content-Type: text/event-stream, X-Accel-Buffering: no, Cache-Control: no-cache

Also add:
  GET /health → {"status": "ok", "city": "arvada-co"}
  GET /metrics → {"cache_hit_rate": X, "avg_latency_ms": X} (from Redis counters)

Error handling: catch ALL exceptions, log to Langfuse, emit SSE error event, then [DONE].
```

### Task 2.8 — Full graph assembly
```
Create agent/graph.py — assemble the complete LangGraph graph.

Use ARCHITECTURE.md for the exact graph shape.
The graph should:
1. Use RedisSaver as the checkpointer (conversation memory from Redis)
2. Define all nodes (import from nodes/ directory)
3. Define routing functions: route_cache, route_intent
4. Wire all edges exactly as in ARCHITECTURE.md
5. Compile with interrupt_before=[] (no human interrupts for now)

Also create agent/__init__.py that exposes:
  from agent import create_app
  
And agent/app.py that:
1. Creates the FastAPI app
2. Creates the asyncpg pool on startup (lifespan event)
3. Creates the Redis client on startup
4. Builds the LangGraph graph
5. Sets up Langfuse tracing (CallbackHandler)
6. Registers the /chat/stream route
```

---

## PHASE 3: INTEGRATION TASKS

### Task 3.1 — Spring Boot integration note
```
I need to integrate the Python agent microservice with Spring Boot.
Create a file docs/SPRING_INTEGRATION.md explaining:
1. The exact HTTP contract (endpoint, headers, SSE format)
2. The AgentProxyController.java code using WebClient + Flux<ServerSentEvent>
3. How to inject city_id from Spring Security JWT into the X-City-ID header
4. How to proxy the SSE stream from Python to React
5. CORS configuration needed for local dev
6. Health check endpoint for load balancer
```

### Task 3.2 — React hook integration
```
Create frontend/useAgentStream.js — the React hook for consuming the SSE stream.
See ARCHITECTURE.md for the complete SSE event protocol.
The hook should expose:
  { tokens, citations, warning, isStreaming, toolStatus, escalation, sendMessage, abort }

Also create frontend/AgentChatWidget.jsx — a reusable component that:
1. Can be embedded in a form wizard (accepts permitType, formField props)
2. Can be used standalone on a /help page
3. Shows streaming tokens as they arrive
4. Renders CitationCard components for each citation
5. Shows a warning banner if judge emits a warning event
6. Shows an escalation card with dept contact if escalation is triggered
7. Has an Abort button that cancels the stream
Uses Tailwind CSS classes only (no custom CSS).
```

---

## DEBUGGING PROMPTS (use when things break)

### Debug: retrieval quality is bad
```
My retrieval Recall@3 is below 0.85 on the golden query set.
The failing queries are: [paste failing queries from evaluator output]

Look at scraper/evaluator.py and agent/db/postgres.py.
Diagnose: is this a chunking problem, embedding problem, or BM25 index problem?
Suggest fixes for the hybrid_search function and the article_to_chunks function.
```

### Debug: TTFT is too slow
```
My time to first token is above 500ms. 
Look at agent/nodes/ and agent/main.py.
Find where sequential awaits could be replaced with asyncio.gather().
Check: is the embedding happening before or in parallel with cache check?
Is intent classification happening in parallel with retrieval?
```

### Debug: judge keeps failing on correct answers
```
My LLM-as-Judge is returning FAIL even when the answer looks correct.
The judge prompt is in agent/nodes/judge.py.
Look at the judge prompt and make it less strict:
- It should only flag fee amounts, phone numbers, and deadlines
- It should NOT flag general process descriptions
- It should NOT flag information that's clearly from the context
Rewrite the judge prompt with these constraints.
```
