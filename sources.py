# scraper/sources.py
# Per-city source definitions to scrape. This is the ONLY place source URLs live.
# Add a new city by creating a new <CITY>_SOURCES dict and registering it in
# SOURCES_BY_CITY below; the scraper/eval select by DEFAULT_CITY_ID (get_sources()).

ARVADA_SOURCES = {
    "city_id": "arvada-co",
    "city_name": "City of Arvada, CO",
    "base_url": "https://www.arvadaco.gov",

    # ── Static HTML pages (httpx scraper) ──────────────────────────────────
    "html_pages": [
        # Building Permits
        {
            "url": "https://www.arvadaco.gov/332/Building-Permits",
            "permit_type": "building_permit",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/419/Residential-Exterior",
            "permit_type": "building_permit_residential_exterior",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/423/Residential-Interior",
            "permit_type": "building_permit_interior_remodel",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/522/Solar",
            "permit_type": "building_permit_solar",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/417/Re-Roofing",
            "permit_type": "building_permit_reroofing",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/346/Fences",
            "permit_type": "building_permit_fence",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/338/Accessory-Dwelling-Units-ADU",
            "permit_type": "building_permit_adu",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/344/Electrical",
            "permit_type": "building_permit_electrical",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/434/Residential-Mechanical-Plumbing",
            "permit_type": "building_permit_mechanical_plumbing",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/412/New-Single-Family",
            "permit_type": "building_permit_new_single_family",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/406/New-Commercial-Buildings-Commercial-Addi",
            "permit_type": "building_permit_commercial",
            "category": "building",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/525/windows-siding-installation",
            "permit_type": "building_permit_windows_siding",
            "category": "building",
            "article_type": "permit",
            "priority": 2,
        },
        # Building resources
        {
            "url": "https://www.arvadaco.gov/1240/Building-Permit-Resources",
            "permit_type": None,
            "category": "building",
            "article_type": "process",
            "priority": 2,
        },
        {
            "url": "https://www.arvadaco.gov/1388/New-Building-Codes-2026",
            "permit_type": None,
            "category": "building",
            "article_type": "process",
            "priority": 1,
        },
        # Licensing
        {
            "url": "https://www.arvadaco.gov/251/Licensing",
            "permit_type": None,
            "category": "licensing",
            "article_type": "process",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/263/Contractor-Licenses",
            "permit_type": "contractor_license",
            "category": "licensing",
            "article_type": "license",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/265/Building-Contractor",
            "permit_type": "contractor_license_building",
            "category": "licensing",
            "article_type": "license",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/283/Municipal-General-Contractor",
            "permit_type": "contractor_license_municipal",
            "category": "licensing",
            "article_type": "license",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/253/Business-Licenses",
            "permit_type": "business_license",
            "category": "business",
            "article_type": "license",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/259/Short-Term-Rentals",
            "permit_type": "str_permit",
            "category": "business",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/1509/Application-Info",
            "permit_type": "str_permit",
            "category": "business",
            "article_type": "process",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/1507/Operating-a-short-term-rental",
            "permit_type": "str_permit",
            "category": "business",
            "article_type": "process",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/402/Food-Trucks",
            "permit_type": "food_truck_permit",
            "category": "business",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/285/Liquor-Licenses",
            "permit_type": "liquor_license",
            "category": "licensing",
            "article_type": "license",
            "priority": 1,
        },
        # ROW & Development
        {
            "url": "https://www.arvadaco.gov/650/Right-of-Way-Permits",
            "permit_type": "row_permit",
            "category": "row",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/651/Development-Permits",
            "permit_type": "development_permit",
            "category": "development",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/363/Documents-Downloads",
            "permit_type": None,
            "category": "development",
            "article_type": "process",
            "priority": 2,
        },
        # Special Events
        {
            "url": "https://www.arvadaco.gov/663/Special-Event-Permits",
            "permit_type": "special_event_permit",
            "category": "events",
            "article_type": "permit",
            "priority": 1,
        },
        {
            "url": "https://www.arvadaco.gov/681/Special-Event-Permit-for-Public-Parks",
            "permit_type": "special_event_permit",
            "category": "events",
            "article_type": "process",
            "priority": 2,
        },
        # Tax
        {
            "url": "https://www.arvadaco.gov/315/Contractors",
            "permit_type": None,
            "category": "tax",
            "article_type": "tax",
            "priority": 1,
        },
    ],

    # ── JavaScript-rendered pages (Playwright scraper) ─────────────────────
    # These need a headless browser because content is rendered client-side
    "js_pages": [
        # NOTE: The CivicPlus /m/faq page is protected by Cloudflare bot detection
        # and cannot be scraped headlessly. All FAQ content is captured instead via
        # the Faq.aspx?TID=N HTML endpoints below (see "faq_endpoints").

        # ── Municode: Code of Ordinances (Part II — the regulatory code) ──────
        {
            "url": "https://library.municode.com/co/arvada/codes/code_of_ordinances",
            "permit_type": None,
            "category": "ordinance",
            "article_type": "ordinance",
            "priority": 2,
            "note": "Municode — scraped via api.municode.com. ALL chapters discovered dynamically.",
            "municode": {
                "client_name": "Arvada",
                "state_abbr": "CO",
                "product_name": "Code of Ordinances",
                # Root node whose children are the chapters. PART II = the actual ordinances.
                # Set to None to also include Part I (Charter).
                "root_node_ids": ["PTIICOOR"],
            },
        },

        # ── Municode: Land Development Code ───────────────────────────────────
        # NOT available via the Municode API (not listed under clientId=1075).
        # LDC chapters are ingested from Word/PDF documents downloaded manually
        # from https://library.municode.com/co/arvada/codes/land_development_code
        # and placed in arvada-agent/ldc_docs/. Processed by ldc_transformer.py.
    ],

    # ── PDFs (download and extract text) ───────────────────────────────────
    "pdfs": [
        {
            "url": "https://www.arvadaco.gov/DocumentCenter/View/6885",
            "name": "2026 Building Fee Schedule",
            "permit_type": "building_permit",
            "category": "building",
            "article_type": "fee_schedule",
            "priority": 1,  # MOST CRITICAL — has all fee tables
            "note": "Contains Table 18-1 (fee tiers) and Table 18-2 (valuation). Must parse tables.",
        },
        {
            "url": "https://www.arvadaco.gov/DocumentCenter/View/206",
            "name": "License and Permitting Guidelines",
            "permit_type": None,
            "category": "licensing",
            "article_type": "process",
            "priority": 1,
            "note": "Overview of all contractor licensing requirements.",
        },
        {
            "url": "https://www.arvadaco.gov/DocumentCenter/View/1144",
            "name": "Olde Town Arvada Historic District Guide",
            "permit_type": "building_permit",
            "category": "building",
            "article_type": "process",
            "priority": 3,
        },
    ],

    # ── FAQ topic pages (Faq.aspx?TID=N — static HTML, not Cloudflare-blocked) ─
    # These are the CivicPlus FAQ category pages. The full set of valid topic IDs
    # was discovered by probing the TID range (the /m/faq JS index is bot-blocked).
    # Each page contains all Q&A pairs for one category; the transformer splits each
    # page into one knowledge article per Q&A. The real category name is extracted
    # from the page content at transform time (do NOT rely on a hardcoded label).
    "faq_endpoints": [
        {"url": f"https://www.arvadaco.gov/Faq.aspx?TID={tid}"}
        for tid in [
            16, 17, 18, 20, 21, 22, 23, 24, 25, 26, 28, 29, 30, 31, 32, 33,
            34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49,
            51, 53,
        ]
    ],
}

