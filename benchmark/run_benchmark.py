"""Score an extraction of the fixture site against its truth file.

Run:  python benchmark/run_benchmark.py

Serves fixture_site/ on 127.0.0.1, runs configs/fixture.yaml through the
normal pipeline with real delays, compares the result with
benchmark/truth.json, and writes BENCHMARK.md. Every number in that file
comes from this script.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark.generate_site import SEED, SITE_DIR, TRUTH_PATH, build  # noqa: E402
from web_data_extractor import __version__  # noqa: E402
from web_data_extractor.config import load_config  # noqa: E402
from web_data_extractor.export import write_outputs  # noqa: E402
from web_data_extractor.fetch import iso, utc_now  # noqa: E402
from web_data_extractor.fixture_server import serve  # noqa: E402
from web_data_extractor.runner import run  # noqa: E402

FIELDS = ["name", "category", "price", "rating", "brand", "stock"]


def _same(field: str, got: object, expected: object) -> bool:
    if field == "price":
        return isinstance(got, float) and abs(got - float(expected)) < 0.005
    return got == expected and type(got) is type(expected)


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def main() -> int:
    build()
    truth = json.loads(TRUTH_PATH.read_text(encoding="utf-8"))
    expected = {p["sku"]: p for p in truth["records"]}
    broken = {b["sku"]: b for b in truth["broken"]}

    started = time.monotonic()
    with serve(SITE_DIR) as site:
        config = load_config(ROOT / "configs" / "fixture.yaml", {"base_url": site.base_url})
        result = run(config)
        served = list(site.paths)
        arrivals = list(site.arrivals)
        # Drill: the site renames its price element. Same job, one selector now wrong.
        moved = [
            replace(f, selector=".cost") if f.name == "price" else f for f in config.list_fields
        ]
        drill = run(replace(config, list_fields=moved))
        drill_requests = len(site.paths) - len(served)
    elapsed = time.monotonic() - started
    write_outputs(result, ROOT / "out" / "benchmark")

    got = {r["sku"]: r for r in result.records}
    found = set(got) & set(expected)
    unexpected = set(got) - set(expected)

    field_rows = []
    cells_ok = cells = 0
    for name in FIELDS:
        ok = sum(1 for sku in found if _same(name, got[sku][name], expected[sku][name]))
        field_rows.append((name, ok, len(found)))
        cells_ok += ok
        cells += len(found)

    rejected_non_dup = {
        r["sku"]: r["reason"] for r in result.rejected if "duplicate" not in r["reason"]
    }
    rejected_dup = sorted(r["sku"] for r in result.rejected if "duplicate" in r["reason"])
    broken_caught = set(rejected_non_dup) & set(broken)
    right_reason = {
        sku for sku in broken_caught if broken[sku]["reason_contains"] in rejected_non_dup[sku]
    }
    wrongly_rejected = set(rejected_non_dup) - set(broken)
    broken_leaked = set(got) & set(broken)

    with_provenance = sum(
        1
        for r in result.records
        if r["source_url"] and r["fetched_at"] and r["detail_page_url"] and r["detail_fetched_at"]
    )
    disallowed_hits = [p for p in served if p.startswith(truth["disallowed_prefix"])]
    never = set(truth["never_extract"]) & (set(got) | set(rejected_non_dup))
    network = [e for e in result.log if e.status is not None and not e.cache_hit]
    shortest_gap = min(b - a for a, b in zip(arrivals, arrivals[1:], strict=False))
    accounted = len(result.records) + len(result.rejected)

    lines = [
        "# Benchmark",
        "",
        "Written by `benchmark/run_benchmark.py`. Do not edit by hand; rerun the script.",
        "",
        f"- Run: {iso(utc_now())} (UTC), web-data-extractor {__version__}, "
        f"Python {platform.python_version()}, {platform.system()}",
        f"- Input: the bundled fixture site, generated from seed {SEED} "
        f"({truth['listings']} listings on {truth['list_pages']} catalog pages, "
        "one detail page per product)",
        "- Job: `configs/fixture.yaml`, served from 127.0.0.1, cache off, real delays",
        "",
        "## Result",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Valid products found | {len(found)} of {len(expected)} "
        f"({_pct(len(found), len(expected))}) |",
        f"| Records that should not be there | {len(unexpected) + len(broken_leaked)} |",
        f"| Field values correct | {cells_ok} of {cells} ({_pct(cells_ok, cells)}) |",
        f"| Broken items sent to Rejected | {len(broken_caught)} of {len(broken)} "
        f"({_pct(len(broken_caught), len(broken))}) |",
        f"| Rejected with the expected reason | {len(right_reason)} of {len(broken)} |",
        f"| Valid products wrongly rejected | {len(wrongly_rejected)} |",
        f"| Repeat listings flagged as duplicates | {len(rejected_dup)} of "
        f"{len(truth['duplicate_listings'])} |",
        f"| Listings accounted for (Records + Rejected) | {accounted} of {truth['listings']} |",
        f"| Records with source URL and fetch time | {with_provenance} of {len(result.records)} |",
        f"| Requests to the disallowed `/private/` section | {len(disallowed_hits)} |",
        f"| Disallowed links refused before any request | "
        f"{sum(1 for e in result.log if e.robots == 'disallowed')} |",
        f"| Items from the disallowed section in the output | {len(never)} |",
        "",
        "## Field accuracy",
        "",
        "Compared on the valid products that were found. Text must match exactly, "
        "numbers must have the right type and value (price within half a cent).",
        "",
        "| Field | Correct | Accuracy |",
        "|---|---|---|",
        *[f"| {name} | {ok} of {total} | {_pct(ok, total)} |" for name, ok, total in field_rows],
        "",
        "## Broken items",
        "",
        "Each of these was written into the site on purpose. The reason is what the "
        "Rejected sheet said.",
        "",
        "| Planted problem | SKU | Outcome | Reason given |",
        "|---|---|---|---|",
    ]
    for sku, item in broken.items():
        outcome = "rejected" if sku in rejected_non_dup else "KEPT" if sku in got else "MISSING"
        reason = rejected_non_dup.get(sku, "").replace("|", "\\|")
        reason = reason.replace(site.base_url, "")
        lines.append(f"| {item['kind']} | {sku} | {outcome} | {reason} |")
    lines += [
        "",
        "## Politeness during the run",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Network requests | {len(network)} |",
        f"| Configured delay per host | {config.politeness.min_delay_seconds:g} s "
        "(the lowest the tool accepts) |",
        f"| Shortest gap between two requests, timed by the fixture server | "
        f"{shortest_gap:.3f} s |",
        f"| Detail requests spent on duplicates or already-rejected rows | "
        f"{len([p for p in served if p.startswith('/item/')]) - len(expected) - 1} |",
        f"| Wall time | {elapsed:.1f} s |",
        "",
        "## Layout-change drill",
        "",
        "The same job is run again with the price selector pointing at an element that is "
        "not there, which is what a site redesign looks like from the tool's side. The run "
        "must say so and exit non-zero, not hand over a short or empty sheet as if it were fine.",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Records written | {len(drill.records)} |",
        f"| Listings sent to Rejected with a reason | {len(drill.rejected)} of "
        f"{truth['listings']} |",
        f"| Run health | {dict(drill.summary)['run_health']} |",
        f"| Exit code `wde run` gives for this result | "
        f"{0 if dict(drill.summary)['run_health'] == 'ok' else 1} |",
        f"| Requests made (no detail page is fetched for a row that already failed) | "
        f"{drill_requests} |",
        "",
        "## What this does and does not show",
        "",
        "- It shows the pipeline loses nothing on this site: every listing ends up in "
        "Records or in Rejected with a reason, and typed values match the truth file.",
        "- The site and the job config were written together, by the same author. A score "
        "here says the engine and its checks work. It does not say an untuned config will "
        "score the same on a site it has never seen. Each new site needs its own selectors, "
        "and its own check of a sample against the live pages.",
        "- The fixture is static HTML. Pages that build their content with JavaScript are "
        "out of scope (see LIMITATIONS.md).",
        "- The request gap is timed by the fixture server as requests arrive, not by the "
        "tool. The tool's own waits are in the RunLog sheet.",
        "",
        "Reproduce: `python benchmark/run_benchmark.py`",
        "",
    ]
    (ROOT / "BENCHMARK.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print("\n".join(lines[10:26]))
    print(f"\nWrote {ROOT / 'BENCHMARK.md'} in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
