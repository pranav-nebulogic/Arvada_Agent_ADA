"""
agent/request_types.py
======================
Canonical Arvada request-type registry for Ask ADA.

This is the agent-side mirror of the smart-lp application catalog. **Keys equal
the smart-lp codes** (the apply-route `:code` and engine application_type code) so
the agent and the engine speak the same language with zero translation. The full
reconciliation against RFP 26-IT-007 Addendum 1 Exhibit A lives in
`arvada-permits-and-licenses/docs/arvada/CATALOG-ROLES-AND-REQUEST-MAP.md`.

Used to: classify a citizen message to a specific request type, route an
escalation to the owning department, and produce a **deep-link** to the citizen
portal apply page (Ask ADA guides the citizen to start the application — it does
NOT create it; the AI stays out of the write/decision path).

Deterministic, no LLM. The intent classifier extracts a free-text phrase; the
matching here is pure Python over a curated alias index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestType:
    key: str            # == smart-lp application_type code
    label: str          # human label for the apply CTA
    module: str         # permit | project | contractor | event
    department: str     # building|engineering|planning|stormwater|city_manager|all_departments
    kb_category: str    # building|row|planning|stormwater|events|licensing (retrieval hint)
    aliases: tuple[str, ...] = ()
    fee_key: str | None = None   # agent fee_tables key when the fee engine can estimate it


def _t(key, label, module, dept, kb, aliases=(), fee_key=None) -> RequestType:
    return RequestType(key, label, module, dept, kb, tuple(aliases), fee_key)


# ── The catalog ────────────────────────────────────────────────────────────────
# Order matters only for alias collisions: earlier entries win a shared bare word.
_TYPES: list[RequestType] = [
    # ── Permits · Building (kb: building) ────────────────────────────────────────
    _t("new_single_family", "New Single Family", "permit", "building", "building",
       ["new single family", "new home", "new house", "single family home", "new construction"], "building_permit"),
    _t("residential_interior", "Residential Interior", "permit", "building", "building",
       ["residential interior", "interior remodel", "remodel", "renovation", "basement finish",
        "kitchen remodel", "bathroom remodel", "finish basement"], "building_permit"),
    _t("residential_exterior", "Residential Exterior", "permit", "building", "building",
       ["residential exterior", "exterior remodel", "siding", "windows", "deck", "patio", "porch"], "building_permit"),
    _t("residential_electrical", "Residential Electrical", "permit", "building", "building",
       ["residential electrical", "electrical", "home electrical", "electrical permit", "rewire"]),
    _t("residential_mechanical", "Residential Mechanical", "permit", "building", "building",
       ["residential mechanical", "mechanical", "hvac", "furnace", "air conditioner", "ac", "heating"]),
    _t("residential_plumbing", "Residential Plumbing", "permit", "building", "building",
       ["residential plumbing", "plumbing", "water heater", "repipe"]),
    _t("adu", "ADU (Accessory Dwelling Unit)", "permit", "building", "building",
       ["adu", "accessory dwelling unit", "granny flat", "backyard cottage", "carriage house"], "building_permit"),
    _t("commercial", "Commercial Building", "permit", "building", "building",
       ["commercial building", "commercial permit", "commercial construction", "tenant finish"], "building_permit"),
    _t("commercial_electrical", "Commercial Electrical", "permit", "building", "building",
       ["commercial electrical"]),
    _t("commercial_mechanical", "Commercial Mechanical", "permit", "building", "building",
       ["commercial mechanical", "commercial hvac"]),
    _t("commercial_plumbing", "Commercial Plumbing", "permit", "building", "building",
       ["commercial plumbing"]),
    _t("comm_misc", "Commercial Misc", "permit", "building", "building",
       ["commercial misc", "commercial miscellaneous"]),
    _t("change_of_use", "Change of Use", "permit", "building", "building",
       ["change of use", "occupancy change", "change of occupancy"], "building_permit"),
    _t("roof", "Roof", "permit", "building", "building",
       ["roof", "reroof", "re-roof", "roofing", "new roof", "roof replacement"]),
    _t("solar", "Solar", "permit", "building", "building",
       ["solar", "solar panels", "pv", "photovoltaic", "rooftop solar", "solar install"], "building_permit_solar"),
    _t("retaining_wall", "Retaining Wall", "permit", "building", "building",
       ["retaining wall"], "retaining_wall"),
    _t("fence", "Fence", "permit", "building", "building",
       ["fence", "privacy fence", "yard fence", "new fence"]),
    _t("sign", "Sign", "permit", "building", "building",
       ["sign", "signage", "sign permit", "monument sign", "wall sign"]),
    _t("demolition", "Demolition", "permit", "building", "building",
       ["demolition", "demo permit", "tear down", "demolish", "teardown"]),
    _t("foundation_only", "Foundation Only", "permit", "building", "building",
       ["foundation only", "foundation permit"]),
    _t("ev_charger", "EV Charger", "permit", "building", "building",
       ["ev charger", "electric vehicle charger", "car charger", "ev charging", "evse"]),
    _t("elevator", "Elevator", "permit", "building", "building", ["elevator", "lift"]),
    _t("fire_pro", "Fire Protection", "permit", "building", "building",
       ["fire protection", "fire sprinkler", "fire alarm", "sprinkler system", "fire suppression"]),
    _t("backflow_device", "Backflow Device", "permit", "building", "building",
       ["backflow", "backflow preventer", "backflow device"]),
    _t("radio_amp", "Radio Amp", "permit", "building", "building",
       ["radio amp", "radio amplifier", "das", "signal booster"]),
    _t("telecom", "Telecom", "permit", "building", "building",
       ["telecom equipment", "telecommunications equipment", "cell equipment"]),
    _t("short_term_rental", "Short Term Rental", "permit", "building", "building",
       ["short term rental", "str", "airbnb", "vrbo", "str permit", "vacation rental"], "str_permit"),
    _t("bldg_receipt", "Building Receipt", "permit", "building", "building", ["building receipt"]),
    _t("misc", "Misc", "permit", "building", "building", ["miscellaneous permit"]),

    # ── Permits · Engineering (kb: row) ──────────────────────────────────────────
    _t("right_of_way", "Right of Way", "permit", "engineering", "row",
       ["right of way", "row", "row permit", "work in the street", "street work", "curb cut", "lane closure"]),
    _t("access_only", "Access Only", "permit", "engineering", "row", ["access only", "access permit"]),
    _t("eng_cip", "CIP (Engineering)", "permit", "engineering", "row", ["cip", "capital improvement"]),
    _t("development_row", "Development ROW", "permit", "engineering", "row",
       ["development row", "development right of way"]),
    _t("dry_utility", "Dry Utility", "permit", "engineering", "row",
       ["dry utility", "gas line", "electric line", "utility line"]),
    _t("flatwork_repair", "Flatwork Repair", "permit", "engineering", "row",
       ["flatwork", "flatwork repair", "sidewalk repair", "concrete repair"]),
    _t("floodplain_dev", "Floodplain Development", "permit", "engineering", "row",
       ["floodplain development", "floodplain permit", "floodplain dev", "floodplain"]),
    _t("mobility", "Mobility", "permit", "engineering", "row", ["mobility permit"]),
    _t("water_sewer_service", "Water/Sewer Service", "permit", "engineering", "row",
       ["water service", "sewer service", "water sewer service", "water tap", "sewer tap", "tap permit"]),
    _t("eng_receipt", "Engineering Receipt", "permit", "engineering", "row", ["engineering receipt"]),

    # ── Permits · Planning (kb: planning) ────────────────────────────────────────
    _t("mother_in_law", "Mother-in-Law", "permit", "planning", "planning",
       ["mother in law", "in-law suite", "mother in law unit", "mil unit"]),
    _t("small_cell", "Small Cell", "permit", "planning", "planning",
       ["small cell", "small cell antenna", "5g antenna"]),
    _t("master_small_cell", "Master Small Cell", "permit", "planning", "planning", ["master small cell"]),
    _t("temp_use", "Temporary Use", "permit", "planning", "planning",
       ["temporary use", "temp use", "temporary use permit"]),
    _t("trans_merch", "Transient Merchant", "permit", "planning", "planning",
       ["transient merchant", "peddler", "door to door sales", "mobile vendor"]),
    _t("film", "Film (Planning)", "permit", "planning", "planning", ["film planning permit"]),
    _t("pln_receipt", "Planning Receipt", "permit", "planning", "planning", ["planning receipt"]),

    # ── Permits · Stormwater (kb: stormwater) ────────────────────────────────────
    _t("site_disturbance", "Site Disturbance", "permit", "stormwater", "stormwater",
       ["site disturbance", "grading permit", "grading", "land disturbance", "erosion control"]),
    _t("post_construction", "Post Construction", "permit", "stormwater", "stormwater",
       ["post construction", "post-construction stormwater"]),

    # ── Permits · Cross-department (kb: building default) ────────────────────────
    _t("driveway", "Driveway", "permit", "all_departments", "row",
       ["driveway", "driveway permit", "new driveway", "driveway approach"]),
    _t("code_appeal", "Code Appeal", "permit", "all_departments", "building",
       ["code appeal", "building code appeal"]),

    # ── Events (kb: events) ──────────────────────────────────────────────────────
    _t("special_event", "Special Event", "event", "planning", "events",
       ["special event", "special event permit", "festival", "parade", "race", "5k", "public event",
        "park event", "olde town event", "fun run"], "special_event_permit"),
    _t("filming", "Filming", "event", "planning", "events",
       ["film", "filming", "movie permit", "film permit", "video shoot", "filming permit", "photo shoot"]),
    _t("block_party", "Block Party", "event", "city_manager", "events",
       ["block party", "street party", "neighborhood party"]),
    _t("tent_membrane", "Tent / Membrane Structure", "event", "all_departments", "events",
       ["tent", "tent permit", "membrane structure", "large tent"]),

    # ── Contractor licensing (kb: licensing) — trade/contractor ONLY ─────────────
    _t("contractor_license_municipal", "Contractor License", "contractor", "building", "licensing",
       ["contractor license", "contractor registration", "general contractor license",
        "municipal contractor license", "register as a contractor", "become a licensed contractor",
        "licensed contractor", "contractor registration"]),
    _t("class_a", "Class A General Contractor", "contractor", "building", "licensing",
       ["class a", "class a contractor", "class a license"]),
    _t("class_b", "Class B General Contractor", "contractor", "building", "licensing",
       ["class b", "class b contractor"]),
    _t("class_c", "Class C General Contractor", "contractor", "building", "licensing",
       ["class c", "class c contractor"]),
    _t("class_d_sub", "Class D Builder's Subcontractor", "contractor", "building", "licensing",
       ["class d", "subcontractor license", "builders subcontractor", "class d subcontractor"]),
    _t("class_h_homeowner", "Class H Homeowner Limited", "contractor", "building", "licensing",
       ["class h", "homeowner license", "homeowner limited", "owner builder"]),
    _t("class_m_mechanical", "Class M Mechanical", "contractor", "building", "licensing",
       ["class m", "mechanical license", "mechanical contractor license", "hvac license"]),
    _t("class_d_moving", "Class D Building Moving", "contractor", "building", "licensing",
       ["building moving license", "house moving license", "class d moving"]),
    _t("class_r_roofing", "Class R Roofing", "contractor", "building", "licensing",
       ["class r", "roofing license", "roofer license", "roofing contractor license"]),
    _t("class_l_limited", "Class L Limited", "contractor", "building", "licensing",
       ["class l", "limited license"]),
    _t("class_p_plumbing", "Class P Plumbing", "contractor", "building", "licensing",
       ["class p", "plumbing license", "plumber license", "plumbing contractor license"]),
    _t("class_e_electrical", "Class E Electrical", "contractor", "building", "licensing",
       ["class e", "electrical license", "electrician license", "electrical contractor license"]),
    _t("class_rm_radon", "Class RM Radon Mitigation", "contractor", "building", "licensing",
       ["class rm", "radon license", "radon mitigation license"]),

    # ── Planning projects (kb: planning) ─────────────────────────────────────────
    _t("pz_variance", "Variance", "project", "planning", "planning",
       ["variance", "zoning variance", "setback variance"]),
    _t("pz_appeal", "Appeal", "project", "planning", "planning",
       ["appeal", "planning appeal", "appeal a decision"]),
    _t("pz_floodplain_variance", "Floodplain Variance", "project", "planning", "planning",
       ["floodplain variance"]),
    _t("pz_preapplication", "Pre-Application", "project", "planning", "planning",
       ["pre-application", "preapplication", "pre application meeting", "pre-app", "preapp"]),
    _t("pz_rezone", "Zoning / Rezoning", "project", "planning", "planning",
       ["rezone", "rezoning", "zoning change", "zone change"]),
    _t("pz_annexation", "Annexation / Disconnection", "project", "planning", "planning",
       ["annexation", "disconnection", "annex"]),
    _t("pz_cup", "Conditional Use Permit", "project", "planning", "planning",
       ["conditional use permit", "cup", "conditional use"]),
    _t("pz_site_plan", "Site Plan", "project", "planning", "planning", ["site plan"]),
    _t("pz_site_plan_amendment", "Site Plan Amendment", "project", "planning", "planning",
       ["site plan amendment"]),
    _t("pz_min_plat", "Minor Subdivision Plat", "project", "planning", "planning",
       ["minor subdivision", "minor plat"]),
    _t("pz_maj_concept", "Major Subdivision — Concept", "project", "planning", "planning",
       ["major subdivision concept", "concept plan"]),
    _t("pz_maj_prelim", "Major Subdivision — Preliminary Plat", "project", "planning", "planning",
       ["preliminary plat", "major subdivision preliminary"]),
    _t("pz_maj_final", "Major Subdivision — Final Plat", "project", "planning", "planning",
       ["final plat", "major subdivision final"]),
    _t("pz_pud_sketch", "PUD Sketch Plan", "project", "planning", "planning", ["pud sketch", "pud sketch plan"]),
    _t("pz_pud_dev", "PUD Development Plan", "project", "planning", "planning", ["pud development plan", "pud dev"]),
    _t("pz_pud_final", "PUD Final Development Plan", "project", "planning", "planning",
       ["pud final development plan"]),
    _t("pz_pud_final_amend", "PUD Final Plan Amendment", "project", "planning", "planning",
       ["pud final amendment"]),
    _t("pz_master_dev", "Master Development Plan", "project", "planning", "planning", ["master development plan"]),
    _t("pz_vacation", "Vacation (Plat / ROW)", "project", "planning", "planning",
       ["vacation", "plat vacation", "row vacation"]),
    _t("pz_height_exception", "Height Exception", "project", "planning", "planning", ["height exception"]),
    _t("pz_ldc_amendment", "LDC Amendment", "project", "planning", "planning",
       ["ldc amendment", "land development code amendment", "code amendment"]),
    _t("pz_minor_modification", "Minor Modification", "project", "planning", "planning",
       ["minor modification", "minor mod"]),
    _t("pz_comp_plan_amendment", "Comprehensive Plan Amendment", "project", "planning", "planning",
       ["comprehensive plan amendment", "comp plan amendment"]),
    _t("pz_altern_sign_program", "Alternative Sign Program", "project", "planning", "planning",
       ["alternative sign program", "alt sign program"]),
    _t("pz_telecommunications", "Telecommunications (Planning)", "project", "planning", "planning",
       ["telecommunications project", "telecom project"]),
    _t("pz_referral", "Referral", "project", "planning", "planning", ["planning referral"]),
    _t("pz_right_of_refusal", "Right of Refusal", "project", "planning", "planning", ["right of refusal"]),
    _t("pz_vested_early", "Vested Right (Early)", "project", "planning", "planning", ["vested right early"]),
    _t("pz_vested_statutory", "Vested Right (Statutory)", "project", "planning", "planning",
       ["vested right statutory"]),
    _t("pz_addl_review", "Additional Development Review", "project", "planning", "planning",
       ["additional development review", "additional review"]),
    _t("pz_other", "Other (Planning)", "project", "planning", "planning", ["other planning"]),
]

REGISTRY: dict[str, RequestType] = {t.key: t for t in _TYPES}


def _norm(s: str) -> str:
    """Lowercase, trim, collapse whitespace/hyphens to single underscores."""
    return re.sub(r"[\s\-]+", "_", s.strip().lower()).strip("_")


# alias (normalized) -> key. First write wins, so list order decides collisions.
_ALIAS_INDEX: dict[str, str] = {}
for _rt in _TYPES:
    for _a in (_rt.key, _rt.label, *_rt.aliases):
        _n = _norm(_a)
        if _n and _n not in _ALIAS_INDEX and _n not in REGISTRY:
            _ALIAS_INDEX[_n] = _rt.key


def normalize(raw: str | None) -> str | None:
    """Map a code or a natural-language phrase to a canonical request-type key.

    Returns None when nothing matches confidently (the agent then simply does not
    offer a deep-link — never guesses a wrong request type)."""
    if not raw:
        return None
    n = _norm(str(raw))
    if n in REGISTRY:
        return n
    return _ALIAS_INDEX.get(n)


def get(key: str | None) -> RequestType | None:
    return REGISTRY.get(key) if key else None


# Surface-aware apply paths. One tenant hostname serves both surfaces, routed by
# path (ADR-0035): the citizen apply flow lives at the root; the agent workbench
# at /agent/*. So the deep-link TARGET depends on WHO is asking — a citizen lands
# in the citizen apply flow, a staff member in the agent intake.
_CITIZEN_PREFIX = {
    "permit": "/permits/apply/",
    "project": "/planning/apply/",
    "event": "/events/apply/",
}
# Citizen contractor licensing has no apply-by-code route yet -> the browse page.
_CITIZEN_CONTRACTOR_PATH = "/contractors"
# The agent workbench has no create-by-type route yet -> its (parameterless)
# intake door; staff pick the type inside the intake form.
_AGENT_INTAKE_PATH = "/agent/intake"


def surface_for(user_type: str | None) -> str:
    """Which portal surface a requester belongs to: 'agent' (staff) | 'citizen'."""
    return "agent" if (user_type or "").lower() == "staff" else "citizen"


def _apply_path(rt: RequestType, surface: str) -> str:
    if surface == "agent":
        return _AGENT_INTAKE_PATH            # no by-type deep-link in the workbench yet
    if rt.module == "contractor":
        return _CITIZEN_CONTRACTOR_PATH      # no apply-by-code route yet -> browse
    return _CITIZEN_PREFIX[rt.module] + rt.key


def apply_url(key: str, surface: str = "citizen", base: str = "") -> str | None:
    """Deep-link to start the application on the requester's portal surface.

    `surface` is 'citizen' or 'agent' (see `surface_for`). `base` is the tenant
    hostname serving both surfaces (e.g. https://arvada.smartlp-pilot.nebulogic.com);
    empty -> returns the relative path for the UI to resolve."""
    rt = REGISTRY.get(key)
    if not rt:
        return None
    path = _apply_path(rt, surface)
    return (base.rstrip("/") + path) if base else path


def contact_bucket(key: str | None) -> str:
    """Owning department -> escalation contact bucket. Only buckets with a
    *verified* phone number are returned (`building` | `planning` | `general`);
    engineering/stormwater/city-manager fall back to the general city line until
    their direct numbers are confirmed."""
    rt = REGISTRY.get(key) if key else None
    if not rt:
        return "general"
    if rt.module == "contractor":
        return "building"            # contractor/trade licensing is issued by Building
    if rt.module == "project":
        return "planning"
    if rt.module == "event":
        return "planning" if rt.key in ("special_event", "filming") else "general"
    return {"building": "building", "planning": "planning"}.get(rt.department, "general")


def prompt_catalog() -> str:
    """Compact grouped list (code — label) for optional embedding in a prompt."""
    by_mod: dict[str, list[RequestType]] = {}
    for t in _TYPES:
        by_mod.setdefault(t.module, []).append(t)
    lines: list[str] = []
    for mod, items in by_mod.items():
        lines.append(f"{mod}:")
        lines.append("  " + ", ".join(t.key for t in items))
    return "\n".join(lines)
