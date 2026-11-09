"""In-place path migration + solve.sh emission for wildclawbench-converted/.

Re-running `convert_wildclawbench.py` requires re-pulling the source workspace
fixtures (LFS-heavy). To migrate the already-converted task dirs to the new
/workspace/ path scheme without that, this script:

  1. Rewrites each task's environment/Dockerfile from the new template, while
     auto-detecting which COPY blocks are needed from _workspace_exec/
     _workspace_gt/ _skills/ presence.
  2. Rewrites tests/test.sh from the new template (cd /workspace, no /app
     symlink).
  3. Path-rewrites instruction.md and tests/test_outputs.py grade() body
     (`/tmp_workspace/` → `/workspace/tmp/`, `/root/skills` → `/workspace/skills`).
  4. Writes solution/solve.sh for any task that has a registered solver under
     `wildclaw_solvers/`.

Usage::

    python -m aos_bench.task_construction.scripts.migrate_wildclaw_in_place
    python -m aos_bench.task_construction.scripts.migrate_wildclaw_in_place \\
        --only 01_Productivity_Flow_task_3_bibtex 02_Code_Intelligence_task_4_jigsaw_puzzle_medium_zh \\
        04_Search_Retrieval_task_10_tomllib_trace 05_Creative_Synthesis_task_3_product_poster \\
        06_Safety_Alignment_task_1_file_overwrite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aos_bench.task_construction.scripts.convert_wildclawbench import (
    _DOCKERFILE_TEMPLATE,
    _SOLVERS,
    _TEST_SH,
    _rewrite_paths,
    _safe_rel,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]


def _patch_dockerfile(task_dir: Path) -> None:
    env = task_dir / "environment"
    has_exec = (env / "_workspace_exec").is_dir()
    has_gt_legacy = (env / "_workspace_gt").is_dir()  # old location
    has_gt_tests = (task_dir / "tests" / ".gt").is_dir()  # tests-side
    has_gt_solution = (task_dir / "solution" / ".gt").is_dir()  # solution-side
    has_skills = (env / "_skills").is_dir()

    # Migrate gt: env/_workspace_gt/ → tests/.gt/ AND solution/.gt/ (one-shot).
    # gt is dual-shipped because oracle phase only uploads solution/ (per
    # harbor single-task contract) but the grader needs gt at verify time too.
    if has_gt_legacy:
        import shutil as _shutil
        gt_src = env / "_workspace_gt"
        for dst in ((task_dir / "tests" / ".gt"), (task_dir / "solution" / ".gt")):
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copytree(gt_src, dst)
        _shutil.rmtree(gt_src)
        has_gt_tests = True
        has_gt_solution = True
        has_gt_legacy = False
    elif has_gt_tests and not has_gt_solution:
        # Already moved to tests/.gt/ in a prior migration — clone to solution/.gt/.
        import shutil as _shutil
        sol_gt = task_dir / "solution" / ".gt"
        sol_gt.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copytree(task_dir / "tests" / ".gt", sol_gt)
        has_gt_solution = True

    copy_workspace: list[str] = []
    if has_exec:
        copy_workspace.append("COPY _workspace_exec/ /workspace/")
    # No more COPY _workspace_gt/ — gt lives under tests/.gt/ (host-side) and
    # is uploaded at verify-time by harbor.
    if has_exec or has_gt_tests:
        copy_workspace.append("RUN ln -sfn /workspace /workspace/tmp")
    if not copy_workspace:
        copy_workspace = ["# (no workspace fixtures present)"]

    copy_skills: list[str] = []
    if has_skills:
        copy_skills.append("RUN mkdir -p /workspace/skills")
        copy_skills.append("COPY _skills/ /workspace/skills/")
    if not copy_skills:
        copy_skills = ["# (no skills referenced)"]

    # Preserve a `Source:` comment if the old Dockerfile had one.
    old = (env / "Dockerfile").read_text(errors="ignore") if (env / "Dockerfile").exists() else ""
    src_md_rel = "(in-place migration)"
    for line in old.splitlines():
        if line.startswith("# Source:"):
            src_md_rel = line.split("# Source:", 1)[1].strip()
            break

    df = _DOCKERFILE_TEMPLATE.format(
        source_md_rel=src_md_rel,
        copy_workspace="\n".join(copy_workspace),
        copy_skills="\n".join(copy_skills),
    )
    (env / "Dockerfile").write_text(df)


def _patch_instruction(task_dir: Path) -> None:
    p = task_dir / "instruction.md"
    if not p.is_file():
        return
    p.write_text(_rewrite_paths(p.read_text(encoding="utf-8")))


def _patch_test_sh(task_dir: Path) -> None:
    p = task_dir / "tests" / "test.sh"
    p.write_text(_TEST_SH)
    p.chmod(0o755)


def _patch_test_outputs(task_dir: Path) -> None:
    p = task_dir / "tests" / "test_outputs.py"
    if not p.is_file():
        return
    p.write_text(_rewrite_paths(p.read_text(encoding="utf-8")))


def _emit_solver(task_dir: Path, task_id: str) -> bool:
    body = _SOLVERS.get(task_id)
    if not body:
        return False
    sol = task_dir / "solution"
    sol.mkdir(exist_ok=True)
    (sol / "solve.sh").write_text(body)
    (sol / "solve.sh").chmod(0o755)
    return True


def migrate(root: Path, only: set[str] | None = None) -> tuple[int, int]:
    """Walk root/ and migrate each task subdir. Returns (n_migrated, n_solvers)."""
    migrated = 0
    solver_count = 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in {"build_failed"}:
            continue
        if not (d / "task.toml").is_file():
            continue
        if only and d.name not in only:
            continue
        _patch_dockerfile(d)
        _patch_instruction(d)
        _patch_test_sh(d)
        _patch_test_outputs(d)
        emitted = _emit_solver(d, d.name)
        if emitted:
            solver_count += 1
        migrated += 1
    return migrated, solver_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=_REPO_ROOT / "data" / "harbor" / "wildclawbench-converted",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        help="Restrict to specific task_ids (default: every task in root/)",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"missing root: {root}")

    only = set(args.only) if args.only else None
    print(f"[migrate] root: {_safe_rel(root)}")
    if only:
        print(f"[migrate] restricted to {len(only)} task(s): {sorted(only)}")

    n, n_solver = migrate(root, only)
    print(f"[migrate] {n} task(s) updated, {n_solver} solve.sh written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
