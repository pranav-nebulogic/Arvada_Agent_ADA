-- init_woodinville_agent.sql — GENERATED from init.sql by gen_city_schema.py
-- City: City of Woodinville (woodinville-wa)  |  schema/database: woodinville_agent
-- Structure is identical to Arvada's; city-specific fee_schedules / change_log
-- rows are loaded separately (fee_schedule_transformer.py), not seeded here.

-- init.sql
-- Run this in DBeaver against your existing Postgres instance.
--
-- TENANT ALIGNMENT:
--   city_id values in this schema must match public.tenants.code in the main project.
--   There is NO cities table here — the main project's public.tenants is the source of truth.
--   e.g. city_id = 'fortwayne_311' matches tenants.code = 'fortwayne_311'
--   For Woodinville we use city_id = 'woodinville-wa'.
--
-- How to run:
--   1. Open DBeaver, connect to your database
--   2. Right-click the database → SQL Editor → New Script
--   3. Paste this file and execute (F5)

-- ── Extensions (run as superuser if needed) ────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector (embeddings + ANN search)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- uuid_generate_v4()
-- pg_search (ParadeDB BM25) is NOT required — we use standard tsvector full-text search

-- ── Schema ────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS woodinville_agent;
-- Keep public in path so the vector type (installed in public schema) is resolvable
SET search_path TO woodinville_agent, public;

-- ── Helper: set RLS context ───────────────────────────────────────────────
-- App must call this before every query:
-- SET LOCAL app.tenant_id = 'woodinville-wa';
-- city_id must match public.tenants.code in the main project.


-- ── knowledge_articles ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_articles (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id         TEXT NOT NULL,   -- matches public.tenants.code (main project)
    article_type    TEXT NOT NULL,  -- permit|license|faq|process|ordinance|tax
    permit_type     TEXT,           -- building_permit|str_permit|food_truck_permit...
    category        TEXT,           -- building|business|licensing|row|events|tax
    title           TEXT NOT NULL,
    slug            TEXT UNIQUE NOT NULL,  -- city_id/category/title-slug
    content         JSONB NOT NULL,         -- full structured article
    tags            TEXT[] NOT NULL DEFAULT '{}',
    source_urls     TEXT[] NOT NULL DEFAULT '{}',
    ordinance_refs  TEXT[] NOT NULL DEFAULT '{}',
    effective_date  DATE,
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'draft',  -- draft|review|published|archived
    last_verified   DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE knowledge_articles ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY ka_city_isolation ON knowledge_articles
      USING (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_ka_city_permit
    ON knowledge_articles (city_id, permit_type);
CREATE INDEX IF NOT EXISTS idx_ka_status
    ON knowledge_articles (city_id, status);


-- ── article_chunks ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS article_chunks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id         TEXT NOT NULL,
    article_id      UUID NOT NULL REFERENCES knowledge_articles(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    section_type    TEXT NOT NULL,  -- summary|eligibility|steps|required_documents|
                                    -- form_fields|fees|timelines|faq|contact
    content_text    TEXT NOT NULL,  -- plain text (for keyword search + display)
    content_tsv     TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content_text)) STORED,
    embedding       vector(1536),   -- text-embedding-3-small
    metadata        JSONB NOT NULL DEFAULT '{}',
    token_count     INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (article_id, chunk_index)
);

