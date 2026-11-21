"""Tests for the rules about backing off for good: Retry-After, blocks, stale robots.txt.

Offline, like the rest of the suite: scripted servers on 127.0.0.1 and virtual time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from web_data_extractor.cli import main
from web_data_extractor.config import ConfigError, parse_config
from web_data_extractor.fetch import (
    BLOCK_STREAK_LIMIT,
    PoliteFetcher,
    Politeness,
    looks_like_block_page,
)
from web_data_extractor.runner import run

OK = (200, {}, "<html><head><title>Catalog</title></head><body>ok</body></html>")
ALLOW_ALL = (200, {}, "User-agent: *\nDisallow:\n")


def make_fetcher(clock, **kwargs) -> PoliteFetcher:
    kwargs.setdefault("jitter", lambda: 0.0)
    return PoliteFetcher(
        kwargs.pop("politeness", Politeness()), sleep=clock.sleep, clock=clock.clock, **kwargs
    )


# -- Retry-After in full ----------------------------------------------------


def test_retry_after_http_date_is_honored(scripted, fake_clock):
    moment = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)
    header = {"Retry-After": format_datetime(moment + timedelta(seconds=20), usegmt=True)}
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (503, header, ""), OK)
    with make_fetcher(fake_clock, now=lambda: moment) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
    assert fake_clock.sleeps == [1.0, 20.0]


def test_retry_after_is_not_cut_down_to_the_backoff_step(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {"Retry-After": "55"}, ""), OK)
    politeness = Politeness(backoff_base_seconds=1.0, max_backoff_seconds=60.0)
    with make_fetcher(fake_clock, politeness=politeness) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
    assert fake_clock.sleeps == [1.0, 55.0]


def test_retry_after_above_the_cap_ends_the_job_for_that_host(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {"Retry-After": "300"}, ""), OK)
    scripted.add("/b", OK)
    with make_fetcher(fake_clock) as fetcher:
        first = fetcher.get(f"{scripted.base_url}/a")
        second = fetcher.get(f"{scripted.base_url}/b")
    assert first.refused == "host-stopped" and second.refused == "host-stopped"
    # One try, no early return, and nothing else sent to the host.
    assert scripted.count("/a") == 1 and scripted.count("/b") == 0
    assert fake_clock.sleeps == [1.0]
    assert "Retry-After of 300s" in next(iter(fetcher.stopped_hosts.values()))


# -- stop on block ----------------------------------------------------------


def test_repeated_429_ends_the_job_for_that_host(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {}, ""))
    scripted.add("/b", OK)
    with make_fetcher(fake_clock, politeness=Politeness(max_retries=6)) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").refused == "host-stopped"
        assert fetcher.get(f"{scripted.base_url}/b").refused == "host-stopped"
    assert scripted.count("/a") == BLOCK_STREAK_LIMIT
    assert scripted.count("/b") == 0
    assert any(e.note.startswith("stopped:") for e in fetcher.log)


def test_a_run_of_403s_ends_the_job_and_403_is_never_retried(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    for path in ("/a", "/b", "/c"):
        scripted.add(path, (403, {}, "forbidden"))
    scripted.add("/d", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").status == 403
        assert fetcher.get(f"{scripted.base_url}/b").status == 403
        assert fetcher.get(f"{scripted.base_url}/c").refused == "host-stopped"
        assert fetcher.get(f"{scripted.base_url}/d").refused == "host-stopped"
    assert [scripted.count(p) for p in ("/a", "/b", "/c", "/d")] == [1, 1, 1, 0]


def test_one_403_between_good_pages_does_not_end_the_job(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/ok", OK)
    scripted.add("/no", (403, {}, "forbidden"))
    with make_fetcher(fake_clock) as fetcher:
        for path in ("/no", "/ok", "/no", "/ok", "/no", "/ok"):
            fetcher.get(scripted.base_url + path)
    assert fetcher.stopped_hosts == {}
    assert scripted.count("/ok") == 3


@pytest.mark.parametrize(
    ("status", "title"),
    [
        (200, "Just a moment..."),
        (403, "Access Denied"),
        (200, "Security check: CAPTCHA"),
        (200, "Before you continue"),
        (503, "Attention Required! | Shield"),
    ],
)
def test_block_or_consent_page_ends_the_job_at_once(scripted, fake_clock, status, title):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (status, {}, f"<html><head><title>{title}</title></head></html>"))
    scripted.add("/b", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").refused == "host-stopped"
        assert fetcher.get(f"{scripted.base_url}/b").refused == "host-stopped"
    assert scripted.count("/a") == 1 and scripted.count("/b") == 0


def test_an_ordinary_page_is_not_mistaken_for_a_block_page():
    assert looks_like_block_page("<title>Catalog page 1</title><p>captcha</p>") == ""
    assert looks_like_block_page("<title>Robot Check</title>") == "Robot Check"


def test_block_page_is_not_written_to_the_cache(scripted, fake_clock, tmp_path):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (200, {}, "<title>Just a moment...</title>"))
    with make_fetcher(fake_clock, cache_dir=tmp_path) as fetcher:
        fetcher.get(f"{scripted.base_url}/a")
    cached = [p.read_text(encoding="utf-8") for p in tmp_path.rglob("*.json")]
    assert not any("Just a moment" in text for text in cached)


def test_blocked_job_is_reported_and_exits_non_zero(scripted, tmp_path, virtual_time, capsys):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/list", (200, {}, "<title>Access Denied</title>"))
    config = tmp_path / "job.yaml"
    config.write_text(
        f"start_urls: ['{scripted.base_url}/list']\ncache: {{enabled: false}}\n"
        "list: {item_selector: li, fields: {name: a}}\n",
        encoding="utf-8",
    )
    assert main(["run", str(config), "--out", str(tmp_path / "out")]) == 1
    printed = capsys.readouterr().out
    assert "run_health: attention: job ended early" in printed
    assert "block or consent page" in printed


# -- robots.txt freshness ---------------------------------------------------


def test_robots_is_fetched_again_after_24_hours_on_a_long_job(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL, (200, {}, "User-agent: *\nDisallow: /a\n"))
    scripted.add("/a", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
        fake_clock.now += 23 * 3600
        assert fetcher.get(f"{scripted.base_url}/a").ok
        assert scripted.count("/robots.txt") == 1
        fake_clock.now += 2 * 3600
        assert fetcher.get(f"{scripted.base_url}/a").refused == "robots"
    assert scripted.count("/robots.txt") == 2


def test_cached_robots_is_never_trusted_past_24_hours(scripted, fake_clock, tmp_path):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", OK)
    start = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)
    later = start + timedelta(hours=25)
    for moment in (start, later):
        with make_fetcher(
            fake_clock, cache_dir=tmp_path, cache_ttl_hours=720, now=lambda m=moment: m
        ) as fetcher:
            result = fetcher.get(f"{scripted.base_url}/a")
    # The page itself may stay cached for 30 days here. robots.txt may not.
    assert result.from_cache
    assert scripted.hits == ["/robots.txt", "/a", "/robots.txt"]


# -- terms of service record ------------------------------------------------


def test_terms_check_is_carried_into_the_summary_or_flagged_as_missing(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/list", (200, {}, "<ul><li><a>Lamp</a></li></ul>"))
    job = {
        "start_urls": [f"{scripted.base_url}/list"],
        "list": {"item_selector": "li", "fields": {"name": "a"}},
    }
    with make_fetcher(fake_clock) as fetcher:
        missing = dict(run(parse_config(job), fetcher=fetcher).summary)
    assert missing["terms_check"].startswith("not recorded")
    job["terms_check"] = {
        "url": "https://site.test/terms",
        "checked_on": "2026-01-05",
        "clause": "section 4 allows automated reading of public pages",
    }
    with make_fetcher(fake_clock) as fetcher:
        recorded = dict(run(parse_config(job), fetcher=fetcher).summary)
    assert "https://site.test/terms read on 2026-01-05" in recorded["terms_check"]
    assert recorded["run_health"] == "ok"
    job["terms_check"] = {"url": "https://site.test/terms"}
    with pytest.raises(ConfigError, match="terms_check needs"):
        parse_config(job)
