# Benchmark

Written by `benchmark/run_benchmark.py`. Do not edit by hand; rerun the script.

- Run: 2026-11-21T05:11:04Z (UTC), web-data-extractor 0.1.0, Python 3.14.3, Windows
- Input: the bundled fixture site, generated from seed 42 (72 listings on 6 catalog pages, one detail page per product)
- Job: `configs/fixture.yaml`, served from 127.0.0.1, cache off, real delays

## Result

| Measure | Result |
|---|---|
| Valid products found | 60 of 60 (100.0%) |
| Records that should not be there | 0 |
| Field values correct | 360 of 360 (100.0%) |
| Broken items sent to Rejected | 9 of 9 (100.0%) |
| Rejected with the expected reason | 9 of 9 |
| Valid products wrongly rejected | 0 |
| Repeat listings flagged as duplicates | 3 of 3 |
| Listings accounted for (Records + Rejected) | 72 of 72 |
| Records with source URL and fetch time | 60 of 60 |
| Requests to the disallowed `/private/` section | 0 |
| Disallowed links refused before any request | 1 |
| Items from the disallowed section in the output | 0 |

## Field accuracy

Compared on the valid products that were found. Text must match exactly, numbers must have the right type and value (price within half a cent).

| Field | Correct | Accuracy |
|---|---|---|
| name | 60 of 60 | 100.0% |
| category | 60 of 60 | 100.0% |
| price | 60 of 60 | 100.0% |
| rating | 60 of 60 | 100.0% |
| brand | 60 of 60 | 100.0% |
| stock | 60 of 60 | 100.0% |

## Broken items

Each of these was written into the site on purpose. The reason is what the Rejected sheet said.

| Planted problem | SKU | Outcome | Reason given |
|---|---|---|---|
| missing_price | SKU-1006 | rejected | price: not found on the page |
| price_not_a_number | SKU-1043 | rejected | price: no number in 'Call for price' |
| price_range | SKU-1017 | rejected | price: more than one number in '$26.86 - $37.60' |
| price_zero | SKU-1028 | rejected | price: 0 is below the minimum 0.01 |
| rating_out_of_range | SKU-1027 | rejected | rating: 9 is above the maximum 5 |
| missing_rating | SKU-1013 | rejected | rating: not found on the page |
| empty_name | SKU-1008 | rejected | name: empty |
| detail_page_404 | SKU-1040 | rejected | stock: detail page returned HTTP 404 |
| detail_page_disallowed | SKU-1055 | rejected | stock: detail page disallowed by robots.txt |

## Politeness during the run

| Measure | Value |
|---|---|
| Network requests | 68 |
| Configured delay per host | 0.5 s (the lowest the tool accepts) |
| Shortest gap between two requests, timed by the fixture server | 0.502 s |
| Detail requests spent on duplicates or already-rejected rows | 0 |
| Wall time | 37.1 s |

## Layout-change drill

The same job is run again with the price selector pointing at an element that is not there, which is what a site redesign looks like from the tool's side. The run must say so and exit non-zero, not hand over a short or empty sheet as if it were fine.

| Measure | Result |
|---|---|
| Records written | 0 |
| Listings sent to Rejected with a reason | 72 of 72 |
| Run health | attention: 0 records, below min_records 40; price filled on 0% of listings, below 90%; stock filled on 0% of listings, below 80%; 100% of listings rejected, above 30% |
| Exit code `wde run` gives for this result | 1 |
| Requests made (no detail page is fetched for a row that already failed) | 7 |

## What this does and does not show

- It shows the pipeline loses nothing on this site: every listing ends up in Records or in Rejected with a reason, and typed values match the truth file.
- The site and the job config were written together, by the same author. A score here says the engine and its checks work. It does not say an untuned config will score the same on a site it has never seen. Each new site needs its own selectors, and its own check of a sample against the live pages.
- The fixture is static HTML. Pages that build their content with JavaScript are out of scope (see LIMITATIONS.md).
- The request gap is timed by the fixture server as requests arrive, not by the tool. The tool's own waits are in the RunLog sheet.

Reproduce: `python benchmark/run_benchmark.py`
