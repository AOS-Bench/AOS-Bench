#!/usr/bin/env python3
"""Scan oracle trial dirs for streams meeting a pass-ratio bar.

Answers "which streams already have oracle evidence at >= the bar but are not
in full-valid" without re-running anything. There are ~1155 heavy trial dirs on
this host across many sweep roots, so the results are already paid for; the
question is only which of them clear the bar.

How a trial's pass ratio is derived: each task in a stream produces a reward,
and a task counts as passed only on reward == 1.0 (exact pass). The ratio is
passed/total for that trial. A stream is reported at its BEST trial, since a
later rerun supersedes an earlier failure -- but the trial dir is printed so the
claim stays checkable.

Reward location varies by sweep generation, so several shapes are accepted. A
stream whose reward file cannot be parsed is reported as unknown rather than
silently dropped, because "no evidence" and "evidence of failure" are different
answers and conflating them would overstate the pool.

Usage:
    scan_oracle_pass_ratio.py --tier heavy --bar 0.90
    scan_oracle_pass_ratio.py --tier heavy --bar 0.90 --not-in-full-valid
"""

from __future__ import annotations

import argparse
import json
import pathlib
import os
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
TIER_V2 = REPO / "data/stream/V1-0/tier-v2"
FULL_VALID_DIRS = ("full-valid", "full-valid-0915-new", "full-valid-diverse0915")

SEARCH_ROOTS = (pathlib.Path(os.environ.get("AOS_BENCH_JOBS_ROOT", "jobs")),)


def promoted_streams() -> set[str]:
    out: set[str] = set()
    for d in FULL_VALID_DIRS:
        root = TIER_V2 / d
        if not root.is_dir():
            continue
        for tier_dir in root.iterdir():
            if not tier_dir.is_dir():
                continue
            for stream in tier_dir.iterdir():
                if stream.is_dir() and not stream.name.startswith("."):
                    out.add(stream.name)
    return out


def _rewards_from(obj: object) -> list[float]:
    """Pull per-task rewards out of whatever shape this generation wrote.

    The canonical location on this host is
    `result.json -> verifier_result.rewards`, a {task_id: reward} dict for the
    whole stream -- one trial dir per STREAM, not per task. Verified against
    heavy-20-scan-226, whose promoted evidence records 19/20 and whose rewards
    dict has 20 entries with one non-1.0 value.
    """
    if isinstance(obj, dict):
        vr = obj.get("verifier_result")
        if isinstance(vr, dict):
            rw = vr.get("rewards")
            if isinstance(rw, dict) and rw:
                return [float(v) for v in rw.values() if isinstance(v, (int, float))]
            if isinstance(rw, list) and rw:
                return [float(v) for v in rw if isinstance(v, (int, float))]

    rewards: list[float] = []

    if isinstance(obj, dict):
        # {"results": [{"reward": 1.0}, ...]} and its aliases
        for key in ("results", "trials", "tasks", "task_results", "per_task"):
            entries = obj.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        for rk in ("reward", "score", "value"):
                            if isinstance(entry.get(rk), (int, float)):
                                rewards.append(float(entry[rk]))
                                break
                if rewards:
                    return rewards
        # {"rewards": {"task": 1.0}} or {"rewards": [1.0, ...]}
        rw = obj.get("rewards")
        if isinstance(rw, dict):
            rewards = [float(v) for v in rw.values() if isinstance(v, (int, float))]
        elif isinstance(rw, list):
            rewards = [float(v) for v in rw if isinstance(v, (int, float))]
    elif isinstance(obj, list):
        for entry in obj:
            if isinstance(entry, dict):
                for rk in ("reward", "score"):
                    if isinstance(entry.get(rk), (int, float)):
                        rewards.append(float(entry[rk]))
                        break
    return rewards


def trial_ratio(trial_dir: pathlib.Path) -> tuple[float, int, int] | None:
    for name in ("result.json", "results.json", "reward.json", "rewards.json"):
        f = trial_dir / name
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        rewards = _rewards_from(data)
        if rewards:
            passed = sum(1 for r in rewards if r == 1.0)
            return passed / len(rewards), passed, len(rewards)
    # Some generations write one reward file per task under a subdir.
    per_task = list(trial_dir.glob("*/reward.json")) + list(
        trial_dir.glob("*/*/reward.json")
    )
    rewards: list[float] = []
    for f in per_task:
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, (int, float)):
            rewards.append(float(data))
        elif isinstance(data, dict):
            for rk in ("reward", "score", "value"):
                if isinstance(data.get(rk), (int, float)):
                    rewards.append(float(data[rk]))
                    break
    if rewards:
        passed = sum(1 for r in rewards if r == 1.0)
        return passed / len(rewards), passed, len(rewards)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="heavy")
    ap.add_argument("--bar", type=float, default=0.90)
    ap.add_argument("--not-in-full-valid", action="store_true")
    ap.add_argument("--limit-dirs", type=int, default=0)
    args = ap.parse_args()

    pattern = re.compile(rf"^{re.escape(args.tier)}-\d+[\w.-]*__[A-Za-z0-9]+$")

    trial_dirs: list[pathlib.Path] = []
    for root in SEARCH_ROOTS:
        if not root.is_dir():
            continue
        for p in root.glob("**/jobs/*/*__*"):
            if p.is_dir() and pattern.match(p.name):
                trial_dirs.append(p)
                if args.limit_dirs and len(trial_dirs) >= args.limit_dirs:
                    break

    best: dict[str, tuple[float, int, int, pathlib.Path]] = {}
    unknown: set[str] = set()
    for d in trial_dirs:
        stream = d.name.split("__")[0]
        got = trial_ratio(d)
        if got is None:
            unknown.add(stream)
            continue
        ratio, passed, total = got
        prev = best.get(stream)
        if prev is None or ratio > prev[0]:
            best[stream] = (ratio, passed, total, d)

    already = promoted_streams()

    print(f"trial dirs scanned: {len(trial_dirs)}")
    print(f"streams with parsable rewards: {len(best)}")
    print(f"streams with evidence but unparsable: {len(unknown - set(best))}")
    print()

    rows = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    hit = [r for r in rows if r[1][0] >= args.bar]
    new = [r for r in hit if r[0] not in already]

    print(f"=== at or above {args.bar:.0%}: {len(hit)} streams ===")
    for name, (ratio, passed, total, d) in hit:
        tag = "" if name in already else "  << NOT in full-valid"
        print(f"  {name:28s} {passed:2d}/{total:<2d} {ratio:6.1%}{tag}")

    print()
    print(f"=== would be NEW at this bar: {len(new)} ===")
    for name, (ratio, passed, total, d) in new:
        print(f"  {name:28s} {passed:2d}/{total:<2d} {ratio:6.1%}")
        print(f"      {d}")

    if not args.not_in_full_valid:
        below = [r for r in rows if r[1][0] < args.bar][:15]
        if below:
            print()
            print(f"=== below the bar (top {len(below)} by ratio) ===")
            for name, (ratio, passed, total, _) in below:
                print(f"  {name:28s} {passed:2d}/{total:<2d} {ratio:6.1%}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
