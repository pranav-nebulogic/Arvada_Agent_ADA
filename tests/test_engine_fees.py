"""
Unit tests for agent/engine_fees.py -- catalog matching and payload conversion.

Pure logic only: no network. The fixture mirrors the SHAPE of Woodinville's real
`/api/public/catalog/{tenant}/application-types` response (code, name,
citizenLabel, citizenDescription, module, sortOrder).
"""
from __future__ import annotations

import pytest

from agent import engine_fees as ef

CATALOG = [
    {"code": "wv_bld_res_new", "name": "Residential New",
     "citizenLabel": "Residential New Construction", "module": "permit", "sortOrder": 10,
     "citizenDescription": "Build a new single-family home or duplex. Building permit only."},
    {"code": "wv_bld_res_new_combo", "name": "Residential New - Combo",
     "citizenLabel": "Residential New Construction (Combo)", "module": "permit", "sortOrder": 11,
     "citizenDescription": "Build a new single-family home or duplex with mechanical and plumbing."},
    {"code": "wv_bld_res_addition", "name": "Residential Addition",
     "citizenLabel": "Residential Addition", "module": "permit", "sortOrder": 12,
     "citizenDescription": "Add floor area to an existing home."},
    {"code": "wv_bld_res_deck", "name": "Residential Deck",
     "citizenLabel": "Residential Deck", "module": "permit", "sortOrder": 20,
     "citizenDescription": "Build or replace a deck."},
    {"code": "wv_bld_com_deck", "name": "Commercial Deck",
     "citizenLabel": "Commercial Deck / Patio Structure", "module": "permit", "sortOrder": 60,
     "citizenDescription": "Build a commercial deck or patio structure."},
    {"code": "wv_appeals", "name": "Appeal / Interpretation",
     "citizenLabel": "Appeal / Interpretation", "module": "planning", "sortOrder": 90,
     "citizenDescription": "Appeal a Type 1 or 2 decision, or request a director interpretation."},
    {"code": "wv_excluded", "name": "Residential Deck Excluded",
     "citizenLabel": "Excluded Deck", "module": "permit", "sortOrder": 5,
     "feeEstimatorExclude": True, "citizenDescription": "Build or replace a deck."},
]


def _labels(rows):
    return [ef.label_for(r) for r in rows]


class TestTokens:
    def test_bare_numbers_are_dropped(self):
        # "2 bedrooms" once matched an Appeal type via "a Type 1 or 2 decision".
        assert "2" not in ef._tokens("2 bedrooms")
        assert "bedrooms" in ef._tokens("2 bedrooms")

    def test_new_is_signal_not_stopword(self):
        # "new" separates Residential New from Residential Addition.
        assert "new" in ef._tokens("new house")

    def test_everyday_words_map_to_catalog_vocabulary(self):
        assert "residential" in ef._tokens("house")
        assert "commercial" in ef._tokens("my shop")


class TestMatching:
    def test_numeric_noise_does_not_match_appeals(self):
        m = ef.match_types("new house 5000 sq ft 2 bedrooms", CATALOG)
        assert "Appeal / Interpretation" not in _labels(m)
        assert _labels(m)[0] == "Residential New Construction"

    def test_description_only_match_is_rejected(self):
        # "building" appears in descriptions but names no type. Arvada showed why
        # this must not price: its contractor LICENCE classes are described as
        # "Construct, alter, or repair any building or structure", so a
        # description-only match answered a remodel fee question with licences.
        # Returning [] makes the caller fall back to the local fee table.
        assert ef.match_types("building permit", CATALOG) == []

    def test_description_still_helps_once_the_name_matches(self):
        # "home" hits the Residential* names via the synonym map; the description
        # then separates New from Addition.
        assert _labels(ef.match_types("new home", CATALOG))[0] == "Residential New Construction"

    def test_fee_estimator_exclusions_are_skipped(self):
        assert "Excluded Deck" not in _labels(ef.match_types("deck", CATALOG))

    def test_no_match_returns_empty_rather_than_guessing(self):
        # Woodinville genuinely has no fence type; a wrong match is worse than none.
        assert ef.match_types("fence", CATALOG) == []


