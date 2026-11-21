"""The async fetch path: several requests in flight, the same rules as PoliteFetcher.

AsyncPoliteFetcher reuses every decision PoliteFetcher makes (scope, robots.txt,
cache, retry, Retry-After, stop rules, the log). What it adds:

- up to `max_concurrency_per_host` requests to one host at the same time;
- the minimum delay is kept between the *start* of any two requests to a host,
  so overlapping requests never arrive closer together than the delay;
- a retry on one request (429, 5xx, Retry-After) holds back every request to
  that host, not only the one that failed;
- robots.txt is read once per host even when many tasks ask for it together.

Use `await aget(...)` / `await aget_many(...)` from async code, or the plain
`get(...)` / `get_many(...)`, which run the same coroutines on a private loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar
from urllib.parse import urldefrag, urlsplit

import httpx

from .fetch import MAX_REDIRECTS, FetchResult, PoliteFetcher, Politeness

T = TypeVar("T")


class AsyncPoliteFetcher(PoliteFetcher):
    def __init__(
        self,
        politeness: Politeness | None = None,
        *,
        async_sleep: Callable[[float], Awaitable[None]] | None = None,
        **kwargs: Any,
    ):
        super().__init__(politeness, **kwargs)
        self._async_sleep = async_sleep or asyncio.sleep
        self._async_client: httpx.AsyncClient | None = None
        self._loop_runner: asyncio.Runner | None = None
        self._slots: dict[str, asyncio.Semaphore] = {}
        self._spacing: dict[str, asyncio.Lock] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        # host -> monotonic time before which no request may start (set by a retry).
        self._hold_until: dict[str, float] = {}
        self._in_flight: dict[str, int] = {}
        # host -> most requests that were in flight together. Shows the limit held.
        self.peak_in_flight: dict[str, int] = {}

    # -- sync facade ----------------------------------------------------

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._loop_runner is None:
            self._loop_runner = asyncio.Runner()
        return self._loop_runner.run(coro)

    def get(self, url: str) -> FetchResult:
        return self._run(self.aget(url))

    def get_many(self, urls: list[str]) -> list[FetchResult]:
        return self._run(self.aget_many(urls))

    def close(self) -> None:
        if self._loop_runner is not None:
            self._loop_runner.run(self.aclose())
            self._loop_runner.close()
            self._loop_runner = None
        super().close()

    # -- async API ------------------------------------------------------

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
            self._async_client = None

    async def __aenter__(self) -> AsyncPoliteFetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
        super().close()

    async def aget_many(self, urls: list[str]) -> list[FetchResult]:
        """Fetch all URLs, overlapping up to the per-host limit. Results keep input order."""
        return list(await asyncio.gather(*(self.aget(url) for url in urls)))

    async def aget(self, url: str) -> FetchResult:
        original = current = urldefrag(url).url
        for _ in range(MAX_REDIRECTS + 1):
            # robots.txt is loaded first, so the shared refusal check below never
            # has to fetch it with the blocking client.
            await self._ensure_robots(current)
            early = self._before_request(original, current)
            if early:
                return early
            response, error = await self._arequest(current, "page", "allowed")
            outcome = self._after_response(original, current, response, error)
            if isinstance(outcome, FetchResult):
                return outcome
            current = outcome
        return self._too_many_redirects(original, current)

    # -- internals ------------------------------------------------------

    def _aclient(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(**self._client_options())
        return self._async_client

    async def _ensure_robots(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return
        if self.allowed_hosts is not None and parts.netloc.lower() not in self.allowed_hosts:
            return  # refused as off-scope; its robots.txt is never requested
        if parts.netloc in self.stopped_hosts:
            return
        origin = f"{parts.scheme}://{parts.netloc}"
        if self._robots_is_fresh(origin):
            return
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            if self._robots_is_fresh(origin):
                return  # another task loaded it while this one waited
            robots_url = f"{origin}/robots.txt"
            cached = self._cached_robots(robots_url)
            if cached is None:
                response, _ = await self._arequest(robots_url, "robots", "robots.txt")
                cached = self._store_robots(robots_url, response)
            self._robots[origin] = (self._robots_policy(robots_url, *cached), self._clock())

    async def _arequest(
        self, url: str, kind: str, robots_label: str
    ) -> tuple[httpx.Response | None, str]:
        host = urlsplit(url).netloc
        delay = self.host_delays.get(host, self.politeness.min_delay_seconds)
        slot = self._slots.setdefault(
            host, asyncio.Semaphore(self.politeness.max_concurrency_per_host)
        )
        response: httpx.Response | None = None
        error = ""
        for attempt in range(1, self.politeness.max_retries + 2):
            async with slot:
                outcome = await self._send_spaced(url, kind, robots_label, attempt, delay)
            if isinstance(outcome, str):
                return None, outcome
            response, error, waited = outcome
            verdict = self._judge(url, kind, robots_label, attempt, waited, response, error, delay)
            if verdict.stopped:
                return (response if kind == "robots" else None), verdict.stopped
            if verdict.retry_gap is None:
                break
            # The whole host backs off, not just this request.
            self._hold_until[host] = max(
                self._hold_until.get(host, float("-inf")), self._clock() + verdict.retry_gap
            )
        return response, error

    async def _send_spaced(
        self, url: str, kind: str, robots_label: str, attempt: int, gap: float
    ) -> tuple[httpx.Response | None, str, float] | str:
        """Send one request no sooner than `gap` after the last one to the host went out.

        The spacing lock is held until the request headers are actually written, not
        just until the request is handed to httpx. Opening a new connection takes
        time, and a start time taken before that would let the next request (on an
        already open connection) reach the server sooner than the delay.
        """
        host = urlsplit(url).netloc
        spacing = self._spacing.setdefault(host, asyncio.Lock())
        await spacing.acquire()
        held = True

        def mark_sent(sent: bool = True) -> None:
            nonlocal held
            if held:
                if sent:
                    self._last_request[host] = self._clock()
                spacing.release()
                held = False

        async def trace(event: str, info: dict[str, Any]) -> None:
            if event.endswith("send_request_headers.started"):
                mark_sent()

        try:
            last = self._last_request.get(host)
            target = max(
                last + gap if last is not None else float("-inf"),
                self._hold_until.get(host, float("-inf")),
            )
            waited = 0.0
            while (remaining := target - self._clock()) > 0:
                await self._async_sleep(remaining)
                waited += remaining
            if host in self.stopped_hosts:
                # Another request ended the job for this host while this one waited.
                reason = f"job ended for this host ({self.stopped_hosts[host]})"
                self._record(
                    url, kind, None, False, robots_label, attempt, waited, f"refused: {reason}"
                )
                mark_sent(sent=False)
                return reason
            if self._at_request_cap(url, kind, robots_label, attempt):
                mark_sent(sent=False)
                return "request cap reached"
            self.network_requests += 1
            self._in_flight[host] = self._in_flight.get(host, 0) + 1
            self.peak_in_flight[host] = max(self.peak_in_flight.get(host, 0), self._in_flight[host])
            try:
                response = await self._aclient().get(
                    url, follow_redirects=kind == "robots", extensions={"trace": trace}
                )
                return response, "", waited
            except httpx.HTTPError as exc:
                return None, f"{type(exc).__name__}: {exc}", waited
            finally:
                self._in_flight[host] -= 1
        finally:
            mark_sent()
