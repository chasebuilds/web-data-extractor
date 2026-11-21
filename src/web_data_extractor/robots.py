"""robots.txt parsing and matching, following RFC 9309.

The standard library parser uses first-match ordering and has no wildcard
support, so a site that writes "Disallow: /*.pdf$" or relies on the
longest-match rule would be read wrongly. This module implements the RFC
rules directly: longest matching pattern wins, Allow wins a tie, and "*"
and "$" are supported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)
    crawl_delay: float | None = None


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


class RobotsPolicy:
    """The rules one host publishes, answerable per user-agent token."""

    def __init__(self, groups: list[_Group], *, verdict: bool | None = None, source: str = ""):
        self._groups = groups
        # verdict short-circuits matching: True allows everything, False nothing.
        self._verdict = verdict
        self.source = source

    @classmethod
    def allow_all(cls, source: str) -> RobotsPolicy:
        return cls([], verdict=True, source=source)

    @classmethod
    def deny_all(cls, source: str) -> RobotsPolicy:
        return cls([], verdict=False, source=source)

    @classmethod
    def parse(cls, text: str, source: str = "robots.txt") -> RobotsPolicy:
        groups: list[_Group] = []
        current: _Group | None = None
        collecting_agents = False
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not collecting_agents or current is None:
                    current = _Group()
                    groups.append(current)
                current.agents.append(value.lower())
                collecting_agents = True
            elif key in ("allow", "disallow") and current is not None:
                collecting_agents = False
                # An empty Disallow means "nothing is disallowed", so it adds no rule.
                if value:
                    current.rules.append((key == "allow", value))
            elif key == "crawl-delay" and current is not None:
                collecting_agents = False
                try:
                    current.crawl_delay = max(0.0, float(value))
                except ValueError:
                    continue
        return cls(groups, source=source)

    def _matching_groups(self, agent_token: str) -> list[_Group]:
        token = agent_token.lower()
        specific = [g for g in self._groups if token in g.agents]
        return specific or [g for g in self._groups if "*" in g.agents]

    def can_fetch(self, agent_token: str, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        if self._verdict is not None:
            return self._verdict
        target = path + (f"?{parts.query}" if parts.query else "")
        best_length, best_allow = -1, True
        for group in self._matching_groups(agent_token):
            for allow, pattern in group.rules:
                if not _pattern_to_regex(pattern).match(target):
                    continue
                length = len(pattern)
                if length > best_length or (length == best_length and allow):
                    best_length, best_allow = length, allow
        return best_allow

    def crawl_delay(self, agent_token: str) -> float | None:
        delays = [
            g.crawl_delay for g in self._matching_groups(agent_token) if g.crawl_delay is not None
        ]
        return max(delays) if delays else None
