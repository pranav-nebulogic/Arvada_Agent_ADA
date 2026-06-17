# scraper/transformer.py
"""
Transforms raw scraped content into structured knowledge articles.
Uses GPT-4.1 Mini with structured output (JSON schema constrained).
Run via: python run_transform.py
"""

import asyncio
import json
import re
from pathlib import Path
from datetime import date

from openai import AsyncOpenAI
import structlog

log = structlog.get_logger()
RAW_DATA_DIR = Path("raw_data")
ARTICLES_DIR = Path("articles")
ARTICLES_DIR.mkdir(exist_ok=True)

client = AsyncOpenAI()

# ── FAQ helpers ───────────────────────────────────────────────────────────────

# Maps FAQ category strings (from JS scraper / Faq.aspx topics) to permit_type
FAQ_CATEGORY_TO_PERMIT_TYPE: dict[str, str | None] = {
    "building_permits": "building_permit",
    "building": "building_permit",
    "short_term_rentals": "str_permit",
    "short_term_rental": "str_permit",
    "str": "str_permit",
    "food_trucks": "food_truck_permit",
    "food_truck": "food_truck_permit",
    "special_events": "special_event_permit",
    "special_event": "special_event_permit",
    "sales_tax": None,
    "tax": None,
    "licensing": "contractor_license",
    "contractor_licenses": "contractor_license",
    "contractor": "contractor_license",
    "business_licenses": "business_license",
    "business": "business_license",
    "liquor_licenses": "liquor_license",
    "liquor": "liquor_license",
    "row": "row_permit",
    "development": "development_permit",
    "faq": None,
}

# Stopwords for keyword extraction. Beyond grammar words this drops the legal /
# civic boilerplate that otherwise dominates tags ("section", "generally",
# "city", "arvada", …) and produced noise tags like #generally / #against.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "what", "how", "when", "where", "why",
    "who", "which", "i", "my", "me", "we", "our", "you", "your", "it", "its",
    "this", "that", "there", "their", "they", "if", "from", "by", "as", "not",
    "shall", "must", "any", "all", "such", "no", "nor", "than", "then",
    # Civic / legal boilerplate
    "city", "arvada", "colorado", "county", "jefferson", "town", "municipal",
    "sec", "section", "sections", "article", "articles", "division", "divisions",
    "chapter", "chapters", "code", "codes", "ordinance", "ordinances",
    "generally", "purpose", "purposes", "application", "applicability",
    "provision", "provisions", "subsection", "paragraph", "herein", "thereof",
    "pursuant", "applicable", "general", "other", "including", "include",
}


def _load_raw(path: Path) -> dict:
    """
    Load a raw_data JSON file, tolerating legacy files that were written in
    Windows cp1252 (older crawler runs) instead of UTF-8. We try UTF-8 first,
    then cp1252, then latin-1 (which can decode any byte sequence).
    """
    last_err: Exception | None = None
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            text = path.read_text(encoding=enc)
        except UnicodeDecodeError as e:
            last_err = e
            continue
        if not text.strip():
            raise ValueError("empty raw file")
        return json.loads(text)
    raise last_err or ValueError("could not decode raw file")


def _extract_keywords(text: str, max_keywords: int = 6) -> list[str]:
    """Extract meaningful, human-readable tags.

    Pass a *title / heading* here, not body text — running this over the full
    legal body is what produced noise tags (e.g. #generally, #against). Words
    are de-duped, stop-filtered, and capped.
    """
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