ALTER TABLE article_chunks ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY ac_city_isolation ON article_chunks
      USING (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ── Indexes ────────────────────────────────────────────────────────────────
-- HNSW for ANN vector search (cosine similarity)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON article_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- GIN for full-text keyword search (tsvector)
CREATE INDEX IF NOT EXISTS idx_chunks_fts
    ON article_chunks USING gin (content_tsv);

-- Composite for metadata filtering
CREATE INDEX IF NOT EXISTS idx_chunks_city_permit
    ON article_chunks ((metadata->>'permit_type'), city_id);
CREATE INDEX IF NOT EXISTS idx_chunks_city_section
    ON article_chunks (city_id, section_type);


-- ── Hybrid search function (tsvector FTS + dense vector, RRF) ─────────────
-- Uses standard PostgreSQL full-text search (no ParadeDB required).
-- Reciprocal Rank Fusion combines keyword rank + vector rank.
CREATE OR REPLACE FUNCTION hybrid_search(
    p_query         TEXT,
    p_embedding     vector(1536),
    p_city_id       TEXT,
    p_permit_type   TEXT DEFAULT NULL,
    p_limit         INTEGER DEFAULT 10
)
RETURNS TABLE (
    chunk_id        UUID,
    article_id      UUID,
    rrf_score       FLOAT,
    content_text    TEXT,
    section_type    TEXT,
    metadata        JSONB
)
LANGUAGE sql STABLE
AS $$
WITH fts_results AS (
    -- Standard PostgreSQL full-text search using generated tsvector column
    SELECT
        id,
        ROW_NUMBER() OVER (ORDER BY ts_rank_cd(content_tsv, query) DESC) AS fts_rank
    FROM article_chunks,
         websearch_to_tsquery('english', p_query) AS query
    WHERE city_id = p_city_id
      AND content_tsv @@ query
      -- A permit_type is a PREFERENCE, not a wall. Requiring an exact match
      -- excluded every city-wide article (permit_type IS NULL), which is
      -- almost the whole corpus: when the classifier said 'building_permit',
      -- Woodinville searched 15 of 4,130 chunks (0.4%) and Arvada 55 of 6,642
      -- (0.8%) -- so the more precisely a citizen asked, the less could be
      -- found. All 1,800 WMC sections and every submittal checklist vanished.
      -- db.py's own permit filter already did it this way; the SQL did not.
      -- RRF still ranks on relevance, so permit-specific chunks keep their edge.
      AND (p_permit_type IS NULL
           OR metadata->>'permit_type' = p_permit_type
           OR metadata->>'permit_type' IS NULL)
    LIMIT 25
),
dense_results AS (
    -- pgvector ANN search (HNSW, cosine distance)
    SELECT
        id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> p_embedding) AS dense_rank
    FROM article_chunks
    WHERE city_id = p_city_id
      -- A permit_type is a PREFERENCE, not a wall. Requiring an exact match
      -- excluded every city-wide article (permit_type IS NULL), which is
      -- almost the whole corpus: when the classifier said 'building_permit',
      -- Woodinville searched 15 of 4,130 chunks (0.4%) and Arvada 55 of 6,642
      -- (0.8%) -- so the more precisely a citizen asked, the less could be
      -- found. All 1,800 WMC sections and every submittal checklist vanished.
      -- db.py's own permit filter already did it this way; the SQL did not.
      -- RRF still ranks on relevance, so permit-specific chunks keep their edge.
      AND (p_permit_type IS NULL
           OR metadata->>'permit_type' = p_permit_type
           OR metadata->>'permit_type' IS NULL)
    ORDER BY embedding <=> p_embedding
    LIMIT 25
),
fused AS (
    SELECT
        COALESCE(f.id, d.id)                                   AS id,
        COALESCE(1.0 / (60 + f.fts_rank), 0)
        + COALESCE(1.0 / (60 + d.dense_rank), 0)              AS rrf_score
    FROM fts_results f
    FULL OUTER JOIN dense_results d ON f.id = d.id
)
SELECT
    ac.id           AS chunk_id,
    ac.article_id,
    fused.rrf_score,
    ac.content_text,
    ac.section_type,
    ac.metadata
FROM fused
JOIN article_chunks ac ON ac.id = fused.id
ORDER BY fused.rrf_score DESC
LIMIT p_limit;
$$;

-- ── fee_schedules ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fee_schedules (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id         TEXT NOT NULL,   -- matches public.tenants.code (main project)
    permit_type     TEXT NOT NULL,
    fee_type        TEXT NOT NULL,       -- permit_fee|plan_review|use_tax|flat|per_trade
    calc_method     TEXT NOT NULL,       -- flat|table_18_1|percentage_of_valuation|per_unit
    value           NUMERIC,             -- flat amount OR percentage rate (e.g., 0.0346)
    table_data      JSONB,               -- [{min, max, base, per_1000_rate}] for tiered fees
    effective_date  DATE NOT NULL,
    expires_date    DATE,
    notes           TEXT
);



