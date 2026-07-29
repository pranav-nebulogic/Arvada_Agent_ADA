"""
engine_fees
===========
Reads the ENGINE's per-tenant application-type catalog and its computed fee
estimate, instead of computing fees from the agent's own `fee_schedules` table.

WHY: the agent's table is sparse -- Woodinville has exactly ONE row (the
building-permit valuation table), so a $825k project quoted $8,435.00 while the
portal showed $14,647.97 all-in. The engine holds the tenant's REAL, fully
configured schedule (plan review, fire review, surcharges, taxes, deposits) and
is what the citizen portal renders, so sourcing from it means chat and portal
can never disagree.

It is also the "city meaning is data" rule: fee lines and permit names are
tenant configuration, not agent code. A new city needs zero changes here.

Both endpoints are ANONYMOUS (`/api/public/catalog/{tenant}/...`) and take the
tenant in the PATH, so no s2s token and no `X-Tenant-Id` juggling -- but they do
need the engine tenant CODE ("woodinville"), not the agent's city_id
("woodinville-wa"). `CityProfile.engine_tenant` does that mapping.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx
from urllib.parse import urlencode

from city_config import get_city
from config import settings
from agent.obs import log

# Catalog is near-static tenant config; the fee estimate is a live calculation
# and is never cached.
_CATALOG_TTL_SECONDS = 600
_catalog_cache: dict[str, tuple[float, list[dict]]] = {}

# Modules that are never a project fee estimate.
#   inspections -- not applied for; Arvada alone has 75 of them.
#   contractor  -- LICENSE classes, not permits. Arvada's "Builder's Unlimited
#                  (I-B)" is described as "Construct, alter, or repair any
#                  building or structure", so a description match on "building"
#                  offered contractor licences as answers to a remodel fee
#                  question. Licensing fees are a different question entirely.
_SKIP_MODULES = {"inspections", "contractor"}

# Minimum score to price from the engine at all. 3.0 == at least one NAME token
# hit; description-only matches are too weak to trust and are what produced the
# contractor-licence regression above. Below this we return nothing and the
# caller falls back to the local fee table.
_MIN_SCORE = 3.0

_WORD = re.compile(r"[a-z0-9]+")
# Words that appear in almost every permit name and so carry no signal for
# telling Woodinville's ~60 types apart. NOTE "new" is deliberately NOT here:
# it separates "Residential New" from "Residential Addition", and stopwording it
# made "new house" miss the very type it names.
_STOPWORDS = {
    "the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "with",
    "permit", "permits", "application", "apply", "city", "fee", "fees",
    "cost", "costs", "price", "how", "much", "is", "my", "i", "what", "need",
}

# Everyday words -> the vocabulary the catalog actually uses. People do not type
# "Residential"; they type "house". Kept tiny and generic (no city specifics) so
# it stays portable.
_SYNONYMS = {
    "house": "residential", "home": "residential", "houses": "residential",
    "apartment": "multi", "condo": "multi", "duplex": "residential",
    "shop": "commercial", "store": "commercial", "restaurant": "commercial",
    "business": "commercial", "office": "commercial",
    "reroof": "roof", "roofing": "roof",
    "remodel": "remodel", "renovation": "remodel", "renovate": "remodel",
}


def _tokens(text: str | None) -> set[str]:
    out: set[str] = set()
    for w in _WORD.findall((text or "").lower()):
        # Bare numbers are pure noise: "2 bedrooms" matched an Appeal type whose
        # description mentions "a Type 1 or 2 decision".
        if w.isdigit() or w in _STOPWORDS:
            continue
        out.add(w)
        if w in _SYNONYMS:
            out.add(_SYNONYMS[w])
    return out


def _base() -> str:
    return (settings.engine_case_api_base or "").rstrip("/")


async def fetch_catalog(city_id: str | None) -> list[dict]:
    """The tenant's application types, cached briefly. [] when unavailable."""
    city = get_city(city_id)
    key = city.city_id
    hit = _catalog_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CATALOG_TTL_SECONDS:
        return hit[1]
    if not _base():
        return []

    url = f"{_base()}/api/public/catalog/{city.engine_tenant}/application-types"
    try:
        async with httpx.AsyncClient(timeout=settings.engine_timeout_seconds) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:  # noqa: BLE001 -- callers degrade to the local table
        log.warning("engine_catalog_failed", city=key, error=str(exc))
        return []

    if not isinstance(rows, list):
        return []
    rows = [r for r in rows if isinstance(r, dict)
            and r.get("module") not in _SKIP_MODULES]
    _catalog_cache[key] = (time.monotonic(), rows)
    log.info("engine_catalog_loaded", city=key, types=len(rows))
    return rows


