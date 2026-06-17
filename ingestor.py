# scraper/ingestor.py
"""
Chunks structured articles and upserts embeddings to Postgres pgvector.
Run: python run_ingest.py

Pipeline:
  article JSON → section chunks → embed → upsert to article_chunks
"""

import asyncio
import json
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import AsyncIterator

import asyncpg
from openai import AsyncOpenAI
import structlog

log = structlog.get_logger()
ARTICLES_DIR = Path("articles")
client = AsyncOpenAI()


def _to_date(value) -> date | None:
    """Coerce a value into a date object for asyncpg DATE columns.

    Accepts date/datetime objects, ISO date strings ('2026-06-10'), and
    ISO datetime strings ('2026-06-10T12:00:00+00:00'). Returns None on
    empty/invalid input so the DB stores NULL rather than crashing.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            try:
                return datetime.fromisoformat(value).date()
            except ValueError:
                log.warning("bad_date_value", value=value)
                return None
    return None

# Embedding config
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
EMBEDDING_BATCH_SIZE = 50  # embed this many chunks at once
MAX_CHUNK_TOKENS = 600      # target max tokens per chunk
# Hard ceiling per embedding request. OpenAI limit is 8192 tokens. We cap by
# BOTH words and characters: word-dense markdown (tables, ids, code) can blow
# past 8192 tokens well under the word budget, so the char cap is the real guard.
# ~14000 chars ≈ 3500-5000 tokens even for dense text — a safe margin.
MAX_EMBED_WORDS = 4000
MAX_EMBED_CHARS = 14000


def _split_text(text: str, max_words: int = MAX_EMBED_WORDS,
                max_chars: int = MAX_EMBED_CHARS) -> list[str]:
    """Split text into pieces that each fit within max_words AND max_chars.

    Breaks at line boundaries, starting a new piece whenever adding the next
    line would exceed either budget. Returns a single-item list if it fits.
    """
    if len(text.split()) <= max_words and len(text) <= max_chars:
        return [text]

    parts: list[str] = []
    current_lines: list[str] = []
    current_words = 0
    current_chars = 0
    for line in text.split("\n"):
        line_words = len(line.split())
        line_chars = len(line) + 1  # +1 for the rejoined newline
        if current_lines and (
            current_words + line_words > max_words
            or current_chars + line_chars > max_chars
        ):
            parts.append("\n".join(current_lines))
            current_lines = []
            current_words = 0
            current_chars = 0
        current_lines.append(line)
        current_words += line_words
        current_chars += line_chars
    if current_lines:
        parts.append("\n".join(current_lines))
    return parts or [text]


# ── Chunking ─────────────────────────────────────────────────────────────────

def article_to_chunks(article: dict) -> list[dict]:
    """
    Split a structured article into retrieval chunks.
    One chunk per section_type, with breadcrumb prepended.
    FAQs get one chunk each (high-precision retrieval targets).
    """
    meta = article["metadata"]
    content = article["content"]
    city_name = meta.get("city_name", "City of Arvada")
    category = meta.get("category", "general")
    title = content.get("title", "Untitled")
    permit_type = meta.get("permit_type", "general")

    # Breadcrumb prefix for every chunk — gives embedding model city + category context
    def breadcrumb(section: str) -> str:
        return f"{city_name} > {category.title()} > {title} > {section.replace('_', ' ').title()}\n\n"

    chunks = []

    def make_chunk(section_type: str, text: str, extra_meta: dict = {}) -> dict:
        if not text or len(text.strip()) < 20:
            return None
        full_text = breadcrumb(section_type) + text.strip()
        return {
            "chunk_id": str(uuid.uuid4()),
            "article_id": article.get("article_id"),
            "city_id": meta.get("city_id", "arvada-co"),
            "section_type": section_type,
            "content_text": full_text,
            "metadata": {
                "permit_type": permit_type,
                "category": category,
                "article_type": meta.get("article_type"),
                "tags": content.get("tags", []),
                "effective_date": meta.get("effective_date"),
                "source_urls": meta.get("source_urls", []),
                "title": title,
                **extra_meta,
            },
            "token_count": len(full_text.split()) * 1.3,  # rough estimate
        }

    # ── Summary chunk (always retrieved for context) ─────────────────────
    if content.get("summary"):
        c = make_chunk("summary", content["summary"])
        if c: chunks.append(c)

    # ── Body — free-form markdown article text (e.g. spreadsheet-imported KB
    #    articles). Split into embedding-sized pieces so long articles still fit.
    if content.get("body"):
        for part in _split_text(content["body"]):
            c = make_chunk("body", part)
            if c: chunks.append(c)

    # ── Eligibility ──────────────────────────────────────────────────────
    if content.get("eligibility"):
        c = make_chunk("eligibility", content["eligibility"])
        if c: chunks.append(c)

    # ── Steps ────────────────────────────────────────────────────────────
    if content.get("steps"):
        steps_text = "\n".join(
            f"Step {s['n']}: {s['title']}\n{s['text']}"
            + (f"\nPortal: {s.get('portal_url', '')}" if s.get('portal_url') else "")
            for s in content["steps"]
        )
        c = make_chunk("steps", steps_text)
        if c: chunks.append(c)

    # ── Required Documents ───────────────────────────────────────────────
    if content.get("required_documents"):
        docs_text = "\n\n".join(
            f"Document: {d['name']}\n"
            f"What it is: {d.get('description', '')}\n"
            f"Where to get it: {d.get('where_to_get', '')}\n"
            f"Example: {d.get('example', '')}"
            for d in content["required_documents"]
        )
        c = make_chunk("required_documents", docs_text)
        if c: chunks.append(c)

    # ── Form Fields (agent-only, not shown on portal) ────────────────────
    if content.get("form_fields"):
        fields_text = "\n\n".join(
            f"Field: {f['field_name']} ({f['label']})\n"
            f"What it means: {f.get('description', '')}\n"
            f"Example: {f.get('example_value', '')}\n"
            f"Why needed: {f.get('why_needed', '')}"
            for f in content["form_fields"]
        )
        c = make_chunk("form_fields", fields_text)
        if c: chunks.append(c)

    # ── Fees ─────────────────────────────────────────────────────────────
    if content.get("fees"):
        fees_text = "\n".join(
            f"- {f['name']}: {f.get('amount') or f.get('rate', 'variable')}"
            + (f" — {f['notes']}" if f.get('notes') else "")
            for f in content["fees"]
        )
        for part in _split_text(fees_text):
            c = make_chunk("fees", part)
            if c: chunks.append(c)

    # ── Timelines ────────────────────────────────────────────────────────
    if content.get("timelines"):
        t = content["timelines"]
        timeline_text = (
            f"Review time: {t.get('review', 'varies')}\n"
            f"Inspection: {t.get('inspection', 'varies')}\n"
            f"Total: {t.get('total', 'varies')}"
        )
        c = make_chunk("timelines", timeline_text)
        if c: chunks.append(c)

    # ── FAQs — one chunk each (split if body exceeds embedding limit) ────────
    for i, faq in enumerate(content.get("faqs", [])):
        answer = faq.get("a", "")
        parts = _split_text(answer)
        for part_idx, part in enumerate(parts):
            label = faq["q"] if len(parts) == 1 else f"{faq['q']} (part {part_idx + 1}/{len(parts)})"
            faq_text = f"Q: {label}\nA: {part}"
            c = make_chunk("faq", faq_text, {
                "faq_index": i,
                "question": faq["q"],
                **({"part": part_idx + 1, "total_parts": len(parts)} if len(parts) > 1 else {}),
            })
            if c:
                chunks.append(c)

    # ── Contact (always appended as final chunk) ──────────────────────────
    if content.get("contact"):
        ct = content["contact"]
        contact_text = (
            f"Department: {ct.get('dept', '')}\n"
            f"Phone: {ct.get('phone', '')}\n"
            f"Address: {ct.get('address', '')}"
        )
        c = make_chunk("contact", contact_text)
        if c: chunks.append(c)

    return [c for c in chunks if c is not None]


# ── Embedding ─────────────────────────────────────────────────────────────────

async def embed_chunks_batch(chunks: list[dict]) -> list[dict]:
    """Embed a batch of chunks. Returns chunks with embedding field added."""
    texts = [c["content_text"] for c in chunks]

    response = await client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
        dimensions=EMBEDDING_DIM,
    )

    for i, chunk in enumerate(chunks):
        chunk["embedding"] = response.data[i].embedding

    return chunks


async def embed_all_chunks(chunks: list[dict]) -> AsyncIterator[list[dict]]:
    """Embed chunks in batches. Yields embedded batches."""
    for i in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
        batch = chunks[i:i + EMBEDDING_BATCH_SIZE]
        embedded = await embed_chunks_batch(batch)
        log.info("embedded_batch",
                 batch_start=i,
                 batch_size=len(embedded),
                 total=len(chunks))
        yield embedded
        await asyncio.sleep(0.1)  # rate limiting


# ── Postgres Upsert ────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO article_chunks (
    id, city_id, article_id, chunk_index, section_type,
    content_text, embedding, metadata, token_count
) VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8, $9)
ON CONFLICT (article_id, chunk_index)
DO UPDATE SET
    content_text = EXCLUDED.content_text,
    embedding = EXCLUDED.embedding,
    metadata = EXCLUDED.metadata,
    token_count = EXCLUDED.token_count
"""

