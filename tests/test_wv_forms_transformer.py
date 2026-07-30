"""
Tests for wv_forms_transformer.py.

Pure logic, no PDFs and no network: the parsing helpers are exercised against
fixtures shaped like what pdfplumber actually returned from the City's forms.

Every case here comes from a real defect found while building this. All three of
Woodinville's document archetypes are MULTI-COLUMN, and pdfplumber's text layer
splices columns together, so each needed table extraction instead:

  * fixtures     "Floor Drain Backwater Valve Bathroom Sink" -- three separate
                 fixtures welded into one by the text layer.
  * plan standards
                 "All drawings should be drawn to scale (1/8 or 1/4 inch) For
                 questions, please contact Development Services at and include a
                 north arrow" -- one column's sentence interleaved with another's.
  * matrix       "Application Form 1 1 1" -- copy counts with the permit-type
                 mapping destroyed.

The matrix is the one that must never silently drift: a one-column offset would
attach the wrong copy counts to the wrong permit, which is worse than having no
answer at all, because it looks right.
"""
from __future__ import annotations

from agent import __name__ as _  # noqa: F401  (keeps import path consistent)

import wv_forms_transformer as wf


# Shaped like page.extract_tables() output for doc 568: spacer columns between
# every real one, and the rotated "Required"/"Submitted" headers that the text
# layer returns reversed.
MATRIX_TABLE = [
    ["Submittal Requirements", "", "", "Building Permit - New Building", "", "",
     "Building Permit - Additions", "", "", "Mechanical or Plumbing Permit", "", "",
     "deriuqeR", "", "dettimbuS", ""],
    ["", "General:", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["Application Form", "", "", "1", "", "", "1", "", "", "1", "", "", "", "", "", ""],
    ["Owner Authorization Form", "", "", "1", "", "", "1", "", "", "", "", "", "", "", "", ""],
    ["", "Building Plans:", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
    ["Building/Construction Plans", "", "", "3", "", "", "3", "", "", "2", "", "", "", "", "", ""],
    ["Structural/Lateral Calculations", "", "", "2", "", "", "2", "", "", "", "", "", "", "", "", ""],
]


def invert(table):
    """Drive the same logic invert_matrix() applies to one table."""
    header = [wf.squash(c) for c in table[0]]
    cols = {i: h for i, h in enumerate(header)
            if i > 0 and h and h.lower() not in wf.NOISE_HEADERS}
    buckets = {i: [] for i in cols}
    section = ""
    import re
    for row in table[1:]:
        cells = [wf.squash(c) for c in row]
        label = cells[0] if cells else ""
        if not label:
            nxt = next((c for c in cells[1:] if c), "")
            if nxt.endswith(":"):
                section = nxt.rstrip(":")
            continue
        for i in cols:
            val = cells[i] if i < len(cells) else ""
            if re.fullmatch(r"\d+", val):
                name = f"{section}: {label}" if section else label
                buckets[i].append((name, val))
    return {cols[i]: dict(v) for i, v in buckets.items() if v}


class TestMatrixInversion:
    def test_permit_types_come_from_the_header(self):
        got = invert(MATRIX_TABLE)
        assert set(got) == {"Building Permit - New Building",
                            "Building Permit - Additions",
                            "Mechanical or Plumbing Permit"}

    def test_rotated_checkbox_headers_are_not_permit_types(self):
        # "deriuqeR"/"dettimbuS" are "Required"/"Submitted" reversed. Treating them
        # as permit types shifts every column and corrupts the whole grid.
        assert "deriuqeR" not in invert(MATRIX_TABLE)
        assert "dettimbuS" not in invert(MATRIX_TABLE)

    def test_copy_counts_land_on_the_right_permit(self):
        # The distinctive cell: Mechanical/Plumbing needs 2 plan sets where the
        # building permits need 3. A one-column offset shows up here first.
        got = invert(MATRIX_TABLE)
        key = "Building Plans: Building/Construction Plans"
        assert got["Building Permit - New Building"][key] == "3"
        assert got["Building Permit - Additions"][key] == "3"
        assert got["Mechanical or Plumbing Permit"][key] == "2"

    def test_blank_cell_means_not_required(self):
        # Structural calcs are required for new builds and additions only.
        got = invert(MATRIX_TABLE)
        key = "Building Plans: Structural/Lateral Calculations"
        assert key in got["Building Permit - New Building"]
        assert key not in got["Mechanical or Plumbing Permit"]

    def test_group_headers_prefix_their_rows(self):
        got = invert(MATRIX_TABLE)
        reqs = got["Building Permit - New Building"]
        assert "General: Application Form" in reqs
        assert "Building Plans: Structural/Lateral Calculations" in reqs


class TestFixtureNoise:
    def test_column_group_headers_are_excluded(self):
        # The text layer returned these looking exactly like fixtures.
        for h in ("DRAINS", "SINKS", "HVAC SYSTEM", "QTY", "Appliances and Equip"):
            assert h.lower() in wf._FIXTURE_GROUPS or h.lower() in {
                x.lower() for x in wf._FIXTURE_GROUPS}

    def test_impact_fee_table_headers_are_excluded(self):
        # The traffic-impact question table shares a page with the plumbing
        # schedule, so YES/NO/QUESTION/CITY ONLY leaked in as "fixtures".
        for h in ("yes", "no", "question", "city only"):
            assert h in wf._FIXTURE_GROUPS


class TestStandardsNoise:
    def test_page_furniture_is_rejected(self):
        for junk in ("YES", "NO", "N/A", "CITY OF WOODINVILLE",
                     "DEVELOPMENT SERVICES", "BUILDING PLAN STANDARDS",
                     "GENERAL NOTES", "QUESTIONS AND CONTACT", "Page 2"):
            assert wf._STANDARDS_NOISE.match(junk), junk

    def test_real_requirements_are_kept(self):
        for keep in ("Project title and description",
                     "Building square footage, by floor and type of space",
                     "ADA accessibility route access, including bathrooms",
                     "Exits to outside of building"):
            assert not wf._STANDARDS_NOISE.match(keep), keep


class TestContactBlock:
    def test_email_stops_at_the_tld(self):
        # These forms wrap the address mid-token, so newlines are stripped before
        # matching -- which previously let the match run on into the next word and
        # produce "PermitCenter@woodinville.gov.YES".
        text = "contact PermitCenter@\nwoodinville.gov.  YES NO QUESTION"
        assert wf.contact_block(text)["email"] == "PermitCenter@woodinville.gov"

    def test_phone_and_hours(self):
        text = ("425-489-2754 - 17301 133rd Avenue NE\n"
                "Monday - Thursday 7:30am - 5:00pm - Friday 7:30am - 4:00pm")
        got = wf.contact_block(text)
        assert got["phone"] == "425-489-2754"
        assert "Monday" in got["hours"]

    def test_missing_pieces_are_simply_absent(self):
        assert wf.contact_block("no contact details here") == {}


class TestAsciiCleaning:
    def test_typography_is_normalised(self):
        # The WMC corpus already carries mojibake em-dashes from an earlier
        # ingest; no reason to add more.
        out = wf.ascii_clean("Fee – schedule — with ‘quotes’")
        assert "–" not in out and "—" not in out
        assert out.isascii()

    def test_squash_collapses_whitespace(self):
        assert wf.squash("  a\n\n  b \t c ") == "a b c"

    def test_slug_is_url_safe(self):
        assert wf.slug("Building Permit - New Building!") == "building-permit-new-building"


class TestExclusionPolicy:
    def test_matrix_and_standards_doc_sets_are_disjoint(self):
        assert not (wf.MATRIX_DOCS & wf.STANDARDS_DOCS)

    def test_signature_and_bond_docs_are_not_configured(self):
        # 617 Owner Authorization, 2136 Cash Performance Guarantee, 564 Assignment
        # of Funds, 565 Surety Bond: indemnity boilerplate with nothing a citizen
        # can be told beyond "this form is required", which the matrix already
        # records. Excluded in sources.py, so they never reach the transformer.
        import sources
        ids = {e["url"].rsplit("/", 1)[-1]
               for e in sources.WOODINVILLE_SOURCES["pdfs"]}
        assert not (ids & {"617", "2136", "564", "565"})
        assert not (ids & {"613", "628"}), "DOCX/XLSX need different extractors"


class TestExtractorRouting:
    """The doc TYPE decides the extractor, never a content sniff.

    Building Plan Standards contains the literal headings "MECHANICAL FIXTURES"
    and "PLUMBING FIXTURES", so sniffing for those pulled 24 plan requirements
    ("Concrete strength", "Foundation wall schedule", "N/A") into form_fields as
    if they were plumbing fixtures.
    """

    def test_standards_docs_never_yield_fixtures(self):
        import re
        src = open("wv_forms_transformer.py", encoding="utf-8").read()
        # The guard must be on doc id, evaluated before fixture_vocab runs.
        assert re.search(r"\[\]\s*if\s+did\s+in\s+STANDARDS_DOCS\s+else\s+fixture_vocab",
                         src), "fixture extraction must be gated on document type"

    def test_the_three_standards_docs_are_declared(self):
        assert wf.STANDARDS_DOCS == {"567", "570", "627"}


class TestSchemaContract:
    """form_fields keys are fixed by ingestor.article_to_chunks.

    It reads f['field_name'] and f['label'] directly, so a wrong key raises
    KeyError at ingest time -- which is how this was found, after 2 of 55
    articles had already been written.
    """

    REQUIRED = {"field_name", "label"}
    OPTIONAL = {"description", "example_value", "why_needed"}

    def test_ingestor_still_expects_these_keys(self):
        src = open("ingestor.py", encoding="utf-8").read()
        for key in self.REQUIRED:
            assert f"f['{key}']" in src, f"ingestor no longer reads {key}"

    def test_transformer_emits_them(self):
        src = open("wv_forms_transformer.py", encoding="utf-8").read()
        for key in self.REQUIRED | self.OPTIONAL:
            assert f'"{key}"' in src, f"transformer does not emit {key}"

    def test_required_documents_keys_match_the_ingestor(self):
        src = open("ingestor.py", encoding="utf-8").read()
        assert "d['name']" in src
        for key in ("description", "where_to_get", "example"):
            assert f"'{key}'" in src or f'"{key}"' in src


class TestPermitTypeMapping:
    """Each inverted article covers ONE permit type and should carry it.

    sources.py sets permit_type=None on 568/614 because each DOCUMENT spans many
    permit types. Before hybrid_search was fixed to include NULL rows, that made
    all 32 checklists invisible whenever the classifier named a permit type.
    """

    def test_known_columns_map_to_corpus_keys(self):
        cases = {
            "Building Permit - New Building": "building_permit",
            "Building Permit - Additions": "building_permit",
            "Mechanical or Plumbing Permit": "building_permit_mechanical_plumbing",
            "Fire Permit": "fire_permit",
            "Sign Permit - Freestanding": "sign_permit",
            "Site Development Permit": "development_permit",
            "Right-of-Way Permit": "row_permit",
            "Tree Removal": "tree_permit",
        }
        for column, expected in cases.items():
            assert wf.permit_type_for(column) == expected, column

    def test_mechanical_wins_over_the_generic_building_match(self):
        # "Mechanical or Plumbing Permit" contains neither "building permit" nor
        # anything ambiguous, but ordering matters if a column ever reads
        # "Building Permit - Mechanical". The specific rule is listed first.
        assert wf.PERMIT_TYPE_BY_COLUMN[0][0] == "mechanical or plumbing"

    def test_land_use_types_stay_none(self):
        # No established key for these, and NULL is no longer invisible.
        for column in ("Short Plat", "Variance", "Design Review", "SEPA/Project Approval"):
            assert wf.permit_type_for(column) is None, column