class TestAudience:
    def test_absent_audience_assumes_residential_and_reports_it(self):
        kept, assumed = ef.narrow_by_audience("deck", ef.match_types("deck", CATALOG))
        assert _labels(kept) == ["Residential Deck"]
        assert assumed is True

    def test_stated_audience_is_honoured_and_not_flagged(self):
        kept, assumed = ef.narrow_by_audience(
            "commercial deck", ef.match_types("commercial deck", CATALOG))
        assert _labels(kept)[0] == "Commercial Deck / Patio Structure"
        assert assumed is False


class TestAmbiguity:
    def test_near_tie_is_ambiguous(self):
        m = ef.match_types("new home", CATALOG)
        m, _ = ef.narrow_by_audience("new home", m)
        assert ef.is_ambiguous(m) is True     # Combo vs non-Combo is a real question

    def test_single_match_is_never_ambiguous(self):
        assert ef.is_ambiguous(ef.match_types("appeal", CATALOG)) is False


class TestPayloadConversion:
    PAYLOAD = {
        "typeCode": "wv_bld_res_new", "typeName": "Residential New",
        "valuationCents": 82500000, "feeSubtotalCents": 1464797,
        "taxesCents": 0, "depositsCents": 0, "allInCents": 1464797,
        "lines": [
            {"code": "wv_fee_building", "name": "Building Permit (valuation)",
             "amountCents": 843500, "kind": "fee", "stage": "building_permit",
             "detail": "By total construction valuation."},
            {"code": "wv_fee_plan_review", "name": "Building Plan Review (65%)",
             "amountCents": 548275, "kind": "fee", "stage": "application",
             "detail": "65% of the building permit fee."},
        ],
    }

    def test_cents_become_dollars(self):
        r = ef.to_fee_result(self.PAYLOAD)
        assert r["total"] == 14647.97
        assert r["valuation"] == 825000.0
        assert r["line_items"][0]["amount"] == 8435.0

    def test_every_line_is_kept(self):
        # The reported bug was chat showing ONLY the permit fee.
        r = ef.to_fee_result(self.PAYLOAD)
        assert len(r["line_items"]) == 2
        assert r["total"] > r["line_items"][0]["amount"]

    def test_alternatives_render_as_portal_labels(self):
        r = ef.to_fee_result(self.PAYLOAD, alternatives=[CATALOG[1]])
        assert r["alternatives"] == ["Residential New Construction (Combo)"]

    def test_malformed_amounts_do_not_crash(self):
        bad = {**self.PAYLOAD, "lines": [{"name": "x", "amountCents": None}]}
        assert ef.to_fee_result(bad)["line_items"][0]["amount"] == 0.0


class TestModuleExclusions:
    """Regression: Arvada answered a remodel FEE question with contractor licences."""

    CONTRACTOR = {
        "code": "i_b", "name": "Builder's Unlimited (I-B)",
        "citizenLabel": "Builder's Unlimited (I-B)", "module": "contractor",
        "sortOrder": 5,
        "citizenDescription": "Construct, alter, or repair any building or structure.",
    }

    def test_contractor_and_inspection_modules_are_not_priceable(self):
        assert "contractor" in ef._SKIP_MODULES
        assert "inspections" in ef._SKIP_MODULES

    def test_licence_class_never_answers_a_project_fee_question(self):
        rows = [r for r in [*CATALOG, self.CONTRACTOR]
                if r.get("module") not in ef._SKIP_MODULES]
        assert "Builder's Unlimited (I-B)" not in _labels(
            ef.match_types("building permit fee for a $25,000 remodel", rows))


