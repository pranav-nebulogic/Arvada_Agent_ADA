"""
fee_engine node
==============
Deterministic fee calculation (no LLM math). Reads the permit_type + valuation
from extracted entities, looks up fee_schedules rows, and computes an itemised
estimate via agent/fee_tables.py. The downstream `generate` node turns the
numbers into a user-facing answer with the estimate disclaimer.
"""
from __future__ import annotations

import db
from agent import engine_fees, fee_answers, fee_tables
from agent.obs import log
from agent.state import AgentState


async def _fee_sources(state: AgentState, permit_type: str) -> list[dict]:
    """Attach the KB document(s) behind the fee numbers so the answer is sourced."""
    if not state.query_embedding:
        return []
    try:
        chunks = await db.hybrid_search(
            state.city_id,
            state.standalone_query or state.query,
            state.query_embedding,
            permit_type=permit_type,
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 — sources are best-effort, never block the estimate
        log.warning("fee_sources_lookup_failed", error=str(exc), permit_type=permit_type)
        return []

    seen: set = set()
    sources: list[dict] = []
    for c in chunks:
        key = (c.get("article_title"), c.get("source_url"))
        if key in seen or not c.get("source_url"):
            continue
        seen.add(key)
        sources.append(
            {
                "n": len(sources) + 1,
                "article_id": c.get("article_id"),
                "title": c.get("article_title") or "City of Arvada Fee Schedule",
                "url": c.get("source_url"),
                "effective_date": str(c.get("effective_date")) if c.get("effective_date") else None,
                "section_type": c.get("section_type"),
                "permit_type": c.get("permit_type"),
            }
        )
        if len(sources) >= 2:
            break
    return sources


def _extract_valuation(state: AgentState) -> float | None:
    """This turn's valuation, else the one the user gave on an EARLIER turn.

    A follow-up ("4 bedrooms, detached") rarely restates the dollar figure. When
    it didn't, we used to hand the engine None, which fell through to the type's
    preEstimate SEED -- so a $500,000 project quietly repriced at $569,445 and
    every valuation-derived line moved. A stated valuation outlives the turn.
    """
    ent = state.entities or {}
    for k in ("fee_valuation", "valuation", "project_valuation"):
        v = ent.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return state.fee_valuation


async def _try_engine(state: AgentState, valuation: float | None) -> dict | None:
    """Price from the ENGINE's tenant-configured schedule when we can.

    The local `fee_schedules` table only ever held the headline permit fee --
    Woodinville has ONE row -- so it under-quoted a real project by ~40% versus
    the portal (plan review, fire review and surcharges were simply missing).
    The engine returns the same itemised, all-in estimate the citizen sees.

    Returns None on ANY miss so the caller falls back to the local table rather
    than losing the answer entirely.
    """
    text = " ".join(filter(None, [
        state.standalone_query or state.query,
        (state.entities or {}).get("permit_type") or state.permit_type or "",
    ]))
    rows = await engine_fees.fetch_catalog(state.city_id)
    if not rows:
        return None
    matches = engine_fees.match_types(text, rows)
    if not matches:
        return None
    # Residential-vs-commercial is an assumption, not a question worth asking on
    # a citizen portal; only the work type is worth clarifying.
    matches, assumed_residential = engine_fees.narrow_by_audience(text, matches)

    # Genuinely ambiguous phrasing ("building permit" -- residential? commercial?)
    # scores several types alike. Pricing one anyway would quote the wrong permit
    # with total confidence, so return the candidates and let the answer ask.
    if engine_fees.is_ambiguous(matches):
        return {
            "source": "engine",
            "needs_permit_type": True,
            "candidates": [engine_fees.label_for(m) for m in matches],
            "disclaimer": fee_tables.disclaimer(state.city_id),
        }

    best = matches[0]
    code = best.get("code")

    # What moves this type's number is tenant config (rule cards), so ask the
    # engine rather than hardcoding. Then read back anything the user already
    # said -- carrying prior turns forward so "actually 4 bedrooms" refines the
    # estimate instead of starting over.
    drivers = await engine_fees.fee_drivers(state.city_id, code)
    answers = dict(state.fee_answers or {})
    defaulted: list[str] = []
    if drivers:
        answers.update(await fee_answers.extract(drivers, text))
        # Fall back to each field's configured defaultValue, exactly as the
        # portal's calculator does, and report which ones we defaulted. Without
        # this, a driver the extractor missed stayed "pending" forever -- the
        # answer once said "You said school fees were not prepaid at plat" while
        # still listing that very question as outstanding. Fields with NO
        # default (e.g. bedrooms per unit) remain genuinely unanswered.
        answers, defaulted = fee_answers.apply_defaults(drivers, answers)

    data = await engine_fees.fee_estimate(state.city_id, code, valuation, answers)
    if not data:
        return None

    # SECOND PASS. Some drivers are only discoverable from the estimate itself:
    # each line may name the field supplying its quantity, and those appear in no
    # rule-card predicate. Woodinville's park, school and transportation impact
    # lines all declare `quantityField: new_units` -- miss it and all three price
    # at $0.00 however much else is answered. The portal recomputes the same way.
    extra_codes = engine_fees.quantity_fields(data) - {d.get("code") for d in drivers}
    if extra_codes:
        extra = await engine_fees.fields_for(state.city_id, code, extra_codes)
        if extra:
            drivers = drivers + extra
            before = dict(answers)
            answers.update(await fee_answers.extract(extra, text))
            answers, more_defaulted = fee_answers.apply_defaults(extra, answers)
            defaulted += more_defaulted
            if answers != before:
                data = await engine_fees.fee_estimate(
                    state.city_id, code, valuation, answers) or data

    result = engine_fees.to_fee_result(
        data, alternatives=matches[1:], drivers=drivers, answered=answers)
    result["assumed_residential"] = assumed_residential
    if defaulted:
        result["defaulted_inputs"] = defaulted   # disclosed in the answer
    result["_fee_answers"] = answers      # persisted onto state by the caller
    # A valuation-driven type priced at zero means the user never gave a
    # valuation -- ask for it rather than quoting $0.00 as if it were the answer.
    if valuation is None and not result.get("total"):
        result["needs_valuation"] = True
        # Drop the all-zero amounts: rendered as a table they read as "this permit
        # costs nothing". Keep the component NAMES so we can still say what the
        # fee is made of while asking for the valuation.
        result["fee_components"] = [
            li["label"] for li in (result.get("line_items") or []) if li.get("label")
        ]
        for k in ("line_items", "fee_subtotal", "taxes", "deposits", "total"):
            result.pop(k, None)
    result["disclaimer"] = fee_tables.disclaimer(state.city_id)
    log.info("fee_from_engine", city=state.city_id, code=best.get("code"),
             lines=len(result.get("line_items") or []), total=result.get("total"))
    return result


async def fee_engine(state: AgentState) -> dict:
    raw_pt = (state.entities or {}).get("permit_type") or state.permit_type
    permit_type = fee_tables.normalize_permit_type(raw_pt)
    valuation = _extract_valuation(state)

    engine_result = await _try_engine(state, valuation)
    if engine_result:
        engine_result["sources"] = await _fee_sources(
            state, permit_type or engine_result.get("permit_type") or "")
        collected = engine_result.pop("_fee_answers", None)
        out = {
            "fee_result": engine_result,
            "entities": {**(state.entities or {}),
                         "permit_type": permit_type or raw_pt},
        }
        if collected is not None:
            out["fee_answers"] = collected
        if valuation is not None:
            out["fee_valuation"] = valuation      # survives into the next turn
        return out

    if not permit_type:
        # No permit type from the classifier AND no tenant-catalog match above
        # (_try_engine returned None) means we have NO evidence this names a real
        # permit. Asking "which permit type?" here legitimises the premise: it
        # routed straight to generate, skipping retrieval and therefore the
        # grounding rules, so "How much does a Tesla Cybertruck permit cost?"
        # came back with a confident menu of plausible permits instead of saying
        # no such thing exists. Route to retrieval so the answer has to be
        # grounded in the KB -- and can honestly say it isn't there.
        log.info("fee_no_permit_type_evidence", city=state.city_id,
                 query=(state.standalone_query or state.query)[:120])
        return {"fee_result": {"permit_type": None, "no_schedule": True,
                               "unresolved_permit_type": True}}

    fee_rows = await db.fetch_fee_schedule(state.city_id, permit_type)
    if not fee_rows:
        # No deterministic schedule -> let retrieval/generation answer from KB.
        return {"fee_result": {"permit_type": permit_type, "no_schedule": True}}

    result = fee_tables.compute_fees(permit_type, fee_rows, valuation=valuation, city_id=state.city_id)
    result["sources"] = await _fee_sources(state, permit_type)
    # Proactive "what changed" enrichment: if this permit's fees changed recently,
    # the fee answer can flag it instead of quoting a number with no context.
    try:
        changes = await db.fetch_recent_changes(
            state.city_id, permit_type=permit_type, change_type="fee", limit=3
        )
        if changes:
            result["recent_changes"] = changes
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort, never block the estimate
        log.warning("fee_change_enrichment_failed", error=str(exc), permit_type=permit_type)
    return {"fee_result": result, "entities": {**(state.entities or {}), "permit_type": permit_type}}
