"""The only place this package touches the network.

Every request goes through PoliteFetcher.get, which checks robots.txt,
waits out the per-host delay, retries with backoff, and records what it did
in a log that ends up in the RunLog sheet.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypedDict, cast
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from . import __version__
from .robots import RobotsPolicy

TOOL_NAME = "web-data-extractor"
REPO_URL = "https://github.com/chasebuilds/web-data-extractor"
USER_AGENT = f"{TOOL_NAME}/{__version__} (+{REPO_URL})"

# Hard floors. Configuration can raise these values, never lower them.
MIN_DELAY_FLOOR = 0.5
MIN_BACKOFF_FLOOR = 0.5
MAX_RETRIES_CEILING = 6
# Requests in flight to one host at once (async path). Spacing still applies to each start.
MAX_CONCURRENCY_CEILING = 4
# A host asking for more than this between requests is skipped, not ignored.
MAX_HONORED_CRAWL_DELAY = 300.0

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5

# A site that keeps answering 403 or 429 is saying no. After this many in a row the
# job ends for that host. There is no way around it in this tool, by design.
BLOCK_STATUSES = frozenset({403, 429})
BLOCK_STREAK_LIMIT = 3
# robots.txt is never trusted for longer than this, in memory or from the disk cache.
ROBOTS_MAX_AGE_HOURS = 24.0
# Backoff waits are stretched by up to this share, so retries from several jobs do
# not line up. Jitter only ever adds time; it cannot take a wait below its floor.
JITTER_FRACTION = 0.25
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BLOCK_TITLE = re.compile(
    r"captcha|are you a (robot|human)|robot check|access denied|attention required"
    r"|just a moment|verify you are human|unusual traffic|pardon our interruption"
    r"|before you continue|request blocked",
    re.IGNORECASE,
)


def block_wording(text: str) -> str:
    """Return the block or consent phrase found in visible page text, if any."""
    match = _BLOCK_TITLE.search(text[:5000])
    return match.group(0) if match else ""


def looks_like_block_page(text: str) -> str:
    """Return the page title if it reads like a block, challenge or consent wall."""
    match = _TITLE.search(text[:20000])
    title = " ".join(match.group(1).split()) if match else ""
    return title if _BLOCK_TITLE.search(title) else ""


class PolitenessError(ValueError):
    """Raised when settings would make the fetcher less polite than the floors."""


@dataclass(frozen=True)
class Politeness:
    min_delay_seconds: float = 1.0
    backoff_base_seconds: float = 1.0
    max_backoff_seconds: float = 60.0
    max_retries: int = 3
    timeout_seconds: float = 20.0
    max_concurrency_per_host: int = 1

    def __post_init__(self) -> None:
        if not self.min_delay_seconds >= MIN_DELAY_FLOOR:
            raise PolitenessError(
                f"min_delay_seconds must be at least {MIN_DELAY_FLOOR} "
                f"(got {self.min_delay_seconds}). The delay cannot be turned off."
            )
        if not self.backoff_base_seconds >= MIN_BACKOFF_FLOOR:
            raise PolitenessError(
                f"backoff_base_seconds must be at least {MIN_BACKOFF_FLOOR} "
                f"(got {self.backoff_base_seconds}). Backoff cannot be turned off."
            )
        if self.max_backoff_seconds < self.backoff_base_seconds:
            raise PolitenessError("max_backoff_seconds must not be below backoff_base_seconds.")
        if not 0 <= self.max_retries <= MAX_RETRIES_CEILING:
            raise PolitenessError(f"max_retries must be between 0 and {MAX_RETRIES_CEILING}.")
        if not self.timeout_seconds > 0:
            raise PolitenessError("timeout_seconds must be above 0.")
        if not 1 <= self.max_concurrency_per_host <= MAX_CONCURRENCY_CEILING:
            raise PolitenessError(
                f"max_concurrency_per_host must be between 1 and {MAX_CONCURRENCY_CEILING}."
            )


@dataclass
class LogEntry:
    seq: int
    url: str
    kind: str  # "robots" or "page"
    status: int | None
    cache_hit: bool
    robots: str  # "allowed", "disallowed", "robots.txt", or "not checked"
    attempt: int
    waited_seconds: float
    fetched_at: str
    note: str = ""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    text: str
    from_cache: bool
    fetched_at: str
    refused: str = ""  # why no request was made: "robots", "off-scope", "request-cap", ...
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.refused

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest() if self.ok else ""


@dataclass(frozen=True)
class _Verdict:
    stopped: str = ""  # non-empty: the job ended for this host
    retry_gap: float | None = None  # set: try again after this many seconds


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CacheEntry(TypedDict):
    url: str
    final_url: str
    status: int
    fetched_at: str
    text: str


class _DiskCache:
    def __init__(self, directory: Path, ttl_hours: float, now: Callable[[], datetime]):
        self._dir = directory
        self._ttl = timedelta(hours=ttl_hours)
        self._now = now

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self._dir / digest[:2] / f"{digest}.json"

    def get(self, url: str, max_age: timedelta | None = None) -> CacheEntry | None:
        path = self._path(url)
        if not path.exists():
            return None
        try:
            entry = cast(CacheEntry, json.loads(path.read_text(encoding="utf-8")))
            fetched = datetime.strptime(entry["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except (ValueError, KeyError, OSError):
            return None
        limit = min(self._ttl, max_age) if max_age else self._ttl
        return entry if self._now() - fetched <= limit else None

    def forget(self, url: str) -> None:
        self._path(url).unlink(missing_ok=True)

    def put(self, url: str, *, final_url: str, status: int, text: str, fetched_at: str) -> None:
        path = self._path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry: CacheEntry = {
            "url": url,
            "final_url": final_url,
            "status": status,
            "fetched_at": fetched_at,
            "text": text,
        }
        path.write_text(json.dumps(entry), encoding="utf-8")


class PoliteFetcher:
    """HTTP GET with robots.txt checks, per-host delay, backoff and a disk cache.

    `sleep` and `clock` exist so tests can run on virtual time. They are not
    reachable from a config file, and the floors in Politeness apply either way.
    """

    def __init__(
        self,
        politeness: Politeness | None = None,
        *,
        cache_dir: str | Path | None = None,
        cache_ttl_hours: float = 24.0,
        allowed_hosts: set[str] | None = None,
        max_requests: int = 500,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] = utc_now,
        jitter: Callable[[], float] | None = None,
    ):
        self.politeness = politeness or Politeness()
        self.allowed_hosts = allowed_hosts
        self.max_requests = max_requests
        self.log: list[LogEntry] = []
        self.network_requests = 0
        self.retries = 0
        self.host_delays: dict[str, float] = {}
        # host -> why the job ended there. Nothing more is requested from these hosts.
        self.stopped_hosts: dict[str, str] = {}
        self._block_streak: dict[str, int] = {}
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._now = now
        self._jitter = jitter or random.random
        self._cache = _DiskCache(Path(cache_dir), cache_ttl_hours, now) if cache_dir else None
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, tuple[RobotsPolicy, float]] = {}
        self._sync_client: httpx.Client | None = None

    def _client_options(self) -> dict[str, Any]:
        return {
            "headers": {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            "timeout": self.politeness.timeout_seconds,
            "follow_redirects": False,
        }

    @property
    def _client(self) -> httpx.Client:
        if self._sync_client is None:
            self._sync_client = httpx.Client(**self._client_options())
        return self._sync_client

    def close(self) -> None:
        if self._sync_client is not None:
            self._sync_client.close()

    def __enter__(self) -> PoliteFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- public ---------------------------------------------------------

    def get(self, url: str) -> FetchResult:
        original = current = urldefrag(url).url
        for _ in range(MAX_REDIRECTS + 1):
            early = self._before_request(original, current)
            if early:
                return early
            response, error = self._request(current, "page", "allowed")
            outcome = self._after_response(original, current, response, error)
            if isinstance(outcome, FetchResult):
                return outcome
            current = outcome
        return self._too_many_redirects(original, current)

    def get_many(self, urls: list[str]) -> list[FetchResult]:
        """Fetch several URLs. This class goes one at a time; AsyncPoliteFetcher overlaps them."""
        return [self.get(url) for url in urls]

    def stop_host(self, url: str, reason: str) -> None:
        """End the job for the host of `url`, and drop that page from the cache."""
        self.stopped_hosts[urlsplit(url).netloc] = reason
        if self._cache:
            self._cache.forget(url)

    def robots_for(self, url: str) -> RobotsPolicy:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        # A long job re-reads robots.txt so a rule change is picked up within a day.
        if not self._robots_is_fresh(origin):
            self._robots[origin] = (self._load_robots(origin), self._clock())
        return self._robots[origin][0]

    # -- internals ------------------------------------------------------
    # The decisions live in plain methods shared with AsyncPoliteFetcher, so both
    # paths refuse, cache, retry and stop by exactly the same rules.

    def _before_request(self, original: str, current: str) -> FetchResult | None:
        """A refusal or a cache hit, or None when a request is needed."""
        refusal = self._refusal(current)
        if refusal:
            reason, robots_label, note = refusal
            self._record(current, "page", None, False, robots_label, 0, 0.0, note)
            return FetchResult(original, current, None, "", False, iso(self._now()), reason)
        cached = self._cache.get(current) if self._cache else None
        if cached:
            self._record(current, "page", cached["status"], True, "allowed", 0, 0.0, "")
            return FetchResult(
                original,
                cached["final_url"],
                cached["status"],
                cached["text"],
                True,
                cached["fetched_at"],
            )
        return None

    def _after_response(
        self, original: str, current: str, response: httpx.Response | None, error: str
    ) -> FetchResult | str:
        """The result, or the next URL when the answer was a redirect."""
        fetched_at = iso(self._now())
        if response is None:
            refused = ""
            if error == "request cap reached":
                refused = "request-cap"
            elif urlsplit(current).netloc in self.stopped_hosts:
                refused = "host-stopped"
            return FetchResult(
                original,
                current,
                None,
                "",
                False,
                fetched_at,
                refused=refused,
                error="" if refused else error,
            )
        if response.status_code in REDIRECT_STATUSES and response.headers.get("location"):
            # Each hop goes back through the scope and robots checks.
            return urldefrag(urljoin(current, response.headers["location"])).url
        text = response.text
        if response.status_code == 200 and self._cache:
            self._cache.put(
                current, final_url=current, status=200, text=text, fetched_at=fetched_at
            )
        return FetchResult(original, current, response.status_code, text, False, fetched_at)

    def _too_many_redirects(self, original: str, current: str) -> FetchResult:
        return FetchResult(
            original, current, None, "", False, iso(self._now()), error="too many redirects"
        )

    def _refusal(self, url: str) -> tuple[str, str, str] | None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return "bad-url", "not checked", "refused: not an http(s) URL"
        if self.allowed_hosts is not None and parts.netloc.lower() not in self.allowed_hosts:
            return "off-scope", "not checked", "refused: host is not in allowed_hosts"
        if parts.netloc in self.stopped_hosts:
            reason = self.stopped_hosts[parts.netloc]
            return "host-stopped", "not checked", f"refused: job ended for this host ({reason})"
        policy = self.robots_for(url)
        if not policy.can_fetch(TOOL_NAME, url):
            return "robots", "disallowed", f"refused: disallowed by robots.txt ({policy.source})"
        if self._delay_for(parts.netloc, policy) > MAX_HONORED_CRAWL_DELAY:
            return (
                "crawl-delay",
                "allowed",
                f"refused: robots.txt asks for a crawl-delay above {MAX_HONORED_CRAWL_DELAY:.0f}s",
            )
        return None

    def _delay_for(self, host: str, policy: RobotsPolicy) -> float:
        delay = max(self.politeness.min_delay_seconds, policy.crawl_delay(TOOL_NAME) or 0.0)
        self.host_delays[host] = delay
        return delay

    def _robots_is_fresh(self, origin: str) -> bool:
        known = self._robots.get(origin)
        return known is not None and self._clock() - known[1] <= ROBOTS_MAX_AGE_HOURS * 3600

    def _load_robots(self, origin: str) -> RobotsPolicy:
        url = f"{origin}/robots.txt"
        cached = self._cached_robots(url)
        if cached:
            return self._robots_policy(url, *cached)
        response, _ = self._request(url, "robots", "robots.txt")
        return self._robots_policy(url, *self._store_robots(url, response))

    def _cached_robots(self, url: str) -> tuple[int | None, str] | None:
        max_age = timedelta(hours=ROBOTS_MAX_AGE_HOURS)
        cached = self._cache.get(url, max_age) if self._cache else None
        if not cached:
            return None
        self._record(url, "robots", cached["status"], True, "robots.txt", 0, 0.0, "")
        return cached["status"], cached["text"]

    def _store_robots(self, url: str, response: httpx.Response | None) -> tuple[int | None, str]:
        status = response.status_code if response is not None else None
        text = response.text if response is not None and status == 200 else ""
        if status is not None and status < 500 and self._cache:
            self._cache.put(
                url, final_url=url, status=status, text=text, fetched_at=iso(self._now())
            )
        return status, text

    @staticmethod
    def _robots_policy(url: str, status: int | None, text: str) -> RobotsPolicy:
        if status == 200:
            return RobotsPolicy.parse(text, source=url)
        if status in (401, 403):
            return RobotsPolicy.deny_all(f"{url} returned {status}, treated as disallow all")
        if status is not None and 400 <= status < 500:
            return RobotsPolicy.allow_all(f"{url} returned {status}, no rules published")
        # Server error or no answer: RFC 9309 says assume complete disallow.
        return RobotsPolicy.deny_all(f"{url} unreachable ({status}), treated as disallow all")

    def _wait(self, host: str, required_gap: float) -> float:
        last = self._last_request.get(host)
        if last is None:
            return 0.0
        waited = 0.0
        # Loop because a sleep may return a little early on a coarse system timer.
        while (remaining := last + required_gap - self._clock()) > 0:
            self._sleep(remaining)
            waited += remaining
        return waited

    def _request(self, url: str, kind: str, robots_label: str) -> tuple[httpx.Response | None, str]:
        host = urlsplit(url).netloc
        # robots.txt is fetched before the host's own crawl-delay is known.
        delay = self.host_delays.get(host, self.politeness.min_delay_seconds)
        gap = delay
        response: httpx.Response | None = None
        error = ""
        for attempt in range(1, self.politeness.max_retries + 2):
            if self._at_request_cap(url, kind, robots_label, attempt):
                return None, "request cap reached"
            waited = self._wait(host, gap)
            self.network_requests += 1
            try:
                response = self._client.get(url, follow_redirects=kind == "robots")
                error = ""
            except httpx.HTTPError as exc:
                response, error = None, f"{type(exc).__name__}: {exc}"
            self._last_request[host] = self._clock()
            verdict = self._judge(url, kind, robots_label, attempt, waited, response, error, delay)
            if verdict.stopped:
                return (response if kind == "robots" else None), verdict.stopped
            if verdict.retry_gap is None:
                break
            gap = verdict.retry_gap
        return response, error

    def _at_request_cap(self, url: str, kind: str, robots_label: str, attempt: int) -> bool:
        if self.network_requests < self.max_requests:
            return False
        self._record(url, kind, None, False, robots_label, attempt, 0.0, "refused: request cap")
        return True

    def _judge(
        self,
        url: str,
        kind: str,
        robots_label: str,
        attempt: int,
        waited: float,
        response: httpx.Response | None,
        error: str,
        delay: float,
    ) -> _Verdict:
        """Log one attempt and decide: stop the host, retry after a gap, or finish."""
        host = urlsplit(url).netloc
        status = response.status_code if response is not None else None
        retryable = status is None or status in RETRY_STATUSES
        last_attempt = attempt > self.politeness.max_retries
        note = error
        retry_after = self._retry_after_seconds(response) if retryable else None
        stop = self._stop_reason(host, kind, status, response, retry_after)
        if stop:
            self.stopped_hosts[host] = stop
            self._record(
                url, kind, status, False, robots_label, attempt, waited, f"stopped: {stop}"
            )
            return _Verdict(stopped=f"job ended for this host ({stop})")
        gap = None
        if retryable and not last_attempt:
            backoff = min(
                self.politeness.max_backoff_seconds,
                self.politeness.backoff_base_seconds * 2 ** (attempt - 1),
            ) * (1 + JITTER_FRACTION * self._jitter())
            # Retry-After is honored in full. A value above the cap never gets
            # here: _stop_reason has already ended the job for the host.
            gap = max(delay, backoff, retry_after or 0.0)
            note = (note + "; " if note else "") + f"retrying after {gap:g}s backoff"
            self.retries += 1
        elif retryable:
            note = (note + "; " if note else "") + "gave up after retries"
        self._record(url, kind, status, False, robots_label, attempt, waited, note)
        return _Verdict(retry_gap=gap)

    def _stop_reason(
        self,
        host: str,
        kind: str,
        status: int | None,
        response: httpx.Response | None,
        retry_after: float | None,
    ) -> str:
        if retry_after is not None and retry_after > self.politeness.max_backoff_seconds:
            return (
                f"Retry-After of {retry_after:.0f}s is above the "
                f"{self.politeness.max_backoff_seconds:g}s cap"
            )
        if status in BLOCK_STATUSES:
            self._block_streak[host] = self._block_streak.get(host, 0) + 1
            if self._block_streak[host] >= BLOCK_STREAK_LIMIT:
                return f"{BLOCK_STREAK_LIMIT} responses of 403 or 429 in a row"
        elif status == 200:
            self._block_streak[host] = 0
        if kind == "page" and response is not None and status in (200, 403, 429, 503):
            title = looks_like_block_page(response.text)
            if title:
                return f"block or consent page ({title!r})"
        return ""

    def _retry_after_seconds(self, response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        value = response.headers.get("retry-after", "").strip()
        if value.isdigit():
            return float(value)
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - self._now()).total_seconds())

    def _record(
        self,
        url: str,
        kind: str,
        status: int | None,
        cache_hit: bool,
        robots: str,
        attempt: int,
        waited: float,
        note: str,
    ) -> None:
        self.log.append(
            LogEntry(
                seq=len(self.log) + 1,
                url=url,
                kind=kind,
                status=status,
                cache_hit=cache_hit,
                robots=robots,
                attempt=attempt,
                waited_seconds=round(waited, 3),
                fetched_at=iso(self._now()),
                note=note,
            )
        )