def label_for(row: dict) -> str:
    """The name the citizen sees on the portal's Apply screen."""
    return row.get("citizenLabel") or row.get("name") or row.get("code") or ""


def match_types(text: str, rows: list[dict], limit: int = 4) -> list[dict]:
    """Rank catalog types against the user's words, best first.

    Matches on citizenLabel/name (weighted) plus citizenDescription, because the
    portal's names alone don't contain the words people actually type: nothing in
    Woodinville is called "building permit", but `wv_bld_res_new`'s description
    says "Build a new single-family home... Building permit only".
    """
    want = _tokens(text)
    if not want or not rows:
        return []
    scored: list[tuple[float, float, dict]] = []
    for r in rows:
        if r.get("feeEstimatorExclude"):
            continue
        name_t = _tokens(label_for(r)) | _tokens(r.get("name"))
        desc_t = _tokens(r.get("citizenDescription"))
        # A name hit is worth far more than a description hit: "building" appears
        # in half the commercial descriptions but names the type in none of them.
        score = 3.0 * len(want & name_t) + 1.0 * len(want & desc_t)
        if score <= 0:
            continue
        # Prefer the more specific name when scores tie (fewer extra words), then
        # the catalog's own ordering -- cities list the common types first.
        score -= 0.01 * len(name_t - want)
        try:
            order = float(r.get("sortOrder") or 999)
        except (TypeError, ValueError):
            order = 999.0
        scored.append((score, order, r))
    scored.sort(key=lambda s: (-s[0], s[1]))
    if not scored or scored[0][0] < _MIN_SCORE:
        return []          # too weak to price OR to ask about -- let the caller fall back
    out = []
    for score, _, r in scored[:limit]:
        row = dict(r)
        row["_score"] = round(score, 3)
        out.append(row)
    return out


# Audience prefixes cities use to split the same work by who is doing it.
# Ordered: the first whose token appears in the query wins.
_AUDIENCES = (
    ("commercial", {"commercial", "business", "shop", "store", "restaurant", "office", "retail"}),
    ("multi", {"multi", "apartment", "condo", "multifamily"}),
    ("mixed", {"mixed"}),
    ("residential", {"residential", "house", "home", "duplex", "dwelling"}),
)


def _audience_of(text: str) -> str | None:
    t = _tokens(text)
    for name, words in _AUDIENCES:
        if t & words:
            return name
    return None


def narrow_by_audience(text: str, matches: list[dict]) -> tuple[list[dict], bool]:
    """Resolve residential-vs-commercial, which is NOT a real question to ask.

    "deck" scores Residential Deck, Commercial Deck and Multi-Family Deck almost
    identically -- but they are the same work for different audiences, and this
    is a citizen portal. If the user named an audience, keep only that one; if
    not, assume residential and report that we assumed, rather than asking a
    question whose answer is nearly always "residential".

    -> (narrowed matches, assumed_residential)
    """
    if not matches:
        return matches, False
    stated = _audience_of(text)
    target = stated or "residential"
    kept = [m for m in matches
            if _audience_of(label_for(m) + " " + (m.get("name") or "")) == target]
    if not kept:
        return matches, False

    # Never let audience narrowing overrule a decisive winner. This exists to
    # choose among near-equals ("deck" -> Residential Deck vs Commercial Deck),
    # NOT to promote a weaker match. "tree removal permit" scored Tree Removal
    # Permit at 8.0 against 3.99 for the next candidate, but Tree Removal carries
    # no audience word, so narrowing dropped it and priced a $17,371 Residential
    # New Construction for a tree question. If the top-scoring match is not in
    # the kept set, the audience is simply not the distinguishing feature here.
    if matches[0] not in kept:
        return matches, False
    return kept, stated is None


def is_ambiguous(matches: list[dict]) -> bool:
    """True when the top match isn't clearly ahead of the runner-up.

    "building permit" scores nearly every building type equally -- it names
    neither residential nor commercial -- so asserting one would silently price
    the wrong permit. Better to price nothing and ask.
    """
    if len(matches) < 2:
        return False
    top, second = matches[0].get("_score", 0), matches[1].get("_score", 0)
    return (top - second) < 1.0


