"""The async fetch path: per-host concurrency, spacing, backoff, cache and robots.txt.

Every server here runs on 127.0.0.1 inside the test. Nothing touches the internet.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from web_data_extractor.afetch import AsyncPoliteFetcher
from web_data_extractor.config import ConfigError, load_config, parse_config
from web_data_extractor.fetch import (
    MAX_CONCURRENCY_CEILING,
    PoliteFetcher,
    Politeness,
    PolitenessError,
    utc_now,
)
from web_data_extractor.runner import run

REPO = Path(__file__).resolve().parent.parent
OK = (200, {}, "<html><body>ok</body></html>")
ALLOW_ALL = (200, {}, "User-agent: *\nDisallow:\n")


def make_async(clock, **kwargs) -> AsyncPoliteFetcher:
    """An async fetcher on virtual time: waits are recorded, not slept."""

    async def asleep(seconds: float) -> None:
        clock.sleep(seconds)
        await asyncio.sleep(0)

    kwargs.setdefault("jitter", lambda: 0.0)
    return AsyncPoliteFetcher(
        kwargs.pop("politeness", Politeness()), async_sleep=asleep, clock=clock.clock, **kwargs
    )


# -- retry and backoff ------------------------------------------------------


def test_429_then_200_honors_retry_after(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/page", (429, {"Retry-After": "3"}, "slow down"), OK)
    with make_async(fake_clock) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/page")
    assert result.status == 200
    assert scripted.count("/page") == 2
    attempts = [e for e in fetcher.log if e.url.endswith("/page")]
    assert [e.status for e in attempts] == [429, 200]
    assert "retrying after 3s" in attempts[0].note
    assert attempts[1].waited_seconds == 3.0
    assert fetcher.retries == 1


def test_5xx_backoff_doubles_each_attempt(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/page", (503, {}, "busy"), (503, {}, "busy"), OK)
    politeness = Politeness(min_delay_seconds=0.5, backoff_base_seconds=1.0)
    with make_async(fake_clock, politeness=politeness) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/page")
    assert result.status == 200
    waits = [e.waited_seconds for e in fetcher.log if e.url.endswith("/page")]
    assert waits == [0.5, 1.0, 2.0]


def test_a_retry_holds_back_other_requests_to_the_same_host(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {"Retry-After": "4"}, "slow down"), OK)
    scripted.add("/b", OK)
    with make_async(fake_clock) as fetcher:
        results = fetcher.get_many([f"{scripted.base_url}/a", f"{scripted.base_url}/b"])
    assert [r.status for r in results] == [200, 200]
    b_entry = next(e for e in fetcher.log if e.url.endswith("/b"))
    # /b was queued behind /a; /a's 429 pushed the whole host back by 4 seconds.
    assert b_entry.waited_seconds == 4.0


def test_over_long_retry_after_ends_the_job_for_the_host(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/a", (429, {"Retry-After": "3600"}, "come back later"))
    scripted.add("/b", OK)
    with make_async(fake_clock) as fetcher:
        first, second = fetcher.get_many([f"{scripted.base_url}/a", f"{scripted.base_url}/b"])
    assert first.refused == "host-stopped"
    assert second.refused == "host-stopped"
    assert scripted.count("/b") == 0
    assert fetcher.stopped_hosts


# -- per-host concurrency and spacing -------------------------------------


class SlowSite:
    """Answers every page after a pause and counts how many requests overlap."""

    def __init__(self, pause: float) -> None:
        self.pause = pause
        self.arrivals: list[float] = []
        self.in_flight = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.base_url = ""


@pytest.fixture
def slow_site() -> Iterator[SlowSite]:
    site = SlowSite(pause=0.8)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with site.lock:
                site.arrivals.append(time.monotonic())
                site.in_flight += 1
                site.peak = max(site.peak, site.in_flight)
            try:
                if self.path == "/robots.txt":
                    body = b"User-agent: *\nDisallow: /private/\n"
                else:
                    time.sleep(site.pause)
                    body = b"<html><body>ok</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with site.lock:
                    site.in_flight -= 1

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.daemon = True
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("limit", [1, 2])
def test_per_host_concurrency_limit_and_spacing_hold(slow_site, limit):
    politeness = Politeness(min_delay_seconds=0.5, max_concurrency_per_host=limit)
    urls = [f"{slow_site.base_url}/p{n}" for n in range(4)]
    with AsyncPoliteFetcher(politeness) as fetcher:
        results = fetcher.get_many(urls)
    assert all(r.ok for r in results)
    assert [r.url for r in results] == urls
    # Requests overlap up to the limit, and never beyond it, as seen by the server.
    assert slow_site.peak == limit
    assert fetcher.peak_in_flight[urlsplit_host(slow_site.base_url)] == limit
    gaps = [b - a for a, b in zip(slow_site.arrivals, slow_site.arrivals[1:], strict=False)]
    # Starts are spaced by the delay. A few ms of slack covers thread scheduling.
    assert min(gaps) >= 0.5 - 0.02


def urlsplit_host(url: str) -> str:
    return url.split("://", 1)[1]


def test_concurrency_limit_cannot_be_set_above_the_ceiling_or_below_one():
    with pytest.raises(PolitenessError):
        Politeness(max_concurrency_per_host=MAX_CONCURRENCY_CEILING + 1)
    with pytest.raises(PolitenessError):
        Politeness(max_concurrency_per_host=0)


def test_config_accepts_and_checks_the_concurrency_setting():
    base = {
        "name": "t",
        "start_urls": ["http://127.0.0.1:1/"],
        "list": {"item_selector": "li", "fields": {"name": "a"}},
    }
    config = parse_config({**base, "politeness": {"max_concurrency_per_host": 2}})
    assert config.politeness.max_concurrency_per_host == 2
    with pytest.raises(ConfigError):
        parse_config({**base, "politeness": {"max_concurrency_per_host": 9}})


# -- robots.txt -------------------------------------------------------------


def test_robots_is_checked_before_every_request_and_read_once(fixture_site, fake_clock):
    base = fixture_site.base_url
    urls = [
        f"{base}/catalog/page-1.html",
        f"{base}/private/clearance.html",
        f"{base}/item/sku-1001.html",
        f"{base}/private/sku-1055.html",
    ]
    politeness = Politeness(max_concurrency_per_host=3)
    with make_async(fake_clock, politeness=politeness) as fetcher:
        results = fetcher.get_many(urls)
    assert [r.refused for r in results] == ["", "robots", "", "robots"]
    assert not [p for p in fixture_site.paths if p.startswith("/private/")]
    assert fixture_site.paths.count("/robots.txt") == 1


def test_off_scope_host_is_refused_without_reading_its_robots(scripted, fake_clock):
    scripted.add("/robots.txt", ALLOW_ALL)
    with make_async(fake_clock, allowed_hosts={"example.invalid"}) as fetcher:
        result = fetcher.get(f"{scripted.base_url}/page")
    assert result.refused == "off-scope"
    assert scripted.hits == []


# -- disk cache -------------------------------------------------------------


def test_cache_serves_reruns_until_the_ttl_runs_out(scripted, fake_clock, tmp_path):
    scripted.add("/robots.txt", ALLOW_ALL)
    scripted.add("/page", OK)
    url = f"{scripted.base_url}/page"
    with make_async(fake_clock, cache_dir=tmp_path, cache_ttl_hours=1) as fetcher:
        first = fetcher.get(url)
    with make_async(fake_clock, cache_dir=tmp_path, cache_ttl_hours=1) as fetcher:
        second = fetcher.get(url)
        assert fetcher.network_requests == 0
    assert not first.from_cache and second.from_cache
    assert second.text == first.text
    assert scripted.count("/page") == 1

    later = utc_now() + timedelta(hours=2)
    with make_async(
        fake_clock, cache_dir=tmp_path, cache_ttl_hours=1, now=lambda: later
    ) as fetcher:
        third = fetcher.get(url)
    assert not third.from_cache
    assert scripted.count("/page") == 2


# -- the full pipeline ------------------------------------------------------


def test_pipeline_gives_the_same_result_on_the_async_path(fixture_site, fake_clock):
    config = load_config(REPO / "configs" / "fixture.yaml", {"base_url": fixture_site.base_url})
    config = replace(config, politeness=replace(config.politeness, max_concurrency_per_host=3))

    def fetcher(cls: type[PoliteFetcher]) -> PoliteFetcher:
        options = {"allowed_hosts": config.allowed_hosts, "max_requests": config.max_requests}
        if cls is AsyncPoliteFetcher:
            return make_async(fake_clock, politeness=config.politeness, **options)
        return PoliteFetcher(
            config.politeness, sleep=fake_clock.sleep, clock=fake_clock.clock, **options
        )

    with fetcher(PoliteFetcher) as plain:
        sequential = run(config, fetcher=plain)
    paths_before = len(fixture_site.paths)
    with fetcher(AsyncPoliteFetcher) as concurrent:
        overlapped = run(config, fetcher=concurrent)

    def strip(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in r.items() if "fetched_at" not in k} for r in rows]

    assert strip(overlapped.records) == strip(sequential.records)
    assert strip(overlapped.rejected) == strip(sequential.rejected)
    assert len(overlapped.records) == 60
    new_paths = fixture_site.paths[paths_before:]
    assert not [p for p in new_paths if p.startswith("/private/")]
    assert dict(overlapped.summary)["max_concurrency_per_host"] == 3