INSERT_ARTICLE_SQL = """
INSERT INTO knowledge_articles (
    id, city_id, article_type, permit_type, category,
    title, slug, content, tags, source_urls, ordinance_refs,
    effective_date, version, status, last_verified
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
ON CONFLICT (slug) DO UPDATE SET
    content = EXCLUDED.content,
    version = knowledge_articles.version + 1,
    last_verified = EXCLUDED.last_verified,
    status = EXCLUDED.status
RETURNING id
"""


async def upsert_article(pool: asyncpg.Pool, article: dict, slug_key: str) -> str:
    """Insert or update a knowledge_article. Returns article_id.

    `slug_key` must be a stable, unique-per-article identifier (the source
    filename stem is used). Deriving the slug from the title is unsafe because
    many ordinance sections share the same 50-char title prefix and would
    collide on ON CONFLICT (slug), silently overwriting each other.
    """
    meta = article["metadata"]
    content = article["content"]

    article_id = str(uuid.uuid4())
    title = content.get("title", "Untitled")
    slug = f"{meta['city_id']}/{meta.get('category', 'general')}/{slug_key}"

    tags = content.get("tags") or meta.get("tags") or []
    ordinance_refs = content.get("ordinance_refs") or meta.get("ordinance_refs") or []

    async with pool.acquire() as conn:
        # SET LOCAL only takes effect inside a transaction, so the RLS tenant
        # context must be set and used within the same transaction block.
        async with conn.transaction():
            # set_config(..., is_local=true) is transaction-scoped like SET LOCAL,
            # but parameterized (no injection) and matches current_setting() in RLS.
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)",
                meta["city_id"],
            )

            row = await conn.fetchrow(
                INSERT_ARTICLE_SQL,
                article_id,
                meta["city_id"],
                meta.get("article_type", "permit"),
                meta.get("permit_type"),
                meta.get("category"),
                title,
                slug,
                json.dumps(content),
                tags,
                meta.get("source_urls", []),
                ordinance_refs,
                _to_date(meta.get("effective_date")),
                meta.get("version", 1),
                meta.get("status", "review"),
                _to_date(meta.get("last_verified")),
            )
        return str(row["id"])