# Portal path per catalog module, mirroring request_types._CITIZEN_PREFIX. The
# engine's catalog uses the same module vocabulary, so the deep link is built the
# same way -- only the CODE and LABEL now come from the tenant instead of a
# hardcoded list.
_CITIZEN_PREFIX = {
    "permit": "/permits/apply/",
    "project": "/planning/apply/",
    "planning": "/planning/apply/",
    "event": "/events/apply/",
}
_AGENT_INTAKE_PATH = "/agent/intake"


async def resolve_apply(city_id: str | None, text: str,
                        surface: str = "citizen") -> dict | None:
    """The 'start this application' CTA, named and coded by THIS city's catalog.

    Previously the CTA came from request_types.REGISTRY -- a static ARVADA list --
    so a Woodinville user asking about a kitchen remodel was offered "Residential
    Interior", a type Woodinville does not have (its catalog calls it
    "Residential Remodel / Tenant Improvement (TI)"), pointing at
    /permits/apply/residential_interior which does not exist there.

    Returns None rather than guessing: no catalog, or nothing matching, means no
    button. A missing CTA is invisible; a broken one is the bug being fixed.
    """
    rows = await fetch_catalog(city_id)
    if not rows:
        return None
    matches = match_types(text, rows)
    if not matches:
        return None
    matches, _assumed = narrow_by_audience(text, matches)
    if not matches:
        return None
    # NOTE: deliberately NOT gated on is_ambiguous, unlike fee pricing. A near-tie
    # here is usually variants of the same work ("Residential Remodel / TI" vs the
    # same "(Combo)"), and the UI already renders "Choose a different service
    # type" next to this button -- so a best guess the citizen can change beats no
    # button. A wrong FEE is a wrong number; a wrong CTA is one click to correct.
    # match_types has already ordered by score then the catalog's own sortOrder,
    # so the pick is the city's preferred variant, not arbitrary.

    best = matches[0]
    code = best.get("code")
    module = best.get("module") or "permit"
    if surface == "agent":
        path = _AGENT_INTAKE_PATH
    else:
        prefix = _CITIZEN_PREFIX.get(module)
        if not prefix or not code:
            return None
        path = f"{prefix}{code}"

    return {
        "code": code,
        "label": label_for(best),      # the exact name on the portal's Apply screen
        "module": module,
        "surface": surface,
        # RELATIVE, never absolute. adaTypes.ts documents this field as a relative
        # path and every consumer calls react-router `navigate(apply.url)` -- an
        # absolute URL is taken as a route, which opened a blank tab titled
        # "/https:/woodinville.smart-l...". Relative also keeps the citizen on
        # their own city's host by construction, so no per-city base is needed.
        "url": path,
    }


# ── Fee drivers ──────────────────────────────────────────────────────────────
# Which answers move the number is TENANT CONFIG, not code. The portal derives it
# in ApplyPreCheckModal.tsx (feeDrivingCodes): every rule card carrying a pay-fee
# obligation contributes the fields named in its predicate. We port that exactly,
# so Arvada and any future city get their own drivers with no change here.
_type_meta_cache: dict[str, tuple[float, dict]] = {}

_QTY_HINT = re.compile(r"(^qty_)|(_count$)|(fixture)", re.I)


def _collect_predicate_fields(pred: Any, out: set[str]) -> None:
    """Walk a rule-card predicate collecting `check.field` names.

    Mirrors collectPredicateFields in ApplyPreCheckModal.tsx: predicates nest
    under `inline`/`items`, and field names may carry an `answer.` prefix.
    """
    if not isinstance(pred, dict):
        return
    inline = pred.get("inline") or pred
    items = inline.get("items") if isinstance(inline, dict) else None
    if not isinstance(items, list):
        return
    for raw in items:
        if not isinstance(raw, dict):
            continue
        field = (raw.get("check") or {}).get("field")
        if field:
            out.add(re.sub(r"^answer\.", "", str(field)))
        _collect_predicate_fields(raw, out)


async def _get_json(url: str) -> Any | None:
    try:
        async with httpx.AsyncClient(timeout=settings.engine_timeout_seconds) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 -- every caller degrades gracefully
        log.warning("engine_get_failed", url=url.rsplit("/", 2)[-2:], error=str(exc))
        return None


def quantity_fields(data: dict) -> set[str]:
    """Field codes the ESTIMATE itself says supply a line's quantity.

    The portal reads these off the estimate response (ApplyPreCheckModal.tsx:192)
    -- they are not in any rule-card predicate, so a single pass misses them.
    Woodinville's park, school and transportation impact lines all declare
    `quantityField: new_units`, and without that answer every one of them prices
    at $0.00 no matter what else is supplied. Discovering them needs a first
    estimate, hence the two-pass in `_try_engine`.
    """
    out: set[str] = set()
    for line in (data or {}).get("lines") or []:
        if isinstance(line, dict) and line.get("quantityField"):
            out.add(str(line["quantityField"]))
    return out


