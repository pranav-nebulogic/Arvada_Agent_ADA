#!/usr/bin/env python3
# run_ingest.py
"""
Phase 1, Step 3: Chunk, embed, and upsert knowledge articles to Postgres pgvector.
Run: python run_ingest.py

Input:  articles/*.json with status="published"
Output: knowledge_articles + article_chunks rows in Postgres

Only articles with status="published" are ingested.
FAQ articles (built by transformer from faq_js) are auto-published.
All other articles require manual review: open articles/*.json, verify the
fees/phone/timelines fields, then change status from "review" to "published".

Next: python run_eval.py  (verify recall@3 >= 0.85 before building the agent)
"""
import asyncio
import os
import sys
from pathlib import Path

# Windows consoles often default to cp1252, which can't encode the emoji banner.
# Force UTF-8 so the script never dies on a print().
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

ARTICLES_DIR = Path("articles")


def _check_env() -> str:
    postgres_url = os.getenv("POSTGRES_URL")
    if not postgres_url:
        print("\n❌  POSTGRES_URL is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    if not os.getenv("OPENAI_API_KEY"):
        print("\n❌  OPENAI_API_KEY is not set (needed for embeddings).")
        sys.exit(1)
    return postgres_url


def _count_publishable() -> tuple[int, int]:
    import json
    all_files = list(ARTICLES_DIR.glob("*.json"))
    published = [
        f for f in all_files
        if json.loads(f.read_text(encoding="utf-8")).get("metadata", {}).get("status") == "published"
    ]
    return len(all_files), len(published)


try:
    import asyncpg  # noqa: F401
    from openai import AsyncOpenAI  # noqa: F401
except ImportError:
    print("\n❌  Missing packages. Run: pip install -r requirements.txt")
    sys.exit(1)

from ingestor import ingest_all
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ]
)


async def main() -> None:
    postgres_url = _check_env()

    if not ARTICLES_DIR.exists() or not any(ARTICLES_DIR.glob("*.json")):
        print("\n❌  No articles found in articles/. Run python run_transform.py first.")
        sys.exit(1)

    total, published = _count_publishable()
    review_count = total - published

    print(f"\n📦  Arvada Agent — Phase 1, Step 3: Ingesting to Postgres pgvector")
    print(f"    Articles total:     {total}")
    print(f"    Ready to ingest:    {published}  (status=published)")
    if review_count > 0:
        print(f"    Awaiting review:    {review_count}  (status=review — set to 'published' to include)")
    print()

    if published == 0:
        print("⚠️   No published articles. Review articles/ and set status='published' where correct.")
        sys.exit(1)

    results = await ingest_all(postgres_url)

    print(f"\n✅  Ingest complete:")
    print(f"    Articles ingested:  {results['ingested']}")
    print(f"    Total chunks:       {results['total_chunks']}")
    if results["failed"] > 0:
        print(f"    Failed:             {results['failed']}")

    print(f"\n✅  Done. Run next step:")
    print("    python run_eval.py\n")

    if results["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