# ── Golden Query Set for Evaluation ────────────────────────────────────────
# 20 test queries with expected article matches.
# Build this BEFORE writing any agent code.
GOLDEN_QUERIES = [
    # Building Permits
    {"query": "How much does a solar permit cost in Arvada?",
     "expected_permit_type": "building_permit_solar",
     "expected_section": "fees",
     "expected_answer_contains": ["$50", "plan review"]},
    {"query": "What documents do I need for a residential exterior permit?",
     "expected_permit_type": "building_permit_residential_exterior",
     "expected_section": "required_documents",
     "expected_answer_contains": ["plot plan", "site plan"]},
    {"query": "How long does a building permit plan review take?",
     "expected_permit_type": "building_permit",
     "expected_section": "timelines",
     "expected_answer_contains": ["4 to 6 weeks", "1 business day"]},
    {"query": "Can I do my own electrical work as a homeowner?",
     "expected_permit_type": "building_permit_electrical",
     "expected_section": "eligibility",
     "expected_answer_contains": ["homeowner"]},
    {"query": "What is the fee for a $25,000 home renovation?",
     "expected_permit_type": "building_permit",
     "expected_section": "fees",
     "expected_answer_contains": ["401.75", "valuation", "Table 18-1"]},
    # STR
    {"query": "What do I need to apply for a short term rental permit?",
     "expected_permit_type": "str_permit",
     "expected_section": "required_documents",
     "expected_answer_contains": ["application", "license"]},
    {"query": "How much does a short term rental permit cost?",
     "expected_permit_type": "str_permit",
     "expected_section": "fees",
     "expected_answer_contains": ["$150"]},
    {"query": "How many short term rentals can one owner have?",
     "expected_permit_type": "str_permit",
     "expected_section": "eligibility",
     "expected_answer_contains": ["3"]},
    # Business & Food Truck
    {"query": "Does a business license in Arvada cost anything?",
     "expected_permit_type": "business_license",
     "expected_section": "fees",
     "expected_answer_contains": ["free"]},
    {"query": "What permits do I need to operate a food truck?",
     "expected_permit_type": "food_truck_permit",
     "expected_section": "eligibility",
     "expected_answer_contains": ["business license", "food truck permit", "$60"]},
    # Special Events
    {"query": "When do I need a special event permit?",
     "expected_permit_type": "special_event_permit",
     "expected_section": "eligibility",
     "expected_answer_contains": ["100 attendees", "public property"]},
    {"query": "How much advance notice is needed for a special event permit?",
     "expected_permit_type": "special_event_permit",
     "expected_section": "timelines",
     "expected_answer_contains": ["60 days"]},
    # Contractor
    {"query": "How do I get a contractor license in Arvada?",
     "expected_permit_type": "contractor_license",
     "expected_section": "steps",
     "expected_answer_contains": ["SMART License & Permits", "arvadapermits.org"]},
    # ROW
    {"query": "What is a right of way permit?",
     "expected_permit_type": "row_permit",
     "expected_section": "summary",
     "expected_answer_contains": ["right-of-way", "construction"]},
    # Use Tax
    {"query": "What is use tax and how is it calculated for a permit?",
     "expected_permit_type": "building_permit",
     "expected_section": "fees",
     "expected_answer_contains": ["3.46", "use tax", "materials"]},
    # Form Help (embedded agent context)
    {"query": "What does 'project valuation' mean on the permit application?",
     "expected_permit_type": "building_permit",
     "expected_section": "form_fields",
     "expected_answer_contains": ["total cost", "labor", "materials"]},
    # Adversarial (agent must not hallucinate — retrieval check only: no match expected)
    {"query": "How much does a Tesla Cybertruck permit cost?",
     "expected_permit_type": None,
     "expected_section": None,
     "expected_answer_contains": [],
     "agent_only": True},
    # Edge case: jurisdiction (agent-layer behaviour, not retrievable from KB)
    {"query": "Is my address at 12345 Wadsworth Blvd in Arvada's jurisdiction?",
     "expected_permit_type": None,
     "expected_section": None,
     "expected_answer_contains": [],
     "agent_only": True},
    # Escalation trigger
    {"query": "I need to speak to someone about a denied permit",
     "expected_permit_type": "building_permit",
     "expected_section": None,
     "expected_intent": "ESCALATE",
     "expected_answer_contains": ["720-898-7620"]},
    # Multi-permit query
    {"query": "I want to run a food truck at a special event. What do I need?",
     "expected_permit_type": None,  # should surface both food_truck + special_event
     "expected_section": None,
     "expected_answer_contains": ["food truck permit", "special event", "business license"]},
]


