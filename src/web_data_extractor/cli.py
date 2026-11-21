"""Command line entry point: wde run | validate | demo | serve-fixture."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

from .config import Config, ConfigError, load_config
from .export import write_outputs
from .fetch import MIN_DELAY_FLOOR, USER_AGENT
from .fixture_server import serve
from .runner import run

DEMO_PORT = 8731


def _vars(pairs: list[str]) -> dict[str, str]:
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"--var expects name=value, got {pair!r}.")
        name, value = pair.split("=", 1)
        out[name] = value
    return out


def _load(args: argparse.Namespace, extra: dict[str, str] | None = None) -> Config:
    config = load_config(args.config, {**_vars(args.var), **(extra or {})})
    if getattr(args, "max_pages", None):
        config = replace(config, max_pages=args.max_pages)
    if getattr(args, "no_cache", False):
        config = replace(config, cache_dir=None)
    return config


def _run_and_report(config: Config, out_dir: str) -> int:
    print(
        f"Job {config.name}: {len(config.start_urls)} start URL(s), up to {config.max_pages} "
        f"list page(s), at least {config.politeness.min_delay_seconds:g}s between requests."
    )
    result = run(config)
    paths = write_outputs(result, out_dir)
    for key, value in result.summary:
        print(f"  {key}: {value}")
    print(f"Wrote {paths['xlsx']} and 4 CSV files in {Path(out_dir)}")
    # A run that was blocked or came back empty must not look like a success to a scheduler.
    return 0 if dict(result.summary)["run_health"] == "ok" else 1


def _cmd_run(args: argparse.Namespace) -> int:
    return _run_and_report(_load(args), args.out)


def _cmd_validate(args: argparse.Namespace) -> int:
    config = _load(args)
    print(f"OK: {config.name}")
    print(f"  start_urls: {', '.join(config.start_urls)}")
    print(f"  allowed_hosts: {', '.join(sorted(config.allowed_hosts))}")
    print(f"  fields: {', '.join(f.name for f in config.fields)}")
    print(f"  delay: {config.politeness.min_delay_seconds:g}s (floor {MIN_DELAY_FLOOR:g}s)")
    print(f"  user-agent: {USER_AGENT}")
    unchecked = config.hosts_needing_terms_check
    if unchecked:
        print(f"  NOT RUNNABLE YET: no terms_check recorded for {', '.join(unchecked)}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    with serve(args.site, args.port) as site:
        print(f"Serving the bundled fixture site from {args.site} at {site.base_url}")
        code = _run_and_report(_load(args, {"base_url": site.base_url}), args.out)
        private = [p for p in site.paths if p.startswith("/private/")]
        print(f"Requests the fixture server saw under /private/ (disallowed): {len(private)}")
    return code


def _cmd_serve(args: argparse.Namespace) -> int:
    with serve(args.site, args.port) as site:
        print(f"Fixture site at {site.base_url} (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wde", description="Config-driven web data extraction that obeys robots.txt."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def config_options(p: argparse.ArgumentParser, default: str | None = None) -> None:
        if default:
            p.add_argument("--config", default=default, help=f"job config (default {default})")
        else:
            p.add_argument("config", help="path to a YAML job config")
        p.add_argument(
            "--var",
            action="append",
            default=[],
            metavar="NAME=VALUE",
            help="override a ${variable} used in the config",
        )

    def run_options(p: argparse.ArgumentParser, out: str) -> None:
        p.add_argument("--out", default=out, help=f"output folder (default {out})")
        p.add_argument("--max-pages", type=int, help="override limits.max_pages")
        p.add_argument("--no-cache", action="store_true", help="do not read or write the cache")

    p_run = sub.add_parser("run", help="run a job and write CSV + XLSX")
    config_options(p_run)
    run_options(p_run, "out")
    p_run.set_defaults(func=_cmd_run)

    p_val = sub.add_parser("validate", help="check a config without any network use")
    config_options(p_val)
    p_val.set_defaults(func=_cmd_validate)

    p_demo = sub.add_parser("demo", help="serve the bundled fixture site locally and extract it")
    config_options(p_demo, "configs/fixture.yaml")
    run_options(p_demo, "out/fixture")
    p_demo.add_argument("--site", default="fixture_site", help="fixture site folder")
    p_demo.add_argument("--port", type=int, default=DEMO_PORT)
    p_demo.set_defaults(func=_cmd_demo)

    p_serve = sub.add_parser("serve-fixture", help="serve the bundled fixture site until stopped")
    p_serve.add_argument("--site", default="fixture_site")
    p_serve.add_argument("--port", type=int, default=DEMO_PORT)
    p_serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