async def fields_for(city_id: str | None, code: str, codes: set[str]) -> list[dict]:
    """Field definitions for specific codes (used for quantity fields)."""
    if not codes:
        return []
    all_fields = await _all_fields(city_id, code)
    return [f for f in all_fields if f.get("code") in codes]


async def _all_fields(city_id: str | None, code: str) -> list[dict]:
    city = get_city(city_id)
    key = f"{city.city_id}/{code}"
    hit = _type_meta_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CATALOG_TTL_SECONDS and "fields" in hit[1]:
        return hit[1]["fields"]
    base = f"{_base()}/api/public/catalog/{city.engine_tenant}/application-types/{code}"
    fields = await _get_json(f"{base}/fields") or []
    fields = fields if isinstance(fields, list) else []
    entry = dict(hit[1]) if hit else {}
    entry["fields"] = fields
    _type_meta_cache[key] = (time.monotonic(), entry)
    return fields


async def fee_drivers(city_id: str | None, code: str) -> list[dict]:
    """The fields whose answers change this type's fee, with their definitions.

    Each entry carries `code`, `label`, `description`, `dataType`, `enumValues`
    and `defaultValue` -- enough to ask a human-readable question and to validate
    the reply without guessing.
    """
    if not _base() or not code:
        return []
    city = get_city(city_id)
    key = f"{city.city_id}/{code}"
    hit = _type_meta_cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CATALOG_TTL_SECONDS:
        return hit[1].get("drivers", [])

    base = f"{_base()}/api/public/catalog/{city.engine_tenant}/application-types/{code}"
    cards = await _get_json(f"{base}/rule-cards") or []
    fields = await _get_json(f"{base}/fields") or []
    if not isinstance(fields, list):
        return []

    driver_codes: set[str] = set()
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        obligations = card.get("obligations") or []
        if not any((o or {}).get("type") == "pay-fee" for o in obligations):
            continue
        _collect_predicate_fields(card.get("predicate"), driver_codes)

    excluded = set(_excluded_estimator_fields(city.city_id, code))
    drivers = [
        f for f in fields
        if isinstance(f, dict)
        and (f.get("code") in driver_codes or _QTY_HINT.search(str(f.get("code") or "")))
        and f.get("code") not in excluded
    ]
    _type_meta_cache[key] = (time.monotonic(), {"drivers": drivers})
    log.info("fee_drivers_resolved", city=city.city_id, code=code,
             drivers=[d.get("code") for d in drivers])
    return drivers


def _excluded_estimator_fields(city_id: str, code: str) -> list[str]:
    """`attributes.feeEstimatorExclude` for a type, from the cached catalog."""
    hit = _catalog_cache.get(city_id)
    if not hit:
        return []
    for row in hit[1]:
        if row.get("code") == code:
            ex = row.get("feeEstimatorExclude")
            return list(ex) if isinstance(ex, list) else []
    return []


def pre_estimate_seed(city_id: str | None, code: str) -> dict:
    """The type's `attributes.preEstimate` -- what the portal's calculator opens with."""
    hit = _catalog_cache.get(get_city(city_id).city_id)
    for row in (hit[1] if hit else []):
        if row.get("code") == code:
            pe = row.get("preEstimate")
            if isinstance(pe, dict):
                return pe
    return {}


async def fee_estimate(city_id: str | None, code: str,
                       valuation: float | None = None,
                       answers: dict | None = None) -> dict[str, Any] | None:
    """Engine-computed, itemised estimate for one type. None on any failure.

    Uses the POST variant, which evaluates the type's rule cards against
    `answers`. The GET variant only accepts valuation, so every answer-driven fee
    -- park, school and transportation impact, fixture counts -- was silently
    missing: a $500k Woodinville house came back $9,570.76 against the portal's
    $36,121.31.
    """
    if not _base() or not code:
        return None
    city = get_city(city_id)
    url = (f"{_base()}/api/public/catalog/{city.engine_tenant}"
           f"/application-types/{code}/fee-estimate")

    seed = pre_estimate_seed(city_id, code)
    body: dict[str, Any] = {"answers": {**(seed.get("answers") or {}), **(answers or {})}}
    if valuation is not None:
        # The engine takes CENTS; passing dollars silently under-quotes by 100x.
        body["valuationCents"] = int(round(float(valuation) * 100))
    elif seed.get("valuationCents") is not None:
        body["valuationCents"] = seed["valuationCents"]

    try:
        async with httpx.AsyncClient(timeout=settings.engine_timeout_seconds) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        # Degrade to the valuation-only GET rather than losing the estimate.
        log.warning("engine_fee_estimate_post_failed", city=city.city_id,
                    code=code, error=str(exc))
        params = {"valuationCents": body["valuationCents"]} if "valuationCents" in body else None
        data = await _get_json(url + ("?" + urlencode(params) if params else ""))
    if not isinstance(data, dict):
        return None
    return data