def _slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def build_ordinance_articles(raw_data: dict) -> list[dict]:
    """
    Split a scraped Municode chapter into one article per section (Sec. X-Y).
    No LLM — the text is verbatim legal code, so it's auto-published.
    Each section becomes a self-contained, high-precision retrieval chunk.
    """
    source_meta = raw_data["source"]
    text = raw_data.get("content", "")
    chapter_heading = source_meta.get("chapter_heading", "Ordinance")
    today = date.today().isoformat()

    # Sections are delimited by "## " markers added by the crawler.
    blocks = re.split(r"\n(?=## )", text)
    articles: list[dict] = []

    for i, block in enumerate(blocks):
        block = block.strip()
        if len(block) < 40:
            continue
        # First line is the heading ("## Sec. 18-1. - ..." or "## Chapter 18 - ...")
        lines = block.split("\n", 1)
        heading = lines[0].lstrip("# ").strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        if len(body) < 20:
            continue

        # Extract ordinance section refs like "Sec. 18-42" / "§ 18-42"
        ord_refs = re.findall(r"(?:Sec\.|§)\s*\d+[-–]\d+", heading + " " + body[:200])

        title = f"{chapter_heading} — {heading}" if heading not in chapter_heading else heading

        articles.append({
            "article_id": None,
            "metadata": {
                "city_id": source_meta.get("city_id", "arvada-co"),
                "city_name": "City of Arvada, CO",
                "article_type": "ordinance",
                "permit_type": source_meta.get("permit_type"),
                "category": "ordinance",
                "tags": _extract_keywords(heading),
                "source_urls": [source_meta.get("url", "")],
                "scraped_at": raw_data.get("scraped_at", today),
                "content_hash": None,
                "effective_date": today,
                "version": 1,
                "status": "published",  # verbatim legal text — no review needed
                "last_verified": today,
                "ordinance_refs": ord_refs,
                "chapter": chapter_heading,
            },
            "content": {
                "title": title,
                "summary": body[:300] + ("…" if len(body) > 300 else ""),
                "eligibility": None,
                "steps": [],
                "required_documents": [],
                "form_fields": [],
                "fees": [],
                "timelines": {},
                "contact": {},
                "faqs": [{"q": heading, "a": body}],
                "ordinance_refs": ord_refs,
                "related_permit_types": [],
            },
            "transform_model": "direct",
            "transform_tokens": 0,
        })

    return articles


def build_faq_article(faq_item: dict, source_meta: dict, index: int) -> dict | None:
    """
    Build a knowledge article directly from a single FAQ {category, question, answer}
    item. No LLM call — the Q&A data is already perfectly structured.
    Each FAQ Q&A becomes its own standalone knowledge article.
    """
    question = (faq_item.get("question") or "").strip()
    answer = (faq_item.get("answer") or "").strip()

    if not question or len(answer) < 10:
        return None

    faq_category = (
        faq_item.get("category")
        or source_meta.get("category")
        or "faq"
    ).lower().replace(" ", "_")

    permit_type = FAQ_CATEGORY_TO_PERMIT_TYPE.get(faq_category)
    today = date.today().isoformat()

    # Simple fee extraction: find dollar amounts mentioned in the answer
    fee_matches = re.findall(r"\$[\d,]+(?:\.\d{2})?", answer)
    fees = [{"name": "See answer", "amount": amt, "notes": None}
            for amt in fee_matches[:3]]  # cap at 3

    content = {
        "title": question,
        "summary": answer[:300] + ("…" if len(answer) > 300 else ""),
        "eligibility": None,
        "steps": [],
        "required_documents": [],
        "form_fields": [],
        "fees": fees,
        "timelines": {},
        "contact": {},
        "faqs": [{"q": question, "a": answer}],
        "ordinance_refs": [],
        "related_permit_types": [],
    }

    slug_base = _slugify(question)
    article_id_str = f"ARVADA-FAQ-{index + 1:03d}"

    return {
        "article_id": None,  # assigned at ingest time
        "faq_article_id": article_id_str,  # human-readable ID for Smart Knowledge UI
        "metadata": {
            "city_id": source_meta.get("city_id", "arvada-co"),
            "city_name": "City of Arvada, CO",
            "article_type": "faq",
            "permit_type": permit_type,
            "category": source_meta.get("category", "faq"),
            "tags": _extract_keywords(question),
            "source_urls": [source_meta.get("url", "")],
            "scraped_at": today,
            "content_hash": None,
            "effective_date": today,
            "version": 1,
            "status": "published",  # FAQs are pre-verified — publish immediately
            "last_verified": today,
            "slug_hint": f"arvada-co/faq/{slug_base}",
        },
        "content": content,
        "transform_model": "direct",  # no LLM used
        "transform_tokens": 0,
    }


