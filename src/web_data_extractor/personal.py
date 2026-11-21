"""A last line of defence against personal contact details ending up in an export.

The real control is the config: fields that hold personal data are not
listed. This guard catches the case where a field that should hold a product
name or a company line turns out to hold someone's email or phone number.
"""

from __future__ import annotations

import re

WITHHELD = "withheld"

_EMAIL = re.compile(r"([\w.+-]+)@[\w-]+(?:\.[\w-]+)+")
# Shared mailboxes of a business are not personal data.
ROLE_MAILBOXES = frozenset(
    [
        "info",
        "sales",
        "support",
        "contact",
        "hello",
        "admin",
        "office",
        "help",
        "enquiries",
        "inquiries",
        "orders",
        "billing",
        "service",
        "customerservice",
        "team",
        "mail",
        "press",
        "jobs",
        "careers",
        "hr",
        "noreply",
        "no-reply",
        "webmaster",
        "marketing",
        "accounts",
        "reception",
        "bookings",
        "booking",
        "returns",
        "shop",
        "store",
    ]
)
_PHONE = re.compile(
    r"^(?:tel:\s*)?(?:\+\d[\d\s().-]{6,18}\d|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})$", re.IGNORECASE
)


def looks_personal(value: str) -> str:
    """Say what kind of personal detail `value` looks like, or return an empty string."""
    for match in _EMAIL.finditer(value):
        if match.group(1).lower() not in ROLE_MAILBOXES:
            return "a personal email address"
    if _PHONE.match(value.strip()):
        return "a phone number"
    return ""
