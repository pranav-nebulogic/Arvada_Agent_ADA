# scraper/crawler.py
"""
Crawls all Arvada sources and saves raw content to ./raw_data/
Run: python run_scraper.py

Two crawlers:
- StaticCrawler: httpx for plain HTML pages and PDFs
- JSCrawler: Playwright for JS-rendered pages (FAQ, Municode)
"""

import asyncio
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, UTC

import httpx
import pdfplumber
import structlog

log = structlog.get_logger()
RAW_DATA_DIR = Path("raw_data")
RAW_DATA_DIR.mkdir(exist_ok=True)

# Polite delay between every request (seconds). arvadaco.gov rate-limits aggressively.
REQUEST_DELAY = 2.5
# On 429: wait this many seconds then retry (up to MAX_RETRIES times)
RETRY_DELAYS = [30, 60, 120]  # 3 attempts with increasing backoff


# ── Helpers ──────────────────────────────────────────────────────────────────

def clean_html(html: str) -> str:
    """Strip nav, footer, scripts, styles. Return main content text."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup.find_all(["nav", "footer", "script", "style", "header",
                               "aside", ".breadcrumb", "#sidebar"]):
        tag.decompose()

    # CivicPlus-specific: main content is in #content or .field-items
    main = (
        soup.find(id="content") or
        soup.find(class_="field-items") or
        soup.find("main") or
        soup.find(id="main-content") or
        soup.body
    )

    if not main:
        return soup.get_text(separator="\n", strip=True)

    # Convert tables to readable text
    for table in main.find_all("table"):
        rows = []
        for row in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            rows.append(" | ".join(cells))
        table.replace_with("\n".join(rows))

    text = main.get_text(separator="\n", strip=True)

    # Clean up excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def raw_path(source_meta: dict) -> Path:
    """Return the expected raw_data path for a source (used for skip-if-exists).

    Filenames must be unique per source. Municode chapter URLs share a long common
    prefix and differ only in the ?nodeId=... suffix, which a naive [:80] truncation
    would drop — collapsing all 30 chapters onto a single filename (so only the first
    ever saved). Key off the stable chapter_id when present, and guard long URLs with
    a short hash of the full URL so distinct URLs never collide.
    """
    chapter_id = source_meta.get("chapter_id")
    if chapter_id:
        slug = "municode-" + re.sub(r"[^a-z0-9]+", "-", chapter_id.lower()).strip("-")
        return RAW_DATA_DIR / f"{slug}.json"

    url = source_meta["url"]
    slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")
    if len(slug) > 80:
        slug = slug[:71] + "-" + sha256(url)[:8]
    return RAW_DATA_DIR / f"{slug}.json"


def save_raw(source_meta: dict, content: str, content_type: str = "html") -> Path:
    """Save raw content with metadata to raw_data/"""
    filename = raw_path(source_meta)
    data = {
        "source": source_meta,
        "content_type": content_type,
        "content": content,
        "content_hash": sha256(content),
        "scraped_at": datetime.now(UTC).isoformat(),
    }
    filename.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("saved_raw", file=str(filename), chars=len(content))
    return filename


# ── Static Crawler (httpx) ───────────────────────────────────────────────────

class StaticCrawler:
    """Scrapes plain HTML pages and PDFs using httpx."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (research bot; permits-agent-scraper)",
        "Accept": "text/html,application/xhtml+xml,application/pdf",
    }

    def __init__(self):
        self.client = httpx.AsyncClient(
            headers=self.HEADERS,
            timeout=30,
            follow_redirects=True,
        )

    async def scrape_html(self, source: dict) -> dict:
        """Scrape a static HTML page with retry on 429."""
        url = source["url"]
        log.info("scraping_html", url=url)

        for attempt, wait in enumerate([0] + RETRY_DELAYS):
            if wait:
                log.warning("rate_limited_retry", url=url, attempt=attempt, wait_s=wait)
                await asyncio.sleep(wait)
            try:
                resp = await self.client.get(url)
                if resp.status_code == 429:
                    if attempt < len(RETRY_DELAYS):
                        continue  # trigger next retry
                    log.error("scrape_html_failed_429", url=url)
                    return {"status": "error", "url": url, "error": "429 after retries"}
                resp.raise_for_status()
                text = clean_html(resp.text)
                return {
                    "status": "ok",
                    "url": url,
                    "content": text,
                    "content_hash": sha256(text),
                    "http_status": resp.status_code,
                    "etag": resp.headers.get("etag"),
                    "last_modified": resp.headers.get("last-modified"),
                }
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < len(RETRY_DELAYS):
                    continue
                log.error("scrape_html_failed", url=url, error=str(e))
                return {"status": "error", "url": url, "error": str(e)}
            except Exception as e:
                log.error("scrape_html_failed", url=url, error=str(e))
                return {"status": "error", "url": url, "error": str(e)}

        return {"status": "error", "url": url, "error": "exhausted retries"}

    async def scrape_pdf(self, source: dict) -> dict:
        """Download and extract text from a PDF with retry on 429."""
        url = source["url"]
        log.info("scraping_pdf", url=url)

        resp = None
        for attempt, wait in enumerate([0] + RETRY_DELAYS):
            if wait:
                log.warning("rate_limited_retry", url=url, attempt=attempt, wait_s=wait)
                await asyncio.sleep(wait)
            try:
                resp = await self.client.get(url)
                if resp.status_code == 429 and attempt < len(RETRY_DELAYS):
                    continue
                resp.raise_for_status()
                break  # success
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < len(RETRY_DELAYS):
                    continue
                log.error("scrape_pdf_failed", url=url, error=str(e))
                return {"status": "error", "url": url, "error": str(e)}
            except Exception as e:
                log.error("scrape_pdf_failed", url=url, error=str(e))
                return {"status": "error", "url": url, "error": str(e)}

        if resp is None:
            return {"status": "error", "url": url, "error": "exhausted retries"}

        try:

            # Save PDF temporarily
            pdf_path = RAW_DATA_DIR / f"temp_{sha256(url)[:8]}.pdf"
            pdf_path.write_bytes(resp.content)

            # Extract text with pdfplumber (preserves tables better than pypdf)
            text_parts = []
            page_count = 0
            with pdfplumber.open(pdf_path) as pdf:
                page_count = len(pdf.pages)
                for page in pdf.pages:
                    # Extract tables first (fee schedules!)
                    tables = page.extract_tables()
                    for table in tables:
                        for row in table:
                            clean_row = [str(cell or "").strip() for cell in row]
                            text_parts.append(" | ".join(clean_row))

                    # Then extract remaining text
                    page_text = page.extract_text(x_tolerance=2, y_tolerance=2)
                    if page_text:
                        text_parts.append(page_text)

            pdf_path.unlink()  # cleanup temp file
            full_text = "\n".join(text_parts)
            full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()

            return {
                "status": "ok",
                "url": url,
                "content": full_text,
                "content_hash": sha256(full_text),
                "name": source.get("name", url),
                "pages": page_count,
            }
        except Exception as e:
            log.error("scrape_pdf_failed", url=url, error=str(e))
            return {"status": "error", "url": url, "error": str(e)}

    async def close(self):
        await self.client.aclose()