# ── FAQ HTML (Faq.aspx?TID=N) parsing ─────────────────────────────────────────

# Ordered substring → category-key checks. The CivicPlus category name is free
# text (e.g. "Building Permits - Signs"), so we map it to a permit_type key.
_FAQ_CATEGORY_CHECKS: list[tuple[str, str]] = [
    ("short term rental", "short_term_rentals"),
    ("short-term rental", "short_term_rentals"),
    ("food truck", "food_trucks"),
    ("special event", "special_events"),
    ("liquor", "liquor_licenses"),
    ("business licens", "business_licenses"),
    ("contractor", "contractor_licenses"),
    ("right-of-way", "row"),
    ("right of way", "row"),
    ("sales tax", "sales_tax"),
    ("use tax", "tax"),
    ("building permit", "building_permits"),
    ("building", "building_permits"),
    ("development", "development"),
]


def _normalize_faq_category(name: str) -> str | None:
    """Map a free-text CivicPlus FAQ category name to a known permit_type key."""
    n = (name or "").lower()
    for sub, key in _FAQ_CATEGORY_CHECKS:
        if sub in n:
            return key
    return None


def _parse_faq_html(content_text: str) -> tuple[str, list[tuple[str, str]]]:
    """
    Parse a cleaned CivicPlus Faq.aspx page into (category_name, [(question, answer), ...]).

    Structure of the cleaned text is deterministic:
        Frequently Asked Questions
        Below you will find information ... about your city or town.
        <Category Name>
        <count>
        All Content
        <Question 1>
        <answer line(s)>
        <Category Name>          <- breadcrumb separator before each subsequent Q
        <Question 2>
        ...
        <Category Name>          <- trailing breadcrumb
    """
    lines = [ln.strip() for ln in content_text.split("\n")]

    try:
        ac = next(i for i, ln in enumerate(lines) if ln == "All Content")
    except StopIteration:
        return "", []

    category = lines[ac - 2].strip() if ac >= 2 else ""
    if not category:
        return "", []

    qa_lines = lines[ac + 1:]

    # Split the Q&A region into blocks delimited by the category breadcrumb line.
    blocks: list[list[str]] = []
    current: list[str] = []
    for ln in qa_lines:
        if ln == category:
            if current:
                blocks.append(current)
                current = []
            continue
        if ln:  # drop blank lines
            current.append(ln)
    if current:
        blocks.append(current)

    qa_pairs: list[tuple[str, str]] = []
    for block in blocks:
        if not block:
            continue
        question = block[0].strip()
        # Join answer lines with spaces; collapse stray space-before-punctuation
        # introduced by inline links being flattened onto their own lines.
        answer = " ".join(block[1:]).strip()
        answer = re.sub(r"\s+([.,;:!?])", r"\1", answer)
        answer = re.sub(r"\s{2,}", " ", answer)
        if question and len(answer) >= 10:
            qa_pairs.append((question, answer))

    return category, qa_pairs


def build_faq_html_articles(raw_data: dict) -> list[dict]:
    """
    Split a scraped Faq.aspx?TID=N page into one knowledge article per Q&A pair.
    No LLM — the Q&A data is already structured. Each article is auto-published.
    """
    source_meta = raw_data["source"]
    content_text = raw_data.get("content", "")
    category_name, qa_pairs = _parse_faq_html(content_text)

    if not qa_pairs:
        return []

    cat_key = _normalize_faq_category(category_name)
    url = source_meta.get("url", "")
    tid_match = re.search(r"TID=(\d+)", url)
    tid = tid_match.group(1) if tid_match else "x"

    # source_meta with a resolved category so build_faq_article maps permit_type
    enriched_meta = {
        **source_meta,
        "category": cat_key or "faq",
        "faq_category_name": category_name,
    }

    articles: list[dict] = []
    for i, (question, answer) in enumerate(qa_pairs):
        faq_item = {
            "category": cat_key or "",
            "question": question,
            "answer": answer,
        }
        article = build_faq_article(faq_item, enriched_meta, i)
        if not article:
            continue
        # Globally-unique id across all topic pages (per-page index alone collides)
        article["faq_article_id"] = f"ARVADA-FAQ-{tid}-{i + 1:03d}"
        # Preserve the human-readable category for the Smart Knowledge UI
        article["metadata"]["faq_category_name"] = category_name
        if category_name:
            tags = article["metadata"].get("tags", [])
            article["metadata"]["tags"] = tags + [category_name]
        articles.append(article)

    return articles


