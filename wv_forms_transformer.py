"""
wv_forms_transformer.py
=======================
Turn Woodinville's application forms and submittal checklists (city_docs/
woodinville-wa/forms/*.pdf) into knowledge articles.

WHY: `WOODINVILLE_SOURCES["pdfs"]` used to be empty, so asked "what documents do
I need for a new single-family home permit?" the agent answered "the specific
document list is not included in the context I have here". The DOC_CHECKLIST
intent had no content for this city at all.

Deterministic -- NO LLM. Like wmc_pdf_transformer.py and
fee_schedule_transformer.py, every line traces to the City's own published form,
so nothing here can be invented.

THREE ARCHETYPES, handled differently:

  MATRIX (568 construction, 614 land use)
      A grid of requirement (rows) x permit type (columns) whose cells are COPY
      COUNTS. Plain text extraction returns "Application Form 1 1 1" -- the
      permit mapping destroyed -- so these need pdfplumber's table extraction.
      Inverted, one article per permit type, into `required_documents`, which
      ingestor.article_to_chunks indexes as its own retrievable section.

  FORM (571, 629, 621, ...)
      The application itself. We keep the informational sidebars, the contact
      block, and the field/fixture vocabularies (-> `form_fields`, indexed
      agent-only) so the agent can help someone COUNT the fixtures the fee
      engine's *_fixture_count drivers ask for.

  PROSE (570, 567, 627, 2097)
      Plan standard requirements -- straight into `body`.

Signature and bond instruments (617, 2136, 564, 565) are excluded in sources.py,
not here: they are indemnity boilerplate with nothing a citizen can be told
beyond "this form is required", which the matrix already records.

Run:
    DEFAULT_CITY_ID=woodinville-wa python wv_forms_transformer.py
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

import pdfplumber

from city_config import get_city
from config import settings
from sources import get_sources

FORMS_DIR_TMPL = "city_docs/{city_id}/forms"
OUT_DIR = Path("articles")

# Rotated "Required"/"Submitted" checkbox headers, which the text layer returns
# reversed. Not permit types -- dropping them is what keeps the grid aligned.
NOISE_HEADERS = {"deriuqeR", "dettimbuS", "required", "submitted", ""}

# Docs whose value is the MATRIX rather than the prose.
MATRIX_DOCS = {"568", "614"}

# Matrix column name -> the permit_type key already used in the corpus.
# sources.py sets permit_type=None on 568/614 because each DOCUMENT spans many
# permit types, but each INVERTED article is about exactly one, so it should
# carry it. Substring match, first hit wins; anything unmatched stays None, which
# is correct rather than a fallback -- hybrid_search now includes NULL rows, so
# NULL no longer means invisible.
PERMIT_TYPE_BY_COLUMN = (
    ("mechanical or plumbing", "building_permit_mechanical_plumbing"),
    ("building permit",        "building_permit"),
    ("fire permit",            "fire_permit"),
    ("sign permit",            "sign_permit"),
    ("site development",       "development_permit"),
    ("right-of-way",           "row_permit"),
    ("right of way",           "row_permit"),
    ("tree removal",           "tree_permit"),
)


def permit_type_for(column: str) -> str | None:
    low = (column or "").lower()
    for needle, key in PERMIT_TYPE_BY_COLUMN:
        if needle in low:
            return key
    return None

# "Plan Standards" docs: two columns at the top (notes | contact) and then a
# full-width YES/NO/N-A checklist of what the plan set must SHOW. Plain text
# extraction splices the two columns together -- "All drawings should be drawn to
# scale (1/8 or 1/4 inch) For questions, please contact Development Services at
# and include a north arrow" is one column's sentence interleaved with the
# other's. The checklist rows come out clean from table extraction.
STANDARDS_DOCS = {"567", "570", "627"}

# Row labels inside the standards checklists that are structure, not content.
_STANDARDS_NOISE = re.compile(
    r"^(yes|no|n/?a|city of woodinville|development services|sta|"
    r".*plan standards|general notes|questions and contact|revised|page \d)",
    re.I)


def ascii_clean(s: str | None) -> str:
    """ASCII-only. The WMC corpus already carries mojibake em-dashes from an
    earlier ingest; no reason to add more."""
    if not s:
        return ""
    s = (s.replace("�", "-").replace("–", "-").replace("—", "-")
           .replace("‘", "'").replace("’", "'")
           .replace("“", '"').replace("”", '"')
           .replace(" ", " ").replace("•", "*"))
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if ord(c) < 128)


def squash(s: str | None) -> str:
    return re.sub(r"\s+", " ", ascii_clean(s)).strip()


def doc_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", squash(s).lower())).strip("-")[:60]


# ── Matrix inversion ─────────────────────────────────────────────────────────
def invert_matrix(pdf_path: Path) -> list[dict]:
    """-> [{permit_type, requirements: [(name, copies)]}] for a checklist PDF.

    Mirrors what a reader does with the printed grid: read across the row for
    your permit's column. Group headers ("General:", "Building Plans:") sit alone
    on a row and prefix every requirement beneath them.
    """
    groups: list[dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or len(table) < 3:
                    continue
                header = [squash(c) for c in table[0]]
                cols = {i: h for i, h in enumerate(header)
                        if i > 0 and h and h.lower() not in NOISE_HEADERS}
                if not cols:
                    continue
                buckets: dict[int, list[tuple[str, str]]] = {i: [] for i in cols}
                section = ""
                for row in table[1:]:
                    cells = [squash(c) for c in row]
                    label = cells[0] if cells else ""
                    if not label:
                        # A group header lives in the next non-empty cell and
                        # ends with a colon.
                        nxt = next((c for c in cells[1:] if c), "")
                        if nxt.endswith(":"):
                            section = nxt.rstrip(":")
                        continue
                    for i in cols:
                        val = cells[i] if i < len(cells) else ""
                        if re.fullmatch(r"\d+", val):
                            name = f"{section}: {label}" if section else label
                            buckets[i].append((name, val))
                for i, permit in cols.items():
                    if buckets[i]:
                        groups.append({"permit_type": permit, "requirements": buckets[i]})
    return groups


# ── Form / prose text ────────────────────────────────────────────────────────
def page_text(pdf_path: Path) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join(ascii_clean(p.extract_text() or "") for p in pdf.pages)


# Column group headers inside the fixture schedules -- not fixtures themselves.
_FIXTURE_GROUPS = {
    "qty", "appliances and equip", "exhaust system", "hvac system", "heaters",
    "boilers & compressors", "other", "drains", "fixtures", "sinks", "systems",
    "traps & pumps",
    # The traffic-impact table shares the page with the plumbing schedule.
    "yes", "no", "question", "city only",
}


def fixture_vocab(pdf_path: Path) -> list[str]:
    """Fixture/appliance names from the mechanical and plumbing schedules.

    These become `form_fields`, so the agent can help a citizen count what the
    fee engine's mechanical_fixture_count / plumbing_fixture_count drivers ask
    for -- it currently asks "how many mechanical fixtures?" with no way to help
    anyone answer.

    Reads TABLES, not text. The schedules are three-column grids, and the text
    layer merges a row across columns: "Floor Drain Backwater Valve Bathroom
    Sink" is three separate fixtures welded into one, and the column headings
    ("DRAINS", "SINKS") come through looking like fixtures. Scoped to the pages
    that actually carry a fixture schedule so the rest of the form's field labels
    ("PROJECT NAME:", "OWNER NAME:") stay out.
    """
    out: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            head = ascii_clean(page.extract_text() or "").upper()
            if "MECHANICAL FIXTURES" not in head and "PLUMBING FIXTURES" not in head:
                continue
            for table in page.extract_tables():
                for row in table or []:
                    for cell in row or []:
                        c = squash(cell)
                        if not c or len(c) > 44:
                            continue
                        if c.endswith(":"):                 # form field label
                            continue
                        if re.match(r"^[A-Z]\.\s", c):      # "E. MECHANICAL FIXTURES"
                            continue
                        if c.lower() in _FIXTURE_GROUPS:    # column group header
                            continue
                        if re.fullmatch(r"[\d\W]+", c):
                            continue
                        if re.search(r"revised|page \d|city of|please answer", c, re.I):
                            continue
                        out.append(c)
    seen: set[str] = set()
    return [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]


def plan_standards(pdf_path: Path) -> list[str]:
    """What a plan set must SHOW, from a "Plan Standards" checklist.

    Table extraction, not text: these pages are two-column at the top and the
    text layer splices the columns into nonsense. The checklist itself is a real
    table, so the rows come out clean ("Building square footage, by floor and
    type of space", "ADA accessibility route access, including bathrooms").
    """
    items: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table or []:
                    for cell in row or []:
                        c = squash(cell)
                        # Real requirements are phrases; headers are short labels
                        # and page furniture is long or matches the noise set.
                        if not (8 < len(c) <= 120):
                            continue
                        if _STANDARDS_NOISE.match(c):
                            continue
                        items.append(c)
    seen: set[str] = set()
    return [x for x in items if not (x.lower() in seen or seen.add(x.lower()))]


def informational(text: str) -> str:
    """The sidebars a citizen actually benefits from, without the form grid."""
    keep: list[str] = []
    for pat in (
        r"WHAT DOES THIS\s+APPLICATION\s+COVER\?(.{0,700}?)WHERE CAN I",
        r"WHERE CAN I\s+FIND MORE\s+INFORMATION\?(.{0,600}?)(?:$|A\.|SECTION)",
        r"(Please answer all questions below.{0,900}?)(?:YES\s+NO|$)",
    ):
        m = re.search(pat, text, re.S | re.I)
        if m:
            keep.append(squash(m.group(1)))
    return "\n\n".join(k for k in keep if len(k) > 40)


CONTACT_RE = re.compile(r"(\d{3}-\d{3}-\d{4})")
# The address wraps mid-token on these forms ("PermitCenter@\nwoodinville.gov"),
# so newlines are stripped before matching -- but the TLD must then be anchored,
# or the match runs on into whatever followed and yields
# "PermitCenter@woodinville.gov.YES".
EMAIL_RE = re.compile(
    r"([A-Za-z0-9._%+-]+@\s?[A-Za-z0-9.-]+\.(?:gov|com|org|net|us|wa\.us))(?![A-Za-z])",
    re.I)


def contact_block(text: str) -> dict:
    phone = CONTACT_RE.search(text)
    email = EMAIL_RE.search(text.replace("\n", ""))
    hours = re.search(r"(Monday\s*[-–]\s*\w+.{0,60}?pm)", text, re.I)
    out = {}
    if phone:
        out["phone"] = phone.group(1)
    if email:
        out["email"] = squash(email.group(1)).replace(" ", "")
    if hours:
        out["hours"] = squash(hours.group(1))
    return out


# ── Article assembly ─────────────────────────────────────────────────────────
def make_article(city, article_id: str, title: str, summary: str, entry: dict,
                 *, body: str = "", required_documents: list[dict] | None = None,
                 form_fields: list[dict] | None = None,
                 contact: dict | None = None) -> dict:
    payload = ascii_clean(body + summary + json.dumps(required_documents or []))
    return {
        "article_id": article_id,
        "metadata": {
            "city_id": city.city_id,
            "city_name": city.city_name,
            "article_type": entry.get("article_type") or "process",
            "permit_type": entry.get("permit_type"),
            "category": entry.get("category") or "building",
            "tags": [t for t in [entry.get("category"), entry.get("article_type"),
                                 "application", "documents"] if t],
            "source_urls": [entry["url"]],
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "content_hash": hashlib.sha256(payload.encode()).hexdigest(),
            "effective_date": None,
            "version": 1,
            # review, not published: run_ingest.py only takes published articles,
            # and these must be spot-checked against the printed grid first -- a
            # transposition bug would attach the wrong copy counts to the wrong
            # permit, which is worse than having no answer.
            "status": "review",
            "last_verified": date.today().isoformat(),
            "provenance": f"Extracted from the City of Woodinville form at {entry['url']}",
        },
        "content": {
            "title": squash(title),
            "summary": squash(summary),
            "eligibility": None,
            "steps": [],
            "required_documents": required_documents or [],
            "form_fields": form_fields or [],
            "fees": [],
            "timelines": {},
            "contact": contact or {},
            "body": ascii_clean(body),
            "faqs": [],
            "ordinance_refs": [],
            "related_permit_types": [entry["permit_type"]] if entry.get("permit_type") else [],
        },
        "transform_model": "wv-forms-transformer",
        "transform_tokens": 0,
    }


def main() -> None:
    city = get_city(settings.default_city_id)
    forms_dir = Path(FORMS_DIR_TMPL.format(city_id=city.city_id))
    if not forms_dir.exists():
        print(f"No forms directory: {forms_dir}\nRun fetch_city_forms.py first.")
        sys.exit(1)
    by_id = {doc_id(e["url"]): e for e in (get_sources().get("pdfs") or [])}
    OUT_DIR.mkdir(exist_ok=True)

    written = 0
    for pdf in sorted(forms_dir.glob("*.pdf")):
        did = pdf.stem
        entry = by_id.get(did)
        if not entry:
            print(f"  skip {pdf.name} — not configured in sources.py")
            continue

        if did in MATRIX_DOCS:
            groups = invert_matrix(pdf)
            if not groups:
                print(f"  !! {pdf.name}: matrix parse produced nothing")
                continue
            for g in groups:
                permit = g["permit_type"]
                docs = [
                    {"name": name,
                     "description": f"{copies} cop{'y' if copies == '1' else 'ies'} required",
                     "where_to_get": "City of Woodinville Development Services",
                     "example": ""}
                    for name, copies in g["requirements"]
                ]
                aid = f"form-wv-submittal-{slug(permit)}"
                # Each inverted article covers ONE permit type, so tag it even
                # though the source document spans many.
                entry_for_permit = {**entry, "permit_type": permit_type_for(permit)}
                art = make_article(
                    city, aid,
                    f"Submittal requirements - {permit} (Woodinville)",
                    (f"What to submit for a {permit} in the City of Woodinville, WA, "
                     f"with the number of copies required for each item, per the City's "
                     f"{entry['name']}."),
                    entry_for_permit, required_documents=docs,
                )
                (OUT_DIR / f"{aid}.json").write_text(
                    json.dumps(art, indent=2, ensure_ascii=True), encoding="utf-8")
                written += 1
                print(f"  wrote {aid:<52} {len(docs):>3} items")
            continue

        text = page_text(pdf)
        info = informational(text)
        contact = contact_block(text)
        # The doc TYPE decides the extractor, not a phrase match. The Building
        # Plan Standards checklist literally contains the headings "MECHANICAL
        # FIXTURES" and "PLUMBING FIXTURES", so a content sniff pulled 24 plan
        # requirements ("Concrete strength", "Foundation wall schedule") in as
        # though they were plumbing fixtures.
        fixtures = [] if did in STANDARDS_DOCS else fixture_vocab(pdf)

        if did in STANDARDS_DOCS:
            items = plan_standards(pdf)
            if not items:
                print(f"  !! {pdf.name}: standards parse produced nothing")
                continue
            body = (f"{entry['name']} - City of Woodinville.\n\n"
                    "Your plan set must show the following:\n\n"
                    + "\n".join(f"* {i}" for i in items))
        elif not info and not fixtures and len(text.split()) < 120:
            print(f"  skip {pdf.name} — no extractable citizen content")
            continue
        else:
            body = info or squash(text)[:4000]
        aid = f"form-wv-{slug(entry['name'])}"
        art = make_article(
            city, aid, f"{entry['name']} (Woodinville)",
            (f"{entry['name']} for the City of Woodinville, WA - what the application "
             f"covers, what it asks for, and who to contact."),
            entry, body=body,
            # Keys are fixed by ingestor.article_to_chunks, which reads
            # field_name / label / description / example_value / why_needed --
            # anything else raises KeyError at ingest rather than degrading.
            form_fields=[{
                "field_name": slug(f).replace("-", "_"),
                "label": f,
                "description": (f"'{f}' is one of the countable items on the City's "
                                f"mechanical/plumbing fixture schedule."),
                "example_value": "1",
                "why_needed": ("Each listed fixture is counted, and the total drives "
                               "the mechanical and plumbing permit fees."),
            } for f in fixtures],
            contact=contact,
        )
        (OUT_DIR / f"{aid}.json").write_text(
            json.dumps(art, indent=2, ensure_ascii=True), encoding="utf-8")
        written += 1
        print(f"  wrote {aid:<52} body={len(body):>5}ch fixtures={len(fixtures):>3}")

    print(f"\n{written} articles written to {OUT_DIR} (status=review)")


if __name__ == "__main__":
    main()
