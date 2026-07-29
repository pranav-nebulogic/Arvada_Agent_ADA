"""
Regression: transform must only process the ACTIVE city's raw files.

`DEFAULT_CITY_ID=woodinville-wa python run_transform.py` re-ran the LLM over
Arvada's 99 scraped pages and wrote 20 Arvada articles stamped
`city_id: woodinville-wa`, because the article builder falls back to
CITY.city_id when a raw source carries no city_id. Ingesting those would have
put Arvada's content in Woodinville's knowledge base.

The filter keys off the URL HOST. That detail is load-bearing: no raw file
carries a city_id at all (checked for both cities), so a city_id-based filter
matches everything and the bug comes straight back.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# transformer.py builds an AsyncOpenAI client at import time, which raises
# without a key. No LLM call is made by these tests -- only the pure file
# filter -- so a placeholder is enough to let the module import.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")

import transformer  # noqa: E402


def write_raw(tmp_path: Path, name: str, url: str, city_id=None) -> Path:
    f = tmp_path / name
    src = {"url": url, "source_type": "html"}
    if city_id is not None:
        src["city_id"] = city_id
    f.write_text(json.dumps({"source": src, "content": "x"}), encoding="utf-8")
    return f


@pytest.fixture
def raws(tmp_path):
    return [
        write_raw(tmp_path, "wv1.json", "https://www.woodinville.gov/184/Business-License"),
        write_raw(tmp_path, "wv2.json", "https://www.woodinville.gov/200/Permitting"),
        write_raw(tmp_path, "arv1.json", "https://www.arvadaco.gov/332/Building-Permits"),
        write_raw(tmp_path, "arv2.json", "https://www.arvadaco.gov/346/Fences"),
    ]


def _use_city(monkeypatch, base_url: str):
    monkeypatch.setattr(transformer, "get_sources", lambda: {"base_url": base_url})


class TestCityFilter:
    def test_only_the_active_city_survives(self, monkeypatch, raws):
        _use_city(monkeypatch, "https://www.woodinville.gov")
        kept = {f.name for f in transformer.filter_raw_files_for_city(raws)}
        assert kept == {"wv1.json", "wv2.json"}

    def test_the_other_city_is_the_mirror_image(self, monkeypatch, raws):
        _use_city(monkeypatch, "https://www.arvadaco.gov")
        kept = {f.name for f in transformer.filter_raw_files_for_city(raws)}
        assert kept == {"arv1.json", "arv2.json"}

    def test_missing_city_id_does_not_leak_the_other_city(self, monkeypatch, raws):
        # The actual failure mode: raw files carry NO city_id, so anything that
        # trusts source["city_id"] keeps every file.
        for f in raws:
            assert json.loads(f.read_text())["source"].get("city_id") is None
        _use_city(monkeypatch, "https://www.woodinville.gov")
        kept = transformer.filter_raw_files_for_city(raws)
        assert all("arvadaco" not in json.loads(f.read_text())["source"]["url"] for f in kept)

    def test_unreadable_file_is_skipped_not_transformed_blind(self, monkeypatch, tmp_path, raws):
        bad = tmp_path / "broken.json"
        bad.write_bytes(b"\xae not json")
        _use_city(monkeypatch, "https://www.woodinville.gov")
        kept = transformer.filter_raw_files_for_city([*raws, bad])
        assert bad not in kept

    def test_no_base_url_configured_keeps_everything(self, monkeypatch, raws):
        # Can't discriminate without a base_url; don't silently drop the corpus.
        _use_city(monkeypatch, "")
        assert len(transformer.filter_raw_files_for_city(raws)) == len(raws)
