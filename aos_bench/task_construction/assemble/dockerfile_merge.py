"""Merge per-task Dockerfiles into one stream-level Dockerfile.

Algorithm
=========

Given N parsed source Dockerfiles + their `TaskSlot` allocations, produce a
single Dockerfile that:

1. Starts from a base image. By default we take the first task's `FROM`. If
   later tasks have a different `FROM` we either:
     - accept it if `accept_mixed_from=True` (manifest opts in), or
     - raise `IncompatibleBaseImageError`.
2. Collects all `apt-get install` packages across tasks, deduplicates, and
   emits ONE `RUN apt-get update && apt-get install -y …` block at the top.
3. Stages each source task's `environment/` directory under
   `_sources/<task_id>/` next to the merged Dockerfile.
4. For every `COPY src dst` in the source Dockerfile:
     - rewrites `src` to `_sources/<task_id>/<src>`
     - resolves relative `dst` against the source Dockerfile's current
       `WORKDIR`, then rewrites task-local paths into the shared `/workspace`
       root. Collision-renamed paths are applied here too.
5. `RUN` bodies get the same path-prefix substitution as instructions/tests.
6. `WORKDIR` lines from the source are translated to shared `/workspace`
   paths; the stream's final `WORKDIR` is `/workspace`.
7. `ENV` and `USER` lines are kept (de-duplicated by ENV key).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from .dockerfile_parse import (
    Instruction,
    ParsedDockerfile,
    parse_dockerfile,
    split_apt_install,
)
from .instruction_rewrite import PathRewriteRule, build_rules, rewrite_text
from .install_mirrors import normalize_domestic_install_sources
from .path_namespace import TaskSlot


# Mirror profiles for pip + apt sources baked into the merged Dockerfile.
#
# - "tencent":  mirrors.cloud.tencent.com. CDN-backed. Best reliability under
#               concurrent oracle sweeps on this host (5/8 success at 8-parallel
#               wheel downloads vs aliyun 0/8 and tuna 0/8 mid-stream drops).
#               Reachable WITHOUT http_proxy from the GLM docker-proxy network.
#               Default for new builds.
# - "huawei":   repo.huaweicloud.com. CDN-backed. Second-best on this host
#               (3/8 at 8-parallel). Use as a fallback when tencent
#               regresses.
# - "aliyun":   mirrors.aliyun.com. Index lookups work but big-wheel downloads
#               drop mid-stream under concurrency on this host (the host's
#               TLS path to aliyun is lossy). Listed for completeness.
# - "tuna":     pypi.tuna.tsinghua.edu.cn. Legacy; collapses at ≥30 parallel
#               workers with "peer closed connection without TLS close_notify".
#               Kept for backwards compat with images already in the registry.
# - "upstream": no mirror swap — apt uses archive.ubuntu.com, pip uses
#               pypi.org. Requires the build/run env to reach the upstream
#               hosts directly or via an HTTP proxy (set --build-arg
#               HTTP_PROXY=... and -e http_proxy=...). Useful when running in
#               an env with full external network and as a control case.
#
# Profile is selected via:
#   - explicit `mirror_profile` kwarg on merge_dockerfiles / merge_from_paths
#   - or AOS_DOCKERFILE_MIRROR env var (one of the profile names)
#   - falling back to "tencent".
MIRROR_PROFILES: dict[str, dict[str, str | None]] = {
    "tencent": {
        "name": "tencent",
        "pip_index": "https://mirrors.cloud.tencent.com/pypi/simple",
        "pip_trusted_host": "mirrors.cloud.tencent.com",
        "uv_index": "https://mirrors.cloud.tencent.com/pypi/simple",
        "apt_main_from": "http://archive.ubuntu.com/ubuntu",
        "apt_main_to": "http://mirrors.cloud.tencent.com/ubuntu",
        "apt_security_from": "http://security.ubuntu.com/ubuntu",
        "apt_security_to": "http://mirrors.cloud.tencent.com/ubuntu",
        "apt_ports_from": "http://ports.ubuntu.com/ubuntu-ports",
        "apt_ports_to": "http://mirrors.cloud.tencent.com/ubuntu-ports",
    },
    "huawei": {
        "name": "huawei",
        "pip_index": "https://repo.huaweicloud.com/repository/pypi/simple",
        "pip_trusted_host": "repo.huaweicloud.com",
        "uv_index": "https://repo.huaweicloud.com/repository/pypi/simple",
        "apt_main_from": "http://archive.ubuntu.com/ubuntu",
        "apt_main_to": "http://repo.huaweicloud.com/ubuntu",
        "apt_security_from": "http://security.ubuntu.com/ubuntu",
        "apt_security_to": "http://repo.huaweicloud.com/ubuntu",
        "apt_ports_from": "http://ports.ubuntu.com/ubuntu-ports",
        "apt_ports_to": "http://repo.huaweicloud.com/ubuntu-ports",
    },
    "aliyun": {
        "name": "aliyun",
        "pip_index": "https://mirrors.aliyun.com/pypi/simple",
        "pip_trusted_host": "mirrors.aliyun.com",
        "uv_index": "https://mirrors.aliyun.com/pypi/simple",
        "apt_main_from": "http://archive.ubuntu.com/ubuntu",
        "apt_main_to": "http://mirrors.aliyun.com/ubuntu",
        "apt_security_from": "http://security.ubuntu.com/ubuntu",
        "apt_security_to": "http://mirrors.aliyun.com/ubuntu",
        "apt_ports_from": "http://ports.ubuntu.com/ubuntu-ports",
        "apt_ports_to": "http://mirrors.aliyun.com/ubuntu-ports",
    },
    "tuna": {
        "name": "tuna",
        "pip_index": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "pip_trusted_host": "pypi.tuna.tsinghua.edu.cn",
        "uv_index": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "apt_main_from": "http://archive.ubuntu.com/ubuntu",
        "apt_main_to": "http://mirrors.tuna.tsinghua.edu.cn/ubuntu",
        "apt_security_from": "http://security.ubuntu.com/ubuntu",
        "apt_security_to": "http://mirrors.tuna.tsinghua.edu.cn/ubuntu",
        "apt_ports_from": "http://ports.ubuntu.com/ubuntu-ports",
        "apt_ports_to": "http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports",
    },
    "upstream": {
        "name": "upstream",
        "pip_index": None,        # no PIP_INDEX_URL ENV — use pypi.org
        "pip_trusted_host": None,
        "uv_index": None,
        "apt_main_from": None,    # no apt sed — keep upstream sources
        "apt_main_to": None,
        "apt_security_from": None,
        "apt_security_to": None,
        "apt_ports_from": None,
        "apt_ports_to": None,
    },
}


def _resolve_mirror_profile(profile: str | None) -> dict[str, str | None]:
    name = profile or os.environ.get("AOS_DOCKERFILE_MIRROR") or "tencent"
    if name not in MIRROR_PROFILES:
        raise ValueError(
            f"unknown mirror_profile {name!r}; expected one of "
            f"{sorted(MIRROR_PROFILES)}"
        )
    return MIRROR_PROFILES[name]


class IncompatibleBaseImageError(RuntimeError):
    pass


@dataclass
class MergedDockerfile:
    text: str
    apt_packages: list[str] = field(default_factory=list)
    base_image: str | None = None
    sources_to_copy: list[tuple[Path, str]] = field(default_factory=list)
    """List of (source_dir, dest_subdir) pairs. dest_subdir is relative to <stream>/environment/."""


_COPY_TOKEN = re.compile(r"\s+")


_TEST_LIKE_SRC_PREFIXES = (
    "tests/", "./tests/",
    "solution/", "./solution/",
    "test/", "./test/",
)
_TEST_LIKE_SRC_EXACT_FILES = {
    "check.py", "./check.py",       # regex-chess
    "test_outputs.py", "./test_outputs.py",
    "test.sh", "./test.sh",
}


def _copy_targets_test_or_solution(args: str) -> bool:
    """Return True if any src token in a COPY/ADD line points at a path that
    belongs to the task's tests/solution tree (i.e. data the agent must not
    see).  This catches the 5 known tb2 leak tasks.

    Conservative: only matches RELATIVE src paths starting with the known
    prefixes / exact filenames; absolute paths (`/foo`, http URLs, --from=...)
    pass through.
    """
    flat = args.replace("\\\n", " ").strip()
    tokens = flat.split()
    while tokens and tokens[0].startswith("--"):
        tokens.pop(0)
    if len(tokens) < 2:
        return False
    srcs = tokens[:-1]  # last token is dst
    for s in srcs:
        if s.startswith(("/", "http://", "https://")):
            continue
        s_clean = s.lstrip("./").rstrip("/")
        # Match either prefix-of-path or exact-file form.
        if any(s.startswith(p) for p in _TEST_LIKE_SRC_PREFIXES):
            return True
        if s in _TEST_LIKE_SRC_EXACT_FILES or s_clean in _TEST_LIKE_SRC_EXACT_FILES:
            return True
    return False


def _copy_dst_shifted_workdir(
    args: str,
    slot: TaskSlot,
    rules: list[PathRewriteRule],
    *,
    source_cwd: str,
) -> str | None:
    """If a COPY's destination was the source task's effective cwd, return the
    rewritten dest (so the merger can emit a `WORKDIR <new-dst>` after the
    COPY). Otherwise return None.

    Motivation: source tasks like ``feal-linear-cryptanalysis`` say
    ``COPY task-deps/ /app/`` then ``RUN gcc -O3 -o feal feal.c`` — the RUN
    assumes cwd is ``/app`` and finds ``feal.c`` there because the COPY
    landed it in the current ``WORKDIR``. After our path-rewrite + collision
    detection, the dst can shift to e.g. ``/workspace/task-deps__feal-...``
    (the collision rename is keyed file-level on ``/app/task-deps``), but
    ``WORKDIR`` is still ``/workspace``, so the RUN can no longer find
    ``feal.c``. This helper detects the shift by replaying the same
    per-src-basename rewrite that ``_rewrite_copy`` does, then comparing
    against the rewritten cwd.

    Returns the rewritten dst (without trailing ``/``) if shifted, else None.
    """
    flat = args.replace("\\\n", " ").strip()
    tokens = flat.split()
    while tokens and tokens[0].startswith("--"):
        tokens.pop(0)
    if len(tokens) < 2:
        return None
    *srcs, dst = tokens

    dst_abs = _resolve_source_path(dst, source_cwd).rstrip("/") or "/"
    src_cwd_norm = source_cwd.rstrip("/") or "/"
    # Was the COPY dst the task's effective cwd? Only relevant in that case.
    if dst_abs != src_cwd_norm:
        return None

    # Replay the per-src rewrite logic from `_rewrite_copy`: for a dir-form
    # dst, each src lands at `<dst>/<basename(src)>`. Apply path rules to
    # that file-level destination — this is where the collision rename
    # (keyed on `/app/task-deps`) actually matches.
    # IMPORTANT: only fire for DIRECTORY srcs (src ends with `/`). For
    # file srcs like `COPY sequences.fasta .`, the rename produces a file
    # path (e.g. `/workspace/sequences.fasta__dna-assembly`); WORKDIR-ing
    # there is wrong because it tries to mkdir a path that conflicts with
    # the existing file from the winning task.
    rewritten_cwd = rewrite_text(src_cwd_norm, rules).rstrip("/")
    for s in srcs:
        if s.startswith(("/", "http://", "https://")):
            continue
        if not s.endswith("/"):
            # File source — collision rename produces a renamed FILE path,
            # not a directory. Skip WORKDIR emission for this src.
            continue
        basename = PurePosixPath(s.rstrip("/")).name
        if not basename:
            continue
        file_dst_abs = f"{dst_abs.rstrip('/')}/{basename}"
        rewritten_file = rewrite_text(file_dst_abs, rules).rstrip("/")
        # The "expected" file dst (i.e. no collision rename) is just
        # <rewritten_cwd>/<basename>. If the actual rewritten differs, the
        # collision detector moved the dst elsewhere.
        expected = f"{rewritten_cwd}/{basename}"
        if rewritten_file != expected and rewritten_file:
            # Shifted! Return the new dst (which is the collision-renamed
            # path — the directory the files actually land in).
            return rewritten_file
    return None


def _rewrite_copy(
    args: str,
    slot: TaskSlot,
    rules: list[PathRewriteRule],
    *,
    source_cwd: str,
) -> list[str]:
    """Rewrite `<flags?> <src...> <dst>` for one COPY/ADD line.

    src tokens are rewritten to `_sources/<task_id>/<orig>`.
    dst token is first resolved against the source Dockerfile's current
    WORKDIR and then rewritten via the path-rewrite rules (which encode the
    shared /workspace mapping and collision renames).
    Flags like `--chown=...` are passed through.

    Returns a list of rewritten "<flags?> <src...> <dst>" bodies (caller
    prepends the COPY/ADD op). Most lines yield a single body; if the
    destination is a directory and per-src file-level collision renames
    differ across srcs, each src is emitted as its own COPY line.
    """
    flat = args.replace("\\\n", " ").strip()
    tokens = flat.split()
    flags: list[str] = []
    while tokens and tokens[0].startswith("--"):
        flags.append(tokens.pop(0))
    if len(tokens) < 2:
        return [args]
    *srcs, dst = tokens

    def stage_src(s: str) -> str:
        if s.startswith(("/", "http://", "https://")):
            return s
        return f"{slot.sources_subdir}/{s}"

    dst_abs = _resolve_source_path(dst, source_cwd).rstrip("/") or "/"
    dst_is_dir_form = (
        dst.endswith("/") or dst in (".", "./") or dst_abs == source_cwd.rstrip("/")
    )

    if dst_is_dir_form and srcs:
        # Compute the per-src final destination and apply path rules to each.
        # If all srcs resolve to file paths under the same parent (the common
        # dir-level case with no file-level rename), emit a single COPY with
        # the dir destination; otherwise split into per-src COPY lines so each
        # rewritten file path is honored exactly.
        per_src: list[tuple[str, str]] = []
        for s in srcs:
            staged = stage_src(s)
            if s.startswith(("/", "http://", "https://")):
                per_src.append((staged, _rewrite_copy_dest_path(dst, source_cwd=source_cwd, rules=rules)))
                continue
            basename = PurePosixPath(s.rstrip("/")).name
            file_dst_abs = f"{dst_abs.rstrip('/')}/{basename}" if basename else dst_abs
            rewritten_file = rewrite_text(file_dst_abs, rules)
            per_src.append((staged, rewritten_file))

        # Check whether every per-src result is `<common_parent>/<basename>`
        # (i.e. no file-level rename diverted any src). If so collapse back to
        # a single dir-form COPY for readability.
        dir_rewritten = _rewrite_copy_dest_path(dst, source_cwd=source_cwd, rules=rules)
        common_parent_form = True
        for (staged, full_dst), s in zip(per_src, srcs):
            basename = PurePosixPath(s.rstrip("/")).name
            expected = f"{dir_rewritten.rstrip('/')}/{basename}" if basename else dir_rewritten
            if full_dst != expected:
                common_parent_form = False
                break
        if common_parent_form:
            new_srcs = [staged for staged, _ in per_src]
            return [" ".join(flags + new_srcs + [dir_rewritten])]
        return [" ".join(flags + [staged] + [full_dst]) for staged, full_dst in per_src]

    new_srcs = [stage_src(s) for s in srcs]
    new_dst = _rewrite_copy_dest_path(dst, source_cwd=source_cwd, rules=rules)
    return [" ".join(flags + new_srcs + [new_dst])]


def _resolve_source_path(path: str, source_cwd: str) -> str:
    has_trailing = path.endswith("/")
    cleaned = path
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in {"", "."}:
        out = source_cwd.rstrip("/") or "/"
    elif cleaned.startswith("/"):
        out = cleaned.rstrip("/") or "/"
    else:
        out = f"{source_cwd.rstrip('/')}/{cleaned.rstrip('/')}"
    return out + ("/" if has_trailing and not out.endswith("/") else "")


def _update_source_cwd(workdir: str, source_cwd: str) -> str:
    target = workdir.strip()
    if not target:
        return source_cwd
    if target.startswith("/"):
        return target.rstrip("/") or "/"
    return f"{source_cwd.rstrip('/')}/{target}".rstrip("/") or "/"


def _rewrite_copy_dest_path(
    dst: str,
    *,
    source_cwd: str,
    rules: list[PathRewriteRule],
) -> str:
    has_trailing = dst.endswith("/")
    # `.` and `./` are dir-form (== current WORKDIR) — preserve that
    # directory semantic so BuildKit doesn't interpret `COPY <file> /workspace`
    # as "save file as /workspace" (which would turn the workspace dir into a
    # regular file and break every subsequent path under it).
    dst_is_dir_form = has_trailing or dst in (".", "./")
    resolved = _resolve_source_path(dst, source_cwd).rstrip("/") or "/"
    rewritten = rewrite_text(resolved, rules)
    if dst_is_dir_form and not rewritten.endswith("/"):
        rewritten += "/"
    return rewritten


def _rewrite_one_path(path: str, slot: TaskSlot, rules: list[PathRewriteRule]) -> str:
    """Apply prefix rules to an absolute path; leave relative paths alone.

    Relative tokens like `./`, `.`, `foo`, `./bar` are kept verbatim so Docker
    resolves them against the current WORKDIR (which we always rewrite). This
    avoids the trap where anchoring to source_workdir hard-codes the wrong
    base when the source has multiple WORKDIR statements.
    """
    has_trailing = path.endswith("/")
    cleaned = path
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in {".", ""} or not cleaned.startswith("/"):
        return path  # relative — Docker resolves via WORKDIR
    cleaned = cleaned.rstrip("/") or "/"
    rewritten = rewrite_text(cleaned, rules)
    if has_trailing and not rewritten.endswith("/"):
        rewritten += "/"
    return rewritten


def _rewrite_dest_path(dst: str, slot: TaskSlot) -> str:
    """Map a destination path inside the source container to the slot namespace.

    - `/app/foo` (or whatever source_workdir is) → `<workspace_dir>/foo`
    - `/workspace/foo` → `<workspace_dir>/foo`  (older tasks)
    - `./foo`, `./`, `foo` → `<workspace_dir>/foo` (relative to source WORKDIR)
    - other absolute `/X` paths (e.g. `/etc/...`, `/usr/...`) are kept as-is.
    """
    has_trailing = dst.endswith("/")
    # Strip leading "./" since it's redundant once we anchor on workspace_dir.
    cleaned = dst
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned == ".":
        cleaned = ""
    cleaned_no_slash = cleaned.rstrip("/")
    src_workdir = slot.source_workdir

    if cleaned_no_slash in {"", "/"}:
        out = slot.workspace_dir
    elif cleaned_no_slash == src_workdir:
        out = slot.workspace_dir
    elif cleaned_no_slash.startswith(src_workdir + "/"):
        out = slot.workspace_dir + cleaned_no_slash[len(src_workdir) :]
    elif cleaned_no_slash == "/workspace":
        out = slot.workspace_dir
    elif cleaned_no_slash.startswith("/workspace/"):
        out = slot.workspace_dir + cleaned_no_slash[len("/workspace") :]
    elif cleaned_no_slash.startswith("/"):
        # Other absolute path like /etc/nginx.conf — leave untouched.
        out = cleaned_no_slash
    else:
        # Relative path — relative to source_workdir.
        out = f"{slot.workspace_dir}/{cleaned_no_slash}"

    return out + ("/" if has_trailing else "")


def _rewrite_workdir(args: str, slot: TaskSlot, rules: list[PathRewriteRule]) -> str:
    return _rewrite_one_path(args.strip(), slot, rules)


def _rewrite_run(args: str, slot: TaskSlot, rules: list[PathRewriteRule]) -> str:
    """Apply prefix-path substitution to a RUN body.

    We do NOT prepend `cd <workspace_dir>` defensively — every per-task block
    starts with `WORKDIR <workspace_dir>` and any source `WORKDIR` line is
    rewritten in place, so Docker's cwd is correct when the RUN executes.
    Forcing a cd would actively break tasks where a deeper `WORKDIR
    /app/foo/src` was set just before this RUN (the cd would yank cwd back
    out of the subdir).
    """
    return rewrite_text(args, rules)


def _resolve_source_workdir(parsed: ParsedDockerfile, default: str = "/app") -> str:
    """Anchor path-rewrites on the FIRST WORKDIR — that's the source task's `home` root.

    Later WORKDIR lines often cd into subdirs (e.g. fix-git's `/app/personal-site`)
    but the `home` is what setup scripts and verifier paths reference.
    """
    return parsed.first_workdir or default


def _home_root(workdir: str) -> str:
    parts = workdir.lstrip("/").split("/")
    if not parts or not parts[0]:
        return "/"
    return "/" + parts[0]


# pool v0.2 staging layout. Must match harbor's POOL_V02_WORKSPACE_STAGE /
# POOL_V02_WORKSPACE_TARGET (harbor/src/harbor/trial/aos_pool.py) — the pool
# sidecar merges <sources-root>/<task_id>/* into <workspace> on READY.
_POOL_V02_WORKSPACE = "/workspace"
_POOL_V02_SOURCES_ROOT = "/aos_internal/sources"


def _stage_collision_target(target: str, task_id: str) -> str:
    """Map a flat-workspace collision target into the v0.2 staging root.

    ``/workspace/.claude/skills__t1`` → ``/aos_internal/sources/t1/.claude/skills__t1``
    so the rename is built under staging like every other path in the task's
    block. Targets that are already staged, or that point outside the flat
    workspace entirely, are returned unchanged.
    """
    stage_root = f"{_POOL_V02_SOURCES_ROOT}/{task_id}"
    if target.startswith(f"{stage_root}/") or target == stage_root:
        return target
    if target == _POOL_V02_WORKSPACE:
        return stage_root
    if not target.startswith(f"{_POOL_V02_WORKSPACE}/"):
        return target
    return stage_root + target[len(_POOL_V02_WORKSPACE) :]


def merge_dockerfiles(
    *,
    slots_with_dockerfiles: list[tuple[TaskSlot, ParsedDockerfile]],
    base_image_override: str | None = None,
    accept_mixed_from: bool = False,
    extra_apt_packages: Iterable[str] = (),
    canary_comment: str | None = None,
    shared_prefixes: Iterable[str] = (),
    collision_renames_per_slot: dict[str, dict[str, str]] | None = None,
    pool_v02: bool = False,
    mirror_profile: str | None = None,
) -> MergedDockerfile:
    if not slots_with_dockerfiles:
        raise ValueError("merge_dockerfiles needs at least one (slot, dockerfile) pair")

    mirror = _resolve_mirror_profile(mirror_profile)

    # v0.2: each task block builds into /aos_internal/sources/<task_id>/
    # instead of /workspace/<task_id>/. The agent's view of /workspace/
    # only gets populated at runtime when the pool sidecar stages a task
    # on PENDING→READY. See docs/framework/task_assign.md §3.4.
    if pool_v02:
        import dataclasses as _dc
        slots_with_dockerfiles = [
            (
                _dc.replace(
                    slot,
                    workspace_dir=f"{_POOL_V02_SOURCES_ROOT}/{slot.task_id}",
                ),
                df,
            )
            for slot, df in slots_with_dockerfiles
        ]
        # Collision renames are computed against the *flat* workspace, since
        # that is what the agent sees once the pool reveals a task, and they
        # are applied as absolute destinations. Left as-is they would emit
        # `COPY ... /workspace/skills__<task_id>` and put the losing side of
        # every collision in the agent's workspace from t0 -- a preview of a
        # task that has not arrived yet.
        #
        # Rebase them under the same staging root as everything else. The
        # reveal hook merges the *contents* of /aos_internal/sources/<id>/
        # into /workspace/, so a file staged at
        # /aos_internal/sources/<id>/skills__<id> surfaces at
        # /workspace/skills__<id> -- exactly the path the instructions and
        # tests were rewritten to expect.
        if collision_renames_per_slot:
            collision_renames_per_slot = {
                task_id: {
                    src: _stage_collision_target(target, task_id)
                    for src, target in renames.items()
                }
                for task_id, renames in collision_renames_per_slot.items()
            }

    base_image = base_image_override
    if base_image is None:
        base_image = slots_with_dockerfiles[0][1].from_image
    if base_image is None:
        raise ValueError("First Dockerfile has no FROM and no base_image_override given")

    # Validate FROMs.
    if not accept_mixed_from:
        for slot, df in slots_with_dockerfiles:
            if df.from_image and df.from_image != base_image:
                raise IncompatibleBaseImageError(
                    f"Task {slot.task_id} uses FROM {df.from_image!r} which differs from "
                    f"the chosen base {base_image!r}. Override with base_image or set accept_mixed_from=True."
                )

    # Collect apt packages.
    all_pkgs: list[str] = list(extra_apt_packages)
    seen_pkgs: set[str] = set(extra_apt_packages)

    per_task_envs: list[tuple[TaskSlot, str]] = []
    per_task_run_blocks: list[tuple[TaskSlot, list[str]]] = []
    sources_to_copy: list[tuple[Path, str]] = []

    for slot, df in slots_with_dockerfiles:
        sources_to_copy.append((slot.source_dir / "environment", slot.sources_subdir))
        block: list[str] = [f"# === {slot.task_id} (pool={slot.pool}, source={slot.source_dir.name}) ==="]
        last_emitted_workdir = ""
        source_cwd = slot.source_workdir
        # Always set WORKDIR to the namespace so following RUN/COPY are predictable.
        block.append(f"WORKDIR {slot.workspace_dir}")
        last_emitted_workdir = slot.workspace_dir

        rules = build_rules(
            slot=slot,
            dockerfile=df,
            shared_prefixes=list(shared_prefixes),
            collision_renames=(collision_renames_per_slot or {}).get(slot.task_id, {}),
        )

        # SkillsBench convention: tasks ship reusable Python helpers under
        # environment/skills/<skill_name>/scripts/*.py. Their solve.sh imports
        # them via either /root/.claude/skills/... (Claude canonical path) or
        # `skills/...` relative path. Copy to BOTH so each style works:
        #   - /root/.claude/skills/  (canonical, harbor adapter mount target)
        #   - /workspace/skills[/__task]  (shared workspace, collision-aware)
        #
        # In v0.2 we DO NOT pre-populate /root/.claude/skills/ because that
        # would mix all tasks' skills into one agent-readable directory. The
        # pool sidecar stages skills per-task on PENDING→READY into /skills/
        # (and optionally /root/.claude/skills/). Per-task workspace skills
        # still get COPY'd because they live inside the per-task workspace
        # subtree (which is itself isolated under /aos_internal/sources/).
        skills_src = slot.source_dir / "environment" / "skills"
        if skills_src.is_dir():
            skills_dst = rewrite_text(f"{_home_root(slot.source_workdir)}/skills", rules)
            if not skills_dst.endswith("/"):
                skills_dst += "/"
            if not pool_v02:
                block.append(
                    f"COPY {slot.sources_subdir}/skills/ /root/.claude/skills/"
                )
            else:
                # v0.2: stage to /aos_internal/skills/<task_id>/ for the pool
                # sidecar to mv to /skills/ (and optionally /root/.claude/skills/)
                # at runtime when this task becomes READY.
                block.append(
                    f"COPY {slot.sources_subdir}/skills/ "
                    f"/aos_internal/skills/{slot.task_id}/"
                )
            block.append(
                f"COPY {slot.sources_subdir}/skills/ {skills_dst}"
            )

        for ins in df.instructions:
            if ins.op == "FROM":
                continue  # handled at top
            if ins.op == "WORKDIR":
                source_cwd = _update_source_cwd(ins.args, source_cwd)
                target = _rewrite_workdir(ins.args, slot, rules)
                if target != last_emitted_workdir:
                    block.append(f"WORKDIR {target}")
                    last_emitted_workdir = target
                continue
            if ins.op == "ENV":
                # Keep as-is — let the user resolve conflicts later.
                per_task_envs.append((slot, ins.args.strip()))
                continue
            if ins.op == "USER":
                block.append(f"# [aos: USER {ins.args.strip()} skipped — stream runs as root]")
                continue
            if ins.op == "RUN":
                pkgs, leftover = split_apt_install(ins.args)
                if pkgs and leftover is None:
                    # Pure apt install — promote to global apt block.
                    for p in pkgs:
                        if p not in seen_pkgs:
                            seen_pkgs.add(p)
                            all_pkgs.append(p)
                    block.append(f"# [aos: apt packages {pkgs} promoted to global apt-get block]")
                    continue
                rewritten = _rewrite_run(ins.args, slot, rules)
                block.append(f"RUN {rewritten}")
                continue
            if ins.op in ("COPY", "ADD"):
                # Temporal isolation defense: drop COPY/ADD lines whose src
                # would pull anything from the source task's tests/ or
                # solution/ tree into the agent's image (5 tb2 tasks ship
                # such lines: overfull-hbox, schemelike-metacircular-eval,
                # break-filter-js-from-html, circuit-fibsqrt, regex-chess).
                # Those test fixtures will arrive at verify-time via
                # StreamVerifier's upload_dir to /tests/<id>/.
                # See docs/framework/verification.md §9.2.
                if _copy_targets_test_or_solution(ins.args):
                    block.append(
                        f"# [aos: dropped `{ins.op} {ins.args.strip()}` "
                        "— tests/solution must not be baked into image]"
                    )
                    continue
                rewritten_lines = _rewrite_copy(
                    ins.args,
                    slot,
                    rules,
                    source_cwd=source_cwd,
                )
                for body in rewritten_lines:
                    block.append(f"{ins.op} {body}")
                # Collision-rename WORKDIR follow-up: if the original COPY's
                # dst was the task's effective cwd (e.g. `COPY task-deps/ /app/`
                # with `WORKDIR /app`), the task expects subsequent RUN to
                # find the copied files in cwd. When collision detection
                # renames the dst (e.g. `/workspace/` → `/workspace/task-deps__<id>/`),
                # the files land elsewhere — subsequent `RUN gcc feal.c`
                # fails because feal.c isn't in cwd. Detect and inject a
                # `WORKDIR <new-dst>` to keep cwd consistent with where
                # files actually landed. The next task's block start emits
                # its own `WORKDIR /workspace/...` so the shift is contained.
                shifted_workdir = _copy_dst_shifted_workdir(
                    ins.args, slot, rules, source_cwd=source_cwd,
                )
                if shifted_workdir and shifted_workdir != last_emitted_workdir:
                    block.append(f"WORKDIR {shifted_workdir}")
                    last_emitted_workdir = shifted_workdir
                continue
            if ins.op == "#":
                block.append(ins.raw)
                continue
            if ins.op in ("CMD", "ENTRYPOINT"):
                # Drop CMD / ENTRYPOINT from source tasks. Multiple CMDs in
                # the merged stage trigger BuildKit's
                # `MultipleInstructionsDisallowed` error, and the stream
                # image's baked CMD is irrelevant anyway — harbor's
                # `docker run` always specifies its own command. Keep an
                # audit comment so the merge author can review.
                block.append(
                    f"# [aos: dropped `{ins.op} {ins.args.strip()}` "
                    "— stream image doesn't use baked CMD/ENTRYPOINT]"
                )
                continue
            # Pass-through for unsupported / unhandled ops.
            block.append(f"# [aos: kept verbatim, original op {ins.op}]")
            block.append(ins.raw)

        per_task_run_blocks.append((slot, block))

    # Assemble the final text.
    out: list[str] = []
    if canary_comment:
        out.append(canary_comment)
    out.append("# AOS-Bench merged Dockerfile — generated by aos_bench.task_construction.assemble.dockerfile_merge")
    out.append("# Do not edit by hand; regenerate via the assemble_stream CLI.")
    out.append("")
    out.append(f"FROM {base_image}")
    out.append("")
    out.append("ENV DEBIAN_FRONTEND=noninteractive")
    # Source tasks expected python:3.x-slim-bookworm (no PEP 668 marker) where
    # `RUN pip install …` works system-wide. ubuntu:24.04 ships
    # EXTERNALLY-MANAGED, so we opt into the pre-PEP-668 behavior globally.
    out.append("ENV PIP_BREAK_SYSTEM_PACKAGES=1")
    # Pip + uv index URL — pulled from the mirror profile so the image can
    # be built against domestic (aliyun / tuna) or upstream (pypi.org) sources.
    # "upstream" leaves these unset so pip/uv use their built-in defaults.
    out.append(f"# AOS-Bench mirror profile: {mirror['name']}")
    if mirror["pip_index"]:
        out.append(f"ENV PIP_INDEX_URL={mirror['pip_index']}")
    if mirror["pip_trusted_host"]:
        out.append(f"ENV PIP_TRUSTED_HOST={mirror['pip_trusted_host']}")
    if mirror["uv_index"]:
        # uv has its own index-url env (it ignores PIP_INDEX_URL).
        out.append(f"ENV UV_INDEX_URL={mirror['uv_index']}")
    # HuggingFace datasets/hub library — leave HF_ENDPOINT at the default
    # (https://huggingface.co). Earlier images forced hf-mirror.com so build
    # could happen offline-of-the-firewall on CN networks, but hf-mirror.com
    # is unreliable for transformers/datasets resolves under load; on the GLM
    # host the docker-side proxy reaches huggingface.co directly so the
    # mirror is a net negative. If a future host truly can't reach external
    # HF, set HF_ENDPOINT on the run-time container instead of baking it in.
    out.append("ENV HF_ENDPOINT=https://huggingface.co")

    # ENV from tasks (de-dup by key).
    #
    # PATH is exempt from the de-dup: it is an ACCUMULATING variable, so
    # keeping only the first task's value silently discards every other task's
    # prefix. Measured: three pool tasks each set ENV PATH
    # (revideo /root/.local/bin, biped /opt/biped-env/bin, formal-crypto
    # /opt/conda/...), 15 of 32 candidates contain two or three of them, and
    # the merged Dockerfile kept exactly one. Both possible outcomes break a
    # build, in different places:
    #   - conda's PATH dropped  -> `conda create` dies with `conda: not found`
    #     at the step right after miniforge reports "installation finished"
    #     (heavy-20-scan-207, STEP 108/152, rc=127)
    #   - conda's PATH kept FIRST -> it shadows the system interpreter and a
    #     later task's pip breaks with
    #     `ImportError: cannot import name 'get_runnable_pip'`
    #     (heavy-20-scan-208/209, STEP 64/188, rc=1)
    # So restoring the dropped entries is not enough; the ORDER decides which
    # task breaks. Entries are therefore split by how the task consumes them:
    #   prefix  - the task needs its bin dir to be the DEFAULT interpreter,
    #             e.g. biped creates /opt/biped-env via `python3 -m venv` and
    #             then calls bare `pip`/`python`, in the Dockerfile and in its
    #             solve.sh. It must win.
    #   suffix  - the task only needs its tools to be FINDABLE, e.g.
    #             formal-crypto's tests/test.sh explicitly runs
    #             `source /opt/conda/etc/profile.d/conda.sh && conda activate`,
    #             so /opt/conda/bin works anywhere on PATH and must not shadow
    #             anyone else's python. (Checked test.sh before deciding this;
    #             moving conda to the tail would otherwise break the oracle.)
    # Within the prefix bucket a venv beats a plain bin dir, because a venv is
    # only ever created to BE the interpreter, whereas /root/.local/bin merely
    # adds user-installed scripts and tolerates being second.
    _PATH_SUFFIX_MARKERS = ("/opt/conda", "/opt/miniconda", "/opt/mambaforge")
    _PATH_VENV_MARKERS = ("-env/bin", "/venv/bin", "/.venv/bin")
    path_prefixes: list[str] = []
    path_suffixes: list[str] = []

    def _record_path(value: str) -> None:
        # Strip quotes and the ${PATH} placeholder; we re-insert it ourselves.
        val = value.strip().strip('"').strip("'")
        for part in val.split(":"):
            part = part.strip()
            if not part or part in ("$PATH", "${PATH}"):
                continue
            bucket = (
                path_suffixes
                if any(m in part for m in _PATH_SUFFIX_MARKERS)
                else path_prefixes
            )
            if part not in bucket:
                bucket.append(part)

    seen_env_keys: set[str] = set()
    for slot, env_arg in per_task_envs:
        # ENV K=V or ENV K V — take the key.
        key = env_arg.split("=", 1)[0].split()[0] if env_arg else ""
        if not key:
            continue
        if key == "PATH":
            _, _, value = env_arg.partition("=")
            _record_path(value or env_arg.split(None, 1)[-1])
            continue
        if key in seen_env_keys:
            continue
        seen_env_keys.add(key)
        out.append(f"ENV {env_arg}")

    if path_prefixes or path_suffixes:
        # venv entries first within the prefix bucket (see note above).
        path_prefixes.sort(
            key=lambda p: 0 if any(m in p for m in _PATH_VENV_MARKERS) else 1
        )
        joined = ":".join([*path_prefixes, "${PATH}", *path_suffixes])
        out.append(f'ENV PATH="{joined}"')

    out.append("")

    # Always include these compatibility packages in every merged stream image:
    #   python-is-python3  — creates /usr/bin/python → python3 symlink so tasks
    #                        originally written for python:3.x-slim (which ships
    #                        `python` as an alias) work under ubuntu:24.04.
    #   python3-pip        — needed so `pip install uv` below works even when
    #                        no source task installed pip.
    #   curl               — many test.sh scripts call curl directly.
    for pkg in ("python-is-python3", "python3-pip", "curl"):
        if pkg not in seen_pkgs:
            seen_pkgs.add(pkg)
            all_pkgs.append(pkg)

    # Global apt block.
    if all_pkgs:
        pkgs_lines = " \\\n    ".join(sorted(set(all_pkgs)))
        # Swap apt sources to the configured mirror before the install. The
        # sed targets both legacy /etc/apt/sources.list and the deb822-format
        # /etc/apt/sources.list.d/*.sources used by Ubuntu 24.04+. We keep
        # `http://` (NOT https) because the bare base image has no
        # ca-certificates yet — apt's GPG signatures already protect integrity.
        # The `|| true` makes it safe on images that don't have either file.
        # The "upstream" profile sets all from/to to None — skip the sed.
        if mirror["apt_main_from"]:
            sed_args = []
            for k_from, k_to in (
                ("apt_main_from", "apt_main_to"),
                ("apt_security_from", "apt_security_to"),
                ("apt_ports_from", "apt_ports_to"),
            ):
                src = mirror[k_from]
                dst = mirror[k_to]
                if src and dst:
                    sed_args.append(f"-e 's|{src}|{dst}|g'")
            sed_expr = " ".join(sed_args)
            out.append(
                "RUN sed -i "
                f"{sed_expr} "
                "/etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true"
            )
        out.append(
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n    "
            f"{pkgs_lines} \\\n    && rm -rf /var/lib/apt/lists/*"
        )
        # NOTE: previously had `git config --global url."https://kkgithub.com/"
        # .insteadOf "https://github.com/"` here. Dropped because:
        #   - kkgithub mirror is incomplete (e.g. AI4EPS/GaMMA → 404),
        #     causing legitimate `pip install git+https://github.com/...`
        #     calls to fail.
        #   - The build-time HTTP_PROXY (passed via --build-arg by oracle_sweep)
        #     makes github.com directly reachable, so the rewrite is unneeded.
        # If a future host loses proxy access, re-introduce insteadOf with a
        # narrower allow-list of repos known to mirror correctly.
        # Pre-install uv so test.sh scripts that run `uvx …` work even when the
        # runtime curl to astral.sh fails (flaky proxy).  pip is already
        # configured to use the tsinghua mirror via PIP_INDEX_URL above.
        out.append("RUN pip install uv")
        # Pre-download Python 3.13 into uv's cache so `uvx -p 3.13 …` at
        # runtime doesn't fetch ~33 MB from github.com/astral-sh/python-build-
        # standalone. uv respects HTTP_PROXY/HTTPS_PROXY which Docker injects
        # from --build-arg HTTP_PROXY=... (already set by oracle_sweep.py).
        # UV_PYTHON_INSTALL_MIRROR can be overridden via --build-arg if needed.
        out.append("ARG UV_PYTHON_INSTALL_MIRROR=")
        out.append("ENV UV_PYTHON_INSTALL_MIRROR=$UV_PYTHON_INSTALL_MIRROR")
        out.append("RUN uv python install 3.13")
        out.append("")

    # ---- Plan A (prelude) DISABLED 2026-07-20: ghproxy.net large-file curl
    # unreliable → every stream that would need sbt/miniforge prelude fails
    # on curl mid-download. Reverted; use whitelist_final to exclude tasks
    # that need these tools. Plan B (RUN body auto-patches in _swap_url_mirrors)
    # is kept.

    # Per-task blocks — the only place where source-task hardcoded URLs appear,
    # so URL mirror substitution applies here only. The preamble above contains
    # our own kkgithub insteadOf line and would self-rewrite if we swapped it.
    task_blocks_text_parts: list[str] = []
    for slot, block in per_task_run_blocks:
        task_blocks_text_parts.extend(block)
        task_blocks_text_parts.append("")
    task_blocks_text = _swap_url_mirrors("\n".join(task_blocks_text_parts))

    # Temporal isolation: tests/, solution/, and ground truth are NOT baked
    # into the agent's image. They're uploaded at verify-/oracle-time by
    # harbor.verifier.stream_verifier.StreamVerifier and
    # harbor.agents.oracle.OracleAgent._run_stream — see
    # docs/framework/verification.md §4.
    #
    # We still need this RUN line as a defensive fix-up for streams whose
    # per-task RUN order created /opt/venv but installed packages to the
    # system path first.
    oracle_lines = "\n".join(
        [
            "",
            # If a per-task RUN created /opt/venv but pip3-installed packages
            # went to the system path FIRST (different RUN ordering), drop a
            # .pth file so the venv's python3 can fall back to system
            # site-packages. .pth entries in a site-packages dir get APPENDED
            # to sys.path, so the venv's own packages still take priority —
            # only missing imports fall through to
            # /usr/local/lib/python3.12/dist-packages.
            "RUN if [ -d /opt/venv/lib/python3.12/site-packages ]; then "
            "echo /usr/local/lib/python3.12/dist-packages "
            "> /opt/venv/lib/python3.12/site-packages/aos_system_fallback.pth; "
            "fi",
            "",
        ]
    )
    text = (
        "\n".join(out)
        + ("\n" if out else "")
        + task_blocks_text
        + oracle_lines
    )
    if pool_v02:
        # v0.2 boilerplate (see docs/framework/task_assign.md §3.4):
        #   * aos_agent uid 1000 user (agent runs as this, not root)
        #   * socat + util-linux for the UDS bridge + runuser command
        #   * sudo with narrow allowlist so agent can install packages
        #     (apt/pip/npm) without gaining a root shell — keeps the
        #     "non-root limits agent" issue in §"agent capability" close
        #     to zero
        #   * /workspace and /skills as agent-owned, empty (pool stages
        #     per-task contents on PENDING→READY)
        #   * /aos_internal as root-only 0700 (sources + skills staging)
        text += (
            "\n# === aos_pool v0.2 isolation boilerplate ===\n"
            # aos_agent MUST land on uid 1000: harbor hardcodes
            # POOL_V02_AGENT_UID = 1000 and passes it to the pool sidecar as
            # --agent-uid, which chowns every revealed task payload to that
            # numeric uid. A different uid means the agent cannot read its own
            # workspace.
            #
            # ubuntu:24.04 (and 22.04+) ship a default `ubuntu` account that
            # already occupies uid 1000, so a plain `useradd -u 1000` fails
            # with "UID 1000 is not unique". Remove that account first — it is
            # an unused distro default with no processes or state. Guard on
            # `id -u ubuntu` = 1000 so we never delete a real account a task
            # image created on purpose.
            "RUN if [ \"$(id -u ubuntu 2>/dev/null)\" = 1000 ]; then \\\n"
            "      userdel -r ubuntu 2>/dev/null || userdel ubuntu; \\\n"
            "    fi \\\n"
            # No `|| true`: a silent failure here leaves the next `install -o
            # aos_agent` to fail instead, far from the cause. Tolerate only the
            # already-correct case, so re-running against an image that
            # already has the user is still fine.
            "    && (id -u aos_agent >/dev/null 2>&1 \\\n"
            "        || useradd -m -u 1000 -s /bin/bash aos_agent) \\\n"
            "    && [ \"$(id -u aos_agent)\" = 1000 ]\n"
            "RUN apt-get update -qq && apt-get install -y --no-install-recommends \\\n"
            "      socat util-linux sudo \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            "RUN install -d -m 0755 -o aos_agent -g aos_agent /workspace /skills\n"
            "RUN install -d -m 0700 -o root -g root /aos_internal /aos_internal/sources /aos_internal/skills /run\n"
            "RUN chmod -R 0700 /aos_internal\n"
            # Many upstream task images install a toolchain under /root — e.g.
            # coursier drops scalac in /root/.local/share/coursier/bin and puts
            # it on the global PATH. /root is 0700, so under v0.2 the agent gets
            # "Permission denied" on a binary its own PATH advertises, and the
            # task is impossible for reasons unrelated to the task.
            #
            # The verifier is NOT affected: it always runs as root (harbor's
            # stream_verifier.py passes verifier.user, which StreamTask never
            # sets, so no -u is added). That asymmetry is the danger — the
            # oracle passes while the agent cannot reach the compiler, so the
            # environment bug scores as a weak model solution.
            #
            # Grant traversal only: o+x on /root itself, never o+r. The agent
            # can walk through to the already-world-readable subdirectories it
            # needs, but still cannot list /root or read a root-only file whose
            # own mode denies it. Audited on a built image: everything reachable
            # this way is toolchain and package cache — no task payload and no
            # credentials, since /aos_internal is a separate 0700 tree that this
            # does not touch.
            "RUN chmod o+x /root\n"
            "# Narrow sudoers: aos_agent can install packages without\n"
            "# gaining root shell — no bash/sh/tee/chmod/chown allowed.\n"
            "RUN echo 'aos_agent ALL=(root) NOPASSWD: "
            "/usr/bin/apt-get, /usr/bin/apt, "
            "/usr/bin/pip, /usr/bin/pip3, "
            "/usr/bin/npm, /usr/bin/yarn' "
            "> /etc/sudoers.d/aos_agent && "
            "chmod 0440 /etc/sudoers.d/aos_agent\n"
            "WORKDIR /workspace\n"
        )
    else:
        text += "WORKDIR /workspace\n"
    if mirror["name"] != "upstream":
        text = normalize_domestic_install_sources(text)
    return MergedDockerfile(
        text=text,
        apt_packages=all_pkgs,
        base_image=base_image,
        sources_to_copy=sources_to_copy,
    )


# Hardcoded URLs in source Dockerfiles (e.g. `RUN curl https://raw.githubusercontent.com/...`,
# `ARG URL=https://github.com/.../releases/download/...`) bypass git/pip/uv mirror
# config because they're literal strings. Sed-replace them at merge time.
#
# Two layers, applied in order:
#
# 1. **Big archive endpoints** → route via ghproxy.net. kkgithub's
#    /archive/<sha-or-tag>.tar.gz and /releases/download/... endpoints abort
#    on multi-hundred-MB tarballs (gcc-13.2.0 ~130 MB, druid sources ~150 MB).
#    ghproxy.net is a transparent HTTPS prefix proxy and handles those
#    sizes more reliably in the CN→github path. Pattern matches both the
#    raw `github.com` URL form AND the `kkgithub.com` form (since source
#    Dockerfiles sometimes pre-rewrite to kkgithub themselves).
#
# 2. **Regular git operations** → kkgithub.com. `git clone` / `git fetch`
#    over the kkgithub mirror works fine for small/medium repos; ghproxy
#    only proxies HTTPS GETs (not git protocol packs).
import re as _re

_URL_REWRITE_PATTERNS: list[tuple[_re.Pattern[str], str]] = [
    # 1. Big archive/release endpoints — route via ghproxy.net using the
    #    canonical github.com path inside the proxy URL. Matches both
    #    `github.com/u/r/archive/...` and `kkgithub.com/u/r/archive/...`
    #    (source dockerfiles sometimes hardcode kkgithub already).
    (
        _re.compile(
            r"https://(?:kkgithub\.com|github\.com)/"
            r"([^/\s\"']+/[^/\s\"']+)/(archive|releases/download)/"
        ),
        r"https://ghproxy.net/https://github.com/\1/\2/",
    ),
    # 2. raw.githubusercontent.com → ghproxy wrap (raw.gitcode.host has
    #    redirect loops on some repos).
    (
        _re.compile(r"https://raw\.githubusercontent\.com/"),
        "https://ghproxy.net/https://raw.githubusercontent.com/",
    ),
    # 3. Regular github URLs → keep github.com direct (via HTTP_PROXY).
    #    Historically we rewrote to kkgithub.com because ghproxy could not
    #    handle git protocol packs, but kkgithub is an incomplete mirror —
    #    many small repos return 404 (verified 2026-07-18: sadiqj/ocaml,
    #    ozkl/doomgeneric, etc). With the corp HTTP_PROXY set in the sweep
    #    (proxy.example.com:8888), plain github.com is reachable, so no
    #    rewrite is needed.
    # (
    #     _re.compile(r"(?<!ghproxy\.net/)https://github\.com/(?!openwall/john)"),
    #     "https://kkgithub.com/",
    # ),
    # huggingface.co is reached via the GLM docker proxy at build-time
    # (injected as HTTP_PROXY build-arg by harbor's compose-build.yaml,
    # 2026-07-21). No rewrite needed — hf-mirror.com 308-redirects back
    # to huggingface.co anyway, so a rewrite would just add a hop.
]


def _swap_url_mirrors(text: str) -> str:
    for pat, repl in _URL_REWRITE_PATTERNS:
        text = pat.sub(repl, text)
    text = _make_git_clone_idempotent(text)
    text = _auto_patch_run_bodies(text)
    return text


# ============ Plan A + B auto-fix (2026-07-20) ============
#
# Individual tasks pass preflight (solo build) but fail when merged because
# they assume their own base image provides sbt/mamba/python3.9/etc. Plan A
# = inject a "prelude" installation step for these tools ONCE if any task in
# the stream uses them. Plan B = auto-patch fragile RUN commands.

def _detect_task_prelude_needs(slots_with_dockerfiles) -> set[str]:
    """Scan all source Dockerfiles for tools that need a merged-stream
    prelude install."""
    needs: set[str] = set()
    for slot, df in slots_with_dockerfiles:
        df_path = slot.source_dir / "environment" / "Dockerfile"
        try: text = df_path.read_text()
        except Exception: continue
        lower = text.lower()
        if 'sbt update' in lower or 'sbt/releases/download' in lower or \
           '/usr/local/sbt/bin/sbt' in lower:
            needs.add('sbt')
        if 'mamba install' in lower or 'micromamba install' in lower or \
           'conda install' in lower or 'conda create' in lower or \
           'miniforge' in lower or 'condaforge/miniforge' in lower:
            needs.add('conda')
        if 'FROM python:3.9' in text or 'FROM python:3.10' in text or \
           'FROM python:3.11' in text:
            needs.add('python-legacy')
    return needs


def _prelude_blocks(needs: set[str], proxy_hint: str | None = None) -> list[str]:
    """Emit RUN blocks to install missing base-image tools once."""
    out: list[str] = []
    proxy_prefix = "https://ghproxy.net/"
    if 'sbt' in needs:
        out.append("# --- prelude: install sbt (task assumes eclipse-temurin base) ---")
        out.append("RUN apt-get update && apt-get install -y --no-install-recommends "
                   "openjdk-17-jdk ca-certificates curl && rm -rf /var/lib/apt/lists/*")
        out.append("ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64")
        out.append("RUN curl -fsSL "
                   f"{proxy_prefix}https://github.com/sbt/sbt/releases/download/v1.9.6/sbt-1.9.6.tgz "
                   "| tar -xz -C /usr/local && ln -sf /usr/local/sbt/bin/sbt /usr/local/bin/sbt")
    if 'conda' in needs:
        out.append("# --- prelude: install miniforge (task assumes conda-forge base) ---")
        out.append("RUN curl -fsSL "
                   f"{proxy_prefix}https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh "
                   "-o /tmp/mf.sh && bash /tmp/mf.sh -b -p /opt/conda && rm /tmp/mf.sh")
        out.append("ENV PATH=/opt/conda/bin:$PATH")
    if 'python-legacy' in needs:
        out.append("# --- prelude: install python3.9/3.10/3.11 via deadsnakes ppa ---")
        out.append("RUN apt-get update && apt-get install -y --no-install-recommends "
                   "software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa "
                   "&& apt-get update && apt-get install -y --no-install-recommends "
                   "python3.9 python3.9-venv python3.10 python3.10-venv "
                   "python3.11 python3.11-venv && rm -rf /var/lib/apt/lists/*")
    return out


# ---- Plan B: RUN body auto-patches -----------------------------------------

_PIP_UPGRADE_RE = _re.compile(r"pip\s+install\s+([^\n&|;]*?--upgrade\s+pip[^\n&|;]*)")
_GIT_RESET_SHA_RE = _re.compile(r"git reset --hard\s+([0-9a-f]{7,40})")


def _auto_patch_run_bodies(text: str) -> str:
    # pip --upgrade pip conflicts with system-installed pip → add --ignore-installed
    def _pip_upgrade(m: "_re.Match[str]") -> str:
        args = m.group(1)
        if '--ignore-installed' in args or '--break-system-packages' in args:
            return m.group(0)
        return f"pip install --ignore-installed --break-system-packages {args}"
    text = _PIP_UPGRADE_RE.sub(_pip_upgrade, text)
    # git reset --hard <sha> can fail on shallow / re-used checkout — wrap
    text = _GIT_RESET_SHA_RE.sub(
        r"(git reset --hard \1 2>/dev/null || echo 'reset skipped: sha not present in checkout')",
        text,
    )
    return text


# Idempotency wrapper: two merged tasks sometimes both `git clone` the same
# repo. Vanilla git-clone fails "destination path 'X' already exists". Wrap
# every clone with `test -d <dest> ||` so the second one no-ops. This keeps
# the tasks' subsequent code working (they still find the checkout at the
# expected path) at the cost of skipping potential branch/commit mismatch.
_GIT_CLONE_RE = _re.compile(
    r"git clone(?P<flags>(?:\s+-{1,2}[\w-]+(?:[= ]+\S+)?)*)\s+"
    r"(?P<url>https?://\S+?)(?:\s+(?P<dest>[^\s;&|<>]+))?(?=[\s;&|<>]|$)"
)


def _make_git_clone_idempotent(text: str) -> str:
    def _repl(m: "_re.Match[str]") -> str:
        url = m.group("url")
        dest = m.group("dest")
        if not dest:
            dest = url.rstrip("/").split("/")[-1]
            if dest.endswith(".git"):
                dest = dest[:-4]
        flags = m.group("flags") or ""
        return f"(test -d {dest} || git clone{flags} {url} {dest})"
    return _GIT_CLONE_RE.sub(_repl, text)


def merge_from_paths(
    slots_and_dockerfile_paths: list[tuple[TaskSlot, Path]],
    *,
    base_image_override: str | None = None,
    accept_mixed_from: bool = False,
    extra_apt_packages: Iterable[str] = (),
    canary_comment: str | None = None,
    shared_prefixes: Iterable[str] = (),
    collision_renames_per_slot: dict[str, dict[str, str]] | None = None,
    pool_v02: bool = False,
    mirror_profile: str | None = None,
) -> MergedDockerfile:
    parsed: list[tuple[TaskSlot, ParsedDockerfile]] = []
    for slot, df_path in slots_and_dockerfile_paths:
        parsed.append((slot, parse_dockerfile(df_path.read_text())))
    return merge_dockerfiles(
        slots_with_dockerfiles=parsed,
        base_image_override=base_image_override,
        accept_mixed_from=accept_mixed_from,
        extra_apt_packages=extra_apt_packages,
        canary_comment=canary_comment,
        shared_prefixes=shared_prefixes,
        collision_renames_per_slot=collision_renames_per_slot,
        pool_v02=pool_v02,
        mirror_profile=mirror_profile,
    )
