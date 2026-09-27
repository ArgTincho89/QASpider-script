"""Command-line interface for the standalone crawler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .crawl import MAX_BROWSER_RECOVERIES, crawl_site


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventory rendered elements on a same-origin website.")
    parser.add_argument("url", help="HTTP(S) URL to start crawling")
    parser.add_argument("--output", default="inventory.json", help="JSON output path (default: inventory.json)")
    parser.add_argument("--max-pages", type=_positive_int, default=None, help="Optional emergency page cap")
    parser.add_argument(
        "--depth",
        "--max-depth",
        dest="max_depth",
        type=_non_negative_int,
        default=None,
        help=(
            "Follow links at most N path segments below the start URL "
            "(e.g. --depth 2 on https://site.com/ covers https://site.com/1/2). "
            "Default: no limit"
        ),
    )
    parser.add_argument("--timeout", type=_positive_int, default=30_000, help="Navigation timeout in milliseconds (default: 30000)")
    return parser


def _write_report(output_path: Path, payload: dict) -> bool:
    """Serialize a report to disk, reporting failure instead of raising."""
    try:
        output_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        return False
    return True


def _failure_report(url: str, max_pages: int | None, max_depth: int | None) -> dict:
    """Build the minimal report written when the crawl cannot return a result.

    It carries no part of the underlying error: an unexpected failure may hold
    a Playwright diagnostic, a call log, headers, or cookies, and none of that
    may reach a file the caller will keep or upload.
    """
    return {
        "startUrl": url,
        "pages": [],
        "stats": {
            "discovered": 0,
            "processed": 0,
            "failed": 0,
            "maxPages": max_pages,
            "maxDepth": max_depth,
            "pending": 0,
            "depthSuppressed": 0,
            "complete": False,
            "truncated": False,
            "issues": [
                {
                    "url": url,
                    "error": "Crawler stopped before it could return a report.",
                }
            ],
            "browserLosses": 0,
            "browserRecoveries": 0,
            "maxBrowserRecoveries": MAX_BROWSER_RECOVERIES,
            "stopReason": "crawler-failed",
            "teardownFailures": 0,
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_path = Path(args.output)
    try:
        result = crawl_site(
            args.url,
            max_pages=args.max_pages,
            max_depth=args.max_depth,
            timeout_ms=args.timeout,
        )
    except Exception:
        # The run is already lost, but the caller still gets a file to look at
        # and a non-zero exit code. The diagnostic stays in the process.
        report = _failure_report(args.url, args.max_pages, args.max_depth)
        if not _write_report(output_path, report):
            print("Crawler failed and the failure report could not be written.", file=sys.stderr)
            return 1
        print(f"Crawler failed before it produced a report; wrote {output_path}.", file=sys.stderr)
        return 1
    if not _write_report(output_path, result):
        print("Crawler report could not be written.", file=sys.stderr)
        return 1
    print(f"Wrote {len(result['pages'])} page records to {output_path}")
    return 0
