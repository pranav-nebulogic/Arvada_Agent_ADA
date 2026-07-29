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
        assert got["url"] == "https://woodinville.example/permits/apply/wv_bld_res_remodel_ti"
        assert "residential_interior" not in got["url"], "the Arvada code must not appear"

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
