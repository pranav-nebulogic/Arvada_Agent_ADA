"""
ldc_transformer.py
==================
Parses the Land Development Code Word document and produces one JSON article
per section, in the same format as the Municode API ordinance articles.

Input:  raw_data/LAND_DEVELOPMENT_CODE_OF_THE_CITY_OF_ARVADA__COLORADO.docx
Output: articles/ord-ldc-<slug>.json  (one per section, ~454 total)

Style hierarchy used:
  Heading 2  → Chapter  (e.g. "Chapter 3 USE REGULATIONS")
  Heading 3  → Article  (e.g. "ARTICLE 3-1. PRIMARY LAND USE REGULATIONS")
  Heading 4  → Division (e.g. "DIVISION 3-1-5. BUSINESS USE OF THE HOME")
  Section    → Section heading → becomes one article
  List 1-5   → Body content of current section
  Paragraph 1→ Body content of current section
  History Note → Ordinance amendment history (appended to body)
  Block *    → Supplemental text (skip preface/intro blocks)
  Hang 1     → Repealed/reserved notices

Run:
  python ldc_transformer.py
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

from docx import Document

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────

DOCX_PATH = Path("raw_data/LAND_DEVELOPMENT_CODE_OF_THE_CITY_OF_ARVADA__COLORADO.docx")
ARTICLES_DIR = Path("articles")
ARTICLES_DIR.mkdir(exist_ok=True)

CITY_ID = "arvada-co"
CITY_NAME = "City of Arvada, CO"
SOURCE_URL = "https://library.municode.com/co/arvada/codes/land_development_code"
TODAY = date.today().isoformat()

# Styles that are "body" content attached to the current section
BODY_STYLES = {"List 1", "List 2", "List 3", "List 4", "List 5",
               "Paragraph 1", "History Note", "Hang 1", "Block 2", "Block 3"}

# Styles that signal a new structural boundary
CHAPTER_STYLE = "Heading 2"
ARTICLE_STYLE = "Heading 3"
DIVISION_STYLE = "Heading 4"
SECTION_STYLE = "Section"

# Chapters to skip (front matter / back matter)
SKIP_CHAPTER_KEYWORDS = {"appendix", "appendices", "comparative table", "preface"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80]


def _section_slug(section_num: str, section_title: str) -> str:
    """ord-ldc-3-1-5-3-short-term-rentals"""
    num_part = re.sub(r"[^0-9a-z]+", "-", section_num.lower()).strip("-")
    title_part = _slugify(re.sub(r"^\d[\d\-\.]*\s*", "", section_title))[:40]
    slug = f"ord-ldc-{num_part}"
    if title_part:
        slug = f"{slug}-{title_part}"
    return slug


def _extract_keywords(text: str) -> list[str]:
    """Tags from a section *title* (not body). Drops grammar + civic/legal
    boilerplate so we don't emit noise tags like #generally / #section."""
    stop = {"the", "a", "an", "of", "and", "or", "in", "to", "for", "with",
            "is", "are", "shall", "be", "by", "at", "on", "this", "that",
            "any", "all", "such", "as", "from", "not", "no", "if", "it",
            "its", "may", "will", "has", "have", "was", "were", "been",
            "must", "nor", "than", "then", "herein", "thereof", "pursuant",
            "city", "arvada", "colorado", "county", "jefferson", "municipal",
            "sec", "section", "sections", "article", "articles", "division",
            "divisions", "chapter", "chapters", "code", "codes", "ordinance",
            "ordinances", "generally", "purpose", "purposes", "application",
            "applicability", "provision", "provisions", "subsection",
            "paragraph", "applicable", "general", "other", "including", "include"}
    words = re.findall(r"[a-zA-Z]{3,}", (text or "").lower())
    seen, kw = set(), []
    for w in words:
        if w not in stop and w not in seen:
            seen.add(w)
            kw.append(w)
            if len(kw) == 6:
                break
    return kw


def _summary(body: str) -> str:
    """Return first 300 chars of body, trimmed at sentence boundary."""
    body = body.strip()
    if len(body) <= 300:
        return body
    cut = body.rfind(". ", 0, 300)
    return body[:cut + 1] if cut > 0 else body[:300] + "…"


def _parse_section_number(section_text: str) -> str:
    """Extract the numeric prefix, e.g. '3-1-5-3. Short term rentals.' → '3-1-5-3'"""
    m = re.match(r"^([\d][\d\-]*)", section_text)
    return m.group(1).rstrip("-") if m else ""


