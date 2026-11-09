"""Generate AOS-Bench streams: dataset × pool × N × seed → data/stream/<name>/.

Each stream is named ``<dataset>-<pool>-<N>-<seed>`` (e.g. ``tb2-swe-3-42``).

Sampling is deterministic given (pool, seed): we sort candidate tasks by
task_id, then call ``random.Random(seed).sample(...)``. So if a new task gets
added to the source pool later, the older streams are NOT silently
re-shuffled — but the next generated stream picks a fresh draw.

Usage::

    # 默认矩阵：tb2 × {swe, ml-ds, sci, security} × {3, 5, 10}
    python -m aos_bench.task_construction.scripts.generate_streams

    # 仅 tb2-swe，N=3 和 5
    python -m aos_bench.task_construction.scripts.generate_streams \\
        --dataset tb2 --pool swe --n 3 5

    # 自定义 seed
    python -m aos_bench.task_construction.scripts.generate_streams --seed 7

    # 指向自定义源 / 输出目录
    python -m aos_bench.task_construction.scripts.generate_streams \\
        --harbor-dir data/harbor --out-dir data/stream
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from aos_bench.task_construction.assemble.assemble import (
    StreamSpec,
    TaskRef,
    assemble,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]


# Default repo-relative locations.
DEFAULT_HARBOR_DIR = _REPO_ROOT / "data" / "harbor"
DEFAULT_OUT_DIR = _REPO_ROOT / "data" / "stream"
DEFAULT_BLACKLIST = _REPO_ROOT / "data" / "oracle_blacklist.txt"


def _load_blacklist(path: Path) -> set[str]:
    """Read a blacklist file: one task_id per line, # comments + blanks allowed.
    Inline comments after the task_id are also allowed (split on '#'/whitespace).
    """
    if not path.is_file():
        return set()
    out: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # strip inline comment + whitespace; first token is the task_id
        tid = line.split("#", 1)[0].strip().split()[0]
        out.add(tid)
    return out


# ---------------------------------------------------------------------------
# Pool definitions: which source `category` strings belong to which pool.
# ---------------------------------------------------------------------------


# Each entry maps a pool name to:
#   - dataset (which subdir under <harbor>/),
#   - tasks_subdir (where tasks live, "" for repo root, "tasks" for skillsbench),
#   - categories (set of category strings considered in-pool).
@dataclass(frozen=True)
class PoolDef:
    dataset_key: str  # "tb2" / "skillsbench"
    pool: str  # human-readable suffix (e.g. "swe")
    repo_subdir: str  # under <harbor>/, where tasks live
    categories: tuple[str, ...]


_TB2_REPO = "terminal-bench-2"
_SKB_REPO = "skillsbench"
_WCB_REPO = "wildclawbench-converted"


_POOLS: list[PoolDef] = [
    # ── TB-2 ──────────────────────────────────────────────────────────────
    PoolDef(
        dataset_key="tb2",
        pool="swe",
        repo_subdir=_TB2_REPO,
        categories=(
            "software-engineering",
            "debugging",
            "system-administration",
        ),
    ),
    PoolDef(
        dataset_key="tb2",
        pool="ml-ds",
        repo_subdir=_TB2_REPO,
        categories=(
            "machine-learning",
            "model-training",
            "data-science",
            "data-processing",
        ),
    ),
    PoolDef(
        dataset_key="tb2",
        pool="sci",
        repo_subdir=_TB2_REPO,
        categories=("scientific-computing", "mathematics"),
    ),
    PoolDef(
        dataset_key="tb2",
        pool="security",
        repo_subdir=_TB2_REPO,
        categories=("security",),
    ),
    # ── SkillsBench ──────────────────────────────────────────────────────
    PoolDef(
        dataset_key="skillsbench",
        pool="security",
        repo_subdir=f"{_SKB_REPO}/tasks",
        categories=("security", "Security", "Security & CTF"),
    ),
    PoolDef(
        dataset_key="skillsbench",
        pool="data",
        repo_subdir=f"{_SKB_REPO}/tasks",
        categories=(
            "data-analysis",
            "data-processing",
            "data-science",
            "data-validation",
            "data-comparison",
            "data statistics",
            "document-processing",
            "document-generation",
            "document-editing",
            "latex-extraction",
            "spreadsheet",
            "Data Visualization",
            # extended: media + office + image processing
            "image-processing",
            "media-processing",
            "multimodal-processing",
            "office-suite",
            "Office Operation",
            "speech-synthesis",
            "file-management",
        ),
    ),
    PoolDef(
        dataset_key="skillsbench",
        pool="science",
        repo_subdir=f"{_SKB_REPO}/tasks",
        categories=(
            "astronomy",
            "seismology",
            "geophysics",
            "materials_science",
            "chemistry",
            "environmental-modeling",
            "quantum-simulation",
            "engineering",
            "research",
            # extended: control systems + 3D graphics
            "control-systems",
            "3d-graphics",
        ),
    ),
    PoolDef(
        dataset_key="skillsbench",
        pool="engineering",
        repo_subdir=f"{_SKB_REPO}/tasks",
        # NEW pool — pulls in software/systems-engineering tasks that were
        # previously orphaned (no matching pool). These are mostly hard tasks
        # in build/compile/legacy/parallelism domains, which is what V0-2
        # heavy streams need.
        categories=(
            "Compilation & Build",
            "Code Translation",
            "code reproduction",
            "erlang bugfix",
            "flink",
            "ML/NLP",
            "formal method",
            "Parallelization",
            "Legacy Systems",
            "devops-analytics",
            "bgp-route",
            "enterprise-search",
            "web-performance",
        ),
    ),
    # ── WildClawBench (converted from Markdown) ──────────────────────────
    # Source has 6 categories. Social Interaction is multi-turn dialogue
    # which doesn't fit the single-instruction stream model — skip it.
    PoolDef(
        dataset_key="wildclaw",
        pool="productivity",
        repo_subdir=_WCB_REPO,
        categories=("01_Productivity_Flow",),
    ),
    PoolDef(
        dataset_key="wildclaw",
        pool="code",
        repo_subdir=_WCB_REPO,
        categories=("02_Code_Intelligence",),
    ),
    PoolDef(
        dataset_key="wildclaw",
        pool="search",
        repo_subdir=_WCB_REPO,
        categories=("04_Search_Retrieval",),
    ),
    PoolDef(
        dataset_key="wildclaw",
        pool="creative",
        repo_subdir=_WCB_REPO,
        categories=("05_Creative_Synthesis",),
    ),
    PoolDef(
        dataset_key="wildclaw",
        pool="safety",
        repo_subdir=_WCB_REPO,
        categories=("06_Safety_Alignment",),
    ),
]


_DEFAULT_NS = (3, 5, 10)
_DEFAULT_SEED = 42


# WildClawBench tasks whose grader sends an image to the model (VLM judge,
# not text-only LLM judge). Without a working VLM endpoint, these always
# score 0 regardless of agent or oracle output — so they're filtered from
# the candidate pool for streams that target text-only LLM evaluation.
_WILDCLAW_VLM_TASKS: frozenset[str] = frozenset({
    # 02 Code Intelligence — homepage screenshots / jigsaw assemblies / image puzzles
    "02_Code_Intelligence_task_10_acad_homepage_zh",
    "02_Code_Intelligence_task_11_resume_homepage_zh",
    "02_Code_Intelligence_task_12_connect_the_dots_hard_zh",
    "02_Code_Intelligence_task_3_jigsaw_puzzle_zh",
    "02_Code_Intelligence_task_4_jigsaw_puzzle_medium_zh",
    "02_Code_Intelligence_task_5_jigsaw_puzzle_hard_zh",
    "02_Code_Intelligence_task_7_connect_the_dots_medium_img_zh",
    "02_Code_Intelligence_task_8_link_a_pix_color_zh",
    "02_Code_Intelligence_task_9_link_a_pix_color_easy_zh",
    # 05 Creative Synthesis — generated posters / slides / outfit images
    "05_Creative_Synthesis_task_10_social_poster_multi_crop",
    "05_Creative_Synthesis_task_3_product_poster",
    "05_Creative_Synthesis_task_6_clothing_outfit_to_model_image",
    "05_Creative_Synthesis_task_7_paper_to_poster",
    "05_Creative_Synthesis_task_8_repo_to_homepage",
    "05_Creative_Synthesis_task_9_repo_to_slides",
})


# ---------------------------------------------------------------------------
# Task discovery.
# ---------------------------------------------------------------------------


def _is_encrypted(task_dir: Path) -> bool:
    env_dir = task_dir / "environment"
    if not env_dir.is_dir():
        return False
    return any(p.name.endswith(".enc") for p in env_dir.glob("*"))


def _is_multistage(dockerfile_path: Path) -> bool:
    try:
        text = dockerfile_path.read_text(errors="ignore")
    except OSError:
        return False
    n = 0
    for line in text.splitlines():
        if line.lstrip().upper().startswith("FROM "):
            n += 1
    return n >= 2


# Image prefixes known to be apt-compatible (Debian / Ubuntu lineage). Anything
# else uses a different package manager and won't survive being merged onto an
# ubuntu base — `RUN dnf install ...` becomes `dnf: not found`.
_APT_COMPATIBLE_PREFIXES = (
    "ubuntu:",
    "debian:",
    "python:",  # python:3.x-slim* / python:3.x-bookworm / python:3.x-bullseye
    "node:",  # node:*-slim
    "buildpack-deps:",
    "ros:",
    "tensorflow/tensorflow:",  # ubuntu-based
    "pytorch/pytorch:",  # debian-based
    "nvidia/cuda:",  # ubuntu-based
)


def _has_apt_compatible_base(dockerfile_path: Path) -> bool:
    try:
        text = dockerfile_path.read_text(errors="ignore")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.upper().startswith("FROM "):
            # Take everything after FROM, then strip optional `AS <name>`.
            image = stripped[5:].split(" AS ", 1)[0].split(" as ", 1)[0].strip()
            return any(image.startswith(p) for p in _APT_COMPATIBLE_PREFIXES)
    # No FROM line at all — let downstream validation handle it.
    return True


def _has_unbuildable_pattern(dockerfile_path: Path) -> tuple[bool, str | None]:
    """Detect source-Dockerfile patterns we know break the merger / build.

    Returns (is_bad, reason).
    """
    try:
        text = dockerfile_path.read_text(errors="ignore")
    except OSError:
        return False, None
    if "deb-src " in text or "apt-get source " in text:
        # `apt-get source` requires a deb-src line for the right distro; tasks
        # that hardcode deb.debian.org on an ubuntu base can't fetch source.
        return True, "uses apt-get source / deb-src"
    if "playwright install" in text:
        # `playwright install --with-deps chromium` downloads ~130 MB browser
        # binaries from a non-mirrored CDN; routinely times out.
        return True, "downloads playwright browsers"
    if "openaipublic.blob.core.windows.net" in text:
        # Azure blob hosting GPT-2 weights — not behind any CN-friendly mirror.
        return True, "downloads openaipublic Azure blob"
    if "syzkaller.git" in text:
        # 4 GB+ repo; kkgithub returns 504 trying to proxy a packfile that big.
        return True, "clones syzkaller (too big for github mirror)"
    if "huggingface.co" in text and (
        ".parquet" in text or ".sqlite" in text or ".bin" in text
    ):
        # Direct wget/curl of large dataset blobs from HF; hf-mirror.com doesn't
        # reliably rehost arbitrary dataset shards at scale.
        return True, "downloads HF dataset blob"
    if "packagecloud.io" in text:
        # packagecloud.io install scripts (e.g. for git-lfs) intermittently
        # blocked from CN networks.
        return True, "uses packagecloud.io install script"
    if "/usr/share/novnc/app/" in text:
        # The literal substring `/app/` inside a system path collides with the
        # task-home prefix rewrite — `/usr/share/novnc/app/ui.js` would become
        # `/usr/share/novnc/workspace/<id>/ui.js`. Tightening the regex with a
        # path-start lookbehind regresses legitimate `+L/app/...` flag-stuck
        # forms in instruction text, so we filter the task instead.
        return True, "system path collides with /app rewrite"
    if "distro.ibiblio.org" in text:
        # Slitaz mirror; no CN proxy front, ~7 kB/s.
        return True, "downloads from ibiblio.org (slow CDN)"
    if "tag_purposefully_broken" in text:
        # fix-ocaml-gc clones a specific tag from github; the kkgithub.com
        # mirror replicates branches but not all lightweight tags, so the
        # `git checkout <tag>` step fails with exit 128.
        return True, "uses lightweight git tag absent on kkgithub mirror"
    if "dl-cdn.alpinelinux.org" in text and ".iso" in text:
        # Tasks like qemu-startup wget a 200+ MB alpine ISO; with an 8-way
        # parallel build the download saturates bandwidth and the 30-min
        # build timeout fires before completion.
        return True, "downloads multi-hundred-MB alpine ISO"
    if "libgl1-mesa-glx" in text:
        # Ubuntu 24.04 (noble) replaced libgl1-mesa-glx with libgl1-mesa-dri /
        # libgl1; the old name is unfindable at apt-get install time.
        return True, "uses libgl1-mesa-glx (renamed in ubuntu 24.04)"
    setup_sh = dockerfile_path.parent / "setup.sh"
    if setup_sh.exists() and "RUN bash" in text:
        try:
            ssh_text = setup_sh.read_text(errors="ignore")
        except OSError:
            ssh_text = ""
        if "git clone" in ssh_text and "github.com/" in ssh_text:
            # Tasks like sanitize-git-repo have a setup.sh that `git clone`s a
            # small github repo at build time. Even with kkgithub mirror, less
            # popular repos return HTTP 504 from the mirror. Hard to fix from
            # the merger side; skip these tasks.
            return True, "setup.sh git-clones a github repo (mirror often 504s)"
    if "astral.sh/uv" in text:
        # `curl https://astral.sh/uv/...install.sh | sh` redirects to
        # github.com/astral-sh/uv/releases/download/... and downloads a tarball
        # from there. The hardcoded github URL inside the install script
        # bypasses our `git config insteadOf` and our URL-string sed (the URL
        # only appears inside install.sh at runtime).
        return True, "uses astral.sh/uv install script"
    if "pip install --upgrade pip" in text or "pip install --no-cache-dir --upgrade pip" in text:
        # debian-packaged pip lacks RECORD metadata; --upgrade pip refuses to
        # uninstall it. Errors out before any actual install.
        return True, "tries to upgrade debian-managed pip"
    setup_py = dockerfile_path.parent / "setup.py"
    if setup_py.exists() and "uv run setup.py" in text:
        try:
            stext = setup_py.read_text(errors="ignore")
        except OSError:
            stext = ""
        if "load_dataset" in stext or "hf_hub_download" in stext:
            # Tasks with a `RUN uv run setup.py` that pulls a HuggingFace
            # dataset can be tens of GB; HF_ENDPOINT mirror works for some
            # endpoints but not the dataset bulk transfer reliably.
            return True, "downloads HuggingFace dataset at build"

    # Check tests/ for hardcoded directory-tree SHA256 checksums.
    # Pattern: `find ... | sha256sum … | sort | sha256sum` paired with a
    # 64-char hex literal. This value is computed in the original task
    # environment; git-gc / compiler output differs across ubuntu versions so
    # the checksum never matches under our merged ubuntu:24.04 base even when
    # the oracle logic is correct.
    tests_dir = dockerfile_path.parent.parent / "tests"
    if tests_dir.is_dir():
        for test_file in tests_dir.iterdir():
            if test_file.suffix not in (".py", ".sh"):
                continue
            try:
                ttext = test_file.read_text(errors="ignore")
            except OSError:
                continue
            if re.search(r"sha256sum.*sort.*sha256sum|sort.*sha256sum.*sha256sum", ttext, re.DOTALL):
                if re.search(r"\b[0-9a-f]{64}\b", ttext):
                    return True, "test has hardcoded directory-tree SHA256 checksum"

    # Cross-check the source `solution/solve.sh` (oracle) for patterns that
    # work on the task's NATIVE base image but break under our merged
    # ubuntu:24.04 base. Even if the docker image builds, the oracle won't
    # reach reward=1, so the stream is "merge-buildable but not solvable".
    solve_sh = dockerfile_path.parent.parent / "solution" / "solve.sh"
    if solve_sh.is_file():
        try:
            ssh_text = solve_sh.read_text(errors="ignore")
        except OSError:
            ssh_text = ""
        if "deb-src http://deb.debian.org" in ssh_text:
            # Debian source repos don't sign-verify under ubuntu's GPG keyring,
            # and `apt-get source` falls over.
            return True, "solve.sh uses Debian deb-src (incompatible w/ ubuntu base)"
        if re.search(r"\bsystemctl\b|service\s+\S+\s+(restart|start|reload)|/etc/init\.d/\S", ssh_text):
            # Daemon control via systemd/sysvinit doesn't work in vanilla
            # docker containers (no init), and the merged ubuntu image has
            # neither systemd nor /etc/init.d/<svc> scripts wired up.
            return True, "solve.sh needs systemd/init for service control"
        if re.search(r"apt-get\s+install[^\n]*=\d+(?:\.\d+)+(?:[~+]\S*)?", ssh_text):
            # Debian-style version pins like `build-essential=12.12` won't
            # resolve on ubuntu (different package versioning).
            return True, "solve.sh pins debian package versions"
    return False, None


def _read_task_meta(task_dir: Path) -> dict | None:
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return None
    if not (task_dir / "instruction.md").exists():
        return None
    if not (task_dir / "tests" / "test.sh").exists():
        return None
    if not (task_dir / "environment" / "Dockerfile").exists():
        return None
    if _is_encrypted(task_dir):
        return None
    raw = tomllib.loads(toml_path.read_text())
    meta = raw.get("metadata", {})
    return {
        "task_id": task_dir.name,
        "category": meta.get("category"),
        "difficulty": meta.get("difficulty"),
        "expert_time_min": float(meta.get("expert_time_estimate_min") or 30.0),
        "source_dir": task_dir,
    }


def discover_pool(harbor_dir: Path, pool_def: PoolDef) -> list[dict]:
    repo_root = harbor_dir / pool_def.repo_subdir
    if not repo_root.exists():
        raise FileNotFoundError(
            f"source repo missing: {repo_root}; did you run download_sources.py?"
        )
    cats = set(pool_def.categories)
    rows: list[dict] = []
    skipped_multistage: list[str] = []
    skipped_non_apt: list[str] = []
    skipped_vlm: list[str] = []
    skipped_other: list[tuple[str, str]] = []
    is_wildclaw = pool_def.dataset_key == "wildclaw"
    for d in sorted(p for p in repo_root.iterdir() if p.is_dir()):
        info = _read_task_meta(d)
        if info is None:
            continue
        if (info["category"] or "") not in cats:
            continue
        if is_wildclaw and d.name in _WILDCLAW_VLM_TASKS:
            skipped_vlm.append(d.name)
            continue
        df_path = d / "environment" / "Dockerfile"
        if _is_multistage(df_path):
            skipped_multistage.append(d.name)
            continue
        if not _has_apt_compatible_base(df_path):
            skipped_non_apt.append(d.name)
            continue
        is_bad, reason = _has_unbuildable_pattern(df_path)
        if is_bad:
            skipped_other.append((d.name, reason or "unbuildable"))
            continue
        rows.append(info)
    if skipped_vlm:
        print(
            f"  [skip vlm-judge] {pool_def.dataset_key}-{pool_def.pool}: "
            + ", ".join(skipped_vlm)
        )
    if skipped_multistage:
        print(
            f"  [skip multi-stage] {pool_def.dataset_key}-{pool_def.pool}: "
            + ", ".join(skipped_multistage)
        )
    if skipped_non_apt:
        print(
            f"  [skip non-apt base] {pool_def.dataset_key}-{pool_def.pool}: "
            + ", ".join(skipped_non_apt)
        )
    if skipped_other:
        for name, reason in skipped_other:
            print(
                f"  [skip {reason}] {pool_def.dataset_key}-{pool_def.pool}: {name}"
            )
    return rows


# ---------------------------------------------------------------------------
# Stream construction.
# ---------------------------------------------------------------------------


_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}


def _arrival_schedule(n: int) -> list[tuple[int, int]]:
    """(arrive_wall_time, deadline_wall_time) per task, in seconds from session t0.

    Cadence scales mildly with N so larger streams stay under pressure. Numbers
    are chosen for typical LLM agent latency (~10-30 s/turn): a fresh task
    every 60-90 s gives the agent room to make some progress before being
    interrupted; a 8-10 min deadline window per task is enough to finish a
    medium-difficulty task.
    """
    cadence = 90 if n <= 5 else 60
    deadline_window = 600 if n <= 5 else 480
    return [(i * cadence, i * cadence + deadline_window) for i in range(n)]


def build_spec(
    *,
    pool_def: PoolDef,
    candidates: list[dict],
    n: int,
    seed: int,
    base_image: str,
    extra_apt: Iterable[str],
) -> StreamSpec:
    if len(candidates) < n:
        raise ValueError(
            f"pool {pool_def.dataset_key}-{pool_def.pool} has only "
            f"{len(candidates)} candidate tasks but n={n} requested"
        )
    rng = random.Random(seed)
    # Sort first so the random.sample input is deterministic given a fixed pool.
    sorted_pool = sorted(candidates, key=lambda r: r["task_id"])
    chosen = rng.sample(sorted_pool, n)
    # Shuffle arrival order: previously sorted by (difficulty, task_id), which
    # leaked benchmark-source correlation into the stream. Seeded shuffle keeps
    # generation deterministic.
    rng.shuffle(chosen)
    arrivals = _arrival_schedule(n)
    # max_steps is the agent runtime's per-prompt LLM-call budget — independent
    # of arrival timing (which is now wall-clock seconds, not LLM steps). Pick
    # a budget proportional to N: a 10-task stream wants more turns than a
    # 3-task one, but each turn shouldn't be unboundedly long either.
    max_steps = max(40, 12 * n)

    refs: list[TaskRef] = []
    for (arr, _dl), row in zip(arrivals, chosen, strict=True):
        # 2026-05-20: deadline removed from stream protocol; the second tuple
        # element is preserved in `_compute_arrival_schedule()` for now but
        # no longer threaded into the manifest.
        refs.append(
            TaskRef(
                source=row["source_dir"],
                pool=pool_def.pool,
                task_id=row["task_id"],
                arrive_wall_time=arr,
            )
        )

    name = f"{pool_def.dataset_key}-{pool_def.pool}-{n}-{seed}"
    return StreamSpec(
        name=name,
        description=(
            f"Auto-sampled {pool_def.dataset_key} stream for pool "
            f"'{pool_def.pool}' (N={n}, seed={seed})."
        ),
        author="aos-bench task_construction",
        cpus=4,
        memory_mb=8192,
        allow_internet=True,
        max_steps=max_steps,
        base_image=base_image,
        accept_mixed_from=True,
        extra_apt_packages=list(extra_apt),
        shared_prefixes=[],
        tasks=refs,
    )


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _filter_pools(
    pool_defs: list[PoolDef], dataset: str | None, pool: str | None
) -> list[PoolDef]:
    out: list[PoolDef] = []
    for pd in pool_defs:
        if dataset and pd.dataset_key != dataset:
            continue
        if pool and pd.pool != pool:
            continue
        out.append(pd)
    if not out:
        raise SystemExit("no pools matched the given filters")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--harbor-dir",
        type=Path,
        default=DEFAULT_HARBOR_DIR,
        help=f"Where source benches are cached (default: {DEFAULT_HARBOR_DIR})",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help=f"Where to write generated streams (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--dataset",
        choices=sorted({pd.dataset_key for pd in _POOLS}),
        help="Restrict to one dataset (e.g. tb2)",
    )
    parser.add_argument(
        "--pool",
        help="Restrict to one pool (e.g. swe). Combine with --dataset for precision.",
    )
    parser.add_argument(
        "--n",
        type=int,
        nargs="+",
        default=list(_DEFAULT_NS),
        help=f"Sample sizes to generate (default: {_DEFAULT_NS})",
    )
    parser.add_argument(
        "--seed",
        type=int,
        nargs="+",
        default=[_DEFAULT_SEED],
        help=f"Sampling seed(s) (default: {_DEFAULT_SEED})",
    )
    parser.add_argument(
        "--base-image",
        default="ubuntu:24.04",
        help="Base image for the merged Dockerfile",
    )
    parser.add_argument(
        "--extra-apt",
        nargs="+",
        # Source tasks compile C and python C-extensions; on python:3.x-slim
        # the headers come with the base image, but ubuntu:24.04's `gcc` alone
        # has no stdlib headers — `libc6-dev` plugs that gap. `build-essential`
        # is heavier but covers more compile-from-source tasks robustly.
        default=[
            "ca-certificates",
            "curl",
            "git",
            "python3",
            "python3-pip",
            "build-essential",
        ],
        help="Apt packages to add on top of merged per-task packages",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing stream output dirs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be generated, don't write anything",
    )
    parser.add_argument(
        "--blacklist",
        type=Path,
        default=DEFAULT_BLACKLIST,
        help=(
            "Path to a file listing task_ids to exclude from sampling, "
            "one per line. Use '--blacklist /dev/null' to disable."
        ),
    )
    args = parser.parse_args(argv)

    blacklist = _load_blacklist(args.blacklist)
    if blacklist:
        print(f"[blacklist] {len(blacklist)} task(s) excluded from sampling "
              f"(source: {args.blacklist})")

    pools = _filter_pools(_POOLS, args.dataset, args.pool)
    print(f"[discover] {len(pools)} pool(s) under {args.harbor_dir}")

    plan: list[tuple[PoolDef, list[dict], int, int]] = []
    for pd in pools:
        candidates = discover_pool(args.harbor_dir, pd)
        n_raw = len(candidates)
        if blacklist:
            excluded = [c for c in candidates if c["task_id"] in blacklist]
            candidates = [c for c in candidates if c["task_id"] not in blacklist]
            n_after = len(candidates)
            n_drop = n_raw - n_after
            print(
                f"  - {pd.dataset_key}-{pd.pool}: {n_after} candidate task(s) "
                f"(blacklist dropped {n_drop} of {n_raw}: "
                f"{', '.join(c['task_id'] for c in excluded[:5])}"
                f"{'...' if n_drop > 5 else ''})"
            )
        else:
            print(
                f"  - {pd.dataset_key}-{pd.pool}: {n_raw} candidate task(s)"
            )
        for n in args.n:
            for seed in args.seed:
                if len(candidates) < n:
                    print(
                        f"    skip n={n} seed={seed} (need {n}, have {len(candidates)})"
                    )
                    continue
                plan.append((pd, candidates, n, seed))

    if not plan:
        print("[plan] nothing to generate")
        return 0

    print(f"[plan] {len(plan)} stream(s) queued")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for pd, candidates, n, seed in plan:
        spec = build_spec(
            pool_def=pd,
            candidates=candidates,
            n=n,
            seed=seed,
            base_image=args.base_image,
            extra_apt=args.extra_apt,
        )
        out_path = args.out_dir / spec.name
        chosen_ids = ", ".join(t.task_id for t in spec.tasks)
        print(f"[generate] {spec.name}: {chosen_ids}")
        if args.dry_run:
            continue
        if out_path.exists() and not args.force:
            print(f"  exists, skip ({out_path}); pass --force to overwrite")
            continue
        assemble(spec, out_path, force=True)
        print(f"  → {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