# ── Article JSON Schema ──────────────────────────────────────────────────────
# This is the schema GPT-4.1 Mini must produce.
# city-variable fields use config_key references, not hardcoded values.

# NOTE: OpenAI structured outputs with strict=True require EVERY object to set
# "additionalProperties": false AND to list every property key in "required".
# Optional fields are expressed by allowing null (e.g. ["string", "null"]).
ARTICLE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "summary", "eligibility", "steps", "required_documents",
                 "form_fields", "fees", "timelines", "contact", "faqs",
                 "ordinance_refs", "related_permit_types"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string", "description": "2-3 sentences max"},
        "eligibility": {"type": ["string", "null"], "description": "Who can apply for this"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["n", "title", "text", "portal_url"],
                "properties": {
                    "n": {"type": "integer"},
                    "title": {"type": "string", "description": "Short step title"},
                    "text": {"type": "string", "description": "Full step instructions"},
                    "portal_url": {"type": ["string", "null"], "description": "Direct URL if this step involves a portal"},
                }
            }
        },
        "required_documents": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description", "where_to_get", "example"],
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "where_to_get": {"type": ["string", "null"]},
                    "example": {"type": ["string", "null"]},
                }
            }
        },
        "form_fields": {
            "type": "array",
            "description": "Fields in the permit application form, with plain-English explanations",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field_name", "label", "description", "example_value", "why_needed"],
                "properties": {
                    "field_name": {"type": "string", "description": "Technical field name"},
                    "label": {"type": "string", "description": "Human-readable label"},
                    "description": {"type": "string", "description": "Plain English explanation"},
                    "example_value": {"type": ["string", "null"]},
                    "why_needed": {"type": ["string", "null"]},
                }
            }
        },
        "fees": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "amount", "rate", "config_key", "notes"],
                "properties": {
                    "name": {"type": "string"},
                    "amount": {"type": ["string", "null"], "description": "COPY VERBATIM from source. If unknown, use null."},
                    "rate": {"type": ["string", "null"], "description": "Percentage rate if applicable"},
                    "config_key": {"type": ["string", "null"], "description": "e.g. fees.building.review"},
                    "notes": {"type": ["string", "null"]},
                }
            }
        },
        "timelines": {
            "type": "object",
            "additionalProperties": False,
            "required": ["review", "inspection", "total"],
            "properties": {
                "review": {"type": ["string", "null"]},
                "inspection": {"type": ["string", "null"]},
                "total": {"type": ["string", "null"]},
            }
        },
        "contact": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dept", "phone", "address", "config_key"],
            "properties": {
                "dept": {"type": ["string", "null"]},
                "phone": {"type": ["string", "null"], "description": "COPY VERBATIM from source"},
                "address": {"type": ["string", "null"]},
                "config_key": {"type": ["string", "null"]},
            }
        },
        "faqs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["q", "a"],
                "properties": {
                    "q": {"type": "string"},
                    "a": {"type": "string"},
                }
            }
        },
        "ordinance_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "e.g. ['Chapter 18', '§18-42', '§98-84']"
        },
        "related_permit_types": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Other permit types the applicant may also need"
        },
    }
}

