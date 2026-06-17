"""
db.py
=====
Shared asyncpg pool + hybrid_search wrapper.

RLS: every tenant-scoped query must run inside a transaction that first sets
`app.tenant_id` via set_config (SET LOCAL doesn't accept bind params). The
`tenant_conn` async context manager handles that.
"""
from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import asyncpg
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings
from brand import scrub, scrub_obj

_pool: asyncpg.Pool | None = None

# Transient connection drops (incl. Windows WinError 121 surfacing as OSError).
_DB_RETRYABLE = (
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    ConnectionError,
    OSError,
)
_db_retry = retry(
    retry=retry_if_exception_type(_DB_RETRYABLE),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    stop=stop_after_attempt(3),
    reraise=True,
)


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.postgres_url,
            min_size=1,
            max_size=10,
            server_settings={"search_path": "arvada_agent, public"},
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except OSError:
            # WinError 121 on idle-connection teardown against remote PG
            pass
        _pool = None


@contextlib.asynccontextmanager
async def tenant_conn(city_id: str) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection with RLS tenant context set for the transaction."""
    if not city_id:
        # RLS policies compare against app.tenant_id; an empty tenant would make
        # every USING clause fall through. Refuse rather than risk a cross-tenant read.
        raise ValueError("tenant_conn requires a non-empty city_id")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            applied = await conn.fetchval(
                "SELECT set_config('app.tenant_id', $1, true)", city_id
            )
            if applied != city_id:
                # set_config echoes the value it set; a mismatch means RLS isn't
                # scoped as intended, so we must not run tenant queries on this conn.
                raise RuntimeError(
                    f"RLS tenant context not applied (wanted {city_id!r}, got {applied!r})"
                )
            yield conn


def _vec_literal(embedding: list[float]) -> str:
    """pgvector literal — asyncpg has no native vector codec."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