def to_fee_result(data: dict, alternatives: list[dict] | None = None,
                  drivers: list[dict] | None = None,
                  answered: dict | None = None) -> dict:
    """Engine payload -> the shape prompts.fee_user() already renders.

    Amounts are converted cents -> dollars here so the model never does math on
    them, and every line keeps its own name/detail so the answer can itemise.

    PENDING LINES: the engine returns impact-fee lines at zero until their
    drivers are answered -- school impact is $0.00 until it knows the bedroom
    count, then $17,550.00. Printed literally that reads as "this fee is free",
    the same trap as the all-zero table fixed earlier. A zero line is marked
    `pending` whenever fee drivers are still unanswered, so the answer can say
    what it depends on instead of quoting a number.
    """
    def dollars(cents: Any) -> float:
        try:
            return round(int(cents) / 100.0, 2)
        except (TypeError, ValueError):
            return 0.0

    def money(cents: Any) -> str:
        """Pre-formatted currency. The model must never format money itself --
        left to it, exact floats render as "$590.0" and "$1024.12"."""
        return f"${dollars(cents):,.2f}"

    answered = answered or {}
    unanswered = [d for d in (drivers or []) if d.get("code") not in answered]
    unanswered_labels = [d.get("label") or d.get("code") for d in unanswered]

    lines = []
    for ln in data.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        amount = dollars(ln.get("amountCents"))
        item = {
            "label": ln.get("name") or ln.get("code"),
            "amount": amount,
            "amount_display": money(ln.get("amountCents")),
            "kind": ln.get("kind"),          # fee | tax | deposit
            "stage": ln.get("stage"),        # when it is due
            "detail": ln.get("detail"),      # cites the schedule/code section
        }
        if amount == 0 and unanswered:
            item["pending"] = True
            item.pop("amount_display")       # so it can never be printed as $0.00
            # Deliberately NOT naming a specific driver per line. The engine does
            # not tell us which rule card produced which line, and attributing
            # every pending line to every unanswered field produced nonsense --
            # "Park impact fee — depends on 50% of school impact fees were paid
            # at final plat". The unanswered questions are listed once, together,
            # in `pending_inputs`; a vague-but-true pointer beats a precise lie.
        lines.append(item)

    # Staging: what is due now vs at permit issuance. The applicant's first
    # question is almost always "what do I pay to submit?" -- in the demo
    # scenario that is $4,054.26 of a $36,121.31 total.
    def stage_total(match) -> int:
        return sum(int(l.get("amountCents") or 0)
                   for l in (data.get("lines") or [])
                   if isinstance(l, dict) and match(l.get("stage")))

    at_submittal = stage_total(lambda s: s == "application")
    at_issuance = int(data.get("allInCents") or 0) - at_submittal

    return {
        "source": "engine",
        "permit_type": data.get("typeCode"),
        "permit_label": data.get("typeName"),
        "valuation": (dollars(data.get("valuationCents"))
                      if data.get("valuationCents") is not None else None),
        "line_items": lines,
        "fee_subtotal": dollars(data.get("feeSubtotalCents")),
        "taxes": dollars(data.get("taxesCents")),
        "deposits": dollars(data.get("depositsCents")),
        "total": dollars(data.get("allInCents")),
        "total_display": money(data.get("allInCents")),
        "due_at_submittal_display": money(at_submittal),
        "due_at_issuance_display": money(at_issuance),
        # Non-empty => the total is PARTIAL and these questions would change it.
        "pending_inputs": [
            {"code": d.get("code"), "label": d.get("label"),
             "description": d.get("description"), "options": d.get("enumValues")}
            for d in unanswered
        ],
        "answers_used": answered,
        "alternatives": [label_for(a) for a in (alternatives or [])],
    }