# ── Woodinville, WA ─────────────────────────────────────────────────────────
# CivicPlus city site (woodinville.gov), same CMS as Arvada — the StaticCrawler
# HTML path transfers. The Woodinville Municipal Code and the 2026 Fee Schedule
# are ingested from local PDFs (wmc_pdf_transformer.py / fee_schedule_transformer.py),
# NOT scraped (codepublishing.com is bot-blocked). The FAQ is a single-page
# accordion parsed by woodinville_faq.py. So js_pages/faq_endpoints are empty here;
# html_pages are the permit/licensing process pages (LLM transform → human review).
WOODINVILLE_SOURCES = {
    "city_id": "woodinville-wa",
    "city_name": "City of Woodinville, WA",
    "base_url": "https://www.woodinville.gov",
    "html_pages": [
        {"url": "https://www.woodinville.gov/200/Permitting",
         "permit_type": None, "category": "building", "article_type": "process", "priority": 1},
        {"url": "https://www.woodinville.gov/199/Development-Services",
         "permit_type": None, "category": "development", "article_type": "process", "priority": 1},
        {"url": "https://www.woodinville.gov/364/Commercial-Residential-Construction-Perm",
         "permit_type": "building_permit", "category": "building", "article_type": "permit", "priority": 1},
        {"url": "https://www.woodinville.gov/366/Mechanical-Plumbing-Permits",
         "permit_type": "building_permit_mechanical_plumbing", "category": "building", "article_type": "permit", "priority": 1},
        {"url": "https://www.woodinville.gov/367/Electrical-Permits",
         "permit_type": "building_permit_electrical", "category": "building", "article_type": "permit", "priority": 2},
        {"url": "https://www.woodinville.gov/368/Site-Development-Permit",
         "permit_type": "development_permit", "category": "development", "article_type": "permit", "priority": 1},
        {"url": "https://www.woodinville.gov/369/Fire-Permits",
         "permit_type": "fire_permit", "category": "building", "article_type": "permit", "priority": 2},
        {"url": "https://www.woodinville.gov/370/Inspections",
         "permit_type": None, "category": "building", "article_type": "process", "priority": 2},
        {"url": "https://www.woodinville.gov/371/Water-Sewer-Permits",
         "permit_type": "row_permit", "category": "development", "article_type": "permit", "priority": 2},
        {"url": "https://www.woodinville.gov/372/Sign-Permits",
         "permit_type": "sign_permit", "category": "building", "article_type": "permit", "priority": 2},
        {"url": "https://www.woodinville.gov/463/Portable-Temporary-Signs",
         "permit_type": "sign_permit", "category": "building", "article_type": "permit", "priority": 3},
        {"url": "https://www.woodinville.gov/373/Right-of-Way-Permits",
         "permit_type": "row_permit", "category": "row", "article_type": "permit", "priority": 1},
        {"url": "https://www.woodinville.gov/379/Special-Events",
         "permit_type": "special_event_permit", "category": "events", "article_type": "permit", "priority": 1},
        {"url": "https://www.woodinville.gov/416/Tree-Protection-Removal",
         "permit_type": "tree_permit", "category": "development", "article_type": "permit", "priority": 1},
        {"url": "https://www.woodinville.gov/348/Applications-Forms",
         "permit_type": None, "category": "building", "article_type": "process", "priority": 2},
        {"url": "https://www.woodinville.gov/347/Apply-for-a-Permit-Online",
         "permit_type": None, "category": "building", "article_type": "process", "priority": 1},
        {"url": "https://www.woodinville.gov/207/Pre-Application-Meetings",
         "permit_type": None, "category": "development", "article_type": "process", "priority": 2},

        # ── Licensing ───────────────────────────────────────────────────────
        # The whole licensing branch was missing, which is why the agent had
        # nothing to say about running a business in Woodinville.
        {"url": "https://www.woodinville.gov/183/Licenses",
         "permit_type": None, "category": "licensing", "article_type": "process", "priority": 1},
        {"url": "https://www.woodinville.gov/184/Business-License",
         "permit_type": "business_license", "category": "licensing", "article_type": "license", "priority": 1},
        {"url": "https://www.woodinville.gov/592/Business-Licenses",
         "permit_type": "business_license", "category": "licensing", "article_type": "license", "priority": 1},
        {"url": "https://www.woodinville.gov/580/Proposed-Business-License-Changes",
         "permit_type": "business_license", "category": "licensing", "article_type": "process", "priority": 3},
        {"url": "https://www.woodinville.gov/185/Peddlers-License",
         "permit_type": "peddlers_license", "category": "licensing", "article_type": "license", "priority": 2},
        {"url": "https://www.woodinville.gov/186/Pet-License",
         "permit_type": "pet_license", "category": "licensing", "article_type": "license", "priority": 2},

        # ── Business support ────────────────────────────────────────────────
        {"url": "https://www.woodinville.gov/587/Doing-Business",
         "permit_type": None, "category": "licensing", "article_type": "process", "priority": 2},
        {"url": "https://www.woodinville.gov/588/For-Businesses",
         "permit_type": None, "category": "licensing", "article_type": "process", "priority": 2},
        {"url": "https://www.woodinville.gov/590/Small-Business-Resources",
         "permit_type": None, "category": "licensing", "article_type": "process", "priority": 3},

        # ── Land use & planning ─────────────────────────────────────────────
        {"url": "https://www.woodinville.gov/374/Land-Use-Zoning",
         "permit_type": None, "category": "planning", "article_type": "process", "priority": 1},
        {"url": "https://www.woodinville.gov/210/Long-Range-Planning",
         "permit_type": None, "category": "planning", "article_type": "process", "priority": 3},
        {"url": "https://www.woodinville.gov/286/Planning-Commission",
         "permit_type": None, "category": "planning", "article_type": "process", "priority": 3},
        {"url": "https://www.woodinville.gov/287/Design-Review-Committee",
         "permit_type": None, "category": "planning", "article_type": "process", "priority": 3},

        # ── Compliance & code ───────────────────────────────────────────────
        {"url": "https://www.woodinville.gov/378/Code-Enforcement",
         "permit_type": None, "category": "compliance", "article_type": "process", "priority": 2},
        {"url": "https://www.woodinville.gov/165/Codes-Ordinances-Resolutions",
         "permit_type": None, "category": "compliance", "article_type": "process", "priority": 2},

        # ── Fees ────────────────────────────────────────────────────────────
        # The fee SCHEDULE itself is ingested from the local PDF
        # (fee_schedule_transformer.py); this is the city's fees landing page.
        {"url": "https://www.woodinville.gov/380/Fees",
         "permit_type": None, "category": "fees", "article_type": "process", "priority": 1},

        # ── Stormwater / surface water ──────────────────────────────────────
        {"url": "https://www.woodinville.gov/391/Surface-Water-Management",
         "permit_type": None, "category": "stormwater", "article_type": "process", "priority": 2},
        {"url": "https://www.woodinville.gov/393/Stormwater-Permit-NPDES",
         "permit_type": "stormwater_permit", "category": "stormwater", "article_type": "permit", "priority": 2},
        {"url": "https://www.woodinville.gov/394/Stormwater-Utility",
         "permit_type": None, "category": "stormwater", "article_type": "process", "priority": 3},
        {"url": "https://www.woodinville.gov/469/Source-Control-Inspections-for-Businesse",
         "permit_type": None, "category": "stormwater", "article_type": "process", "priority": 3},
    ],
    # WMC comes from local PDFs; no Municode API for Woodinville.
    "js_pages": [],
    # DocumentCenter PDFs can be added here as {"url", "name", ...}; the 2026 Fee
    # Schedule is already ingested from the local copy (fee_schedule_transformer.py).
    "pdfs": [],
    # FAQ is a single-page accordion handled by woodinville_faq.py (not TID pages).
    "faq_endpoints": [],
}


