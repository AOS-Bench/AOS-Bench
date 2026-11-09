"""CLI entrypoint: ``python -m aos_bench.traj_analysis``.

    python -m aos_bench.traj_analysis \
        --jobs-dir jobs \
        --out analysis

The jobs tree is opened read-only; nothing under ``--jobs-dir`` is written or
mutated, which matters because this is routinely run against a live run while
sibling trials are still executing.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from aos_bench.traj_analysis import report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aos_bench.traj_analysis",
        description="Analyse completed AOSBench stream trials (health, metrics, performance, behaviour).",
    )
    parser.add_argument(
        "--jobs-dir",
        required=True,
        type=Path,
        help="Harbor jobs directory containing main-<arm>-stream-<tier>/ subdirs (read-only).",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output directory for summary.json / trials.csv / findings.csv / report.md.",
    )
    parser.add_argument(
        "--arm",
        action="append",
        default=None,
        help="Restrict to these arms (repeatable). Default: all discovered.",
    )
    parser.add_argument(
        "--tier",
        action="append",
        default=None,
        help="Restrict to these tiers (repeatable). Default: all discovered.",
    )
    parser.add_argument(
        "--extra-dir",
        action="append",
        default=None,
        type=Path,
        help=(
            "Additional *flattened* trial dir to fold in, e.g. "
            "jobs-clean. Layout is "
            "<job>__<stream>__<suffix>/ rather than <job>/<trial>/. Repeatable. "
            "Trials keep a source_cohort label so preserved data is never "
            "silently mixed with the live tree."
        ),
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress the stdout totals line."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.jobs_dir.is_dir():
        print(f"jobs dir not found: {args.jobs_dir}", file=sys.stderr)
        return 2

    bundles = report.analyse(args.jobs_dir)
    for extra in args.extra_dir or []:
        if not extra.is_dir():
            print(f"extra dir not found: {extra}", file=sys.stderr)
            return 2
        bundles.extend(report.analyse_flat(extra))
    if args.arm:
        keep = set(args.arm)
        bundles = [b for b in bundles if b.record.arm in keep]
    if args.tier:
        keep = set(args.tier)
        bundles = [b for b in bundles if b.record.tier in keep]

    if not bundles:
        print("no trials matched", file=sys.stderr)
        return 1

    summary = report.build_summary(bundles)
    report.write_outputs(args.out, bundles, summary)

    if not args.quiet:
        print(json.dumps(summary["totals"], sort_keys=True))
        print(f"wrote {args.out}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
