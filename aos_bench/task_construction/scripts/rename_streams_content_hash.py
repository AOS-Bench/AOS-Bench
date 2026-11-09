#!/usr/bin/env python3
"""Rename streams to a content-derived scheme: v1-<tier>-<size>-<hash8>.

Why content-derived rather than a random uid: the same task set must always
produce the same name. That makes a re-run reproducible, makes diffs between
two deliveries meaningful, and makes duplicate streams (same tasks, different
name) detectable instead of silently shipping twice.

Name layout, e.g. `v1-heavy-20-7c1a9f3b`:

    v1          schema generation
    heavy       tier (heavy-plus is shortened to hplus)
    20          task count -- kept visible on purpose: the overlap gate is
                |A&B| / min(|A|,|B|), so the smaller stream binds, and having
                the size in the name shows at a glance which side that is
    7c1a9f3b    first 8 hex of sha256 over the sorted task-id list

Why 8 hex and not 3. Collisions follow the birthday bound n^2/2N, not n/N, so
short hashes fail much earlier than intuition suggests. Measured:

    hex digits   buckets        26 streams   132      418      1000
    3                4,096          7.63%   87.89%  100.00%  100.00%
    4               65,536          0.49%   12.36%   73.55%   99.95%
    6           16,777,216          0.00%    0.05%    0.52%    2.93%
    8        4,294,967,296          0.00%    0.00%    0.00%    0.01%

8 digits keeps 1000 streams at 0.01%. But probability is not a guarantee, so
collisions are also *detected*: if two different task sets would take the same
name, the hash is lengthened for that stream until unique. Two streams with an
IDENTICAL task set are a different thing -- they legitimately map to one name,
and that is reported as a duplicate rather than renamed around.

What is deliberately NOT changed: `[environment].docker_image`. The tag embeds
the old stream name and the image is already pushed under it; worse, some tools
reconstruct the tag from the stream name (oracle_sweep.py's reuse path,
rebuild_v1_0_imagehub.py). Rewriting the name but not the registry would make
those paths look for an image that does not exist. The tag stays as the
historical record of which build produced this stream, and a `legacy_name`
field preserves the old identifier.

Usage:
    rename_streams_content_hash.py --root data/stream/V1-0/valid-0917 --dry-run
    rename_streams_content_hash.py --root data/stream/V1-0/valid-0917
    rename_streams_content_hash.py --root ... --undo   # revert from the map
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import tomllib

TIER_ABBREV = {"heavy-plus": "hplus"}
MAP_NAME = ".rename-map.json"


def task_ids(stream_dir: pathlib.Path) -> list[str]:
    spec = tomllib.loads((stream_dir / "stream.toml").read_text())
    return sorted(
        str(a["task_id"])
        for a in spec.get("arrivals", [])
        if isinstance(a, dict) and a.get("task_id")
    )


def content_hash(ids: list[str], digits: int) -> str:
    # Newline-joined sorted ids: order-independent, and a task id cannot
    # contain a newline, so no two different sets can serialise identically.
    payload = "\n".join(ids).encode()
    return hashlib.sha256(payload).hexdigest()[:digits]


def new_name(tier: str, ids: list[str], digits: int) -> str:
    t = TIER_ABBREV.get(tier, tier)
    return f"v1-{t}-{len(ids):02d}-{content_hash(ids, digits)}"


def plan(root: pathlib.Path, digits: int) -> tuple[list[tuple], list[tuple]]:
    """Returns (renames, duplicates)."""
    streams: list[tuple[str, str, pathlib.Path, list[str]]] = []
    for tier_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for sd in sorted(p for p in tier_dir.iterdir() if p.is_dir()):
            if not (sd / "stream.toml").is_file():
                continue
            streams.append((tier_dir.name, sd.name, sd, task_ids(sd)))

    # Group by task set: identical sets are duplicates, not collisions.
    by_set: dict[tuple[str, tuple[str, ...]], list[tuple]] = {}
    for tier, name, path, ids in streams:
        by_set.setdefault((tier, tuple(ids)), []).append((tier, name, path, ids))

    duplicates = [v for v in by_set.values() if len(v) > 1]

    # Assign names, lengthening on genuine collision (different set, same name).
    taken: dict[str, tuple[str, tuple[str, ...]]] = {}
    renames: list[tuple] = []
    for (tier, ids_t), group in sorted(by_set.items(), key=lambda kv: kv[0][1]):
        ids = list(ids_t)
        d = digits
        while True:
            cand = new_name(tier, ids, d)
            owner = taken.get(cand)
            if owner is None or owner == (tier, ids_t):
                break
            d += 1
            if d > 64:
                raise RuntimeError(f"cannot disambiguate {cand}")
        taken[cand] = (tier, ids_t)
        for tier_, old, path, _ in group:
            renames.append((tier_, old, cand, path, d))
    return renames, duplicates


def apply_rename(
    root: pathlib.Path, renames: list[tuple], dry: bool
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for tier, old, new, path, digits in renames:
        mapping[new] = old
        if dry:
            extra = f"  (hash lengthened to {digits})" if digits != 8 else ""
            print(f"  {tier}/{old}\n    -> {new}{extra}")
            continue

        spec_path = path / "stream.toml"
        text = spec_path.read_text()
        # Only the stream's own `name` key is rewritten. docker_image is left
        # alone on purpose (see module docstring).
        text, n = re.subn(
            r'(?m)^(name\s*=\s*)"' + re.escape(old) + r'"',
            r'\1"' + new + '"',
            text,
        )
        if n == 0:
            print(f"  WARN {old}: no `name = \"{old}\"` line; not renamed")
            continue
        if "legacy_name" not in text:
            text = text.replace(
                f'name        = "{new}"',
                f'name        = "{new}"\n'
                f'# Renamed to the content-derived scheme on 2026-09-17; the\n'
                f'# image tag still carries the original name, and some tools\n'
                f'# reconstruct the tag from it, so it is preserved here.\n'
                f'legacy_name = "{old}"',
                1,
            )
        spec_path.write_text(text)

        dst = path.parent / new
        if dst.exists() and dst != path:
            print(f"  WARN {new} already exists; leaving {old} in place")
            continue
        if dst != path:
            path.rename(dst)
        print(f"  {tier}/{old} -> {new}")
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--digits", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--undo", action="store_true")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    if not root.is_dir():
        print(f"no such dir: {root}")
        return 1

    map_path = root / MAP_NAME

    if args.undo:
        if not map_path.is_file():
            print(f"no {MAP_NAME} to undo from")
            return 1
        mapping = json.loads(map_path.read_text())
        for new, old in mapping.items():
            for tier_dir in root.iterdir():
                if not tier_dir.is_dir():
                    continue
                cur = tier_dir / new
                if not cur.is_dir():
                    continue
                spec = cur / "stream.toml"
                t = spec.read_text()
                t = re.sub(
                    r'(?m)^(name\s*=\s*)"' + re.escape(new) + r'"',
                    r'\1"' + old + '"',
                    t,
                )
                t = re.sub(r'(?m)^# Renamed to the content-derived.*\n', "", t)
                t = re.sub(r'(?m)^# image tag still carries.*\n', "", t)
                t = re.sub(r'(?m)^# reconstruct the tag from it.*\n', "", t)
                t = re.sub(r'(?m)^legacy_name\s*=.*\n', "", t)
                spec.write_text(t)
                cur.rename(tier_dir / old)
                print(f"  reverted {new} -> {old}")
        map_path.unlink()
        return 0

    renames, duplicates = plan(root, args.digits)

    if duplicates:
        print(f"duplicate task sets ({len(duplicates)} group(s)):")
        for group in duplicates:
            names = ", ".join(g[1] for g in group)
            print(f"  same tasks: {names}")
        print()

    lengthened = [r for r in renames if r[4] != args.digits]
    print(f"{len(renames)} stream(s) to rename, {len(lengthened)} needed a longer hash")
    print()
    mapping = apply_rename(root, renames, args.dry_run)

    if not args.dry_run:
        map_path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")
        print(f"\nrename map written to {map_path} (use --undo to revert)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
