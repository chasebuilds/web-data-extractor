"""Loading and validating the YAML job config.

Validation is strict on purpose: an unknown key is an error, because a typo
like "min_delay_second" silently falling back to a default is how a polite
job turns into an impolite one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .fetch import Politeness, PolitenessError

FIELD_TYPES = ("text", "number", "integer", "url")
RESERVED_COLUMNS = (
    "source_url",
    "fetched_at",
    "source_sha256",
    "detail_page_url",
    "detail_fetched_at",
    "detail_sha256",
    "warnings",
    "reason",
)
# Hosts a job may read without a terms_check record: this machine, and the two
# sandbox sites that exist to be scraped.
DEMO_HOSTS = ("127.0.0.1", "localhost", "[::1]", "books.toscrape.com", "quotes.toscrape.com")
_VAR = re.compile(r"\$\{(\w+)\}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    selector: str = ""
    attr: str = ""
    regex: str = ""
    map: dict[str, Any] | None = None
    type: str = "text"
    multiple: bool = False
    join: str = ", "
    required: bool = False
    min: float | None = None
    max: float | None = None
    default: Any = None


@dataclass(frozen=True)
class Pagination:
    next_selector: str = ""
    page_pattern: str = ""
    start: int = 1
    end: int = 1


@dataclass(frozen=True)
class Config:
    name: str
    start_urls: list[str]
    allowed_hosts: set[str]
    politeness: Politeness
    cache_dir: str | None
    cache_ttl_hours: float
    max_pages: int
    max_detail_pages: int
    max_requests: int
    item_selector: str
    list_fields: list[FieldSpec]
    pagination: Pagination
    detail_url_field: str = ""
    detail_fields: list[FieldSpec] = field(default_factory=list)
    unique_by: list[str] = field(default_factory=list)
    terms_check: dict[str, str] = field(default_factory=dict)
    min_records: int = 1
    min_fill_rate: dict[str, float] = field(default_factory=dict)
    max_rejected_share: float | None = None
    sha256: str = ""

    @property
    def fields(self) -> list[FieldSpec]:
        return [*self.list_fields, *self.detail_fields]

    @property
    def hosts_needing_terms_check(self) -> list[str]:
        if self.terms_check:
            return []
        return sorted(
            h
            for h in self.allowed_hosts
            if h.rsplit(":", 1)[0] not in DEMO_HOSTS and h not in DEMO_HOSTS
        )


def _take(section: dict, allowed: set[str], where: str) -> dict:
    if not isinstance(section, dict):
        raise ConfigError(f"{where} must be a mapping.")
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(unknown)}. Allowed: {', '.join(sorted(allowed))}."
        )
    return section


def _substitute(node: Any, variables: dict[str, str]) -> Any:
    if isinstance(node, str):

        def replace(match: re.Match[str]) -> str:
            if match.group(1) not in variables:
                raise ConfigError(f"variable ${{{match.group(1)}}} is not defined under vars.")
            return str(variables[match.group(1)])

        return _VAR.sub(replace, node)
    if isinstance(node, list):
        return [_substitute(item, variables) for item in node]
    if isinstance(node, dict):
        return {key: _substitute(value, variables) for key, value in node.items()}
    return node


def _field(name: str, raw: Any, where: str) -> FieldSpec:
    if name in RESERVED_COLUMNS:
        raise ConfigError(f"{where}.{name}: this column name is reserved for the export.")
    if isinstance(raw, str):
        raw = {"selector": raw}
    spec = _take(
        raw or {},
        {
            "selector",
            "attr",
            "regex",
            "map",
            "type",
            "multiple",
            "join",
            "required",
            "min",
            "max",
            "default",
        },
        f"{where}.{name}",
    )
    if spec.get("type", "text") not in FIELD_TYPES:
        raise ConfigError(f"{where}.{name}: type must be one of {', '.join(FIELD_TYPES)}.")
    if spec.get("regex"):
        try:
            re.compile(spec["regex"])
        except re.error as exc:
            raise ConfigError(f"{where}.{name}: bad regex ({exc}).") from exc
    if spec.get("map") is not None and not isinstance(spec["map"], dict):
        raise ConfigError(f"{where}.{name}: map must be a mapping.")
    return FieldSpec(name=name, **spec)


def _fields(raw: Any, where: str) -> list[FieldSpec]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{where} must list at least one field.")
    return [_field(str(name), spec, where) for name, spec in raw.items()]


def _positive_int(section: dict, key: str, default: int, where: str) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"{where}.{key} must be a whole number of at least 1.")
    return value


def parse_config(data: Any, overrides: dict[str, str] | None = None) -> Config:
    top = _take(
        data,
        {
            "name",
            "vars",
            "start_urls",
            "allowed_hosts",
            "politeness",
            "cache",
            "limits",
            "list",
            "pagination",
            "detail",
            "unique_by",
            "terms_check",
            "health",
        },
        "config",
    )
    variables = {**(top.get("vars") or {}), **(overrides or {})}
    top = _substitute({k: v for k, v in top.items() if k != "vars"}, variables)

    name = str(top.get("name") or "extract")
    if not re.fullmatch(r"[\w.-]+", name):
        raise ConfigError("name may only use letters, digits, dot, dash and underscore.")

    pag = _take(
        top.get("pagination") or {}, {"next_selector", "page_pattern", "start", "end"}, "pagination"
    )
    if pag.get("next_selector") and pag.get("page_pattern"):
        raise ConfigError("pagination: use next_selector or page_pattern, not both.")
    pattern = pag.get("page_pattern", "")
    if pattern and "{page}" not in pattern:
        raise ConfigError("pagination.page_pattern must contain {page}.")
    pagination = Pagination(
        next_selector=pag.get("next_selector", ""),
        page_pattern=pattern,
        start=_positive_int(pag, "start", 1, "pagination") if pattern else 1,
        end=_positive_int(pag, "end", 1, "pagination") if pattern else 1,
    )
    if pagination.end < pagination.start:
        raise ConfigError("pagination.end must not be below pagination.start.")

    start_urls = top.get("start_urls") or []
    if isinstance(start_urls, str):
        start_urls = [start_urls]
    if pattern and not start_urls:
        start_urls = [pattern.replace("{page}", str(pagination.start))]
    if not start_urls:
        raise ConfigError("start_urls must list at least one URL.")
    for url in start_urls:
        parts = urlsplit(str(url))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError(f"start_urls: {url!r} is not an http(s) URL.")
    hosts = top.get("allowed_hosts") or [urlsplit(u).netloc for u in start_urls]
    allowed_hosts = {str(h).lower() for h in hosts}

    pol = _take(
        top.get("politeness") or {},
        {
            "min_delay_seconds",
            "backoff_base_seconds",
            "max_backoff_seconds",
            "max_retries",
            "timeout_seconds",
        },
        "politeness",
    )
    try:
        politeness = Politeness(**pol)
    except (PolitenessError, TypeError) as exc:
        raise ConfigError(f"politeness: {exc}") from exc

    cache = _take(top.get("cache") or {}, {"dir", "ttl_hours", "enabled"}, "cache")
    cache_dir = str(cache.get("dir", ".wde-cache")) if cache.get("enabled", True) else None

    limits = _take(
        top.get("limits") or {}, {"max_pages", "max_detail_pages", "max_requests"}, "limits"
    )

    lst = _take(top.get("list") or {}, {"item_selector", "fields"}, "list")
    if not lst.get("item_selector"):
        raise ConfigError("list.item_selector is required.")
    list_fields = _fields(lst.get("fields"), "list.fields")

    det = _take(top.get("detail") or {}, {"url_field", "fields"}, "detail")
    detail_fields: list[FieldSpec] = []
    detail_url_field = ""
    if det:
        detail_url_field = str(det.get("url_field", ""))
        list_field_types = {f.name: f.type for f in list_fields}
        if list_field_types.get(detail_url_field) != "url":
            raise ConfigError("detail.url_field must name a list field with type: url.")
        detail_fields = _fields(det.get("fields"), "detail.fields")

    names = [f.name for f in [*list_fields, *detail_fields]]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise ConfigError(f"field name(s) used twice: {', '.join(repeated)}.")
    unique_by = top.get("unique_by") or []
    if isinstance(unique_by, str):
        unique_by = [unique_by]
    missing = [n for n in unique_by if n not in names]
    if missing:
        raise ConfigError(f"unique_by names unknown field(s): {', '.join(missing)}.")

    terms = _take(top.get("terms_check") or {}, {"url", "checked_on", "clause"}, "terms_check")
    if terms and not all(terms.get(k) for k in ("url", "checked_on", "clause")):
        raise ConfigError("terms_check needs url, checked_on and clause, or leave it out.")

    health = _take(
        top.get("health") or {}, {"min_records", "min_fill_rate", "max_rejected_share"}, "health"
    )
    min_records = health.get("min_records", 1)
    if not isinstance(min_records, int) or isinstance(min_records, bool) or min_records < 0:
        raise ConfigError("health.min_records must be a whole number of 0 or more.")
    fill = health.get("min_fill_rate") or {}
    if not isinstance(fill, dict):
        raise ConfigError("health.min_fill_rate must map field names to a share from 0 to 1.")
    shares = {**fill, "max_rejected_share": health.get("max_rejected_share", 1)}
    for key, share in shares.items():
        if key != "max_rejected_share" and key not in names:
            raise ConfigError(f"health.min_fill_rate names an unknown field: {key}.")
        if isinstance(share, bool) or not isinstance(share, int | float) or not 0 <= share <= 1:
            raise ConfigError(f"health: {key} must be a share from 0 to 1.")
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode("utf-8"))

    return Config(
        name=name,
        start_urls=[str(u) for u in start_urls],
        allowed_hosts=allowed_hosts,
        politeness=politeness,
        cache_dir=cache_dir,
        cache_ttl_hours=float(cache.get("ttl_hours", 24)),
        max_pages=_positive_int(limits, "max_pages", 5, "limits"),
        max_detail_pages=_positive_int(limits, "max_detail_pages", 50, "limits"),
        max_requests=_positive_int(limits, "max_requests", 200, "limits"),
        item_selector=str(lst["item_selector"]),
        list_fields=list_fields,
        pagination=pagination,
        detail_url_field=detail_url_field,
        detail_fields=detail_fields,
        unique_by=list(unique_by),
        terms_check={k: str(v) for k, v in terms.items()},
        min_records=min_records,
        min_fill_rate={str(k): float(v) for k, v in fill.items()},
        max_rejected_share=health.get("max_rejected_share"),
        sha256=digest.hexdigest(),
    )


def load_config(path: str | Path, overrides: dict[str, str] | None = None) -> Config:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML ({exc}).") from exc
    return parse_config(data, overrides)
