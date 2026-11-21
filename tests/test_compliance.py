"""The tests that back the compliance claims in the README.

All of them run against servers on 127.0.0.1. Nothing here touches the internet.
"""

from __future__ import annotations

import time

import pytest

from web_data_extractor.config import ConfigError, parse_config
from web_data_extractor.fetch import (
    MIN_BACKOFF_FLOOR,
    MIN_DELAY_FLOOR,
    REPO_URL,
    TOOL_NAME,
    USER_AGENT,
    PoliteFetcher,
    Politeness,
    PolitenessError,
)

OK = (200, {}, "<html><body>ok</body></html>")
ALLOW_ALL = (200, {}, "User-agent: *\nDisallow:\n")


def make_fetcher(clock, **kwargs) -> PoliteFetcher:
    kwargs.setdefault("jitter", lambda: 0.0)
    return PoliteFetcher(
        kwargs.pop("politeness", Politeness()), sleep=clock.sleep, clock=clock.clock, **kwargs
    )


# -- robots.txt -----------------------------------------------------------


def test_disallowed_path_is_refused_and_never_requested(fixture_site, fake_clock):
    with make_fetcher(fake_clock) as fetcher:
        result = fetcher.get(f"{fixture_site.base_url}/private/clearance.html")
    assert result.refused == "robots"
    assert not result.ok and result.text == ""
    assert fixture_site.paths == ["/robots.txt"]
    entry = fetcher.log[-1]
    assert entry.robots == "disallowed" and entry.status is None


def test_allowed_path_is_fetched_after_robots(fixture_site, fake_clock):
    with make_fetcher(fake_clock) as fetcher:
        result = fetcher.get(f"{fixture_site.base_url}/catalog/page-1.html")
    assert result.ok
    assert fixture_site.paths == ["/robots.txt", "/catalog/page-1.html"]
    assert [e.robots for e in fetcher.log] == ["robots.txt", "allowed"]


