"""
ingest_subset.py
================
Chunk, embed and upsert a SUBSET of articles/*.json, selected by article_id prefix.

Why this exists alongside run_ingest.py: `ingest_all()` re-embeds every published
article for the city. Woodinville is now ~1,900 articles, so adding a couple of
dozen would mean paying to re-embed all of them. This does just the new ones.

Tenant safety: refuses outright if any selected file belongs to another city, and
goes through `db.get_pool(city_id)` so `search_path` is bound correctly -- a raw
asyncpg.create_pool() lands in the default schema where `knowledge_articles` does
not exist.

Review gate: only ingests `status="published"` unless --include-review is passed,
mirroring run_ingest.py. The transformers deliberately write `status="review"`.

Run:
    DEFAULT_CITY_ID=woodinville-wa python ingest_subset.py --prefix form-wv-
    DEFAULT_CITY_ID=woodinville-wa python ingest_subset.py --prefix form-wv- --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from config import settings
from city_config import get_city

ARTICLES_DIR = Path("articles")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", required=True, help="article_id prefix, e.g. form-wv-")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-review", action="store_true",
                    help="ingest status=review too (default: published only)")
    args = ap.parse_args()

    city = get_city(settings.default_city_id)
    files = sorted(ARTICLES_DIR.glob(f"{args.prefix}*.json"))
    if not files:
        print(f"No articles matching {args.prefix}*.json")
        sys.exit(1)

    selected, skipped = [], []
    for f in files:
        art = json.loads(f.read_text(encoding="utf-8"))
        cid = art.get("metadata", {}).get("city_id")
        if cid != city.city_id:
            print(f"REFUSING {f.name}: city_id={cid} != {city.city_id}")
            sys.exit(1)
        status = art.get("metadata", {}).get("status")
        if status != "published" and not args.include_review:
            skipped.append((f.name, status))
            continue
        selected.append((f, art))

    print(f"\n  {city.city_name} — {args.prefix}*")
    print(f"    matched:  {len(files)}")
    print(f"    to ingest: {len(selected)}")
    if skipped:
        print(f"    skipped (not published): {len(skipped)}"
              f"  -- pass --include-review to force")
    if args.dry_run:
        for f, art in selected[:10]:
            c = art["content"]
            print(f"      {f.stem:<58} docs={len(c.get('required_documents') or [])} "
                  f"fields={len(c.get('form_fields') or [])} body={len(c.get('body') or '')}")
        print("\n  dry run — nothing written")
        return
    if not selected:
        print("\n  nothing to ingest")
        return

    # Imported here so --dry-run works without an OPENAI_API_KEY.
    import db as agent_db
    from ingestor import (article_to_chunks, embed_all_chunks,
                          upsert_article, upsert_chunks)

    pool = await agent_db.get_pool(city.city_id)
    total = 0
    for f, art in selected:
        chunks = article_to_chunks(art)
        embedded: list[dict] = []
        async for batch in embed_all_chunks(chunks):
            embedded.extend(batch)
        aid = await upsert_article(pool, art, art["article_id"])
        await upsert_chunks(pool, aid, embedded, city.city_id)
        total += len(embedded)
        print(f"      {f.stem:<58} {len(embedded):>3} chunks")
    print(f"\n  done: {len(selected)} articles, {total} chunks")


if __name__ == "__main__":
    asyncio.run(main())