-- ── article_sources (freshness monitoring) ────────────────────────────────
CREATE TABLE IF NOT EXISTS article_sources (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id             TEXT NOT NULL,
    article_id          UUID REFERENCES knowledge_articles(id) ON DELETE CASCADE,
    source_url          TEXT NOT NULL,
    last_fetched_at     TIMESTAMPTZ,
    last_fetched_hash   TEXT,    -- SHA-256 of content body
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|fresh|stale|error
    error_message       TEXT,
    UNIQUE (city_id, source_url)
);


-- ── article_views (Smart Knowledge "Trending") ────────────────────────────
-- Append-only view log. "Trending" = articles with the most views over a recent
-- window (e.g. last 30 days). Written fire-and-forget when an article page opens.
CREATE TABLE IF NOT EXISTS article_views (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id     TEXT NOT NULL,
    article_id  UUID NOT NULL REFERENCES knowledge_articles(id) ON DELETE CASCADE,
    viewed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE article_views ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY av_city_isolation ON article_views
      USING (city_id = current_setting('app.tenant_id', true))
      WITH CHECK (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_article_views_city_time
    ON article_views (city_id, viewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_article_views_city_article
    ON article_views (city_id, article_id);


-- ── kb_feedback (Smart Knowledge article feedback -> Feedback SR queue) ────
-- Thumbs up/down on an article. A thumbs-down prompts a reason + comment and is
-- raised as a Feedback SR for admin review (read comment -> edit & publish the
-- article). SR# is derived from the row id (SR-KB-#####).
CREATE TABLE IF NOT EXISTS kb_feedback (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id       TEXT NOT NULL,
    article_id    UUID REFERENCES knowledge_articles(id) ON DELETE SET NULL,
    article_title TEXT,
    rating        TEXT NOT NULL,                 -- 'good' | 'bad'
    reason        TEXT,                           -- short reason category (bad only)
    comment       TEXT,                           -- free-text detail
    status        TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE kb_feedback ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY kf_city_isolation ON kb_feedback
      USING (city_id = current_setting('app.tenant_id', true))
      WITH CHECK (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_kb_feedback_city_time ON kb_feedback (city_id, created_at DESC);


-- ── change_log ("What Changed?" fee & code alerts) ─────────────────────────
-- Records material changes to fees, ordinances, processes, document
-- requirements, and deadlines. Two consumers:
--   1. WHATS_CHANGED intent  -> "what permit fees changed this year?" answers.
--   2. Inline enrichment      -> a fee/permit answer can proactively note
--      "heads up: this fee changed effective Jan 1, 2026 (was $X, now $Y)".
-- Populate at ingest time when a new schedule/article version supersedes an old
-- one; Woodinville's change rows are loaded separately (not seeded here).
CREATE TABLE IF NOT EXISTS change_log (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id            TEXT NOT NULL,   -- matches public.tenants.code (main project)
    change_type        TEXT NOT NULL,   -- fee|ordinance|process|document|deadline
    permit_type        TEXT,            -- canonical permit key, or NULL if city-wide
    category           TEXT,            -- building|business|licensing|row|events|tax
    title              TEXT NOT NULL,   -- short headline of the change
    summary            TEXT NOT NULL,   -- plain-English description
    old_value          TEXT,            -- previous value (e.g. "$385.50" / "3.00%")
    new_value          TEXT,            -- new value (e.g. "$401.75" / "3.46%")
    ordinance_ref      TEXT,            -- e.g. "17.08.040" when change_type = ordinance
    source_article_id  UUID REFERENCES knowledge_articles(id) ON DELETE SET NULL,
    effective_date     DATE NOT NULL,   -- when the change takes/took effect
    detected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE change_log ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY cl_city_isolation ON change_log
      USING (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_change_log_city_eff
    ON change_log (city_id, effective_date DESC);
CREATE INDEX IF NOT EXISTS idx_change_log_city_permit
    ON change_log (city_id, permit_type, effective_date DESC);



-- ── escalation_queue ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escalation_queue (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    city_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    query           TEXT NOT NULL,
    conversation    JSONB,           -- last N turns of context
    intent          TEXT,
    permit_type     TEXT,
    routed_to_dept  TEXT,            -- building|revenue|planning|city_clerk
    status          TEXT NOT NULL DEFAULT 'open',  -- open|assigned|resolved
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE escalation_queue ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY eq_city_isolation ON escalation_queue
      USING (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