def _indent_prefix(style_name: str) -> str:
    """Convert List 1–5 to indentation prefix for readable body text."""
    m = re.match(r"List (\d)", style_name)
    if m:
        return "  " * (int(m.group(1)) - 1)
    return ""


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_ldc() -> list[dict]:
    doc = Document(DOCX_PATH)
    articles = []

    current_chapter = ""
    current_article = ""
    current_division = ""
    current_section_title = ""
    current_section_num = ""
    current_body_lines: list[str] = []
    in_front_matter = True  # skip everything before the first real chapter

    def flush_section():
        """Finalise the current section into an article dict."""
        if not current_section_title:
            return
        body = "\n".join(current_body_lines).strip()
        if not body:
            return

        num = current_section_num
        slug = _section_slug(num, current_section_title)
        filename = f"{slug}.json"

        article = {
            "article_id": None,
            "metadata": {
                "city_id": CITY_ID,
                "city_name": CITY_NAME,
                "article_type": "ordinance",
                "permit_type": None,
                "category": "ordinance",
                "tags": _extract_keywords(current_section_title),
                "source_urls": [SOURCE_URL],
                "scraped_at": TODAY + "T00:00:00+00:00",
                "content_hash": None,
                "effective_date": TODAY,
                "version": 1,
                "status": "published",
                "last_verified": TODAY,
                "ordinance_refs": [num] if num else [],
                "chapter": current_chapter,
                "article": current_article,
                "division": current_division,
            },
            "content": {
                "title": current_section_title,
                "summary": _summary(body),
                "eligibility": None,
                "steps": [],
                "required_documents": [],
                "form_fields": [],
                "fees": [],
                "timelines": {},
                "contact": {},
                "faqs": [
                    {
                        "q": current_section_title,
                        "a": body,
                    }
                ],
                "ordinance_refs": [num] if num else [],
                "related_permit_types": [],
            },
            "transform_model": "direct-from-docx",
            "transform_tokens": 0,
        }
        articles.append((filename, article))

    for para in doc.paragraphs:
        text = para.text.strip()
        style = para.style.name

        if not text:
            continue

        # ── Chapter boundary ────────────────────────────────────────────────
        if style == CHAPTER_STYLE:
            # Skip appendices and front matter
            if any(kw in text.lower() for kw in SKIP_CHAPTER_KEYWORDS):
                in_front_matter = True
                flush_section()
                current_section_title = ""
                current_body_lines = []
                continue
            flush_section()
            current_section_title = ""
            current_body_lines = []
            in_front_matter = False
            current_chapter = text
            current_article = ""
            current_division = ""
            continue

        if in_front_matter:
            continue

        # ── Article boundary ────────────────────────────────────────────────
        if style == ARTICLE_STYLE:
            flush_section()
            current_section_title = ""
            current_body_lines = []
            current_article = text
            current_division = ""
            continue

        # ── Division boundary ───────────────────────────────────────────────
        if style == DIVISION_STYLE:
            flush_section()
            current_section_title = ""
            current_body_lines = []
            current_division = text
            continue

        # ── New section ─────────────────────────────────────────────────────
        if style == SECTION_STYLE:
            flush_section()
            current_section_title = text
            current_section_num = _parse_section_number(text)
            current_body_lines = []
            continue

        # ── Body content ─────────────────────────────────────────────────────
        if style in BODY_STYLES and current_section_title:
            prefix = _indent_prefix(style)
            current_body_lines.append(f"{prefix}{text}")
            continue

    # Flush the last section
    flush_section()
    return articles


def main():
    print(f"Parsing {DOCX_PATH} …")
    articles = parse_ldc()
    print(f"Parsed {len(articles)} sections")

    written = 0
    skipped = 0
    for filename, article in articles:
        out_path = ARTICLES_DIR / filename
        out_path.write_text(json.dumps(article, indent=2, ensure_ascii=False), encoding="utf-8")
        written += 1

    print(f"Written:  {written} articles → articles/ord-ldc-*.json")
    print(f"Skipped:  {skipped}")

    # Quick sanity check
    print("\nSample articles:")
    for fname, art in articles[:3]:
        print(f"  {fname}")
        print(f"    chapter:  {art['metadata']['chapter']}")
        print(f"    division: {art['metadata']['division']}")
        print(f"    title:    {art['content']['title']}")
        body_len = len(art["content"]["faqs"][0]["a"]) if art["content"]["faqs"] else 0
        print(f"    body:     {body_len} chars")


if __name__ == "__main__":
    main()
