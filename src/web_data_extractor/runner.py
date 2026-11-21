"""The crawl: list pages, optional detail pages, validation, duplicate removal."""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin

from . import __version__
from .afetch import AsyncPoliteFetcher
from .config import Config, ConfigError
from .extract import extract_fields, parse_html
from .fetch import (
    USER_AGENT,
    FetchResult,
    LogEntry,
    PoliteFetcher,
    block_wording,
    iso,
    utc_now,
)

NOT_FOUND = "not found on the page"


@dataclass
class _Row:
    values: dict[str, Any]
    raws: dict[str, str]
    problems: dict[str, str]
    source_url: str
    fetched_at: str
    source_sha256: str = ""
    detail_page_url: str = ""
    detail_fetched_at: str = ""
    detail_sha256: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    config: Config
    records: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    log: list[LogEntry]
    summary: list[tuple[str, Any]]

    @property
    def meta_columns(self) -> list[str]:
        detail = (
            ["detail_page_url", "detail_fetched_at", "detail_sha256"]
            if self.config.detail_fields
            else []
        )
        return ["source_url", "fetched_at", "source_sha256", *detail]

    @property
    def record_columns(self) -> list[str]:
        return [*(f.name for f in self.config.fields), *self.meta_columns, "warnings"]

    @property
    def rejected_columns(self) -> list[str]:
        return ["reason", *(f.name for f in self.config.fields), *self.meta_columns]


def _fetch_problem(result: FetchResult) -> str:
    if result.refused == "robots":
        return "disallowed by robots.txt"
    if result.refused == "host-stopped":
        return "not fetched (the job ended for this host, see Summary)"
    if result.refused:
        return f"not fetched ({result.refused})"
    if result.error:
        return f"fetch failed ({result.error})"
    return f"returned HTTP {result.status}"


def _key(row: _Row, names: list[str]) -> tuple | None:
    key = tuple(row.values.get(n) for n in names)
    return None if any(part is None for part in key) else key


def _mark_duplicates(rows: list[_Row], names: list[str]) -> int:
    first_seen: dict[tuple, _Row] = {}
    count = 0
    for row in rows:
        if row.reasons:
            continue
        key = _key(row, names)
        if key is None:
            continue
        if key in first_seen:
            label = ", ".join(f"{n}={v}" for n, v in zip(names, key, strict=True))
            row.reasons.append(
                f"duplicate of an earlier row ({label}), first seen on {first_seen[key].source_url}"
            )
            count += 1
        else:
            first_seen[key] = row
    return count


def _required_failures(row: _Row, specs: list) -> list[str]:
    return [
        f"{s.name}: {row.problems[s.name]}" for s in specs if s.required and s.name in row.problems
    ]