class TestMoneyFormatting:
    def test_amounts_are_preformatted_for_display(self):
        # Left to format floats itself the model emitted "$590.0" / "$1024.12".
        r = ef.to_fee_result(TestPayloadConversion.PAYLOAD)
        assert r["total_display"] == "$14,647.97"
        assert r["line_items"][0]["amount_display"] == "$8,435.00"

    def test_display_and_numeric_agree(self):
        r = ef.to_fee_result(TestPayloadConversion.PAYLOAD)
        assert r["total_display"] == f"${r['total']:,.2f}"


class TestUnresolvedPermitTypeGrounding:
    """Regression: "How much does a Tesla Cybertruck permit cost?" got a confident
    menu of plausible permits instead of a decline.

    With no permit_type from the classifier AND no tenant-catalog match, there is
    no evidence the query names a real permit. The old code returned
    needs_permit_type, which graph._route_fee sent straight to `generate` --
    bypassing retrieval and therefore the grounding rules. It must route to
    retrieval instead so the answer has to be grounded, and can say it isn't there.
    """

    @pytest.mark.asyncio
    async def test_no_evidence_routes_to_retrieval(self, monkeypatch):
        from agent.nodes import fee_engine as fe
        from agent.state import AgentState

        async def no_catalog(_state, _valuation):
            return None                      # engine/catalog produced nothing

        monkeypatch.setattr(fe, "_try_engine", no_catalog)
        st = AgentState(query="How much does a Tesla Cybertruck permit cost?",
                        city_id="arvada-co", session_id="t", entities={})
        out = await fe.fee_engine(st)
        fr = out["fee_result"]
        assert fr.get("no_schedule") is True, "must fall through to grounded retrieval"
        assert fr.get("unresolved_permit_type") is True
        assert not fr.get("needs_permit_type"), \
            "asking 'which permit type?' legitimises a permit that does not exist"

    def test_route_fee_sends_it_to_retrieve(self):
        from agent.graph import _route_fee
        from agent.state import AgentState
        st = AgentState(query="q", city_id="arvada-co", session_id="t")
        st.fee_result = {"no_schedule": True, "unresolved_permit_type": True}
        assert _route_fee(st) == "retrieve"


