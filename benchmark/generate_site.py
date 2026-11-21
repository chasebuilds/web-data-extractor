"""Build the fixture site and its truth file from one seed.

Run:  python benchmark/generate_site.py

The site is a small paginated catalog with a detail page per product. Clean
truth is generated first, then written into HTML with the kind of mess real
catalogs have (mixed price formats, stray whitespace, relative links), plus a
known set of broken items and repeated listings. Because the truth is known,
benchmark/run_benchmark.py can score an extraction exactly.

Everything here is synthetic. No product, brand or price is real.
"""

from __future__ import annotations

import html
import json
import random
from pathlib import Path

SEED = 42
CLEAN_PRODUCTS = 60
DUPLICATE_LISTINGS = 3
PER_PAGE = 12

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "fixture_site"
TRUTH_PATH = ROOT / "benchmark" / "truth.json"

ADJECTIVES = [
    "Compact",
    "Heavy-Duty",
    "Slim",
    "Stackable",
    "Adjustable",
    "Magnetic",
    "Recycled",
    "Foldable",
    "Weatherproof",
    "Low-Profile",
    "Modular",
    "Quiet",
    "Anti-Slip",
    "Clear",
]
MATERIALS = ["Steel", "Bamboo", "Aluminum", "Walnut", "Felt", "Glass", "Cork", "Canvas"]
NOUNS = [
    "Desk Lamp",
    "Monitor Stand",
    "File Box",
    "Pen & Pencil Tray",
    "Cable Organizer",
    "Bookend Set",
    "Label Printer",
    "Paper Cutter",
    "Whiteboard",
    "Drawer Unit",
    "Laptop Riser",
    "Storage Bin",
    "Wall Shelf",
    "Letter Sorter",
    "Task Chair Mat",
    "Clip & Pin Holder",
]
CATEGORIES = ["Lighting", "Storage", "Desk Accessories", "Furniture", "Print & Label"]
BRANDS = ["Northfield", "Alder & Pine", "Kestrel Works", "Bluewater", "Tallgrass", "Quarry Lane"]

# kind -> the phrase the Rejected sheet's reason is expected to contain
BROKEN_KINDS = {
    "missing_price": "price: not found on the page",
    "price_not_a_number": "price: no number",
    "price_range": "price: more than one number",
    "price_zero": "price: 0 is below the minimum",
    "rating_out_of_range": "rating: 9 is above the maximum",
    "missing_rating": "rating: not found on the page",
    "empty_name": "name: empty",
    "detail_page_404": "detail page returned HTTP 404",
    "detail_page_disallowed": "detail page disallowed by robots.txt",
}

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
</head>
<body>
<header>
<p class="site-name">Fixture Supply Co. (synthetic test site)</p>
<nav><a href="/index.html">Home</a> | <a href="/catalog/page-1.html">Catalog</a> |
<a href="/private/clearance.html">Members clearance</a></nav>
</header>
<main>
{body}
</main>
</body>
</html>
"""


def _slug(sku: str) -> str:
    return sku.lower()


def _make_products(rng: random.Random, count: int) -> list[dict]:
    names: set[str] = set()
    while len(names) < count:
        names.add(f"{rng.choice(ADJECTIVES)} {rng.choice(MATERIALS)} {rng.choice(NOUNS)}")
    products = []
    for index, name in enumerate(sorted(names)):
        products.append(
            {
                "sku": f"SKU-{1001 + index}",
                "name": name,
                "price": round(rng.choice([rng.uniform(2.5, 60), rng.uniform(60, 1900)]), 2),
                "rating": rng.randint(1, 5),
                "category": rng.choice(CATEGORIES),
                "brand": rng.choice(BRANDS),
                "stock": rng.choice([0, rng.randint(1, 40), rng.randint(41, 900)]),
            }
        )
    rng.shuffle(products)
    return products


def _price_text(rng: random.Random, price: float) -> str:
    return rng.choice(
        [
            f"${price:,.2f}",
            f"$ {price:,.2f}",
            f"USD {price:.2f}",
            f"{price:,.2f} USD",
            f"&nbsp;${price:,.2f}&nbsp;",
        ]
    )


def _rating_text(rng: random.Random, rating: int) -> str:
    return rng.choice(
        [f"{rating} out of 5", f"Rated {rating} stars", f"{rating}/5", f"{rating}.0", f"{rating}"]
    )


def _name_html(rng: random.Random, name: str) -> str:
    escaped = html.escape(name)
    style = rng.randrange(4)
    if style == 0:
        return f"\n        {escaped}\n      "
    if style == 1:
        first, _, rest = escaped.partition(" ")
        return f"<b>{first}</b>  {rest}"
    if style == 2:
        return escaped.replace(" ", "   ", 1)
    return escaped


def _link(rng: random.Random, path: str) -> str:
    return rng.choice([f"../{path}", f"/{path}", f"../{path}#reviews"])


def _stock_text(rng: random.Random, stock: int) -> str:
    return rng.choice([f"In stock: {stock} units", f"{stock} available", f"Stock: {stock}"])


def _list_item(rng: random.Random, product: dict, broken: str = "") -> str:
    slug = _slug(product["sku"])
    path = f"item/{slug}.html"
    if broken == "detail_page_404":
        path = f"item/{slug}-discontinued.html"
    elif broken == "detail_page_disallowed":
        path = f"private/{slug}.html"

    price = f'<span class="price">{_price_text(rng, product["price"])}</span>'
    if broken == "missing_price":
        price = ""
    elif broken == "price_not_a_number":
        price = '<span class="price">Call for price</span>'
    elif broken == "price_range":
        low = product["price"]
        price = f'<span class="price">${low:,.2f} - ${low * 1.4:,.2f}</span>'
    elif broken == "price_zero":
        price = '<span class="price">$0.00</span>'

    rating = f'<span class="rating">{_rating_text(rng, product["rating"])}</span>'
    if broken == "rating_out_of_range":
        rating = '<span class="rating">9/5</span>'
    elif broken == "missing_rating":
        rating = ""

    name = "" if broken == "empty_name" else _name_html(rng, product["name"])
    parts = [
        f'    <h3 class="name"><a href="{_link(rng, path)}">{name}</a></h3>',
        f'    <span class="category">{html.escape(product["category"])}</span>',
        f"    {price}" if price else "",
        f"    {rating}" if rating else "",
    ]
    inner = "\n".join(p for p in parts if p)
    return f'  <article class="product" data-sku="{product["sku"]}">\n{inner}\n  </article>'


def _detail_page(rng: random.Random, product: dict) -> str:
    body = f"""<article class="detail" data-sku="{product["sku"]}">
