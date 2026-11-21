from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_data_extractor.fixture_server import ServedSite, serve

REPO = Path(__file__).resolve().parent.parent
FIXTURE_SITE = REPO / "fixture_site"


class FakeClock:
    """Virtual time, so delay and backoff tests assert exact waits without really waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def virtual_time(monkeypatch, fake_clock) -> FakeClock:
    """Put the command line path on virtual time, where no fetcher can be passed in."""
    import web_data_extractor.fetch as fetch_module

    stand_in = SimpleNamespace(sleep=fake_clock.sleep, monotonic=fake_clock.clock)
    monkeypatch.setattr(fetch_module, "time", stand_in)
    return fake_clock


@pytest.fixture
def fixture_site() -> Iterator[ServedSite]:
    with serve(FIXTURE_SITE) as site:
        yield site


class ScriptedSite:
    """A tiny server whose answers are scripted per path.

    routes[path] is a list of (status, headers, body). Each request pops the
    next answer; the last one repeats.
    """

    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[int, dict[str, str], str]]] = {}
        self.hits: list[str] = []
        self.base_url = ""

    def add(self, path: str, *answers: tuple[int, dict[str, str], str]) -> None:
        self.routes[path] = list(answers)

    def count(self, path: str) -> int:
        return self.hits.count(path)


@pytest.fixture
def scripted() -> Iterator[ScriptedSite]:
    site = ScriptedSite()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.hits.append(self.path)
            answers = site.routes.get(self.path)
            if not answers:
                status, headers, body = 404, {}, "not found"
            else:
                status, headers, body = answers.pop(0) if len(answers) > 1 else answers[0]
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
