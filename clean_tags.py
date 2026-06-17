"""
clean_tags.py
=============
One-off maintenance: recompute the `tags` column for every knowledge article.

The original ingest derived tags by taking the first N words of the section
*body*, which produced noise tags such as #generally, #against, #section. This
recomputes tags from the article **title** only, using the same stop-word
filtering the transformers now apply, so existing rows match new ingests.

Run:  .venv\\Scripts\\python.exe clean_tags.py
"""
from __future__ import annotations

import asyncio
import re

import asyncpg

from config import settings

# Mirror of transformer._STOPWORDS (kept inline so this script doesn't import
# transformer.py, which instantiates an OpenAI client at module load).
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "what", "how", "when", "where", "why",
    "who", "which", "i", "my", "me", "we", "our", "you", "your", "it", "its",
    "this", "that", "there", "their", "they", "if", "from", "by", "as", "not",
    "shall", "must", "any", "all", "such", "no", "nor", "than", "then",
    "city", "arvada", "colorado", "county", "jefferson", "town", "municipal",
    "sec", "section", "sections", "article", "articles", "division", "divisions",
    "chapter", "chapters", "code", "codes", "ordinance", "ordinances",
    "generally", "purpose", "purposes", "application", "applicability",
    "provision", "provisions", "subsection", "paragraph", "herein", "thereof",
    "pursuant", "applicable", "general", "other", "including", "include",
}


def _extract_keywords(text: str, max_keywords: int = 6) -> list[str]:
    words = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        if w not in _STOPWORDS and w not in seen:
            seen.add(w)
            keywords.append(w)
        if len(keywords) >= max_keywords:
            break
    return keywords


async def main() -> None:
    city_id = settings.default_city_id
    pool = await asyncpg.create_pool(
        settings.postgres_url,
        min_size=1,
        max_size=4,
        server_settings={"search_path": "arvada_agent, public"},
    )
    changed = unchanged = 0
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", city_id)
                rows = await conn.fetch(
                    "SELECT id, title, tags FROM knowledge_articles WHERE city_id = $1",
                    city_id,
                )
                print(f"Scanning {len(rows):,} articles for tag cleanup…", flush=True)

                updates = []  # (id, new_tags) only where tags actually change
                for r in rows:
                    new_tags = _extract_keywords(r["title"] or "")
                    if new_tags == list(r["tags"] or []):
                        unchanged += 1
                    else:
                        updates.append((r["id"], new_tags))
                changed = len(updates)

                if updates:
                    # Bulk: stage rows via COPY into a temp table, then one UPDATE join.
                    await conn.execute(
                        "CREATE TEMP TABLE _tag_updates (id uuid PRIMARY KEY, tags text[]) ON COMMIT DROP"
                    )
                    await conn.copy_records_to_table("_tag_updates", records=updates)
                    await conn.execute(
                        "UPDATE knowledge_articles k SET tags = t.tags "
                        "FROM _tag_updates t WHERE k.id = t.id"
                    )
    finally:
        await pool.close()

    print(f"Done. rewritten={changed:,}  unchanged={unchanged:,}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