class TestResolveApply:
    """Regression: a Woodinville user asking to remodel a kitchen was offered
    "Residential Interior" -- an ARVADA type. Woodinville calls it
    "Residential Remodel / Tenant Improvement (TI)", so the CTA pointed at
    /permits/apply/residential_interior, which does not exist there.
    """

    WV = [
        {"code": "wv_bld_res_remodel_ti", "name": "Residential Remodel / TI",
         "citizenLabel": "Residential Remodel / Tenant Improvement (TI)",
         "module": "permit", "sortOrder": 30,
         "citizenDescription": "Remodel an existing home: kitchen, bathroom, interior work."},
        {"code": "wv_bld_com_ti", "name": "Commercial Tenant Improvement",
         "citizenLabel": "Commercial Tenant Improvement (TI)",
         "module": "permit", "sortOrder": 70,
         "citizenDescription": "Remodel a commercial space."},
    ]

    @pytest.mark.asyncio
    async def test_uses_this_citys_label_and_code(self, monkeypatch):
        async def catalog(_c):
            return self.WV
        monkeypatch.setattr(ef, "fetch_catalog", catalog)
        monkeypatch.setattr(ef, "get_city",
                            lambda c: type("C", (), {"portal_base_url": "https://woodinville.example"})())
        got = await ef.resolve_apply("woodinville-wa", "i want to remodel my kitchen")
        assert got["label"] == "Residential Remodel / Tenant Improvement (TI)"
        assert got["code"] == "wv_bld_res_remodel_ti"
        assert got["url"] == "/permits/apply/wv_bld_res_remodel_ti"
        assert "residential_interior" not in got["url"], "the Arvada code must not appear"

    @pytest.mark.asyncio
    async def test_url_is_relative_never_absolute(self, monkeypatch):
        """adaTypes.ts: `url` is a RELATIVE path; consumers call navigate(apply.url).

        An absolute URL is treated by react-router as a route, which opened a
        blank tab titled "/https:/woodinville.smart-l...". The first version of
        this suite asserted the ABSOLUTE form, so the tests passed on the bug --
        this assertion is the one that actually protects the contract.
        """
        async def catalog(_c):
            return self.WV
        monkeypatch.setattr(ef, "fetch_catalog", catalog)
        monkeypatch.setattr(ef, "get_city",
                            lambda c: type("C", (), {"portal_base_url": "https://woodinville.example"})())
        for surface in ("citizen", "agent"):
            got = await ef.resolve_apply("woodinville-wa", "remodel my kitchen", surface=surface)
            assert got["url"].startswith("/"), got["url"]
            assert "://" not in got["url"], got["url"]
            assert "woodinville.example" not in got["url"], got["url"]

    @pytest.mark.asyncio
    async def test_no_catalog_means_no_button(self, monkeypatch):
        async def empty(_c):
            return []
        monkeypatch.setattr(ef, "fetch_catalog", empty)
        # Falling back to the static Arvada registry is what caused the bug.
        assert await ef.resolve_apply("woodinville-wa", "remodel my kitchen") is None

    @pytest.mark.asyncio
    async def test_ambiguous_variants_still_get_a_button(self, monkeypatch):
        """Combo vs non-Combo is a near-tie, but they are the same work and the
        UI offers "Choose a different service type" right next to the button.
        No button at all was the over-correction after the Arvada-label bug."""
        async def catalog(_c):
            return [*self.WV,
                    {"code": "wv_bld_res_remodel_ti_combo",
                     "name": "Residential Remodel / TI - Combo",
                     "citizenLabel": "Residential Remodel / TI (Combo)",
                     "module": "permit", "sortOrder": 31,
                     "citizenDescription": "Remodel an existing home with mechanical and plumbing."}]
        monkeypatch.setattr(ef, "fetch_catalog", catalog)
        monkeypatch.setattr(ef, "get_city",
                            lambda c: type("C", (), {"portal_base_url": ""})())
        got = await ef.resolve_apply("woodinville-wa", "i want to remodel my kitchen")
        assert got is not None, "an ambiguous variant pair must still offer a CTA"
        assert got["code"].startswith("wv_bld_res_remodel_ti")

    @pytest.mark.asyncio
    async def test_no_match_means_no_button(self, monkeypatch):
        async def catalog(_c):
            return self.WV
        monkeypatch.setattr(ef, "fetch_catalog", catalog)
        assert await ef.resolve_apply("woodinville-wa", "how do I adopt a dog") is None

    @pytest.mark.asyncio
    async def test_staff_go_to_agent_intake(self, monkeypatch):
        async def catalog(_c):
            return self.WV
        monkeypatch.setattr(ef, "fetch_catalog", catalog)
        monkeypatch.setattr(ef, "get_city",
                            lambda c: type("C", (), {"portal_base_url": "https://woodinville.example"})())
        got = await ef.resolve_apply("woodinville-wa", "remodel a kitchen", surface="agent")
        assert got["url"].endswith("/agent/intake")