async def upsert_chunks(pool: asyncpg.Pool, article_id: str,
                        chunks: list[dict], city_id: str):
    """Upsert all chunks for an article."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", city_id
            )

            for i, chunk in enumerate(chunks):
                await conn.execute(
                    UPSERT_SQL,
                    chunk["chunk_id"],
                    city_id,
                    article_id,
                    i,
                    chunk["section_type"],
                    chunk["content_text"],
                    str(chunk["embedding"]),  # pgvector format
                    json.dumps(chunk["metadata"]),
                    int(chunk.get("token_count", 0)),
                )

    log.info("chunks_upserted", article_id=article_id, count=len(chunks))


# ── Main Ingest Runner ─────────────────────────────────────────────────────────

async def ingest_all(postgres_url: str) -> dict:
    """Ingest all articles from articles/ into Postgres pgvector."""
    import os
    pool = await asyncpg.create_pool(
        postgres_url or os.getenv("POSTGRES_URL"),
        min_size=2, max_size=5,
        server_settings={"search_path": "arvada_agent, public"},
    )

    article_files = list(ARTICLES_DIR.glob("*.json"))
    # Only ingest published articles (not drafts/review)
    published = [f for f in article_files
                 if json.loads(f.read_text(encoding="utf-8")).get("metadata", {}).get("status") == "published"]

    log.info("ingesting", total=len(article_files), published=len(published),
             skipped=len(article_files) - len(published))

    results = {"ingested": 0, "failed": 0, "skipped": 0, "total_chunks": 0}

    # Pre-load slugs that already have chunks in the DB so we can skip them on
    # re-runs and avoid paying for re-embedding articles that are already complete.
    async with pool.acquire() as conn:
        existing_slugs = {
            row["slug"]
            for row in await conn.fetch(
                "SELECT slug FROM knowledge_articles WHERE city_id = 'arvada-co'"
                " AND id IN (SELECT DISTINCT article_id FROM article_chunks)"
            )
        }
    log.info("existing_in_db", count=len(existing_slugs))

    for article_file in published:
        try:
            article = json.loads(article_file.read_text(encoding="utf-8"))
            city_id = article["metadata"]["city_id"]
            meta = article["metadata"]
            slug = f"{city_id}/{meta.get('category', 'general')}/{article_file.stem}"

            # Skip articles that are already fully ingested (article + chunks present)
            if slug in existing_slugs:
                results["skipped"] += 1
                continue

            # 1. Upsert article record (slug keyed on unique filename stem)
            article_id = await upsert_article(pool, article, article_file.stem)
            article["article_id"] = article_id

            # 2. Chunk the article
            chunks = article_to_chunks(article)
            log.info("chunked", article=article["content"].get("title"), chunks=len(chunks))

            # 3. Embed + upsert chunks
            async for embedded_batch in embed_all_chunks(chunks):
                await upsert_chunks(pool, article_id, embedded_batch, city_id)
                results["total_chunks"] += len(embedded_batch)

            results["ingested"] += 1

        except Exception as e:
            log.error("ingest_failed", file=str(article_file), error=str(e))
            results["failed"] += 1

    try:
        await pool.close()
    except OSError:
        # WinError 121 (semaphore timeout) can fire when closing connections
        # to a remote Postgres server — the work is already done, safe to ignore.
        pass
    log.info("ingest_complete", **results)
    return results