@_db_retry
async def hybrid_search(
    city_id: str,
    query: str,
    embedding: list[float],
    permit_type: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """RRF of tsvector FTS + pgvector ANN. Returns chunk dicts."""
    async with tenant_conn(city_id) as conn:
        rows = await conn.fetch(
            "SELECT chunk_id, article_id, rrf_score, content_text, section_type, metadata "
            "FROM hybrid_search($1, $2::vector, $3, $4, $5)",
            query,
            _vec_literal(embedding),
            city_id,
            permit_type,
            limit,
        )
    return [_row_to_chunk(r) for r in rows]


def _row_to_chunk(row: asyncpg.Record) -> dict[str, Any]:
    import json

    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    meta = meta or {}
    source_urls = meta.get("source_urls") or []
    return {
        "chunk_id": str(row["chunk_id"]),
        "article_id": str(row["article_id"]),
        "rrf_score": float(row["rrf_score"]) if row["rrf_score"] is not None else 0.0,
        "content_text": scrub(row["content_text"]),       # keep the competitor name out of LLM context + display
        "section_type": row["section_type"],
        "article_title": scrub(meta.get("title")),
        "source_url": source_urls[0] if source_urls else None,
        "source_urls": source_urls,
        "effective_date": meta.get("effective_date") or meta.get("last_verified"),
        "permit_type": meta.get("permit_type"),
        "metadata": meta,
    }


@_db_retry
async def fetch_fee_schedule(city_id: str, permit_type: str) -> list[dict[str, Any]]:
    """Deterministic fee rows for a permit type (used by the fee engine)."""
    import json

    async with tenant_conn(city_id) as conn:
        # DISTINCT ON guards against duplicate seed rows (fee_schedules has no
        # unique constraint), keeping the newest effective row per fee component.
        rows = await conn.fetch(
            "SELECT DISTINCT ON (fee_type, calc_method) "
            "  permit_type, fee_type, calc_method, value, table_data, effective_date, notes "
            "FROM fee_schedules WHERE city_id = $1 AND permit_type = $2 "
            "ORDER BY fee_type, calc_method, effective_date DESC",
            city_id,
            permit_type,
        )
    out = []
    for r in rows:
        td = r["table_data"]
        if isinstance(td, str):
            td = json.loads(td)
        out.append(
            {
                "permit_type": r["permit_type"],
                "fee_type": r["fee_type"],
                "calc_method": r["calc_method"],
                "value": float(r["value"]) if r["value"] is not None else None,
                "table_data": td,
                "effective_date": r["effective_date"],
                "notes": r["notes"],
            }
        )
    return out


@_db_retry
async def fetch_recent_changes(
    city_id: str,
    permit_type: str | None = None,
    category: str | None = None,
    change_type: str | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Recent fee/code/process changes for the "What Changed?" feature.

    Ordered newest-effective first. When `permit_type` is given, rows for that
    permit AND city-wide rows (permit_type IS NULL) are both returned, so a
    building-permit question still surfaces a city-wide tax change.
    """
    clauses = ["city_id = $1"]
    params: list[Any] = [city_id]
    if permit_type:
        params.append(permit_type)
        clauses.append(f"(permit_type = ${len(params)} OR permit_type IS NULL)")
    if category:
        params.append(category)
        clauses.append(f"category = ${len(params)}")
    if change_type:
        params.append(change_type)
        clauses.append(f"change_type = ${len(params)}")
    params.append(limit)
    sql = (
        "SELECT id, change_type, permit_type, category, title, summary, "
        "       old_value, new_value, ordinance_ref, source_article_id, effective_date "
        "FROM change_log "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY effective_date DESC, detected_at DESC "
        f"LIMIT ${len(params)}"
    )
    async with tenant_conn(city_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [
        {
            "id": str(r["id"]),
            "change_type": r["change_type"],
            "permit_type": r["permit_type"],
            "category": r["category"],
            "title": r["title"],
            "summary": r["summary"],
            "old_value": r["old_value"],
            "new_value": r["new_value"],
            "ordinance_ref": r["ordinance_ref"],
            "source_article_id": str(r["source_article_id"]) if r["source_article_id"] else None,
            "effective_date": str(r["effective_date"]) if r["effective_date"] else None,
        }
        for r in rows
    ]


@_db_retry
async def list_articles(
    city_id: str,
    q: str | None = None,
    category: str | None = None,
    article_type: str | None = None,
    limit: int = 3000,
) -> list[dict[str, Any]]:
    """Lightweight article index (no chunk text) for the browsable knowledge base."""
    clauses = ["city_id = $1", "status <> 'archived'"]
    params: list[Any] = [city_id]
    if category:
        params.append(category)
        clauses.append(f"category = ${len(params)}")
    if article_type:
        params.append(article_type)
        clauses.append(f"article_type = ${len(params)}")
    if q:
        params.append(f"%{q}%")
        idx = len(params)
        clauses.append(f"(title ILIKE ${idx} OR content->>'summary' ILIKE ${idx})")
    params.append(limit)
    sql = (
        "SELECT id, title, article_type, category, permit_type, "
        "       content->>'summary' AS summary, effective_date, ordinance_refs, tags "
        "FROM knowledge_articles "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY category NULLS LAST, article_type, title "
        f"LIMIT ${len(params)}"
    )
    async with tenant_conn(city_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [
        {
            "id": str(r["id"]),
            "title": scrub(r["title"]),
            "article_type": r["article_type"],
            "category": r["category"],
            "permit_type": r["permit_type"],
            "summary": scrub(r["summary"]),
            "effective_date": str(r["effective_date"]) if r["effective_date"] else None,
            "ordinance_refs": list(r["ordinance_refs"] or []),
            "tags": list(r["tags"] or []),
        }
        for r in rows
    ]


@_db_retry
async def fetch_article(city_id: str, article_id: str) -> dict[str, Any] | None:
    """Fetch one knowledge article (structured content) for the in-app viewer."""
    import json

    async with tenant_conn(city_id) as conn:
        try:
            row = await conn.fetchrow(
                "SELECT id, title, slug, article_type, category, permit_type, content, "
                "       tags, source_urls, ordinance_refs, effective_date, last_verified, updated_at "
                "FROM knowledge_articles "
                "WHERE city_id = $1 AND id = $2::uuid AND status <> 'archived'",
                city_id,
                article_id,
            )
        except (asyncpg.DataError, ValueError):
            return None  # malformed UUID
    if not row:
        return None

    content = row["content"]
    if isinstance(content, str):
        content = json.loads(content)
    return {
        "id": str(row["id"]),
        "title": scrub(row["title"]),
        "slug": row["slug"],
        "article_type": row["article_type"],
        "category": row["category"],
        "permit_type": row["permit_type"],
        "content": scrub_obj(content or {}),
        "tags": list(row["tags"] or []),
        "source_urls": list(row["source_urls"] or []),
        "ordinance_refs": list(row["ordinance_refs"] or []),
        "effective_date": str(row["effective_date"]) if row["effective_date"] else None,
        "last_verified": str(row["last_verified"]) if row["last_verified"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@_db_retry
async def related_articles(
    city_id: str, article_id: str, limit: int = 4
) -> list[dict[str, Any]]:
    """Semantically-nearest articles to `article_id`, via the stored chunk embeddings.

    Reuses the embeddings already in `article_chunks` (no re-embedding): we take the
    centroid of the source article's chunk vectors and rank every OTHER article by the
    cosine distance of its closest chunk to that centroid. Returns the same lightweight
    shape as `list_articles` so the in-app viewer can render the cards directly.
    Empty when the source article has no embedded chunks.
    """
    sql = (
        "WITH src AS ("
        "  SELECT avg(embedding) AS centroid FROM article_chunks"
        "  WHERE city_id = $1 AND article_id = $2::uuid AND embedding IS NOT NULL"
        ") "
        "SELECT ka.id, ka.title, ka.article_type, ka.category, ka.permit_type, "
        "       ka.content->>'summary' AS summary, "
        "       MIN(ac.embedding <=> src.centroid) AS dist "
        "FROM article_chunks ac "
        "CROSS JOIN src "
        "JOIN knowledge_articles ka ON ka.id = ac.article_id "
        "WHERE ac.city_id = $1 AND ac.article_id <> $2::uuid "
        "  AND ac.embedding IS NOT NULL AND src.centroid IS NOT NULL "
        "  AND ka.status <> 'archived' "
        "GROUP BY ka.id "  # ka.id is the PK, so the other ka.* columns are functionally dependent
        "ORDER BY dist "
        "LIMIT $3"
    )
    async with tenant_conn(city_id) as conn:
        try:
            rows = await conn.fetch(sql, city_id, article_id, limit)
        except (asyncpg.DataError, ValueError):
            return []  # malformed UUID
    return [
        {
            "id": str(r["id"]),
            "title": scrub(r["title"]),
            "article_type": r["article_type"],
            "category": r["category"],
            "permit_type": r["permit_type"],
            "summary": scrub(r["summary"]),
        }
        for r in rows
    ]


_VIEWS_DDL = """
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
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_article_views_city_time
    ON article_views (city_id, viewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_article_views_city_article
    ON article_views (city_id, article_id);
"""


_FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS kb_feedback (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city_id       TEXT NOT NULL,
    article_id    UUID REFERENCES knowledge_articles(id) ON DELETE SET NULL,
    article_title TEXT,
    rating        TEXT NOT NULL,
    reason        TEXT,
    comment       TEXT,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE kb_feedback ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY kf_city_isolation ON kb_feedback
      USING (city_id = current_setting('app.tenant_id', true))
      WITH CHECK (city_id = current_setting('app.tenant_id', true));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_kb_feedback_city_time ON kb_feedback (city_id, created_at DESC);
"""


def _sr_number(fid: int) -> str:
    """Derive a stable SR number from the feedback row id."""
    return f"SR-KB-{int(fid):05d}"


async def ensure_feedback_table() -> None:
    """Idempotently create kb_feedback (best-effort; never blocks boot)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_FEEDBACK_DDL)
    except Exception:  # noqa: BLE001
        pass


@_db_retry
async def record_feedback(
    city_id: str, article_id: str | None, article_title: str | None,
    rating: str, reason: str | None, comment: str | None,
) -> dict[str, Any]:
    """Insert a feedback row; returns its SR number + timestamp."""
    rating = "bad" if str(rating).lower() in ("bad", "down", "thumbs_down", "negative") else "good"
    async with tenant_conn(city_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO kb_feedback (city_id, article_id, article_title, rating, reason, comment) "
            "VALUES ($1, $2::uuid, $3, $4, $5, $6) RETURNING id, created_at",
            city_id, article_id, scrub(article_title), rating, reason, scrub(comment),
        )
    return {"sr_number": _sr_number(row["id"]), "id": int(row["id"]),
            "rating": rating, "created_at": row["created_at"].isoformat()}


@_db_retry
async def list_feedback(
    city_id: str, rating: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Feedback SR queue (newest first), optionally filtered by rating."""
    clauses = ["city_id = $1"]
    params: list[Any] = [city_id]
    if rating in ("good", "bad"):
        params.append(rating)
        clauses.append(f"rating = ${len(params)}")
    params.append(limit)
    sql = (
        "SELECT id, article_id, article_title, rating, reason, comment, status, created_at "
        f"FROM kb_feedback WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    async with tenant_conn(city_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [
        {
            "id": int(r["id"]), "sr_number": _sr_number(r["id"]),
            "article_id": str(r["article_id"]) if r["article_id"] else None,
            "article_title": scrub(r["article_title"]),
            "rating": r["rating"], "reason": r["reason"], "comment": scrub(r["comment"]),
            "status": r["status"], "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


@_db_retry
async def get_feedback(city_id: str, feedback_id: int) -> dict[str, Any] | None:
    async with tenant_conn(city_id) as conn:
        r = await conn.fetchrow(
            "SELECT id, article_id, article_title, rating, reason, comment, status, created_at "
            "FROM kb_feedback WHERE city_id = $1 AND id = $2",
            city_id, feedback_id,
        )
    if not r:
        return None
    return {
        "id": int(r["id"]), "sr_number": _sr_number(r["id"]),
        "article_id": str(r["article_id"]) if r["article_id"] else None,
        "article_title": scrub(r["article_title"]),
        "rating": r["rating"], "reason": r["reason"], "comment": scrub(r["comment"]),
        "status": r["status"], "created_at": r["created_at"].isoformat(),
    }


@_db_retry
async def update_article(
    city_id: str, article_id: str,
    title: str | None = None, summary: str | None = None,
    content: dict | None = None, publish: bool = True,
) -> dict[str, Any] | None:
    """Admin edit: patch title/summary/content and (by default) publish."""
    import json

    async with tenant_conn(city_id) as conn:
        async with conn.transaction():
            try:
                row = await conn.fetchrow(
                    "SELECT content FROM knowledge_articles WHERE city_id = $1 AND id = $2::uuid",
                    city_id, article_id,
                )
            except (asyncpg.DataError, ValueError):
                return None
            if not row:
                return None
            cur = row["content"]
            if isinstance(cur, str):
                cur = json.loads(cur)
            cur = cur or {}
            if content is not None:
                cur = content
            if summary is not None:
                cur["summary"] = summary
            sets = ["content = $3::jsonb", "updated_at = NOW()"]
            params: list[Any] = [city_id, article_id, json.dumps(cur)]
            if title is not None:
                params.append(title); sets.append(f"title = ${len(params)}")
            if publish:
                sets.append("status = 'published'")
            await conn.execute(
                f"UPDATE knowledge_articles SET {', '.join(sets)} WHERE city_id = $1 AND id = $2::uuid",
                *params,
            )
    return await fetch_article(city_id, article_id)


async def ensure_views_table() -> None:
    """Idempotently create the article_views table (for Trending).

    The shared schema (init.sql) defines it, but a long-lived UAT DB may predate
    this feature — so we create-if-missing at startup. Best-effort: a failure here
    must not block app boot (Trending simply stays empty).
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_VIEWS_DDL)
    except Exception:  # noqa: BLE001 — never fatal
        pass


@_db_retry
async def record_view(city_id: str, article_id: str) -> None:
    """Append one article view (fire-and-forget). No-op if the table is missing."""
    try:
        async with tenant_conn(city_id) as conn:
            await conn.execute(
                "INSERT INTO article_views (city_id, article_id) VALUES ($1, $2::uuid)",
                city_id,
                article_id,
            )
    except (asyncpg.UndefinedTableError, asyncpg.DataError, ValueError):
        return  # table not migrated yet, or malformed UUID — silently skip


@_db_retry
async def nav_articles(city_id: str) -> list[dict[str, Any]]:
    """Lightweight list for the article-page left nav (real articles, no ordinances)."""
    sql = (
        "SELECT id, title, article_type, category FROM knowledge_articles "
        "WHERE city_id = $1 AND status <> 'archived' AND article_type <> 'ordinance' "
        "ORDER BY category NULLS LAST, title"
    )
    async with tenant_conn(city_id) as conn:
        rows = await conn.fetch(sql, city_id)
    return [
        {"id": str(r["id"]), "title": scrub(r["title"]),
         "article_type": r["article_type"], "category": r["category"]}
        for r in rows
    ]


@_db_retry
async def trending_articles(
    city_id: str, limit: int = 12, days: int = 30
) -> list[dict[str, Any]]:
    """Most-viewed articles over the last `days`, in the list_articles shape."""
    sql = (
        "SELECT ka.id, ka.title, ka.article_type, ka.category, ka.permit_type, "
        "       ka.content->>'summary' AS summary, ka.effective_date, ka.ordinance_refs, "
        "       count(*) AS views "
        "FROM article_views av "
        "JOIN knowledge_articles ka ON ka.id = av.article_id "
        "WHERE av.city_id = $1 AND av.viewed_at >= NOW() - ($2 || ' days')::interval "
        "  AND ka.status <> 'archived' "
        "GROUP BY ka.id "
        "ORDER BY views DESC, ka.title "
        "LIMIT $3"
    )
    try:
        async with tenant_conn(city_id) as conn:
            rows = await conn.fetch(sql, city_id, str(days), limit)
    except asyncpg.UndefinedTableError:
        return []
    return [
        {
            "id": str(r["id"]),
            "title": scrub(r["title"]),
            "article_type": r["article_type"],
            "category": r["category"],
            "permit_type": r["permit_type"],
            "summary": scrub(r["summary"]),
            "effective_date": str(r["effective_date"]) if r["effective_date"] else None,
            "ordinance_refs": list(r["ordinance_refs"] or []),
            "views": int(r["views"]),
        }
        for r in rows
    ]


async def insert_escalation(
    city_id: str,
    session_id: str,
    query: str,
    conversation: list[dict],
    intent: str | None,
    permit_type: str | None,
    routed_to_dept: str | None,
) -> str:
    """Write a row to escalation_queue; returns the ticket UUID."""
    import json

    async with tenant_conn(city_id) as conn:
        row = await conn.fetchrow(
            "INSERT INTO escalation_queue "
            "(city_id, session_id, query, conversation, intent, permit_type, routed_to_dept) "
            "VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7) RETURNING id",
            city_id,
            session_id,
            query,
            json.dumps(conversation),
            intent,
            permit_type,
            routed_to_dept,
        )
    return str(row["id"])


async def health_check() -> dict[str, Any]:
    """Lightweight DB readiness probe."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM article_chunks WHERE city_id = $1",
                settings.default_city_id,
            )
        return {"ok": True, "chunks": int(count)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