class TestPendingLinesAndStaging:
    """Impact fees come back as $0.00 until their drivers are answered.

    Printed literally that reads as "this fee is free" -- and on a 4-bedroom
    detached house the school line is $17,550, so implying zero is the worst
    error available. A zero line with unanswered drivers is marked `pending` and
    carries what it depends on, with no amount_display to print.
    """

    DRIVERS = [
        {"code": "bedrooms_per_unit", "label": "Bedrooms per Unit", "dataType": "int"},
        {"code": "housing_type", "label": "Housing Type", "dataType": "enum",
         "enumValues": ["Detached single-family", "Duplex / townhome / multiplex"]},
    ]
    PAYLOAD = {
        "typeCode": "wv_bld_res_new_combo", "typeName": "Residential New - Combo",
        "valuationCents": 50000000, "allInCents": 1077076,
        "lines": [
            {"name": "Building Permit (valuation)", "amountCents": 551000,
             "kind": "fee", "stage": "building_permit"},
            {"name": "Building Plan Review (65%)", "amountCents": 358150,
             "kind": "fee", "stage": "application"},
            {"name": "Fire Plan Review (13.2%)", "amountCents": 47276,
             "kind": "fee", "stage": "application"},
            {"name": "School impact fee", "amountCents": 0,
             "kind": "fee", "stage": "building_permit"},
        ],
    }

    def test_zero_line_with_unanswered_driver_is_pending(self):
        r = ef.to_fee_result(self.PAYLOAD, drivers=self.DRIVERS, answered={})
        school = [l for l in r["line_items"] if l["label"] == "School impact fee"][0]
        assert school["pending"] is True
        assert "amount_display" not in school, "a pending fee must not be printable as $0.00"
        # No per-line attribution: the engine does not say which rule card made
        # which line, and guessing produced "Park impact fee - depends on 50% of
        # school impact fees were paid at final plat".
        assert "depends_on" not in school

    def test_priced_lines_are_never_pending(self):
        r = ef.to_fee_result(self.PAYLOAD, drivers=self.DRIVERS, answered={})
        priced = [l for l in r["line_items"] if l["label"].startswith("Building Permit")][0]
        assert not priced.get("pending")
        assert priced["amount_display"] == "$5,510.00"

    def test_all_drivers_answered_means_nothing_pending(self):
        r = ef.to_fee_result(self.PAYLOAD, drivers=self.DRIVERS,
                             answered={"bedrooms_per_unit": "4",
                                       "housing_type": "Detached single-family"})
        assert not any(l.get("pending") for l in r["line_items"])
        assert r["pending_inputs"] == []

    def test_pending_inputs_carry_the_question(self):
        r = ef.to_fee_result(self.PAYLOAD, drivers=self.DRIVERS, answered={})
        codes = {p["code"] for p in r["pending_inputs"]}
        assert codes == {"bedrooms_per_unit", "housing_type"}
        ht = [p for p in r["pending_inputs"] if p["code"] == "housing_type"][0]
        assert ht["options"] == ["Detached single-family", "Duplex / townhome / multiplex"]

    def test_staging_splits_submittal_from_issuance(self):
        # Plan reviews are due at submittal; the permit fee at issuance.
        r = ef.to_fee_result(self.PAYLOAD, drivers=[], answered={})
        assert r["due_at_submittal_display"] == "$4,054.26"
        assert r["due_at_issuance_display"] == "$6,716.50"

    def test_no_drivers_means_no_pending_marks(self):
        # Types with no fee-driving questions must render exactly as before.
        r = ef.to_fee_result(self.PAYLOAD, drivers=[], answered={})
        assert not any(l.get("pending") for l in r["line_items"])


class TestPredicateWalk:
    def test_collects_nested_answer_prefixed_fields(self):
        pred = {"inline": {"items": [
            {"check": {"field": "answer.housing_type"}},
            {"inline": {"items": [{"check": {"field": "bedrooms_per_unit"}}]}},
        ]}}
        out: set = set()
        ef._collect_predicate_fields(pred, out)
        assert out == {"housing_type", "bedrooms_per_unit"}

    def test_malformed_predicate_does_not_raise(self):
        out: set = set()
        for bad in (None, "x", 3, {}, {"inline": {}}, {"items": "no"}):
            ef._collect_predicate_fields(bad, out)
        assert out == set()


