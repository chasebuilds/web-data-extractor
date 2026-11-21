from __future__ import annotations

import copy
from pathlib import Path

import pytest

from web_data_extractor.config import ConfigError, load_config, parse_config

REPO = Path(__file__).resolve().parent.parent

BASE = {
    "name": "job",
    "start_urls": ["https://shop.test/list"],
    "list": {
        "item_selector": "li",
        "fields": {"name": "a", "link": {"selector": "a", "attr": "href", "type": "url"}},
    },
}


def job(**changes):
    data = copy.deepcopy(BASE)
    data.update(changes)
    return data


def test_minimal_config_gets_polite_defaults():
    config = parse_config(job())
    assert config.politeness.min_delay_seconds == 1.0
    assert config.allowed_hosts == {"shop.test"}
    assert config.max_pages == 5
    assert [f.name for f in config.fields] == ["name", "link"]


@pytest.mark.parametrize("name", ["fixture.yaml", "books_toscrape.yaml", "quotes_toscrape.yaml"])
def test_bundled_configs_load(name):
    config = load_config(REPO / "configs" / name)
    assert config.politeness.min_delay_seconds >= 0.5
    assert config.max_pages <= 10


def test_bundled_live_configs_only_target_the_sandbox_sites():
    hosts = set()
    for name in ("books_toscrape.yaml", "quotes_toscrape.yaml"):
        hosts |= load_config(REPO / "configs" / name).allowed_hosts
    assert hosts == {"books.toscrape.com", "quotes.toscrape.com"}


def test_variables_are_substituted_and_overridable():
    data = job(vars={"base": "https://a.test"}, start_urls=["${base}/list"])
    assert parse_config(data).start_urls == ["https://a.test/list"]
    assert parse_config(data, {"base": "https://b.test"}).start_urls == ["https://b.test/list"]
    with pytest.raises(ConfigError, match="not defined"):
        parse_config(job(start_urls=["${nope}/list"]))


def test_page_pattern_supplies_the_start_url():
    data = job(pagination={"page_pattern": "https://shop.test/p/{page}", "start": 2, "end": 4})
    del data["start_urls"]
    config = parse_config(data)
    assert config.start_urls == ["https://shop.test/p/2"]
    assert config.pagination.end == 4


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"start_urls": []}, "start_urls must list"),
        ({"start_urls": ["ftp://x/y"]}, "not an http"),
        ({"politness": {}}, "unknown key"),
        ({"politeness": {"min_delay_second": 2}}, "unknown key"),
        ({"politeness": {"min_delay_seconds": 0.2}}, "must be at least"),
        ({"politeness": {"backoff_base_seconds": 0}}, "Backoff cannot be turned off"),
        ({"politeness": {"max_retries": 50}}, "max_retries"),
        ({"limits": {"max_pages": 0}}, "at least 1"),
        ({"list": {"item_selector": "", "fields": {"a": "b"}}}, "item_selector is required"),
        ({"list": {"item_selector": "li", "fields": {}}}, "at least one field"),
        ({"list": {"item_selector": "li", "fields": {"a": {"type": "date"}}}}, "type must be"),
        ({"list": {"item_selector": "li", "fields": {"a": {"regex": "("}}}}, "bad regex"),
        ({"list": {"item_selector": "li", "fields": {"source_url": "a"}}}, "reserved"),
        ({"pagination": {"next_selector": "a", "page_pattern": "x{page}"}}, "not both"),
        ({"pagination": {"page_pattern": "https://shop.test/p"}}, "must contain"),
        ({"detail": {"url_field": "name", "fields": {"x": "p"}}}, "type: url"),
        ({"detail": {"url_field": "link", "fields": {"name": "p"}}}, "used twice"),
        ({"unique_by": ["nope"]}, "unknown field"),
    ],
)
def test_bad_configs_are_rejected_with_a_reason(changes, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(job(**changes))


def test_invalid_yaml_is_a_config_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)