<h1>{html.escape(product["name"])}</h1>
<dl>
<dt>Brand</dt><dd class="brand"> {html.escape(product["brand"])} </dd>
<dt>Category</dt><dd>{html.escape(product["category"])}</dd>
</dl>
<p class="stock">{_stock_text(rng, product["stock"])}</p>
<p class="description">Synthetic catalog entry for testing. Not a real product.</p>
<p><a href="/catalog/page-1.html">Back to catalog</a></p>
</article>"""
    return PAGE.format(title=html.escape(product["name"]), body=body)


def render_site(seed: int = SEED) -> tuple[dict[str, str], dict]:
    """Return ({relative path: file text}, truth) for the given seed."""
    rng = random.Random(seed)
    products = _make_products(rng, CLEAN_PRODUCTS + len(BROKEN_KINDS))
    clean = products[:CLEAN_PRODUCTS]
    broken = dict(zip(BROKEN_KINDS, products[CLEAN_PRODUCTS:], strict=True))

    # (product, broken kind, is a repeat listing)
    listings: list[tuple[dict, str, bool]] = [(p, "", False) for p in clean]
    listings += [(p, kind, False) for kind, p in broken.items()]
    rng.shuffle(listings)
    repeats = rng.sample(clean, DUPLICATE_LISTINGS)
    for product in repeats:
        first = next(i for i, entry in enumerate(listings) if entry[0] is product)
        listings.insert(rng.randint(first + 1, len(listings)), (product, "", True))

    files: dict[str, str] = {
        "robots.txt": "User-agent: *\nDisallow: /private/\n",
        "index.html": PAGE.format(
            title="Fixture Supply Co.",
            body="<p>Synthetic site for testing a web extractor. "
            '<a href="/catalog/page-1.html">Browse the catalog</a>.</p>',
        ),
    }

    pages = [listings[i : i + PER_PAGE] for i in range(0, len(listings), PER_PAGE)]
    for number, page in enumerate(pages, start=1):
        items = "\n".join(_list_item(rng, product, kind) for product, kind, _ in page)
        next_link = (
            f'<li class="next"><a href="page-{number + 1}.html">Next</a></li>'
            if number < len(pages)
            else ""
        )
        prev_link = (
            f'<li class="previous"><a href="page-{number - 1}.html">Previous</a></li>'
            if number > 1
            else ""
        )
        body = f"""<h1>Catalog, page {number} of {len(pages)}</h1>
<aside class="promo"><h3 class="name"><a href="/index.html">Free shipping week</a></h3>
<span class="price">$0.00</span></aside>
<section class="products">
{items}
</section>
<ul class="pager">{prev_link}{next_link}</ul>"""
        files[f"catalog/page-{number}.html"] = PAGE.format(
            title=f"Catalog page {number}", body=body
        )

    for product in clean:
        files[f"item/{_slug(product['sku'])}.html"] = _detail_page(rng, product)
    for kind, product in broken.items():
        if kind == "detail_page_404":
            continue
        folder = "private" if kind == "detail_page_disallowed" else "item"
        files[f"{folder}/{_slug(product['sku'])}.html"] = _detail_page(rng, product)

    members_only = {
        "sku": "SKU-9999",
        "name": "Members Only Clearance Lot",
        "price": 1.0,
        "rating": 5,
        "category": "Storage",
        "brand": "Northfield",
        "stock": 1,
    }
    files["private/clearance.html"] = PAGE.format(
        title="Members clearance",
        body='<section class="products">\n' + _list_item(rng, members_only) + "\n</section>",
    )

    truth = {
        "seed": seed,
        "list_pages": len(pages),
        "listings": len(listings),
        "records": sorted(clean, key=lambda p: p["sku"]),
        "broken": [
            {"sku": p["sku"], "kind": kind, "reason_contains": BROKEN_KINDS[kind]}
            for kind, p in broken.items()
        ],
        "duplicate_listings": sorted(p["sku"] for p in repeats),
        "disallowed_prefix": "/private/",
        "never_extract": ["SKU-9999"],
    }
    return files, truth


def build(site_dir: Path = SITE_DIR, truth_path: Path = TRUTH_PATH, seed: int = SEED) -> dict:
    files, truth = render_site(seed)
    for relative, text in files.items():
        target = site_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    truth_path.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8", newline="\n")
    return truth


if __name__ == "__main__":
    result = build()
    print(
        f"Wrote {SITE_DIR} ({result['listings']} listings on {result['list_pages']} pages, "
        f"{len(result['records'])} valid products, {len(result['broken'])} broken, "
        f"{len(result['duplicate_listings'])} repeats) and {TRUTH_PATH}"
    )
