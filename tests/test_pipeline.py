"""End to end: the bundled fixture site, scored against its truth file."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest
from openpyxl import load_workbook

from benchmark.generate_site import render_site
from web_data_extractor.cli import main
from web_data_extractor.config import load_config, parse_config
from web_data_extractor.export import write_outputs
from web_data_extractor.fetch import PoliteFetcher
from web_data_extractor.runner import run

REPO = Path(__file__).resolve().parent.parent
TRUTH = json.loads((REPO / "benchmark" / "truth.json").read_text(encoding="utf-8"))


def run_fixture(site, clock, **changes):
    config = load_config(REPO / "configs" / "fixture.yaml", {"base_url": site.base_url})
    config = replace(config, **changes)
    fetcher = PoliteFetcher(
        config.politeness,
        allowed_hosts=config.allowed_hosts,
        max_requests=config.max_requests,
        sleep=clock.sleep,
        clock=clock.clock,
    )
    with fetcher:
        return run(config, fetcher=fetcher)


@pytest.fixture
def result(fixture_site, fake_clock):
    return run_fixture(fixture_site, fake_clock)


def test_committed_fixture_site_matches_the_seeded_generator():
    files, truth = render_site()
    assert truth == TRUTH
    on_disk = {
        p.relative_to(REPO / "fixture_site").as_posix()
        for p in (REPO / "fixture_site").rglob("*")
        if p.is_file()
    }
    assert on_disk == set(files)
    for relative, text in files.items():
        assert (REPO / "fixture_site" / relative).read_text(encoding="utf-8") == text, relative


def test_every_valid_product_is_extracted_with_correct_values(result):
    got = {r["sku"]: r for r in result.records}
    assert set(got) == {p["sku"] for p in TRUTH["records"]}
    assert len(result.records) == len(TRUTH["records"])
    for product in TRUTH["records"]:
        row = got[product["sku"]]
        for name in ("name", "category", "rating", "brand", "stock"):
            assert row[name] == product[name], (product["sku"], name)
        assert row["price"] == pytest.approx(product["price"])


def test_every_record_carries_source_url_and_fetch_time(result, fixture_site):
    for row in result.records:
        assert row["source_url"].startswith(f"{fixture_site.base_url}/catalog/page-")
        assert row["detail_page_url"].startswith(f"{fixture_site.base_url}/item/")
        assert row["fetched_at"].endswith("Z") and row["detail_fetched_at"].endswith("Z")


def test_every_broken_item_is_rejected_with_the_expected_reason(result):
    reasons = {r["sku"]: r["reason"] for r in result.rejected if "duplicate" not in r["reason"]}
    assert set(reasons) == {b["sku"] for b in TRUTH["broken"]}
    for broken in TRUTH["broken"]:
        assert broken["reason_contains"] in reasons[broken["sku"]], broken


def test_repeat_listings_are_rejected_as_duplicates_not_dropped(result):
    duplicates = [r for r in result.rejected if "duplicate" in r["reason"]]
    assert sorted(r["sku"] for r in duplicates) == TRUTH["duplicate_listings"]
    assert all("first seen on http" in r["reason"] for r in duplicates)


def test_no_listing_disappears(result):
    assert len(result.records) + len(result.rejected) == TRUTH["listings"]


def test_disallowed_section_is_never_requested_during_a_full_run(result, fixture_site):
    assert not [p for p in fixture_site.paths if p.startswith("/private/")]
    assert "SKU-9999" not in {r["sku"] for r in [*result.records, *result.rejected]}
    refused = [e for e in result.log if e.robots == "disallowed"]
    assert len(refused) == 1 and "/private/" in refused[0].url


def test_duplicates_and_broken_rows_cost_no_detail_requests(result, fixture_site):
    detail_hits = [p for p in fixture_site.paths if p.startswith("/item/")]
    # 60 valid products plus the one dead link that has to be tried to find out.
    assert len(detail_hits) == len(TRUTH["records"]) + 1
    assert len(detail_hits) == len(set(detail_hits))


def test_full_run_waits_the_delay_before_every_request_after_the_first(result, fake_clock):
    network = [e for e in result.log if e.status is not None and not e.cache_hit]
    assert len(fake_clock.sleeps) == len(network) - 1
    assert all(s == pytest.approx(0.5) for s in fake_clock.sleeps)


def test_page_cap_stops_the_crawl_and_says_so(fixture_site, fake_clock):
    capped = run_fixture(fixture_site, fake_clock, max_pages=2)
    assert dict(capped.summary)["list_pages_read"] == 2
    assert "/catalog/page-3.html" not in fixture_site.paths
    assert any("max_pages cap" in str(v) for k, v in capped.summary if k == "note")


def test_detail_cap_rejects_the_rest_with_a_reason(fixture_site, fake_clock):
    capped = run_fixture(fixture_site, fake_clock, max_pages=1, max_detail_pages=3)
    assert len([p for p in fixture_site.paths if not p.startswith("/catalog/")]) <= 4
    assert any("max_detail_pages cap" in r["reason"] for r in capped.rejected)
    assert len(capped.records) + len(capped.rejected) == 12


def test_page_pattern_pagination_stops_at_the_first_missing_page(fixture_site, fake_clock):
    config = load_config(REPO / "configs" / "fixture.yaml", {"base_url": fixture_site.base_url})
    pattern = f"{fixture_site.base_url}/catalog/page-{{page}}.html"
    config = replace(
        config,
        start_urls=[pattern.replace("{page}", "1")],
        pagination=replace(config.pagination, next_selector="", page_pattern=pattern, end=9),
        detail_fields=[],
        detail_url_field="",
    )
    fetcher = PoliteFetcher(config.politeness, sleep=fake_clock.sleep, clock=fake_clock.clock)
    with fetcher:
        outcome = run(config, fetcher=fetcher)
    catalog = [p for p in fixture_site.paths if p.startswith("/catalog/")]
    assert catalog == [f"/catalog/page-{n}.html" for n in range(1, 8)]
    assert dict(outcome.summary)["list_pages_read"] == TRUTH["list_pages"]


def test_optional_field_that_cannot_be_read_becomes_a_warning(scripted, fake_clock):
    scripted.add("/robots.txt", (200, {}, ""))
    scripted.add(
        "/list",
        (200, {}, '<ul><li><a>Lamp</a><i class="w">heavy</i></li><li><a>Bin</a></li></ul>'),
    )
    config = parse_config(
        {
            "start_urls": [f"{scripted.base_url}/list"],
            "list": {
                "item_selector": "li",
                "fields": {
                    "name": {"selector": "a", "required": True},
                    "weight": {"selector": ".w", "type": "number"},
                },
            },
        }
    )
    fetcher = PoliteFetcher(config.politeness, sleep=fake_clock.sleep, clock=fake_clock.clock)
    with fetcher:
        outcome = run(config, fetcher=fetcher)
    lamp, bin_ = outcome.records
    assert lamp["weight"] is None and lamp["warnings"] == "weight: no number in 'heavy'"
    assert bin_["weight"] is None and bin_["warnings"] == ""


# -- export ---------------------------------------------------------------


def test_workbook_has_the_four_sheets_and_matching_rows(result, tmp_path):
    paths = write_outputs(result, tmp_path)
    workbook = load_workbook(paths["xlsx"])
    assert workbook.sheetnames == ["Records", "Rejected", "RunLog", "Summary"]
    assert workbook["Records"].max_row == len(result.records) + 1
    assert workbook["Rejected"].max_row == len(result.rejected) + 1
    assert workbook["RunLog"].max_row == len(result.log) + 1
    header = [c.value for c in workbook["Records"][1]]
    assert header[-7:] == [
        "source_url",
        "fetched_at",
        "source_sha256",
        "detail_page_url",
        "detail_fetched_at",
        "detail_sha256",
        "warnings",
    ]
    assert [c.value for c in workbook["Rejected"][1]][0] == "reason"
    assert [c.value for c in workbook["RunLog"][1]][:6] == [
        "seq",
        "url",
        "kind",
        "status",
        "cache_hit",
        "robots",
    ]
    summary = {row[0].value: row[1].value for row in workbook["Summary"].iter_rows(min_row=2)}
    assert summary["records"] == len(TRUTH["records"])
    assert summary["export_check"] == "passed"
    price = header.index("price") + 1
    assert isinstance(workbook["Records"].cell(row=2, column=price).value, float)


def test_csv_files_match_the_workbook(result, tmp_path):
    paths = write_outputs(result, tmp_path)
    with paths["Records"].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(result.records)
    assert rows[0]["source_url"] and rows[0]["fetched_at"]
    with paths["RunLog"].open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == len(result.log)
    assert paths["Rejected"].exists() and paths["Summary"].exists()


def test_scraped_text_that_looks_like_a_formula_is_stored_as_text(result, tmp_path):
    result.records[0]["name"] = '=HYPERLINK("http://bad.test","x")'
    paths = write_outputs(result, tmp_path)
    cell = load_workbook(paths["xlsx"])["Records"].cell(row=2, column=2)
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK")
    with paths["Records"].open(encoding="utf-8-sig", newline="") as handle:
        assert next(csv.DictReader(handle))["name"].startswith("'=")


# -- command line ---------------------------------------------------------


def test_demo_command_writes_the_outputs(tmp_path, virtual_time, capsys):
    code = main(
        [
            "demo",
            "--config", str(REPO / "configs" / "fixture.yaml"),
            "--site", str(REPO / "fixture_site"),
            "--port", "0",
            "--out", str(tmp_path),
        ]
    )  # fmt: skip
    assert code == 0
    assert (tmp_path / "fixture-catalog.xlsx").exists()
    assert (tmp_path / "records.csv").exists()
    assert "disallowed): 0" in capsys.readouterr().out


def test_validate_command_and_bad_config_exit_code(tmp_path, capsys):
    assert main(["validate", str(REPO / "configs" / "books_toscrape.yaml")]) == 0
    assert "books-toscrape" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "start_urls: [https://x.test/]\npoliteness: {min_delay_seconds: 0}\n"
        "list: {item_selector: li, fields: {a: b}}\n",
        encoding="utf-8",
    )
    assert main(["validate", str(bad)]) == 2
    assert "min_delay_seconds must be at least" in capsys.readouterr().err
