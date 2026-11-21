"""Run-health gate, soft-block stop, provenance hashes, terms record, personal-data guard."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

import web_data_extractor
from web_data_extractor.cli import main
from web_data_extractor.config import ConfigError, FieldSpec, load_config, parse_config
from web_data_extractor.extract import extract_fields, parse_html
from web_data_extractor.fetch import JITTER_FRACTION, PoliteFetcher, Politeness
from web_data_extractor.personal import looks_personal
from web_data_extractor.runner import run

REPO = Path(__file__).resolve().parent.parent
ALLOW_ALL = (200, {}, "User-agent: *\nDisallow:\n")


def fixture_run(site, clock, **changes):
    config = load_config(REPO / "configs" / "fixture.yaml", {"base_url": site.base_url})
    config = replace(config, **changes)
    fetcher = PoliteFetcher(
        config.politeness, allowed_hosts=config.allowed_hosts, sleep=clock.sleep, clock=clock.clock
    )
    with fetcher:
        return run(config, fetcher=fetcher)


def scripted_run(scripted, clock, job):
    job = {"start_urls": [f"{scripted.base_url}/list"], **job}
    fetcher = PoliteFetcher(sleep=clock.sleep, clock=clock.clock, jitter=lambda: 0.0)
    with fetcher:
        return run(parse_config(job), fetcher=fetcher), fetcher


# -- run-health gate --------------------------------------------------------


def test_healthy_fixture_run_passes_the_gate(fixture_site, fake_clock):
    summary = dict(fixture_run(fixture_site, fake_clock).summary)
    assert summary["run_health"] == "ok"
    assert summary["fill_rate[sku]"] == 1.0
    assert 0.9 < summary["fill_rate[price]"] < 1.0
    assert summary["rejected_share"] == pytest.approx(12 / 72, abs=0.001)


def test_a_selector_that_stops_matching_fails_the_gate(fixture_site, fake_clock):
    config = load_config(REPO / "configs" / "fixture.yaml", {"base_url": fixture_site.base_url})
    moved = [replace(f, selector=".cost") if f.name == "price" else f for f in config.list_fields]
    outcome = fixture_run(fixture_site, fake_clock, list_fields=moved)
    summary = dict(outcome.summary)
    assert outcome.records == []
    assert summary["run_health"].startswith("attention:")
    assert "below min_records 40" in summary["run_health"]
    assert "price filled on 0% of listings" in summary["run_health"]
    assert "rejected, above 30%" in summary["run_health"]
    # Every listing is still accounted for, and no detail page was requested for nothing.
    assert len(outcome.rejected) == 72
    assert not [p for p in fixture_site.paths if p.startswith("/item/")]


def test_optional_field_that_goes_missing_is_caught_by_fill_rate(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    items = "".join(f"<li><a>Item {n}</a></li>" for n in range(10))
    scripted.add("/list", (200, {}, f"<ul>{items}</ul>"))
    job = {
        "list": {"item_selector": "li", "fields": {"name": "a", "colour": ".colour"}},
        "health": {"min_fill_rate": {"colour": 0.5}},
    }
    outcome, _ = scripted_run(scripted, fake_clock, job)
    assert len(outcome.records) == 10
    assert "colour filled on 0% of listings, below 50%" in dict(outcome.summary)["run_health"]


def test_cli_exits_non_zero_when_the_gate_fails(tmp_path, virtual_time, capsys):
    code = main(
        [
            "demo",
            "--config", str(REPO / "configs" / "fixture.yaml"),
            "--site", str(REPO / "fixture_site"),
            "--port", "0",
            "--out", str(tmp_path),
            "--max-pages", "1",
        ]
    )  # fmt: skip
    assert code == 1
    assert "below min_records 40" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("health", "message"),
    [
        ({"min_records": -1}, "min_records"),
        ({"min_fill_rate": {"nope": 0.5}}, "unknown field"),
        ({"min_fill_rate": {"name": 1.5}}, "share from 0 to 1"),
        ({"max_rejected_share": 2}, "share from 0 to 1"),
        ({"min_record": 3}, "unknown key"),
    ],
)
def test_bad_health_settings_are_rejected(health, message):
    job = {
        "start_urls": ["https://shop.test/list"],
        "list": {"item_selector": "li", "fields": {"name": "a"}},
        "health": health,
    }
    with pytest.raises(ConfigError, match=message):
        parse_config(job)


# -- soft block -------------------------------------------------------------


def test_empty_page_with_block_wording_ends_the_job(scripted, fake_clock, tmp_path):
    scripted.add("/robots.txt", ALLOW_ALL)
    body = "<title>Shop</title><p>We detected unusual traffic from your network.</p>"
    scripted.add("/list", (200, {}, body + '<a class="next" href="/list2">next</a>'))
    scripted.add("/list2", (200, {}, "<ul><li><a>Lamp</a></li></ul>"))
    job = {
        "list": {"item_selector": "li", "fields": {"name": "a"}},
        "pagination": {"next_selector": "a.next"},
    }
    outcome, fetcher = scripted_run(scripted, fake_clock, job)
    assert scripted.count("/list2") == 0
    assert "block wording ('unusual traffic')" in next(iter(fetcher.stopped_hosts.values()))
    assert dict(outcome.summary)["run_health"].startswith("attention: job ended early")


def test_empty_page_without_block_wording_is_only_a_note(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/list", (200, {}, "<title>Shop</title><p>No products in this category.</p>"))
    outcome, fetcher = scripted_run(
        scripted, fake_clock, {"list": {"item_selector": "li", "fields": {"name": "a"}}}
    )
    assert fetcher.stopped_hosts == {}
    assert any("had no items" in str(v) for k, v in outcome.summary if k == "note")


# -- jitter -----------------------------------------------------------------


def test_backoff_jitter_only_ever_adds_time(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (503, {}, ""), (503, {}, ""), (200, {}, "ok"))
    politeness = Politeness(min_delay_seconds=1.0, backoff_base_seconds=2.0)
    fetcher = PoliteFetcher(
        politeness, sleep=fake_clock.sleep, clock=fake_clock.clock, jitter=lambda: 1.0
    )
    with fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
    assert fake_clock.sleeps == [1.0, 2.0 * (1 + JITTER_FRACTION), 4.0 * (1 + JITTER_FRACTION)]


def test_default_jitter_stays_between_the_plain_backoff_and_its_ceiling(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (503, {}, ""), (200, {}, "ok"))
    fetcher = PoliteFetcher(
        Politeness(backoff_base_seconds=4.0), sleep=fake_clock.sleep, clock=fake_clock.clock
    )
    with fetcher:
        fetcher.get(f"{scripted.base_url}/a")
    assert 4.0 <= fake_clock.sleeps[-1] <= 4.0 * (1 + JITTER_FRACTION)


# -- provenance -------------------------------------------------------------


def test_each_record_carries_the_hash_of_the_pages_it_was_read_from(fixture_site, fake_clock):
    outcome = fixture_run(fixture_site, fake_clock, max_pages=1)
    row = outcome.records[0]
    for url_key, hash_key in (
        ("source_url", "source_sha256"),
        ("detail_page_url", "detail_sha256"),
    ):
        relative = row[url_key].removeprefix(fixture_site.base_url + "/")
        on_disk = (REPO / "fixture_site" / relative).read_bytes()
        assert row[hash_key] == hashlib.sha256(on_disk).hexdigest()


def test_summary_names_the_extractor_version_and_config_hash(fixture_site, fake_clock):
    summary = dict(fixture_run(fixture_site, fake_clock, max_pages=1).summary)
    assert summary["extractor_version"] == web_data_extractor.__version__
    assert len(summary["config_sha256"]) == 64
    same = load_config(REPO / "configs" / "fixture.yaml", {"base_url": "http://127.0.0.1:1"})
    assert same.sha256 == summary["config_sha256"]


# -- terms record -----------------------------------------------------------


def test_a_real_site_cannot_be_run_without_a_terms_check_record():
    job = {
        "start_urls": ["https://shop.example.com/list"],
        "list": {"item_selector": "li", "fields": {"name": "a"}},
    }
    config = parse_config(job)
    assert config.hosts_needing_terms_check == ["shop.example.com"]
    # Raised before a fetcher exists, so nothing is requested.
    with pytest.raises(ConfigError, match="no terms_check recorded for shop.example.com"):
        run(config)
    job["terms_check"] = {
        "url": "https://shop.example.com/terms",
        "checked_on": "2026-01-05",
        "clause": "section 4 allows automated reading of public pages",
    }
    assert parse_config(job).hosts_needing_terms_check == []


@pytest.mark.parametrize("name", ["fixture.yaml", "books_toscrape.yaml", "quotes_toscrape.yaml"])
def test_bundled_configs_only_use_demo_hosts(name):
    assert load_config(REPO / "configs" / name).hosts_needing_terms_check == []


# -- personal-data guard ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("jordan.smith@shop.example.com", "a personal email address"),
        ("Ask for Sam: sam@shop.example.com", "a personal email address"),
        ("info@shop.example.com", ""),
        ("Email sales@shop.example.com or support@shop.example.com", ""),
        ("+1 606 555 0100", "a phone number"),
        ("(606) 555-0100", "a phone number"),
        ("tel:+16065550100", "a phone number"),
        ("SKU-1001", ""),
        ("2026-11-20", ""),
        ("1234567890", ""),
        ("Compact Steel Desk Lamp", ""),
    ],
)
def test_looks_personal(value, expected):
    assert looks_personal(value) == expected


def test_personal_details_are_withheld_from_records_and_from_rejected():
    html = '<li><a class="n">Lamp</a><span class="c">pat.doe@shop.example.com</span></li>'
    node = parse_html(html).select_one("li")
    specs = [FieldSpec("name", selector=".n"), FieldSpec("contact", selector=".c")]
    values, raws, problems = extract_fields(node, specs, "https://shop.example.com/")
    assert values == {"name": "Lamp", "contact": None}
    assert raws["contact"] == "[withheld]"
    assert problems["contact"] == "withheld: looks like a personal email address"


def test_withheld_value_never_reaches_any_output_row(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add(
        "/list",
        (
            200,
            {},
            '<ul><li><a>Lamp</a><i class="c">pat.doe@shop.example.com</i></li>'
            '<li><a>Bin</a><i class="c">info@shop.example.com</i></li></ul>',
        ),
    )
    job = {
        "list": {
            "item_selector": "li",
            "fields": {"name": "a", "contact": {"selector": ".c", "required": True}},
        },
        "health": {"min_records": 0},
    }
    outcome, _ = scripted_run(scripted, fake_clock, job)
    assert [r["contact"] for r in outcome.records] == ["info@shop.example.com"]
    assert outcome.rejected[0]["contact"] == "[withheld]"
    assert "pat.doe" not in repr(outcome.records) + repr(outcome.rejected)
