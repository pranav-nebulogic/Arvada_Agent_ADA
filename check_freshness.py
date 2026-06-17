"""
check_freshness.py
==================
Content-freshness monitor for the knowledge base. Re-fetches each article's
source URL, hashes the visible text (SHA-256), and diffs it against the last
hash stored in `article_sources`. Changed pages are flagged `stale` so a
targeted re-scrape/re-ingest can be triggered.

RE-SCRAPE CADENCE (recommended)
  - Fee schedules + permit/license pages:   weekly  (fees & forms change)
  - FAQ pages:                                monthly
  - Municode ordinances / Land Development Code: quarterly (or on council update)
Run this on that cadence (e.g. a scheduled task / cron / CI job). Automation of
the re-ingest itself is optional; for now it produces a report of what changed.

Usage:
  python check_freshness.py                 # check all sources for the city
  python check_freshness.py --limit 50      # sample
  python check_freshness.py --concurrency 8
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

import httpx
from dotenv import load_dotenv

load_dotenv()

from config import settings
import db

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _visible_hash(body: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", body)
    text = _TAGS.sub(" ", text)
    text = _WS.sub(" ", text).strip().lower()
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


async def _collect_sources(city_id: str, limit: int) -> list[tuple[str, str]]:
    """Return distinct (article_id, source_url) pairs."""
    async with db.tenant_conn(city_id) as conn:
        rows = await conn.fetch(
            "SELECT id, unnest(source_urls) AS url FROM knowledge_articles "
            "WHERE city_id = $1 AND source_urls <> '{}'",
            city_id,
        )
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for r in rows:
        url = r["url"]
        if not url or not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append((str(r["id"]), url))
        if limit and len(out) >= limit:
            break
    return out


async def _prior_hash(conn, city_id: str, url: str) -> str | None:
    row = await conn.fetchrow(
        "SELECT last_fetched_hash FROM article_sources WHERE city_id = $1 AND source_url = $2",
        city_id,
        url,
    )
    return row["last_fetched_hash"] if row else None


async def _record(conn, city_id, article_id, url, new_hash, status, error):
    await conn.execute(
        "INSERT INTO article_sources "
        "(city_id, article_id, source_url, last_fetched_at, last_fetched_hash, status, error_message) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7) "
        "ON CONFLICT (city_id, source_url) DO UPDATE SET "
        "  article_id = EXCLUDED.article_id, last_fetched_at = EXCLUDED.last_fetched_at, "
        "  last_fetched_hash = EXCLUDED.last_fetched_hash, status = EXCLUDED.status, "
        "  error_message = EXCLUDED.error_message",
        city_id,
        article_id,
        url,
        datetime.now(timezone.utc),
        new_hash,
        status,
        error,
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    city_id = settings.default_city_id
    sources = await _collect_sources(city_id, args.limit)
    print(f"Checking {len(sources)} source URLs for {city_id} ...\n")

    sem = asyncio.Semaphore(args.concurrency)
    stale, fresh, errors = [], 0, []

    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "ArvadaAgentFreshness/1.0"}) as http:
        async def check(article_id: str, url: str):
            nonlocal fresh
            async with sem:
                try:
                    resp = await http.get(url)
                    resp.raise_for_status()
                    new_hash = _visible_hash(resp.text)
                except Exception as exc:  # noqa: BLE001
                    errors.append((url, str(exc)[:80]))
                    async with db.tenant_conn(city_id) as conn:
                        await _record(conn, city_id, article_id, url, None, "error", str(exc)[:200])
                    return
                async with db.tenant_conn(city_id) as conn:
                    prior = await _prior_hash(conn, city_id, url)
                    status = "fresh" if (prior is None or prior == new_hash) else "stale"
                    await _record(conn, city_id, article_id, url, new_hash, status, None)
                if status == "stale":
                    stale.append(url)
                else:
                    fresh += 1

        await asyncio.gather(*(check(aid, url) for aid, url in sources))

    print(f"Fresh/unchanged: {fresh}")
    print(f"Stale (changed): {len(stale)}")
    print(f"Errors:          {len(errors)}")
    if stale:
        print("\nCHANGED — re-scrape/re-ingest these:")
        for u in stale[:50]:
            print(f"  {u}")
    if errors:
        print("\nFetch errors:")
        for u, e in errors[:20]:
            print(f"  {u}  ->  {e}")

    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
