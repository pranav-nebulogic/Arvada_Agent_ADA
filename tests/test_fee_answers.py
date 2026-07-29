"""
Tests for agent/fee_answers.py.

The LLM proposes; `validate()` disposes. These pin the disposal half, because
that is what stops a hallucinated value from silently moving a fee: on a
4-bedroom detached house the school impact line alone is $17,550, so a wrong
`housing_type` is thousands of dollars stated with total confidence. A DROPPED
value just gets asked for again.
"""
from __future__ import annotations

from agent import fee_answers as fa

DRIVERS = [
    {"code": "housing_type", "dataType": "enum", "label": "Housing Type",
     "enumValues": ["Detached single-family", "Duplex / townhome / multiplex"]},
    {"code": "bedrooms_per_unit", "dataType": "int", "label": "Bedrooms per Unit"},
    {"code": "adu_status", "dataType": "enum", "label": "Is this an ADU?",
     "enumValues": ["No", "Yes"]},
    {"code": "school_prepaid_at_plat", "dataType": "boolean", "label": "Prepaid at plat"},
    {"code": "prior_site_pm_trips", "dataType": "numeric", "label": "Prior PM trips"},
]


class TestEnums:
    def test_exact_value_kept(self):
        assert fa.validate(DRIVERS, {"housing_type": "Detached single-family"}) == {
            "housing_type": "Detached single-family"}

    def test_case_insensitive_match(self):
        assert fa.validate(DRIVERS, {"adu_status": "yes"}) == {"adu_status": "Yes"}

    def test_partial_word_resolves_when_unambiguous(self):
        # People type "detached", not "Detached single-family".
        assert fa.validate(DRIVERS, {"housing_type": "detached"}) == {
            "housing_type": "Detached single-family"}

    def test_invented_value_is_dropped_not_guessed(self):
        # "condo" is not one of this field's values. Dropping it means we ask;
        # guessing it would move the school impact fee by thousands.
        assert fa.validate(DRIVERS, {"housing_type": "condo"}) == {}

    def test_ambiguous_partial_is_dropped(self):
        drivers = [{"code": "x", "dataType": "enum", "enumValues": ["Red brick", "Red tile"]}]
        assert fa.validate(drivers, {"x": "red"}) == {}


class TestScalars:
    def test_int_extracted_from_prose(self):
        assert fa.validate(DRIVERS, {"bedrooms_per_unit": "4 bedrooms"}) == {
            "bedrooms_per_unit": "4"}

    def test_thousands_separators_survive(self):
        assert fa.validate(DRIVERS, {"bedrooms_per_unit": "1,200"}) == {
            "bedrooms_per_unit": "1200"}

    def test_numeric_keeps_decimals(self):
        assert fa.validate(DRIVERS, {"prior_site_pm_trips": "1.23"}) == {
            "prior_site_pm_trips": "1.23"}

    def test_non_numeric_int_is_dropped(self):
        assert fa.validate(DRIVERS, {"bedrooms_per_unit": "a few"}) == {}

    def test_booleans_normalise(self):
        assert fa.validate(DRIVERS, {"school_prepaid_at_plat": "no"}) == {
            "school_prepaid_at_plat": "false"}
        assert fa.validate(DRIVERS, {"school_prepaid_at_plat": "TRUE"}) == {
            "school_prepaid_at_plat": "true"}

    def test_unparseable_boolean_is_dropped(self):
        assert fa.validate(DRIVERS, {"school_prepaid_at_plat": "maybe"}) == {}


class TestUnknownFields:
    def test_field_that_does_not_drive_this_type_is_dropped(self):
        # The model may echo a field from another type; only this type's drivers count.
        assert fa.validate(DRIVERS, {"meter_size": "1 inch"}) == {}

    def test_empty_and_none_are_dropped(self):
        assert fa.validate(DRIVERS, {"bedrooms_per_unit": "", "adu_status": None}) == {}

    def test_no_drivers_means_nothing_is_accepted(self):
        assert fa.validate([], {"housing_type": "Detached single-family"}) == {}


class TestRealisticReply:
    def test_a_whole_sentence_of_answers(self):
        raw = {
            "housing_type": "detached",
            "bedrooms_per_unit": "4",
            "adu_status": "No",
            "school_prepaid_at_plat": "no",
            "made_up_field": "42",
            "prior_site_pm_trips": "not sure",
        }
        assert fa.validate(DRIVERS, raw) == {
            "housing_type": "Detached single-family",
            "bedrooms_per_unit": "4",
            "adu_status": "No",
            "school_prepaid_at_plat": "false",
        }


class TestApplyDefaults:
    """The portal's calculator opens with each field's configured default.

    Mirroring that stops a driver the extractor missed from staying pending
    forever -- one answer said "You said school fees were not prepaid at plat"
    while still listing that same question as outstanding.
    """

    WITH_DEFAULTS = [
        {"code": "housing_type", "dataType": "enum", "label": "Housing Type",
         "enumValues": ["Detached single-family", "Duplex / townhome / multiplex"],
         "defaultValue": "Detached single-family"},
        {"code": "school_prepaid_at_plat", "dataType": "boolean",
         "label": "Prepaid at plat", "defaultValue": "No"},
        {"code": "bedrooms_per_unit", "dataType": "int", "label": "Bedrooms per Unit"},
    ]

    def test_missing_values_take_their_default(self):
        out, defaulted = fa.apply_defaults(self.WITH_DEFAULTS, {})
        assert out["housing_type"] == "Detached single-family"
        assert out["school_prepaid_at_plat"] == "false"
        assert "Housing Type" in defaulted and "Prepaid at plat" in defaulted

    def test_a_field_without_a_default_stays_unanswered(self):
        # bedrooms drives the school tier and must be ASKED, never invented.
        out, _ = fa.apply_defaults(self.WITH_DEFAULTS, {})
        assert "bedrooms_per_unit" not in out

    def test_user_answers_are_never_overwritten(self):
        out, defaulted = fa.apply_defaults(
            self.WITH_DEFAULTS, {"housing_type": "Duplex / townhome / multiplex"})
        assert out["housing_type"] == "Duplex / townhome / multiplex"
        assert "Housing Type" not in defaulted

    def test_defaults_are_reported_so_they_can_be_disclosed(self):
        # Silently defaulting housing_type picks a school-impact tier worth
        # thousands on the citizen's behalf.
        _, defaulted = fa.apply_defaults(self.WITH_DEFAULTS, {})
        assert defaulted, "defaults must be reported, not applied invisibly"
