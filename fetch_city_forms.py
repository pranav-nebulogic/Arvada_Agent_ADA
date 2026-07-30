"""
fetch_city_forms.py
===================
Download a city's configured application-form PDFs to `city_docs/<city_id>/forms/`.

Separate from run_scraper.py on purpose: the scraper turns a PDF into TEXT via
crawler.scrape_pdf(), but the submittal checklists are MATRICES -- requirement x
permit type, with copy counts in the cells -- and plain text extraction destroys
the mapping, returning "Application Form 1 1 1". wv_forms_transformer.py needs
the original file so it can use pdfplumber's table extraction.

Politeness is not optional here: woodinville.gov sits behind Cloudflare and
answered 32 parallel requests with a bot challenge and then HTTP 429. Reuses
crawler.py's REQUEST_DELAY and RETRY_DELAYS rather than inventing new numbers.

Run:
    DEFAULT_CITY_ID=woodinville-wa python fetch_city_forms.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

from crawler import REQUEST_DELAY, RETRY_DELAYS
from city_config import get_city
from config import settings
from sources import get_sources

# Same UA the crawler presents -- identifies the bot honestly.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (research bot; permits-agent-scraper)",
    "Accept": "application/pdf,*/*",
}


def doc_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def fetch(client: httpx.Client, url: str, dest: Path) -> str:
    """-> 'ok' | 'cached' | 'not-pdf' | 'failed'. Retries on 429 with backoff."""
    if dest.exists() and dest.stat().st_size > 2000:
        return "cached"
    for attempt, wait in enumerate([0, *RETRY_DELAYS]):
        if wait:
            print(f"      rate limited; waiting {wait}s before retry {attempt}")
            time.sleep(wait)
        try:
            resp = client.get(url, follow_redirects=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"      request error: {exc}")
            continue
        if resp.status_code == 429:
            continue
        if resp.status_code != 200:
            print(f"      HTTP {resp.status_code}")
            continue
        body = resp.content
        # Content-Type lies sometimes; the magic bytes do not. A Cloudflare
        # challenge page is 200 text/html and would otherwise be saved as a PDF.
        if not body.startswith(b"%PDF"):
            return "not-pdf"
        dest.write_bytes(body)
        return "ok"
    return "failed"


def main() -> None:
    city = get_city(settings.default_city_id)
    src = get_sources()
    pdfs = src.get("pdfs") or []
    if not pdfs:
        print(f"No PDFs configured for {city.city_id} in sources.py")
        sys.exit(1)

    out = Path("city_docs") / city.city_id / "forms"
    out.mkdir(parents=True, exist_ok=True)
    print(f"\n  {city.city_name} — {len(pdfs)} configured PDFs -> {out}")
    print(f"  {REQUEST_DELAY}s between requests, backoff {RETRY_DELAYS} on 429\n")

    tally: dict[str, int] = {}
    with httpx.Client(headers=HEADERS) as client:
        for i, entry in enumerate(pdfs, 1):
            url = entry["url"]
            dest = out / f"{doc_id(url)}.pdf"
            print(f"  [{i:>2}/{len(pdfs)}] {entry['name'][:58]}")
            status = fetch(client, url, dest)
            tally[status] = tally.get(status, 0) + 1
            print(f"      -> {status}  ({dest.name})")
            if status != "cached" and i < len(pdfs):
                time.sleep(REQUEST_DELAY)

    print(f"\n  done: {tally}")
    if tally.get("failed") or tally.get("not-pdf"):
        print("  NOTE: re-run to retry; existing files are skipped.")


if __name__ == "__main__":
    main()