def run(config: Config, *, fetcher: PoliteFetcher | None = None) -> RunResult:
    unchecked = config.hosts_needing_terms_check
    if unchecked:
        raise ConfigError(
            f"no terms_check recorded for {', '.join(unchecked)}. A person reads the site's "
            "terms of use first, then records url, checked_on and clause in the config."
        )
    started = utc_now()
    clock_start = time.monotonic()
    owns_fetcher = fetcher is None
    # More than one request in flight per host needs the async path; one is plain sequential.
    fetcher_class = (
        AsyncPoliteFetcher if config.politeness.max_concurrency_per_host > 1 else PoliteFetcher
    )
    fetcher = fetcher or fetcher_class(
        config.politeness,
        cache_dir=config.cache_dir,
        cache_ttl_hours=config.cache_ttl_hours,
        allowed_hosts=config.allowed_hosts,
        max_requests=config.max_requests,
    )
    try:
        rows, list_pages, notes = _crawl_lists(config, fetcher)
        list_names = [f.name for f in config.list_fields]
        dedupe_early = bool(config.unique_by) and all(n in list_names for n in config.unique_by)
        duplicates = 0
        for row in rows:
            row.reasons.extend(_required_failures(row, config.list_fields))
        if dedupe_early:
            # Done before detail pages so a duplicate never costs the site a request.
            duplicates = _mark_duplicates(rows, config.unique_by)
        detail_pages = _follow_details(config, fetcher, rows) if config.detail_fields else 0
        for row in rows:
            if not row.reasons:
                row.reasons.extend(_required_failures(row, config.detail_fields))
        if config.unique_by and not dedupe_early:
            duplicates = _mark_duplicates(rows, config.unique_by)
    finally:
        if owns_fetcher:
            fetcher.close()

    records, rejected = [], []
    for row in rows:
        meta = {
            "source_url": row.source_url,
            "fetched_at": row.fetched_at,
            "source_sha256": row.source_sha256,
            "detail_page_url": row.detail_page_url,
            "detail_fetched_at": row.detail_fetched_at,
            "detail_sha256": row.detail_sha256,
        }
        if row.reasons:
            # Show what was on the page for a field that failed, not a blank.
            shown = {
                f.name: row.values[f.name]
                if row.values.get(f.name) is not None
                else row.raws.get(f.name)
                for f in config.fields
            }
            rejected.append({"reason": "; ".join(row.reasons), **shown, **meta})
        else:
            warnings = [
                f"{name}: {problem}"
                for name, problem in row.problems.items()
                if problem != NOT_FOUND
            ]
            records.append({**row.values, **meta, "warnings": "; ".join(warnings)})

    log = fetcher.log
    refused = Counter(e.note for e in log if e.note.startswith("refused"))
    summary: list[tuple[str, Any]] = [
        ("job", config.name),
        ("started_utc", iso(started)),
        ("finished_utc", iso(utc_now())),
        ("duration_seconds", round(time.monotonic() - clock_start, 1)),
        ("user_agent", USER_AGENT),
        ("extractor_version", __version__),
        ("config_sha256", config.sha256),
        ("list_pages_read", list_pages),
        ("detail_pages_read", detail_pages),
        ("network_requests", fetcher.network_requests),
        ("cache_hits", sum(1 for e in log if e.cache_hit)),
        ("retries", fetcher.retries),
        ("max_concurrency_per_host", config.politeness.max_concurrency_per_host),
        ("robots_refusals", sum(1 for e in log if e.robots == "disallowed")),
        ("items_seen", len(rows)),
        ("records", len(records)),
        ("records_with_warnings", sum(1 for r in records if r["warnings"])),
        ("rejected", len(rejected)),
        ("rejected_as_duplicate", duplicates),
    ]
    summary += [
        (f"delay_seconds[{host}]", delay) for host, delay in sorted(fetcher.host_delays.items())
    ]
    summary += [(f"refusal: {note}", count) for note, count in sorted(refused.items())]
    summary += [("note", note) for note in notes]
    summary += [
        (f"stopped[{host}]", reason) for host, reason in sorted(fetcher.stopped_hosts.items())
    ]
    terms = config.terms_check
    summary.append(
        (
            "terms_check",
            f"{terms['url']} read on {terms['checked_on']}: {terms['clause']}"
            if terms
            else "not recorded in the config (a person must read the site's terms first)",
        )
    )
    concerns = [f"job ended early for {host}" for host in sorted(fetcher.stopped_hosts)]
    if list_pages == 0:
        concerns.append("no list page could be read")
    if len(records) < config.min_records:
        concerns.append(f"{len(records)} records, below min_records {config.min_records}")
    # Fill rate is over distinct listings, so a selector that stops matching shows up
    # here even when the field is required and its rows went to Rejected.
    distinct = [r for r in rows if not any(x.startswith("duplicate") for x in r.reasons)]
    for spec in config.fields:
        filled = sum(1 for r in distinct if r.values.get(spec.name) is not None)
        rate = filled / len(distinct) if distinct else 0.0
        summary.append((f"fill_rate[{spec.name}]", round(rate, 3)))
        wanted = config.min_fill_rate.get(spec.name)
        if wanted is not None and rate < wanted:
            concerns.append(f"{spec.name} filled on {rate:.0%} of listings, below {wanted:.0%}")
    share = len(rejected) / len(rows) if rows else 0.0
    summary.append(("rejected_share", round(share, 3)))
    if config.max_rejected_share is not None and share > config.max_rejected_share:
        concerns.append(f"{share:.0%} of listings rejected, above {config.max_rejected_share:.0%}")
    summary.append(("run_health", "ok" if not concerns else "attention: " + "; ".join(concerns)))
    return RunResult(config, records, rejected, log, summary)