# ── City source registry ────────────────────────────────────────────────────
SOURCES_BY_CITY = {
    "arvada-co": ARVADA_SOURCES,
    "woodinville-wa": WOODINVILLE_SOURCES,
}


def get_sources(city_id: str | None = None) -> dict:
    """Return the SOURCES dict for the active city (DEFAULT_CITY_ID by default)."""
    from config import settings
    cid = (city_id or settings.default_city_id or "arvada-co").strip()
    if cid not in SOURCES_BY_CITY:
        raise KeyError(f"No sources defined for city_id {cid!r}; add a <CITY>_SOURCES dict")
    return SOURCES_BY_CITY[cid]


# Woodinville eval set — grounded in the ingested WMC + 2026 Fee Schedule + FAQ.
WOODINVILLE_GOLDEN_QUERIES = [
    {"query": "How much is a building permit for a $25,000 project in Woodinville?",
     "expected_permit_type": "building_permit", "expected_section": "fees",
     "expected_answer_contains": ["510", "valuation"]},
    {"query": "What is the building permit fee for a $1,000 project?",
     "expected_permit_type": "building_permit", "expected_section": "fees",
     "expected_answer_contains": ["195"]},
    {"query": "How much does a tree removal permit cost without construction?",
     "expected_permit_type": None, "expected_section": None,
     "expected_answer_contains": ["43"]},
    {"query": "When is a building permit required in Woodinville?",
     "expected_permit_type": None, "expected_section": "faq",
     "expected_answer_contains": ["permit is required"]},
    {"query": "What building codes has Woodinville adopted?",
     "expected_permit_type": None, "expected_section": None,
     "expected_answer_contains": ["building code"]},
    {"query": "How is building permit valuation determined?",
     "expected_permit_type": "building_permit", "expected_section": None,
     "expected_answer_contains": ["valuation"]},
    {"query": "How do I get a garage sale permit?",
     "expected_permit_type": None, "expected_section": "faq",
     "expected_answer_contains": []},
    {"query": "What is the fee for a boundary line adjustment?",
     "expected_permit_type": None, "expected_section": None,
     "expected_answer_contains": ["5,387"]},
]

GOLDEN_QUERIES_BY_CITY = {
    "arvada-co": GOLDEN_QUERIES,
    "woodinville-wa": WOODINVILLE_GOLDEN_QUERIES,
}


def get_golden_queries(city_id: str | None = None) -> list:
    from config import settings
    cid = (city_id or settings.default_city_id or "arvada-co").strip()
    return GOLDEN_QUERIES_BY_CITY.get(cid, GOLDEN_QUERIES)