class TestValuationSurvivesTheTurn:
    """A follow-up rarely restates the dollar figure.

    When it didn't, valuation arrived as None and fee_estimate fell through to
    the type's preEstimate SEED -- repricing a $500,000 project at $569,445 and
    moving every valuation-derived line without saying so.
    """

    def test_prior_turn_valuation_is_reused(self):
        from agent.nodes.fee_engine import _extract_valuation
        from agent.state import AgentState
        st = AgentState(query="4 bedrooms, detached", city_id="woodinville-wa",
                        session_id="t", entities={}, fee_valuation=500000.0)
        assert _extract_valuation(st) == 500000.0

    def test_this_turn_wins_over_the_remembered_one(self):
        from agent.nodes.fee_engine import _extract_valuation
        from agent.state import AgentState
        st = AgentState(query="make it $700,000", city_id="woodinville-wa",
                        session_id="t", entities={"fee_valuation": 700000.0},
                        fee_valuation=500000.0)
        assert _extract_valuation(st) == 700000.0

    def test_absent_everywhere_stays_none(self):
        from agent.nodes.fee_engine import _extract_valuation
        from agent.state import AgentState
        st = AgentState(query="deck permit", city_id="woodinville-wa",
                        session_id="t", entities={})
        assert _extract_valuation(st) is None


class TestQuantityFieldDiscovery:
    """Some drivers exist only in the ESTIMATE response, not in any rule card.

    Woodinville's park, school and transportation impact lines each declare
    `quantityField: new_units`. Miss it and all three price at $0.00 no matter
    what else is answered -- the estimate looked complete at $10,770.76 when the
    real figure was $36,121.31. The portal recomputes the same way.
    """

    def test_quantity_fields_are_read_off_the_lines(self):
        data = {"lines": [
            {"name": "Building Permit", "quantityField": None},
            {"name": "Park impact fee", "quantityField": "new_units"},
            {"name": "School impact fee", "quantityField": "new_units"},
            {"name": "Mechanical fixtures", "quantityField": "mechanical_fixture_count"},
        ]}
        assert ef.quantity_fields(data) == {"new_units", "mechanical_fixture_count"}

    def test_no_quantity_fields_is_empty_not_an_error(self):
        assert ef.quantity_fields({"lines": [{"name": "x"}]}) == set()
        assert ef.quantity_fields({}) == set()
        assert ef.quantity_fields({"lines": None}) == set()

    def test_malformed_lines_are_skipped(self):
        assert ef.quantity_fields({"lines": ["nope", 7, None]}) == set()


class TestAudienceNeverOverrulesAWinner:
    """Audience narrowing chooses among near-equals; it must not promote a
    weaker match over a decisive one.

    "How much does a tree removal permit cost without construction?" scored
    Tree Removal Permit 8.0 against 3.99 for the next candidate. Tree Removal
    carries no audience word, so narrowing dropped it and kept Residential New
    Construction -- KAI then quoted $17,371.31 for a house permit in answer to a
    $43 tree question.
    """

    ROWS = [
        {"code": "wv_tree_removal", "name": "Tree Removal Permit",
         "citizenLabel": "Tree Removal Permit", "module": "permit", "sortOrder": 40,
         "citizenDescription": "Permit to remove a tree, with or without construction."},
        {"code": "wv_bld_res_new", "name": "Residential New",
         "citizenLabel": "Residential New Construction", "module": "permit", "sortOrder": 10,
         "citizenDescription": "Build a new single-family home. Construction permit."},
    ]

    def test_decisive_winner_survives_narrowing(self):
        q = "How much does a tree removal permit cost without construction?"
        m = ef.match_types(q, self.ROWS)
        assert ef.label_for(m[0]) == "Tree Removal Permit"
        kept, assumed = ef.narrow_by_audience(q, m)
        assert ef.label_for(kept[0]) == "Tree Removal Permit"
        assert assumed is False, "nothing was assumed -- the winner had no audience"

    def test_narrowing_still_works_among_equals(self):
        rows = [
            {"code": "res_deck", "name": "Residential Deck", "citizenLabel": "Residential Deck",
             "module": "permit", "sortOrder": 1, "citizenDescription": "Build a deck."},
            {"code": "com_deck", "name": "Commercial Deck", "citizenLabel": "Commercial Deck",
             "module": "permit", "sortOrder": 2, "citizenDescription": "Build a deck."},
        ]
        m = ef.match_types("deck", rows)
        kept, assumed = ef.narrow_by_audience("deck", m)
        assert [ef.label_for(k) for k in kept] == ["Residential Deck"]
        assert assumed is True
