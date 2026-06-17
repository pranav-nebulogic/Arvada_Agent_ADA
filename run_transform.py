#!/usr/bin/env python3
# run_transform.py
"""
Phase 1, Step 2: Transform raw scraped data into structured knowledge articles.
Run: python run_transform.py

Input:  raw_data/*.json   (produced by run_scraper.py)
Output: articles/*.json   (one article per source; FAQ files split into one per Q&A)

FAQ articles (faq_js source type) are built directly — no LLM, auto-published.
All other articles use GPT-4.1 Mini structured extraction and are saved as
status=review. You must manually set status="published" before ingestion.

Next: python run_ingest.py
"""
import asyncio
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

try:
    from openai import AsyncOpenAI  # noqa: F401  — verify key is set before we start
    import os
    if not os.getenv("OPENAI_API_KEY"):
        print("\n❌  OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)
except ImportError:
    print("\n❌  openai package not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

from transformer import transform_all
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ]
)

RAW_DATA_DIR = Path("raw_data")


async def main() -> None:
    raw_count = len([f for f in RAW_DATA_DIR.glob("*.json") if f.name != "manifest.json"])
    if raw_count == 0:
        print("\n❌  No raw data found in raw_data/. Run python run_scraper.py first.")
        sys.exit(1)

    print(f"\n🔄  Arvada Agent — Phase 1, Step 2: Transforming {raw_count} raw files\n")

    results = await transform_all()

    print(f"\n✅  Done. Run next step:")
    print("    python run_ingest.py\n")

    if results["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
