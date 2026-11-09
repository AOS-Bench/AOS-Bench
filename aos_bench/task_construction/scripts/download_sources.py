"""Clone Tier-A source repos into a local cache for assembly.

Sources currently fetched:
- Terminal-Bench 2.0: https://github.com/laude-institute/terminal-bench-2
- SkillsBench:        https://github.com/benchflow-ai/skillsbench
- AutomationBench:    https://github.com/zapier/AutomationBench (public tasks)

Both are public GitHub repos. We do shallow clones (`--depth 1`) into the
cache so the on-disk footprint stays small.

Usage::

    python -m aos_bench.task_construction.scripts.download_sources \\
        [--cache-dir data/harbor] \\
        [--no-skillsbench] [--no-tb2]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# Default to <repo>/data/harbor (gitignored). Override with $AOS_TASK_CACHE.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE = Path(os.environ.get("AOS_TASK_CACHE", str(_REPO_ROOT / "data" / "harbor")))


SOURCES: dict[str, dict[str, str]] = {
    "terminal-bench-2": {
        "url": "https://github.com/laude-institute/terminal-bench-2.git",
        "subdir": "terminal-bench-2",
    },
    "skillsbench": {
        "url": "https://github.com/benchflow-ai/skillsbench.git",
        "subdir": "skillsbench",
    },
    "automationbench-public": {
        "url": "https://github.com/zapier/AutomationBench.git",
        "subdir": "automationbench-public",
        # The public task set is executable Python, so pin both code and data.
        "revision": "a321764ace3cfbe42289e6a13abef2f0f4f56fad",
    },
}


def _git(args: Iterable[str], **kw) -> int:
    cmd = ["git"]
    # Prefer an explicitly configured process proxy. Only clear inherited Git
    # config when the process has no proxy environment of its own; otherwise
    # ``-c http.proxy=`` also disables the working environment proxy.
    has_env_proxy = any(
        os.environ.get(name)
        for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
    )
    if not has_env_proxy:
        cmd.extend(["-c", "http.proxy=", "-c", "https.proxy="])
    cmd.extend(args)
    return subprocess.call(cmd, **kw)


def _clone_or_update(
    name: str,
    url: str,
    dest: Path,
    *,
    force_refresh: bool,
    revision: str | None = None,
) -> None:
    if dest.exists():
        if force_refresh:
            shutil.rmtree(dest)
        else:
            print(f"[skip] {name}: {dest} already exists (use --refresh to overwrite)")
            return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[clone] {name} → {dest}")
    rc = _git(["clone", "--depth", "1", "--no-checkout", url, str(dest)])
    if rc != 0:
        raise SystemExit(f"git clone {url} failed (exit {rc})")
    if revision:
        rc = _git(["-C", str(dest), "fetch", "--depth", "1", "origin", revision])
        if rc != 0:
            raise SystemExit(f"git fetch {url}@{revision} failed (exit {rc})")
        target = "FETCH_HEAD"
    else:
        target = "HEAD"
    rc = _git(["-C", str(dest), "checkout", "--detach", target])
    if rc != 0:
        raise SystemExit(f"git checkout {name}@{revision or 'HEAD'} failed (exit {rc})")


def prepare_source(name: str, cache_dir: Path, *, force_refresh: bool = False) -> Path:
    """Prepare one named source and return its checkout directory."""
    try:
        info = SOURCES[name]
    except KeyError as exc:
        raise ValueError(f"unknown source {name!r}; choose from {sorted(SOURCES)}") from exc
    dest = cache_dir / info["subdir"]
    _clone_or_update(
        name,
        info["url"],
        dest,
        force_refresh=force_refresh,
        revision=info.get("revision"),
    )
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE,
        help=f"Cache directory (default: {DEFAULT_CACHE})",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-clone even if cache dirs already exist",
    )
    for name in SOURCES:
        parser.add_argument(
            f"--no-{name}",
            dest=f"include_{name.replace('-', '_')}",
            action="store_false",
            default=True,
            help=f"Skip {name}",
        )
    args = parser.parse_args(argv)

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"[cache] {args.cache_dir}")

    for name, info in SOURCES.items():
        flag = getattr(args, f"include_{name.replace('-', '_')}")
        if not flag:
            print(f"[skip] {name} (disabled)")
            continue
        prepare_source(name, args.cache_dir, force_refresh=args.refresh)

    print("[done] sources ready in", args.cache_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
