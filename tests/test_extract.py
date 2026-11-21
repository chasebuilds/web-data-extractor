from __future__ import annotations

import pytest

from web_data_extractor.config import FieldSpec
from web_data_extractor.extract import extract_fields, parse_html
from web_data_extractor.normalize import absolute_url, clean_text, parse_integer, parse_number

BASE = "https://shop.test/catalog/page-1.html"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1,299.00", 1299.0),
        ("$ 45.50", 45.5),
        ("USD 45.5", 45.5),
        ("45.50 USD", 45.5),
        ("£51.77", 51.77),
        ("1.299,00 €", 1299.0),
        ("12,5", 12.5),
        ("1,234,567", 1234567.0),
        ("1.234.567", 1234567.0),
        ("-3.5", -3.5),
        ("Price: 45.", 45.0),
        ("7", 7.0),
    ],
)
def test_parse_number_accepts(text, expected):
    assert parse_number(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text", ["Call for price", "", "$12.00 - $15.00", "from 3 to 5", "1,23,4", "1.2.3"]
)
def test_parse_number_refuses_to_guess(text):
    with pytest.raises(ValueError):
        parse_number(text)


def test_parse_integer():
    assert parse_integer("4.0") == 4
    assert parse_integer("1,200") == 1200
    with pytest.raises(ValueError, match="not a whole number"):
        parse_integer("4.5")


def test_clean_text_collapses_whitespace_and_nbsp():
    assert clean_text("  a\n\t b  c ") == "a b c"


def test_absolute_url_resolves_and_drops_fragment():
    assert absolute_url("../item/a.html#reviews", BASE) == "https://shop.test/item/a.html"
    assert absolute_url("/item/a.html", BASE) == "https://shop.test/item/a.html"
    with pytest.raises(ValueError):
        absolute_url("mailto:someone", BASE)


ITEM = """
<article class="product" data-sku="SKU-1">
  <h3><a href="../item/sku-1.html" title="Full Title">Short &amp;
     sweet</a></h3>
  <p class="star-rating Three"></p>
  <span class="price">$1,299.00</span>
  <span class="rating">4 out of 5</span>
  <a class="tag">red</a><a class="tag">blue</a>
  <span class="bad">n/a</span>
</article>
"""


def extract(*specs: FieldSpec):
    node = parse_html(ITEM).select_one("article")
    return extract_fields(node, list(specs), BASE)


def test_text_attribute_and_self_selector():
    values, _, problems = extract(
        FieldSpec("sku", attr="data-sku"),
        FieldSpec("name", selector="h3 a"),
        FieldSpec("title", selector="h3 a", attr="title"),
    )
    assert values == {"sku": "SKU-1", "name": "Short & sweet", "title": "Full Title"}
    assert problems == {}


def test_regex_map_and_types():
    values, raws, problems = extract(
        FieldSpec("price", selector=".price", type="number"),
        FieldSpec("rating", selector=".rating", regex=r"(\d+)", type="integer"),
        FieldSpec(
            "stars",
            selector="p.star-rating",
            attr="class",
            regex=r"star-rating (\w+)",
            map={"Three": 3},
        ),
        FieldSpec("link", selector="h3 a", attr="href", type="url"),
    )
    assert values == {
        "price": 1299.0,
        "rating": 4,
        "stars": 3,
        "link": "https://shop.test/item/sku-1.html",
    }
    assert raws["price"] == "$1,299.00"
    assert problems == {}


def test_multiple_values_are_joined():
    values, _, _ = extract(FieldSpec("tags", selector=".tag", multiple=True, join=" | "))
    assert values["tags"] == "red | blue"


def test_problems_are_reported_with_the_text_that_was_on_the_page():
    values, raws, problems = extract(
        FieldSpec("missing", selector=".nope"),
        FieldSpec("bad", selector=".bad", type="number"),
        FieldSpec("nomatch", selector=".bad", regex=r"(\d+)"),
        FieldSpec("unmapped", selector=".bad", map={"yes": 1}),
        FieldSpec("toohigh", selector=".rating", regex=r"(\d+)", type="integer", max=3),
    )
    assert all(v is None for v in values.values())
    assert problems["missing"] == "not found on the page"
    assert problems["bad"] == "no number in 'n/a'"
    assert problems["nomatch"] == "pattern did not match 'n/a'"
    assert problems["unmapped"] == "no mapping for 'n/a'"
    assert problems["toohigh"] == "4 is above the maximum 3"
    assert raws["bad"] == "n/a"


def test_default_fills_a_gap_but_never_hides_an_unreadable_value():
    values, _, problems = extract(
        FieldSpec("missing", selector=".nope", default="unknown"),
        FieldSpec("bad", selector=".bad", type="number", default=0),
    )
    assert values == {"missing": "unknown", "bad": None}
    assert list(problems) == ["bad"]
