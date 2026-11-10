#!/usr/bin/env python3
"""Measure what Tedd's reads deliver from synthetic projects larger than one recall."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from tedd_project_recall.runner import run_lab


def parser() -> argparse.ArgumentParser:
    """Build the intentionally path-free research CLI."""

    result = argparse.ArgumentParser(
        description=(
            "Evaluate frozen reads of large synthetic projects in a private disposable graph."
        )
    )
    result.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="per-command timeout; does not alter projects or measurements",
    )
    result.add_argument(
        "--pretty",
        action="store_true",
        help="indent the sanitized JSON report",
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run the frozen protocol and emit exactly one report to stdout."""

    args = parser().parse_args(argv)
    try:
        report = run_lab(timeout_seconds=args.timeout_seconds)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"project recall evaluation failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