TRANSFORM_SYSTEM_PROMPT = """You are a knowledge article writer for a city government permits platform.
Your job is to transform raw scraped web content into structured, accurate knowledge articles.

CRITICAL RULES — violating these destroys user trust in a government system:
1. NEVER invent fees, phone numbers, dates, or deadlines. Copy them VERBATIM from the source.
   If the source doesn't mention a fee, set the amount field to null.
2. NEVER add information not in the source. If it's not there, omit it.
3. For form_fields: only include fields explicitly mentioned in the source.
4. For steps: preserve the exact order and completeness from the source.
5. Use config_key fields like "fees.building.review" for any value that varies by city.
6. Phone numbers and addresses must be copied exactly, character for character.
7. If you see a dollar amount that might be out of date, include it but add a note field: "verify current amount with city".
8. ordinance_refs: extract any section number references like §18-42, Chapter 18, etc.
9. Output ONLY valid JSON matching the schema. No markdown, no preamble, no explanation.
"""


async def transform_raw_to_article(raw_data: dict) -> dict | None:
    """
    Transform one raw scraped file into a structured article.
    Uses GPT-4.1 Mini with structured output (Batch API for bulk runs).
    """
    source = raw_data["source"]
    content = raw_data["content"]

    # Skip if too short (scraping likely failed)
    if len(content) < 200:
        log.warning("content_too_short", url=source.get("url"), chars=len(content))
        return None

    # Build the user prompt
    user_prompt = f"""
SOURCE URL: {source.get('url', 'unknown')}
PERMIT TYPE: {source.get('permit_type', 'general')}
CATEGORY: {source.get('category', 'general')}
ARTICLE TYPE: {source.get('article_type', 'permit')}
CITY: City of Arvada, CO
SCRAPED DATE: {raw_data.get('scraped_at', date.today().isoformat())}

RAW CONTENT:
{content[:8000]}  # truncate very long content, we'll handle in chunks

Transform the above into a structured knowledge article following the JSON schema exactly.
Remember: copy fees, phone numbers, and addresses VERBATIM. Do not invent anything.
"""

    log.info("transforming", url=source.get("url"))

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1-mini-2025-04-14",
            messages=[
                {"role": "system", "content": TRANSFORM_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "knowledge_article",
                    "strict": True,
                    "schema": ARTICLE_SCHEMA,
                }
            },
            temperature=0,  # deterministic extraction
            max_tokens=4000,
        )

        article_content = json.loads(response.choices[0].message.content)

        # Build the full article with metadata
        article = {
            "article_id": None,  # assigned at ingest time
            "metadata": {
                "city_id": source.get("city_id", "arvada-co"),
                "city_name": "City of Arvada, CO",
                "article_type": source.get("article_type", "permit"),
                "permit_type": source.get("permit_type"),
                "category": source.get("category"),
                "source_urls": [source.get("url")],
                "scraped_at": raw_data.get("scraped_at"),
                "content_hash": raw_data.get("content_hash"),
                "effective_date": date.today().isoformat(),
                "version": 1,
                "status": "review",  # human must review before publishing
                "last_verified": date.today().isoformat(),
            },
            "content": article_content,
            "transform_model": "gpt-4.1-mini-2025-04-14",
            "transform_tokens": response.usage.total_tokens,
        }

        log.info("transform_ok",
                 title=article_content.get("title", "untitled"),
                 tokens=response.usage.total_tokens)
        return article

    except Exception as e:
        log.error("transform_failed", url=source.get("url"), error=str(e))
        return None


