from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .crawl import Crawler
from .db import Database
from .fetch import Fetcher, Politeness
from . import export as export_mod


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_run(args: argparse.Namespace) -> int:
    start_file = Path(__file__).resolve().parent / args.scan
    if not start_file.exists():
        logging.error("Start file not found: %s", start_file)
        return 2

    start_url = start_file.read_text(encoding="utf-8").strip()
    if not start_url:
        logging.error("Start file is empty: %s", start_file)
        return 2

    fetcher = Fetcher(Politeness(delay=args.delay, jitter=args.jitter))
    db = Database(args.db)
    crawler = Crawler(
        db=db,
        fetcher=fetcher,
        start_url=start_url,
        max_pages=args.max_pages,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    try:
        success = crawler.run()
    finally:
        db.close()

    return 0 if success else 1

def cmd_export(args: argparse.Namespace) -> int:
    _setup_logging(getattr(args, "verbose", False))
    export_mod.export(args.db, out_json=args.out_json, out_html=args.out_html, embed=args.embed)
    print("exported")
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="storyset", description="Storyset JSON crawler")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("run", help="run the crawler")
    s.add_argument("-d", "--db", default="storyset/sqlite.db", help="SQLite db path")
    s.add_argument("--scan", default="scan", help="Path to the start URL file relative to storyset/")
    s.add_argument("--max-pages", type=int, default=0)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--delay", type=float, default=2.0)
    s.add_argument("--jitter", type=float, default=1.0)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_run)

    e = sub.add_parser("export", help="export images.json and/or viewer HTML")
    e.add_argument("-d", "--db", default="storyset/sqlite.db", help="SQLite db path")
    e.add_argument("--out-json", help="output JSON path (e.g. storyset/data/images.json)")
    e.add_argument("--out-html", help="output HTML path (e.g. storyset/data/index.html)")
    e.add_argument("--embed", action="store_true", help="embed data into HTML")
    e.add_argument("--verbose", action="store_true")
    e.set_defaults(func=cmd_export)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))
    return args.func(args)

