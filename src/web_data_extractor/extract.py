"""Applying field specs to a parsed HTML node."""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from .config import FieldSpec
from .normalize import cast, clean_text
from .personal import WITHHELD, looks_personal


def parse_html(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "html.parser")


def _raw(node: Tag, attr: str) -> str | None:
    if not attr:
        return clean_text(node.get_text(" "))
    value = node.get(attr)
    if value is None:
        return None
    return clean_text(" ".join(value) if isinstance(value, list) else str(value))


def _process(raw: str, spec: FieldSpec, base_url: str) -> Any:
    value: Any = raw
    if spec.regex:
        match = re.search(spec.regex, raw)
        if not match:
            raise ValueError(f"pattern did not match {raw!r}")
        value = clean_text(match.group(1) if match.groups() else match.group(0))
    if spec.map is not None:
        if value not in spec.map:
            raise ValueError(f"no mapping for {value!r}")
        value = spec.map[value]
    if isinstance(value, str):
        if value == "":
            raise ValueError("empty")
        value = cast(value, spec.type, base_url)
        if spec.type == "text":
            personal = looks_personal(value)
            if personal:
                raise ValueError(f"{WITHHELD}: looks like {personal}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{value:g} is below the minimum {spec.min:g}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{value:g} is above the maximum {spec.max:g}")
    return value


def extract_fields(
    node: Tag, specs: list[FieldSpec], base_url: str
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Return (values, raw strings as seen on the page, problems) keyed by field name.

    A field with a problem has the value None. Whether that rejects the row
    or becomes a warning is decided later, from spec.required.
    """
    values: dict[str, Any] = {}
    raws: dict[str, str] = {}
    problems: dict[str, str] = {}
    for spec in specs:
        nodes = node.select(spec.selector) if spec.selector else [node]
        found = [r for r in (_raw(n, spec.attr) for n in nodes) if r is not None]
        if not spec.multiple:
            found = found[:1]
        value: Any = None
        if not found:
            problems[spec.name] = "not found on the page"
        else:
            raws[spec.name] = spec.join.join(found)
            try:
                parts = [_process(r, spec, base_url) for r in found]
                value = spec.join.join(str(p) for p in parts) if spec.multiple else parts[0]
            except ValueError as exc:
                problems[spec.name] = str(exc)
                if str(exc).startswith(WITHHELD):
                    # Keep the value out of the Rejected sheet too.
                    raws[spec.name] = "[withheld]"
        # A default fills a gap. It never papers over a value that was there but unreadable.
        if not found and spec.default is not None:
            value = spec.default
            del problems[spec.name]
        values[spec.name] = value
    return values, raws, problems
