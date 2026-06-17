#!/usr/bin/env python3
# scraper/run_scraper.py
"""
Phase 1, Step 1: Scrape all Arvada sources.
Run: python scraper/run_scraper.py

Output: scraper/raw_data/*.json  (one file per source)
Next:   python scraper/run_transform.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Windows consoles often default to cp1252, which can't encode the emoji banner
# or scraped unicode. Force UTF-8 so the script never dies on a print().
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

# Check Playwright is installed
try:
    from playwright.async_api import async_playwright
except ImportError:
    print("\n❌ Playwright not installed. Run:")
    print("   pip install playwright")
    print("   playwright install chromium")
    sys.exit(1)

from sources import ARVADA_SOURCES
from crawler import scrape_all
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ]
)


async def main():
    print("\n🏗️  Arvada Agent — Phase 1: Scraping\n")
    print(f"Sources: {len(ARVADA_SOURCES['html_pages'])} HTML pages, "
          f"{len(ARVADA_SOURCES['pdfs'])} PDFs, "
          f"{len(ARVADA_SOURCES['js_pages'])} JS pages")
    print("Output: scraper/raw_data/\n")

    results = await scrape_all(ARVADA_SOURCES)

    print("\n" + "="*50)
    print(f"✅ Scraped:  {results['scraped']}")
    print(f"❌ Failed:   {results['failed']}")
    print(f"⏭️  Skipped:  {results['skipped']}")
    print("="*50)

    if results['failed'] > 0:
        print("\n⚠️  Some sources failed. Check scraper/raw_data/ for details.")
        print("Re-run to retry failed sources (already-scraped files are skipped).")

    print(f"\n✅ Done. Run next step:")
    print("   python scraper/run_transform.py\n")

    # Save manifest
    manifest = {
        "scraped_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "city_id": ARVADA_SOURCES["city_id"],
        **results,
    }
    Path("raw_data/manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
