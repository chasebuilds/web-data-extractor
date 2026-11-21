from __future__ import annotations

import pytest

from web_data_extractor.robots import RobotsPolicy

AGENT = "web-data-extractor"
BASE = "https://site.test"


def allowed(robots: str, path: str, agent: str = AGENT) -> bool:
    return RobotsPolicy.parse(robots).can_fetch(agent, BASE + path)


def test_empty_file_allows_everything():
    assert allowed("", "/anything")


def test_simple_prefix_disallow():
    robots = "User-agent: *\nDisallow: /private/\n"
    assert not allowed(robots, "/private/page.html")
    assert not allowed(robots, "/private/")
    assert allowed(robots, "/private")
    assert allowed(robots, "/public/private/")


def test_longest_match_wins_regardless_of_order():
    robots = "User-agent: *\nDisallow: /shop/\nAllow: /shop/public/\n"
    assert allowed(robots, "/shop/public/item")
    assert not allowed(robots, "/shop/cart")
    flipped = "User-agent: *\nAllow: /shop/public/\nDisallow: /shop/\n"
    assert allowed(flipped, "/shop/public/item")


def test_allow_wins_a_tie():
    assert allowed("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a")


def test_wildcard_and_end_anchor():
    robots = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /search?*sort=\n"
    assert not allowed(robots, "/files/report.pdf")
    assert allowed(robots, "/files/report.pdf.html")
    assert not allowed(robots, "/search?q=x&sort=price")
    assert allowed(robots, "/search?q=x")


def test_specific_group_replaces_wildcard_group():
    robots = "User-agent: *\nDisallow: /\n\nUser-agent: Web-Data-Extractor\nDisallow: /only-this/\n"
    assert allowed(robots, "/page")
    assert not allowed(robots, "/only-this/x")
    assert not allowed(robots, "/page", agent="otherbot")


def test_several_agents_can_share_one_group():
    robots = "User-agent: a\nUser-agent: web-data-extractor\nDisallow: /x\n"
    assert not allowed(robots, "/x")


def test_empty_disallow_allows_everything():
    assert allowed("User-agent: *\nDisallow:\n", "/x")


def test_comments_and_junk_lines_are_ignored():
    robots = "# hello\nUser-agent: * # everyone\nnonsense line\nDisallow: /x # no\n"
    assert not allowed(robots, "/x")
    assert allowed(robots, "/y")


def test_robots_txt_itself_is_always_fetchable():
    assert RobotsPolicy.deny_all("test").can_fetch(AGENT, BASE + "/robots.txt")


def test_deny_all_and_allow_all():
    assert not RobotsPolicy.deny_all("test").can_fetch(AGENT, BASE + "/x")
    assert RobotsPolicy.allow_all("test").can_fetch(AGENT, BASE + "/x")


@pytest.mark.parametrize(
    ("robots", "expected"),
    [
        ("User-agent: *\nCrawl-delay: 5\n", 5.0),
        ("User-agent: *\nCrawl-delay: 2.5\n", 2.5),
        ("User-agent: *\nCrawl-delay: soon\n", None),
        ("User-agent: *\nDisallow: /x\n", None),
        ("User-agent: *\nCrawl-delay: 1\n\nUser-agent: web-data-extractor\nCrawl-delay: 9\n", 9.0),
    ],
)
def test_crawl_delay(robots, expected):
    assert RobotsPolicy.parse(robots).crawl_delay(AGENT) == expected