def _crawl_lists(config: Config, fetcher: PoliteFetcher) -> tuple[list[_Row], int, list[str]]:
    pattern = config.pagination.page_pattern
    pending = deque(config.start_urls)
    if pattern:
        pending.extend(
            pattern.replace("{page}", str(n))
            for n in range(config.pagination.start, config.pagination.end + 1)
        )
    seen: set[str] = set()
    rows: list[_Row] = []
    notes: list[str] = []
    pages = 0
    while pending:
        url = urldefrag(pending.popleft()).url
        if url in seen:
            continue
        if pages >= config.max_pages:
            notes.append(f"stopped at the max_pages cap ({config.max_pages}); next was {url}")
            break
        seen.add(url)
        result = fetcher.get(url)
        if not result.ok:
            notes.append(f"list page {url} {_fetch_problem(result)}")
            if pattern:
                break
            continue
        pages += 1
        soup = parse_html(result.text)
        items = soup.select(config.item_selector)
        if not items:
            notes.append(f"list page {url} had no items matching {config.item_selector!r}")
            wording = block_wording(soup.get_text(" "))
            if wording:
                # An empty page that talks about a block is a block. Stop, do not retry.
                fetcher.stop_host(
                    result.final_url, f"page with no items and block wording ({wording!r})"
                )
                break
            if pattern:
                break
        for item in items:
            values, raws, problems = extract_fields(item, config.list_fields, result.final_url)
            rows.append(
                _Row(values, raws, problems, result.final_url, result.fetched_at, result.sha256)
            )
        if config.pagination.next_selector:
            link = soup.select_one(config.pagination.next_selector)
            href = link.get("href") if link else None
            if href:
                pending.append(urljoin(result.final_url, str(href)))
    return rows, pages, notes


def _follow_details(config: Config, fetcher: PoliteFetcher, rows: list[_Row]) -> int:
    names = [f.name for f in config.detail_fields]
    wanted: list[tuple[_Row, str]] = []
    for row in rows:
        if row.reasons:
            continue
        url = row.values.get(config.detail_url_field)
        problem = ""
        if not url:
            problem = "detail page link missing"
        elif len(wanted) >= config.max_detail_pages:
            problem = f"detail page not fetched (max_detail_pages cap of {config.max_detail_pages})"
        else:
            wanted.append((row, str(url)))
            continue
        _mark_detail_missing(row, names, problem)
    # One batch, so the async fetcher can overlap requests within its per-host limit.
    results = fetcher.get_many([url for _, url in wanted])
    read = 0
    for (row, _), result in zip(wanted, results, strict=True):
        if not result.ok:
            _mark_detail_missing(row, names, f"detail page {_fetch_problem(result)}")
            continue
        values, raws, problems = extract_fields(
            parse_html(result.text), config.detail_fields, result.final_url
        )
        row.values.update(values)
        row.raws.update(raws)
        row.problems.update(problems)
        row.detail_page_url = result.final_url
        row.detail_fetched_at = result.fetched_at
        row.detail_sha256 = result.sha256
        read += 1
    return read


def _mark_detail_missing(row: _Row, names: list[str], problem: str) -> None:
    row.values.update(dict.fromkeys(names))
    row.problems.update(dict.fromkeys(names, problem))