async def transform_all() -> dict:
    """
    Transform all raw_data/ files into articles/.

    Special handling:
    - faq_js files: each Q&A item becomes its own article (no LLM, published immediately)
    - All other files: GPT-4.1 Mini structured extraction (status=review, needs human sign-off)
    """
    raw_files = list(RAW_DATA_DIR.glob("*.json"))
    # Skip the manifest file written by run_scraper.py
    raw_files = [f for f in raw_files if f.name != "manifest.json"]
    log.info("transforming_all", count=len(raw_files))

    results = {"transformed": 0, "faq_articles": 0, "failed": 0, "skipped": 0}

    # Process priority 1 sources first
    def priority_sort(f: Path) -> int:
        try:
            data = _load_raw(f)
            return data["source"].get("priority", 99)
        except Exception:
            return 99

    sorted_files = sorted(raw_files, key=priority_sort)

    for raw_file in sorted_files:
        try:
            raw_data = _load_raw(raw_file)
        except Exception as e:
            log.error("read_raw_failed", file=str(raw_file), error=str(e))
            results["failed"] += 1
            continue

        source_type = raw_data.get("source", {}).get("source_type", "")

        # ── FAQ JS files: split into one article per Q&A ────────────────────
        if source_type == "faq_js":
            try:
                faq_items = json.loads(raw_data["content"])
                if not isinstance(faq_items, list):
                    raise ValueError("faq_js content is not a list")
            except Exception as e:
                log.error("faq_parse_failed", file=str(raw_file), error=str(e))
                results["failed"] += 1
                continue

            source_meta = raw_data["source"]
            saved = 0
            for i, item in enumerate(faq_items):
                # Skip raw_text fallback items (unstructured)
                if "raw_text" in item and "question" not in item:
                    log.warning("faq_item_unstructured", index=i, file=str(raw_file))
                    continue

                article = build_faq_article(item, source_meta, i)
                if article:
                    out_name = f"faq-{i:03d}-{raw_file.stem}.json"
                    (ARTICLES_DIR / out_name).write_text(
                        json.dumps(article, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    saved += 1

            log.info("faq_split_done", file=raw_file.name, articles=saved)
            results["faq_articles"] += saved
            results["transformed"] += saved
            continue

        # ── FAQ HTML pages (Faq.aspx?TID=N): split into one article per Q&A ──
        if source_type == "faq_html":
            faq_articles = build_faq_html_articles(raw_data)
            if not faq_articles:
                log.warning("faq_html_no_pairs", file=raw_file.name)
                results["skipped"] += 1
                continue
            tid_match = re.search(r"TID=(\d+)", raw_data["source"].get("url", ""))
            tid = tid_match.group(1) if tid_match else raw_file.stem
            saved = 0
            for i, art in enumerate(faq_articles):
                out_name = f"faq-tid{tid}-{i:03d}.json"
                (ARTICLES_DIR / out_name).write_text(
                    json.dumps(art, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                saved += 1
            log.info("faq_html_split_done", file=raw_file.name, articles=saved)
            results["faq_articles"] += saved
            results["transformed"] += saved
            continue

        # ── Municode ordinance chapters: split into one article per section ──
        if source_type == "municode_api":
            ord_articles = build_ordinance_articles(raw_data)
            saved = 0
            for j, art in enumerate(ord_articles):
                out_name = f"ord-{raw_file.stem}-{j:03d}.json"
                (ARTICLES_DIR / out_name).write_text(
                    json.dumps(art, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                saved += 1
            log.info("ordinance_split_done", file=raw_file.name, sections=saved)
            results.setdefault("ordinance_articles", 0)
            results["ordinance_articles"] += saved
            results["transformed"] += saved
            continue

        # ── All other sources: GPT-4.1 Mini structured extraction ───────────
        article = await transform_raw_to_article(raw_data)

        if article:
            article_file = ARTICLES_DIR / raw_file.name
            article_file.write_text(
                json.dumps(article, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            results["transformed"] += 1
        else:
            results["failed"] += 1

        # Rate limit: GPT-4.1 Mini is fast but respect API limits
        await asyncio.sleep(0.2)

    log.info("transform_complete", **results)

    print("\n" + "="*60)
    print("TRANSFORM COMPLETE")
    print("="*60)
    print(f"  Total articles:    {results['transformed']}")
    print(f"  FAQ articles:      {results['faq_articles']}  (auto-published, no review needed)")
    print(f"  Failed:            {results['failed']}")
    print()
    print("HUMAN REVIEW REQUIRED for non-FAQ articles before ingestion:")
    print("  ✓ fees[].amount — copy verbatim from city website")
    print("  ✓ contact.phone — verify phone numbers are current")
    print("  ✓ timelines — verify review times are current")
    print("  ✓ eligibility — verify who can apply")
    print("  ✓ steps — verify portal URLs are correct")
    print(f"\nArticles saved to: {ARTICLES_DIR}/")
    print("After review, set status: 'published' and run: python run_ingest.py")

    return results
