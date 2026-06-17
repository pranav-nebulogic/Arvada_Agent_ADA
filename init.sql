-- init.sql
-- Run this in DBeaver against your existing Postgres instance.
--
-- TENANT ALIGNMENT:
--   city_id values in this schema must match public.tenants.code in the main project.
--   There is NO cities table here — the main project's public.tenants is the source of truth.
--   e.g. city_id = 'fortwayne_311' matches tenants.code = 'fortwayne_311'
--   For Arvada (standalone pilot) we use city_id = 'arvada-co'.
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
CREATE SCHEMA IF NOT EXISTS arvada_agent;
-- Keep public in path so the vector type (installed in public schema) is resolvable
SET search_path TO arvada_agent, public;

-- ── Helper: set RLS context ───────────────────────────────────────────────
-- App must call this before every query:
-- SET LOCAL app.tenant_id = 'arvada-co';
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
      AND (p_permit_type IS NULL OR metadata->>'permit_type' = p_permit_type)
    LIMIT 25
),
dense_results AS (
    -- pgvector ANN search (HNSW, cosine distance)
    SELECT
        id,
        ROW_NUMBER() OVER (ORDER BY embedding <=> p_embedding) AS dense_rank
    FROM article_chunks
    WHERE city_id = p_city_id
      AND (p_permit_type IS NULL OR metadata->>'permit_type' = p_permit_type)
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

-- Seed Arvada building permit fees (Table 18-1, 2026 schedule)
INSERT INTO fee_schedules (city_id, permit_type, fee_type, calc_method, value, table_data, effective_date) VALUES
('arvada-co', 'building_permit', 'permit_fee', 'table_18_1', NULL, '[
    {"min": 1,      "max": 500,       "base": 34.00,    "per_1000_rate": 0},
    {"min": 501,    "max": 2000,      "base": 34.00,    "per_1000_rate": 30.50},
    {"min": 2001,   "max": 25000,     "base": 79.75,    "per_1000_rate": 14.00},
    {"min": 25001,  "max": 50000,     "base": 401.75,   "per_1000_rate": 10.10},
    {"min": 50001,  "max": 100000,    "base": 654.25,   "per_1000_rate": 7.00},
    {"min": 100001, "max": 500000,    "base": 1004.25,  "per_1000_rate": 5.60},
    {"min": 500001, "max": 1000000,   "base": 3244.25,  "per_1000_rate": 4.85},
    {"min": 1000001,"max": 999999999, "base": 5669.25,  "per_1000_rate": 3.45}
]', '2026-01-01'),
('arvada-co', 'building_permit',        'plan_review',  'flat', 32.50, NULL, '2026-01-01'),
('arvada-co', 'building_permit',        'use_tax',      'percentage_of_valuation', 0.0346, NULL, '2026-01-01'),
('arvada-co', 'building_permit_solar',  'permit_fee',   'flat', 45.00, NULL, '2026-01-01'),
('arvada-co', 'building_permit_solar',  'plan_review',  'flat', 32.50, NULL, '2026-01-01'),
('arvada-co', 'building_permit_windows_siding', 'permit_fee', 'flat', 45.00, NULL, '2026-01-01'),
('arvada-co', 'food_truck_permit',      'permit_fee',   'flat', 60.00, NULL, '2026-01-01'),
('arvada-co', 'str_permit',             'permit_fee',   'flat', 150.00, NULL, '2026-01-01'),
('arvada-co', 'special_event_permit',   'permit_fee',   'flat', 125.00, NULL, '2026-01-01'),
('arvada-co', 'retaining_wall',         'permit_fee',   'flat', 45.00, NULL, '2026-01-01'),
('arvada-co', 'retaining_wall',         'plan_review',  'flat', 32.50, NULL, '2026-01-01')
ON CONFLICT DO NOTHING;


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
-- one; the seed rows below model Arvada's 2025 -> 2026 transition.
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

-- Seed Arvada's 2026 schedule changes (mirrors the fee_schedules seed above).
INSERT INTO change_log
    (city_id, change_type, permit_type, category, title, summary, old_value, new_value, ordinance_ref, effective_date) VALUES
('arvada-co', 'fee', 'building_permit', 'building',
    'Building permit fees updated for 2026',
    'The Table 18-1 building permit fee schedule was updated under the 2026 schedule. A $25,000 project''s permit fee is now $401.75.',
    '$385.25', '$401.75', NULL, '2026-01-01'),
('arvada-co', 'fee', 'building_permit', 'tax',
    'Construction use tax rate increased',
    'The construction use tax applied to building permit valuations rose as part of the 2026 schedule.',
    '3.00%', '3.46%', NULL, '2026-01-01'),
('arvada-co', 'fee', 'str_permit', 'licensing',
    'Short-term rental permit fee increased',
    'The annual short-term rental permit fee was raised for 2026.',
    '$125.00', '$150.00', NULL, '2026-01-01'),
('arvada-co', 'fee', 'building_permit_solar', 'building',
    'Solar permit fee standardized',
    'Residential rooftop solar permits now carry a standardized flat permit fee for 2026.',
    '$50.00', '$45.00', NULL, '2026-01-01'),
('arvada-co', 'process', 'str_permit', 'licensing',
    'Short-term rental renewals move online',
    'Short-term rental license renewals are now submitted through the Arvada Permits portal instead of in person.',
    NULL, NULL, NULL, '2026-01-01')
ON CONFLICT DO NOTHING;


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
