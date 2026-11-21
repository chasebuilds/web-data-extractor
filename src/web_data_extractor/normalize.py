"""Turning scraped strings into typed values, strictly.

Each parser either returns a value or raises ValueError with a reason a
client can read. Nothing here guesses: a string holding two numbers is an
error, not "the first one".
"""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin

_WHITESPACE = re.compile(r"\s+")
_NUMBER_TOKEN = re.compile(r"(?<![\d.,])-?\d[\d.,]*")
_THOUSANDS_COMMA = re.compile(r"-?\d{1,3}(,\d{3})+")
_THOUSANDS_DOT = re.compile(r"-?\d{1,3}(\.\d{3})+")


def clean_text(value: str) -> str:
    return _WHITESPACE.sub(" ", value.replace(" ", " ")).strip()


def parse_number(value: str) -> float:
    tokens = [t.rstrip(".,") for t in _NUMBER_TOKEN.findall(value)]
    if not tokens:
        raise ValueError(f"no number in {value!r}")
    if len(tokens) > 1:
        raise ValueError(f"more than one number in {value!r}")
    token = tokens[0]
    if "," in token and "." in token:
        # The separator that appears last is the decimal mark.
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        if _THOUSANDS_COMMA.fullmatch(token):
            token = token.replace(",", "")
        elif token.count(",") == 1:
            token = token.replace(",", ".")
        else:
            raise ValueError(f"cannot read separators in {value!r}")
    elif token.count(".") > 1:
        if not _THOUSANDS_DOT.fullmatch(token):
            raise ValueError(f"cannot read separators in {value!r}")
        token = token.replace(".", "")
    return float(token)


def parse_integer(value: str) -> int:
    number = parse_number(value)
    if number != int(number):
        raise ValueError(f"{value!r} is not a whole number")
    return int(number)


def absolute_url(value: str, base_url: str) -> str:
    joined = urldefrag(urljoin(base_url, value.strip())).url
    if not joined.startswith(("http://", "https://")):
        raise ValueError(f"{value!r} is not a web link")
    return joined


def cast(value: str, type_name: str, base_url: str) -> object:
    if type_name == "number":
        return parse_number(value)
    if type_name == "integer":
        return parse_integer(value)
    if type_name == "url":
        return absolute_url(value, base_url)
    return value
