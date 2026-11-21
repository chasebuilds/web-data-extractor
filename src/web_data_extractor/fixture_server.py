"""A local static server for the bundled fixture site.

Used by `wde demo`, the tests and the benchmark. It records every path it
serves, which is how the tests prove a disallowed page was never requested.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@dataclass
class ServedSite:
    base_url: str
    requests: list[tuple[str, str]] = field(default_factory=list)  # (path, user-agent)
    arrivals: list[float] = field(default_factory=list)  # monotonic time of each request

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]


class _Handler(SimpleHTTPRequestHandler):
    site: ServedSite

    def do_GET(self) -> None:
        self.site.arrivals.append(time.monotonic())
        self.site.requests.append((self.path, self.headers.get("User-Agent", "")))
        super().do_GET()

    def log_message(self, *args: object) -> None:
        return


@contextmanager
def serve(directory: str | Path, port: int = 0) -> Iterator[ServedSite]:
    directory = Path(directory)
    if not (directory / "robots.txt").exists():
        raise FileNotFoundError(f"{directory} does not look like the fixture site.")
    site = ServedSite(base_url="")
    handler = type("Handler", (_Handler,), {"site": site})
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(handler, directory=str(directory)))
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