# ── JS Crawler (Playwright) ──────────────────────────────────────────────────

class JSCrawler:
    """Scrapes JavaScript-rendered pages using Playwright."""

    async def scrape_faq_page(self, source: dict) -> dict:
        """
        Scrape the Arvada FAQ page at /m/faq
        CivicPlus renders FAQ items via JS — need full browser.
        Returns list of FAQ items: [{category, question, answer}]
        """
        from playwright.async_api import async_playwright

        url = source["url"]
        log.info("scraping_faq_js", url=url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # "domcontentloaded" is reliable; "networkidle" never settles on
            # CivicPlus pages (continuous analytics/long-poll traffic).
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Give client-side JS a moment to render the FAQ accordions
            try:
                await page.wait_for_selector(".faq-item, .accordion-item, [class*='faq']",
                                             timeout=15000)
            except Exception:
                log.warning("faq_selector_timeout", url=url, note="falling back to body text")
            await page.wait_for_timeout(2000)

            # Extract all FAQ items
            faqs = await page.evaluate("""
                () => {
                    const items = [];
                    // Try multiple CivicPlus FAQ selectors
                    const containers = document.querySelectorAll(
                        '.accordion-item, .faq-item, [data-type="faq"], .FAQ'
                    );
                    containers.forEach(container => {
                        const category = container.closest('[data-category]')
                            ?.dataset?.category || '';
                        const question = container.querySelector(
                            '.accordion-header, .faq-question, h3, h4'
                        )?.innerText?.trim() || '';
                        const answer = container.querySelector(
                            '.accordion-body, .faq-answer, .answer'
                        )?.innerText?.trim() || '';
                        if (question) items.push({category, question, answer});
                    });
                    return items;
                }
            """)

            # Fallback: get all text if structured extraction fails
            if not faqs:
                log.warning("faq_structured_failed", url=url, fallback="text_extraction")
                text = await page.evaluate("() => document.body.innerText")
                faqs = [{"raw_text": text}]

            await browser.close()

        content = json.dumps(faqs, indent=2)
        return {
            "status": "ok",
            "url": url,
            "content": content,
            "content_hash": sha256(content),
            "faq_count": len(faqs),
        }

    async def scrape_municode_chapter(self, chapter_id: str) -> dict:
        """
        Scrape a Municode chapter (JS-rendered).
        Each chapter needs a headless browser to render.
        """
        from playwright.async_api import async_playwright

        url = f"https://library.municode.com/co/arvada/codes/code_of_ordinances?nodeId={chapter_id}"
        log.info("scraping_municode", chapter=chapter_id, url=url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            await page.goto(url, wait_until="networkidle", timeout=60000)

            # Municode content is in the main codified law area
            await page.wait_for_selector(".chunk-content, .codified-laws-content",
                                         timeout=15000)

            # Click "Expand all" if available to get full text
            expand_btn = page.locator("button:has-text('Expand All')")
            if await expand_btn.count() > 0:
                await expand_btn.first.click()
                await page.wait_for_timeout(2000)

            text = await page.evaluate("""
                () => {
                    const content = document.querySelector(
                        '.chunk-content, .codified-laws-content, #codes-content'
                    );
                    return content ? content.innerText : document.body.innerText;
                }
            """)

            await browser.close()

        return {
            "status": "ok",
            "url": url,
            "chapter_id": chapter_id,
            "content": text,
            "content_hash": sha256(text),
        }


# ── Municode API Crawler ──────────────────────────────────────────────────────

class MunicodeAPICrawler:
    """
    Scrapes the full Municode code of ordinances via the JSON API
    (api.municode.com) instead of rendering pages. Far more reliable.

    Flow:
      1. resolve client → product (Code of Ordinances) → latest job
      2. walk the TOC under the configured root node(s) to find every chapter
      3. fetch /CodesContent per chapter (returns the whole chapter as HTML)
    """

    API = "https://api.municode.com"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (research bot; permits-agent-scraper)",
        "Accept": "application/json",
    }

    def __init__(self):
        self.client = httpx.AsyncClient(headers=self.HEADERS, timeout=60, follow_redirects=True)

    async def close(self):
        await self.client.aclose()

    async def _resolve(self, cfg: dict) -> dict:
        """Resolve client_id, product_id, job_id from city config."""
        r = await self.client.get(
            f"{self.API}/Clients/name",
            params={"clientName": cfg["client_name"], "stateAbbr": cfg["state_abbr"]},
        )
        r.raise_for_status()
        client_id = r.json()["ClientID"]

        r = await self.client.get(f"{self.API}/ClientContent/{client_id}")
        r.raise_for_status()
        codes = r.json().get("codes", [])
        want = cfg.get("product_name", "Code of Ordinances").lower()
        product = next(
            (c for c in codes if want in c.get("productName", "").lower()),
            codes[0] if codes else None,
        )
        if not product:
            raise RuntimeError("No Code of Ordinances product found for this client")
        product_id = product["productId"]

        r = await self.client.get(f"{self.API}/Jobs/latest/{product_id}")
        r.raise_for_status()
        job_id = r.json()["Id"]

        log.info("municode_resolved", client_id=client_id, product_id=product_id, job_id=job_id)
        return {"client_id": client_id, "product_id": product_id, "job_id": job_id}

    async def _toc_children(self, ids: dict, node_id: str | None = None) -> list[dict]:
        params = {"jobId": ids["job_id"], "productId": ids["product_id"]}
        if node_id:
            params["nodeId"] = node_id
        r = await self.client.get(f"{self.API}/codesToc/children", params=params)
        r.raise_for_status()
        return r.json()

    async def discover_chapters(self, ids: dict, root_node_ids: list[str] | None) -> list[dict]:
        """Return [{id, heading}] for every chapter under the configured root(s).

        If root_node_ids is None, auto-discovers by fetching the TOC root — this
        handles products like the Land Development Code where the top-level node
        IDs are not known in advance.
        """
        chapters: list[dict] = []

        if root_node_ids is None:
            # Auto-discover: get all top-level nodes from the product root
            top_nodes = await self._toc_children(ids, node_id=None)
            for node in top_nodes:
                # Each top-level node may itself be a chapter, or a container
                # whose children are chapters (e.g. "Part II"). Probe one level
                # deeper if the node has no obvious leaf content marker.
                node_id = node.get("Id", "")
                heading = node.get("Heading", "")
                # Skip pure index nodes (tables, appendices, comparative tables)
                skip_keywords = ["appendix", "appendices", "comparative table",
                                 "state law reference", "supplement history", "preface"]
                if any(kw in heading.lower() for kw in skip_keywords):
                    chapters.append({"id": node_id, "heading": heading})
                    continue
                children = await self._toc_children(ids, node_id)
                await asyncio.sleep(0.3)
                if children:
                    # This is a container — its children are the real chapters
                    for c in children:
                        chapters.append({"id": c["Id"], "heading": c["Heading"]})
                else:
                    # Leaf node — treat as a chapter itself
                    chapters.append({"id": node_id, "heading": heading})
            return chapters

        for root in root_node_ids:
            children = await self._toc_children(ids, root)
            for c in children:
                chapters.append({"id": c["Id"], "heading": c["Heading"]})
            await asyncio.sleep(0.3)
        return chapters

    @staticmethod
    def _doc_text(doc: dict) -> str:
        from bs4 import BeautifulSoup
        return BeautifulSoup(doc.get("Content") or "", "html.parser").get_text(
            separator="\n", strip=True
        )

    async def _codes_content(self, ids: dict, node_id: str) -> list[dict]:
        r = await self.client.get(
            f"{self.API}/CodesContent",
            params={"jobId": ids["job_id"], "productId": ids["product_id"], "nodeId": node_id},
        )
        r.raise_for_status()
        return r.json().get("Docs", [])

    async def _collect_docs(self, ids: dict, node_id: str, depth: int = 0) -> list[dict]:
        """
        Recursively collect content docs for a node.

        Municode's /CodesContent returns full inline section text only when the
        requested node's subtree is small enough (flat chapters, or article/division
        levels). For deeply nested chapters (Chapter → Article → Division → Section)
        the chapter-level call returns headings ONLY (no bodies). In that case we must
        descend the TOC and fetch each child until we reach a level that returns real
        body text. This guarantees we capture every section's full text.
        """
        try:
            docs = await self._codes_content(ids, node_id)
        except Exception as e:
            log.warning("municode_node_fetch_failed", node=node_id, error=str(e))
            return []

        if not docs:
            return []

        # If any doc carries real body text, this subtree is returned inline in full.
        if any(self._doc_text(d) for d in docs):
            return docs

        # TOC-only container: keep its own heading, then recurse into TOC children.
        if depth > 6:  # safety guard against pathological nesting
            return [docs[0]]
        out: list[dict] = [docs[0]]
        try:
            children = await self._toc_children(ids, node_id)
        except Exception as e:
            log.warning("municode_toc_failed", node=node_id, error=str(e))
            return out
        for child in children:
            await asyncio.sleep(0.15)
            out.extend(await self._collect_docs(ids, child["Id"], depth + 1))
        return out

    async def fetch_chapter(self, ids: dict, node_id: str) -> dict:
        """Fetch a chapter's full content (every section body) and flatten to text."""
        docs = await self._collect_docs(ids, node_id)

        parts: list[str] = []
        seen: set = set()
        section_count = 0
        for doc in docs:
            did = doc.get("Id")
            if did and did in seen:
                continue
            if did:
                seen.add(did)
            title = (doc.get("Title") or "").strip()
            text = self._doc_text(doc)
            if title:
                parts.append(f"## {title}")
                if re.match(r"\s*Sec\.", title):
                    section_count += 1
            if text:
                parts.append(text)

        full_text = "\n\n".join(parts)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()
        return {
            "status": "ok",
            "node_id": node_id,
            "content": full_text,
            "content_hash": sha256(full_text),
            "section_count": section_count,
        }


# ── Main Scrape Runner ────────────────────────────────────────────────────────

async def scrape_all(sources: dict) -> dict:
    """
    Scrape all sources and save to raw_data/.
    Returns summary: {scraped: N, failed: N, skipped: N}
    """
    static = StaticCrawler()
    js = JSCrawler()
    results = {"scraped": 0, "failed": 0, "skipped": 0}

    try:
        # ── HTML pages ──────────────────────────────────────────────────────
        log.info("phase_html_pages", count=len(sources["html_pages"]))

        # Scrape priority 1 first, then 2, then 3
        sorted_pages = sorted(sources["html_pages"], key=lambda x: x.get("priority", 99))

        for source in sorted_pages:
            meta = {**source, "source_type": "html"}
            # Skip if already scraped in a previous run
            if raw_path(meta).exists():
                log.info("skip_already_scraped", url=source["url"])
                results["skipped"] += 1
                continue

            result = await static.scrape_html(source)
            if result["status"] == "ok":
                save_raw(meta, result["content"])
                results["scraped"] += 1
            else:
                results["failed"] += 1

            await asyncio.sleep(REQUEST_DELAY)

        # ── PDFs ────────────────────────────────────────────────────────────
        log.info("phase_pdfs", count=len(sources["pdfs"]))

        for source in sources["pdfs"]:
            meta = {**source, "source_type": "pdf"}
            if raw_path(meta).exists():
                log.info("skip_already_scraped", url=source["url"])
                results["skipped"] += 1
                continue

            result = await static.scrape_pdf(source)
            if result["status"] == "ok":
                save_raw(meta, result["content"], "pdf")
                results["scraped"] += 1
            else:
                results["failed"] += 1
            await asyncio.sleep(REQUEST_DELAY)

        # ── FAQ (JS) ────────────────────────────────────────────────────────
        # The CivicPlus /m/faq page is Cloudflare-blocked, so it's no longer in
        # sources. FAQ content now comes entirely from the Faq.aspx?TID=N pages
        # (faq_endpoints below). Keep this phase guarded for backward compat.
        faq_source = next((s for s in sources["js_pages"] if s.get("category") == "faq"), None)
        if faq_source is not None:
            log.info("phase_faq_js")
            faq_meta = {**faq_source, "source_type": "faq_js"}
            if raw_path(faq_meta).exists():
                log.info("skip_already_scraped", url=faq_source["url"])
                results["skipped"] += 1
            else:
                try:
                    faq_result = await js.scrape_faq_page(faq_source)
                    if faq_result["status"] == "ok":
                        save_raw(faq_meta, faq_result["content"], "json")
                        results["scraped"] += 1
                        log.info("faq_scraped", count=faq_result.get("faq_count", 0))
                    else:
                        results["failed"] += 1
                except Exception as e:
                    # Never let the FAQ JS step abort the whole run (Municode still needs to run)
                    log.error("faq_js_failed", url=faq_source["url"], error=str(e))
                    results["failed"] += 1

        # ── FAQ API endpoints ───────────────────────────────────────────────
        log.info("phase_faq_api", count=len(sources["faq_endpoints"]))
        for source in sources["faq_endpoints"]:
            meta = {**source, "source_type": "faq_html"}
            if raw_path(meta).exists():
                log.info("skip_already_scraped", url=source["url"])
                results["skipped"] += 1
                continue
            result = await static.scrape_html(source)
            if result["status"] == "ok":
                save_raw(meta, result["content"])
                results["scraped"] += 1
            else:
                results["failed"] += 1
            await asyncio.sleep(REQUEST_DELAY)

        # ── Municode: Code of Ordinances via API ───────────────────────────────
        municode_source = next(
            (s for s in sources["js_pages"] if s.get("municode")), None
        )
        if municode_source:
            muni_cfg = municode_source["municode"]
            base_url = municode_source["url"].rstrip("/")
            muni = MunicodeAPICrawler()
            try:
                ids = await muni._resolve(muni_cfg)
                chapters = await muni.discover_chapters(ids, muni_cfg.get("root_node_ids"))
                log.info("phase_municode", chapters=len(chapters))

                for ch in chapters:
                    meta = {
                        **municode_source,
                        "url": f"{base_url}?nodeId={ch['id']}",
                        "chapter_id": ch["id"],
                        "chapter_heading": ch["heading"],
                        "source_type": "municode_api",
                    }
                    if raw_path(meta).exists():
                        log.info("skip_already_scraped", chapter=ch["id"])
                        results["skipped"] += 1
                        continue
                    try:
                        result = await muni.fetch_chapter(ids, ch["id"])
                        if result["content"]:
                            save_raw(meta, result["content"])
                            results["scraped"] += 1
                            log.info("municode_chapter_saved", chapter=ch["id"],
                                     sections=result["section_count"])
                        else:
                            log.warning("municode_chapter_empty", chapter=ch["id"])
                            results["failed"] += 1
                    except Exception as e:
                        log.error("municode_chapter_failed", chapter=ch["id"], error=str(e))
                        results["failed"] += 1
                    await asyncio.sleep(1)
            finally:
                await muni.close()

    finally:
        await static.close()

    log.info("scrape_complete", **results)
    return results