def test_robots_is_checked_before_every_request_but_downloaded_once(scripted, fake_clock):
    scripted.add("/robots.txt", (200, {}, "User-agent: *\nDisallow: /no/\n"))
    scripted.add("/a", OK)
    scripted.add("/no/b", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
        assert fetcher.get(f"{scripted.base_url}/no/b").refused == "robots"
        assert fetcher.get(f"{scripted.base_url}/a").ok
    assert scripted.count("/robots.txt") == 1
    assert scripted.count("/no/b") == 0


def test_rules_for_our_agent_beat_the_wildcard_group(scripted, fake_clock):
    scripted.add(
        "/robots.txt",
        (200, {}, f"User-agent: *\nDisallow:\n\nUser-agent: {TOOL_NAME}\nDisallow: /\n"),
    )
    scripted.add("/a", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").refused == "robots"
    assert scripted.count("/a") == 0


@pytest.mark.parametrize("status", [500, 503, 401, 403])
def test_unreadable_robots_means_nothing_is_fetched(scripted, fake_clock, status):
    scripted.add("/robots.txt", (status, {}, ""))
    scripted.add("/a", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").refused == "robots"
    assert scripted.count("/a") == 0


def test_missing_robots_means_no_rules(scripted, fake_clock):
    scripted.add("/a", OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok


def test_redirect_into_a_disallowed_path_is_refused(scripted, fake_clock):
    scripted.add("/robots.txt", (200, {}, "User-agent: *\nDisallow: /private/\n"))
    scripted.add("/go", (302, {"Location": "/private/x"}, ""))
    scripted.add("/private/x", OK)
    with make_fetcher(fake_clock) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/go")
    assert result.refused == "robots"
    assert scripted.count("/private/x") == 0


def test_redirect_to_an_allowed_path_is_followed(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/go", (301, {"Location": "/there"}, ""))
    scripted.add("/there", OK)
    with make_fetcher(fake_clock) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/go")
    assert result.ok and result.final_url.endswith("/there")


def test_host_outside_allowed_hosts_is_refused(scripted, fake_clock):
    with make_fetcher(fake_clock, allowed_hosts={"example.invalid"}) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/a")
    assert result.refused == "off-scope"
    assert scripted.hits == []


# -- delay ----------------------------------------------------------------


def test_delay_is_enforced_between_requests_to_one_host(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    for path in ("/a", "/b", "/c"):
        scripted.add(path, OK)
    with make_fetcher(fake_clock, politeness=Politeness(min_delay_seconds=2.0)) as fetcher:
        for path in ("/a", "/b", "/c"):
            fetcher.get(scripted.base_url + path)
    # robots.txt is the first request to the host, so all three pages wait.
    assert fake_clock.sleeps == [2.0, 2.0, 2.0]
    assert [e.waited_seconds for e in fetcher.log] == [0.0, 2.0, 2.0, 2.0]


def test_delay_is_real_wall_clock_time_by_default(scripted):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", OK)
    with PoliteFetcher(Politeness(min_delay_seconds=MIN_DELAY_FLOOR)) as fetcher:
        started = time.monotonic()
        fetcher.get(f"{scripted.base_url}/a")
        elapsed = time.monotonic() - started
    assert elapsed >= MIN_DELAY_FLOOR


def test_crawl_delay_from_robots_is_honored_when_longer(scripted, fake_clock):
    scripted.add("/robots.txt", (200, {}, "User-agent: *\nCrawl-delay: 7\nDisallow:\n"))
    scripted.add("/a", OK)
    scripted.add("/b", OK)
    with make_fetcher(fake_clock) as fetcher:
        fetcher.get(f"{scripted.base_url}/a")
        fetcher.get(f"{scripted.base_url}/b")
    assert fake_clock.sleeps[-1] == 7.0


def test_crawl_delay_shorter_than_our_floor_does_not_lower_the_delay(scripted, fake_clock):
    scripted.add("/robots.txt", (200, {}, "User-agent: *\nCrawl-delay: 0\nDisallow:\n"))
    scripted.add("/a", OK)
    with make_fetcher(fake_clock) as fetcher:
        fetcher.get(f"{scripted.base_url}/a")
    assert fake_clock.sleeps == [1.0]


@pytest.mark.parametrize("delay", [0, 0.0, 0.1, -1, MIN_DELAY_FLOOR - 0.01, float("nan")])
def test_zero_or_tiny_delay_is_rejected(delay):
    with pytest.raises(PolitenessError, match="cannot be turned off"):
        Politeness(min_delay_seconds=delay)


@pytest.mark.parametrize("base", [0, 0.1, MIN_BACKOFF_FLOOR - 0.01])
def test_zero_backoff_is_rejected(base):
    with pytest.raises(PolitenessError, match="Backoff cannot be turned off"):
        Politeness(backoff_base_seconds=base)


def test_zero_delay_in_a_config_file_is_rejected():
    job = {
        "start_urls": ["http://127.0.0.1:1/"],
        "politeness": {"min_delay_seconds": 0},
        "list": {"item_selector": "li", "fields": {"name": "a"}},
    }
    with pytest.raises(ConfigError, match="min_delay_seconds must be at least"):
        parse_config(job)


# -- backoff --------------------------------------------------------------


def test_429_triggers_growing_backoff_then_succeeds(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {}, ""), (429, {}, ""), OK)
    politeness = Politeness(min_delay_seconds=1.0, backoff_base_seconds=2.0)
    with make_fetcher(fake_clock, politeness=politeness) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/a")
    assert result.ok
    assert scripted.count("/a") == 3
    # 1.0 after robots.txt, then 2s and 4s of backoff.
    assert fake_clock.sleeps == [1.0, 2.0, 4.0]
    assert fetcher.retries == 2
    assert [e.status for e in fetcher.log if e.kind == "page"] == [429, 429, 200]


def test_retry_after_header_is_honored(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {"Retry-After": "30"}, ""), OK)
    with make_fetcher(fake_clock) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
    assert fake_clock.sleeps == [1.0, 30.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_retried_then_given_up(scripted, fake_clock, status):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (status, {}, ""))
    politeness = Politeness(max_retries=2)
    with make_fetcher(fake_clock, politeness=politeness) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/a")
    assert not result.ok and result.status == status
    assert scripted.count("/a") == 3
    assert "gave up" in fetcher.log[-1].note


def test_404_is_not_retried(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    with make_fetcher(fake_clock) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/missing")
    assert result.status == 404
    assert scripted.count("/missing") == 1


def test_request_cap_stops_the_run(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", OK)
    scripted.add("/b", OK)
    with make_fetcher(fake_clock, max_requests=2) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").ok
        assert fetcher.get(f"{scripted.base_url}/b").refused == "request-cap"
    assert scripted.count("/b") == 0


# -- cache ----------------------------------------------------------------


def test_cache_hit_makes_no_request_and_keeps_the_original_fetch_time(
    scripted, fake_clock, tmp_path
):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", OK)
    with make_fetcher(fake_clock, cache_dir=tmp_path) as first:
        fresh = first.get(f"{scripted.base_url}/a")
    with make_fetcher(fake_clock, cache_dir=tmp_path) as second:
        cached = second.get(f"{scripted.base_url}/a")
    assert not fresh.from_cache and cached.from_cache
    assert cached.text == fresh.text and cached.fetched_at == fresh.fetched_at
    assert scripted.hits == ["/robots.txt", "/a"]
    assert [e.cache_hit for e in second.log] == [True, True]


def test_robots_still_applies_to_cached_pages(scripted, fake_clock, tmp_path):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", OK)
    with make_fetcher(fake_clock, cache_dir=tmp_path) as first:
        assert first.get(f"{scripted.base_url}/a").ok
    # The site tightens its rules and the cached robots.txt expires.
    scripted.add("/robots.txt", (200, {}, "User-agent: *\nDisallow: /\n"))
    for path in tmp_path.rglob("*.json"):
        if "robots.txt" in path.read_text(encoding="utf-8"):
            path.unlink()
    with make_fetcher(fake_clock, cache_dir=tmp_path) as second:
        assert second.get(f"{scripted.base_url}/a").refused == "robots"


def test_error_responses_are_not_cached(scripted, fake_clock, tmp_path):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (404, {}, "gone"), OK)
    with make_fetcher(fake_clock, cache_dir=tmp_path) as fetcher:
        assert fetcher.get(f"{scripted.base_url}/a").status == 404
        assert fetcher.get(f"{scripted.base_url}/a").ok


# -- identity -------------------------------------------------------------


def test_user_agent_names_the_tool_and_repo_and_nothing_personal(fixture_site, fake_clock):
    with make_fetcher(fake_clock) as fetcher:
        fetcher.get(f"{fixture_site.base_url}/index.html")
    sent = {agent for _, agent in fixture_site.requests}
    assert sent == {USER_AGENT}
    assert TOOL_NAME in USER_AGENT and REPO_URL in USER_AGENT
    assert "@" not in USER_AGENT
    assert "Mozilla" not in USER_AGENT
