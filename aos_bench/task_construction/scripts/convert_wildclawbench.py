"""WildClawBench → harbor task format converter.

WildClawBench packages each task as a single Markdown file with YAML
frontmatter plus Markdown sections (`## Prompt`, `## Automated Checks`, etc.).
This script splits each task into the four harbor protocol files:

  task_id/
    task.toml
    instruction.md
    environment/
      Dockerfile
    tests/
      test.sh
      test_outputs.py            # the original Python `grade()` function

Workspace fixtures (input data, ground-truth JSON, etc.) live on HuggingFace
under `internlm/WildClawBench/workspace/`. They get pre-downloaded via
huggingface-cli and copied into each task's `environment/_workspace/`, then
the generated Dockerfile `COPY`s them into `/app/`.

Tasks whose fixtures require yt-dlp / ModelScope downloads (videos, large
model weights) are filtered out — they break the offline-build assumption.

Usage::

    python -m aos_bench.task_construction.scripts.convert_wildclawbench \\
        --src    data/harbor/wildclawbench \\
        --out    data/harbor/wildclawbench-converted \\
        --workspace-dir data/harbor/wildclawbench/workspace
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print(
        "PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    raise


_REPO_ROOT = Path(__file__).resolve().parents[3]


def _safe_rel(p: Path) -> str:
    """Best-effort short path: relative to repo root if possible, else absolute."""
    try:
        return str(p.resolve().relative_to(_REPO_ROOT))
    except ValueError:
        return str(p)


# Tasks whose `prepare.sh` step requires yt-dlp / ModelScope / large blob
# downloads. Skip them at convert time.
_REQUIRES_YT_DLP_OR_LARGE_BLOBS = {
    "05_Creative_Synthesis_task_1_match_report",
    "05_Creative_Synthesis_task_2_goal_highlights",
    "05_Creative_Synthesis_task_4_video_notes",
    "05_Creative_Synthesis_task_5_product_launch_video_to_json",
    "05_Creative_Synthesis_task_11_video_en_to_zh_dub",
    "02_Code_Intelligence_task_1_sam3_inference",
    "02_Code_Intelligence_task_2_sam3_debug",
}


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@dataclass
class ParsedTask:
    """One WildClawBench task split into the bits we care about."""

    task_id: str  # frontmatter `id`
    name: str  # frontmatter `name`
    category: str  # frontmatter `category`
    timeout_seconds: int
    workspace_path: str  # e.g. workspace/01_Productivity_Flow/task_10_pdf_digest
    prompt: str  # body of `## Prompt` section
    grade_python: str  # raw Python source of `## Automated Checks` block
    skills: list[str]  # names from `## Skills` block
    source_md: Path

    @property
    def short_name(self) -> str:
        # Take the part after the category prefix:
        # "01_Productivity_Flow_task_10_pdf_digest" → "task_10_pdf_digest"
        prefix = self.category + "_"
        if self.task_id.startswith(prefix):
            return self.task_id[len(prefix) :]
        return self.task_id


# --- parsing -----------------------------------------------------------------


def _parse_md(path: Path) -> ParsedTask | None:
    text = path.read_text(encoding="utf-8")
    fm_match = _FRONTMATTER_RE.match(text)
    if not fm_match:
        print(f"  [skip] {path}: no YAML frontmatter", file=sys.stderr)
        return None
    fm = yaml.safe_load(fm_match.group(1)) or {}
    body = text[fm_match.end() :]

    sections = _split_sections(body)
    prompt = sections.get("Prompt", "").strip()
    grade_block = _extract_python_block(sections.get("Automated Checks", ""))
    workspace_path = _extract_workspace_path(sections.get("Workspace Path", ""))
    skills = _extract_skills(sections.get("Skills", ""))

    if not prompt:
        print(f"  [skip] {path}: no Prompt section", file=sys.stderr)
        return None
    if not grade_block:
        print(f"  [skip] {path}: no Automated Checks Python block", file=sys.stderr)
        return None

    return ParsedTask(
        task_id=str(fm.get("id", "")),
        name=str(fm.get("name", "")),
        category=str(fm.get("category", "")),
        timeout_seconds=int(fm.get("timeout_seconds", 600)),
        workspace_path=workspace_path,
        prompt=prompt,
        grade_python=grade_block,
        skills=skills,
        source_md=path,
    )


def _split_sections(body: str) -> dict[str, str]:
    """Split markdown body into `## Heading` → content map."""
    parts: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                parts[current] = "\n".join(buf)
            current = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    if current is not None:
        parts[current] = "\n".join(buf)
    return parts


def _extract_python_block(text: str) -> str:
    """Pull the first ```python ... ``` fenced block out of the section."""
    m = re.search(r"```python\s*\n(.*?)\n```", text, re.DOTALL)
    return m.group(1) if m else ""


def _extract_workspace_path(text: str) -> str:
    """Pull workspace path out of the `## Workspace Path` section.

    Format is typically a fenced ``` block with one line.
    """
    m = re.search(r"```[a-z]*\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1).strip().splitlines()[0].strip()
    # Fallback: first non-empty line.
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _extract_skills(text: str) -> list[str]:
    """Pull skill names from the `## Skills` section (one per line in fenced block)."""
    m = re.search(r"```[a-z]*\s*\n(.*?)\n```", text, re.DOTALL)
    body = m.group(1) if m else text
    out: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# --- path migration ---------------------------------------------------------
#
# WildClawBench source uses /tmp_workspace/... (and the original docker image
# used /app for the workspace root). For AOS-Bench we apply two layers of
# rewriting to enforce temporal isolation (per docs/framework/verification.md):
#
# 1. Agent-visible paths under /workspace/:
#      /app/foo            → /workspace/foo
#      /tmp_workspace/foo  → /workspace/tmp/foo   (symlink to /workspace/foo)
#      /root/skills/foo    → /workspace/skills/foo
#
# 2. Ground truth is NOT baked into agent image. Old wildclaw layout put it
#    at /workspace/.gt/ (image-baked, agent-visible). New layout: gt ships
#    inside <task>/tests/.gt/ and is only uploaded at verify-/oracle-time
#    via harbor's StreamVerifier / OracleAgent (which inject /tests/<id>/).
#    So in grader bodies we rewrite gt accesses to
#      /workspace/.gt        → ${AOS_TESTS_DIR}/.gt
#      /tmp_workspace/gt     → ${AOS_TESTS_DIR}/.gt
#    `AOS_TESTS_DIR` is set by both StreamVerifier (verify time) and our
#    stream oracle orchestrator (in solve.sh) — see verification.md §10.4.
#
# Plain string replacement works because these prefixes never appear inside
# unrelated identifiers in the corpora we've audited (instruction.md +
# grade() body). Order matters: longer prefixes first so we don't
# half-rewrite (e.g. /tmp_workspace/gt must be tried before /tmp_workspace).


_PATH_REWRITES: tuple[tuple[str, str], ...] = (
    # agent-visible workspace
    ("/tmp_workspace", "/workspace/tmp"),
    ("/root/skills", "/workspace/skills"),
    # gt paths stay as /workspace/.gt (or /workspace/tmp/gt) syntactically; at
    # verify-/oracle-time the test.sh / solve.sh shim installs symlinks:
    #   /workspace/.gt    → $AOS_TESTS_DIR/.gt
    #   /workspace/gt     → /workspace/.gt   (for /workspace/tmp/gt resolution)
    # This avoids rewriting the many variants of gt-path construction inside
    # grader Python source (`Path("/workspace/tmp/gt")`, `workspace / "gt"`,
    # etc.). Trade-off: wildclaw verify must be SEQUENTIAL (symlink target
    # would race in parallel). tb2 / skb graders don't touch /workspace/.gt
    # so they remain parallel-safe.
    # /app/ rewrite intentionally NOT included: zero references in
    # instruction.md / grade() across the corpus, and the substring "/app/"
    # collides with paths like "/usr/share/novnc/app/..." inside other code.
)


def _rewrite_paths(text: str) -> str:
    for src, dst in _PATH_REWRITES:
        text = text.replace(src, dst)
    return text


# --- per-task oracle solvers ------------------------------------------------
#
# WildClawBench does NOT ship a solve.sh. For AOS-Bench's stream oracle to
# include wildclaw tasks, we register hand-written solvers keyed by task_id.
# Each solver reads /workspace/.gt/ ground-truth artifacts and materializes
# the expected agent deliverable files (per the grader's checks).
#
# `_SOLVERS` is a dict task_id → solve.sh body. Tasks without an entry get
# no solution/ dir, and the stream oracle reports them as `no_solver`.


def _load_solvers() -> dict[str, str]:
    """Load solve.sh bodies from the sibling `wildclaw_solvers/` dir.

    Each file is named `<task_id>.sh` and contains a complete bash script;
    convert_wildclawbench writes it into `<task>/solution/solve.sh`.
    """
    solvers_dir = Path(__file__).resolve().parent / "wildclaw_solvers"
    out: dict[str, str] = {}
    if not solvers_dir.is_dir():
        return out
    for f in sorted(solvers_dir.iterdir()):
        if f.suffix == ".sh":
            out[f.stem] = f.read_text()
    return out


_SOLVERS: dict[str, str] = _load_solvers()


# --- emission ----------------------------------------------------------------


_DOCKERFILE_TEMPLATE = """\
# WildClawBench task converted to harbor format by aos_bench.task_construction.
# Source: {source_md_rel}
#
# Temporal isolation (per docs/framework/verification.md):
#   - This image contains ONLY agent-visible inputs: exec fixtures and
#     optional skills. Ground truth (gt/) and tests are NOT baked in.
#   - At verify-/oracle-time, harbor uploads:
#       <task>/tests/   →  /tests/<task_id>/      (includes /tests/<id>/.gt/)
#       <stream>/solution/ → /solution/           (oracle only)
#     and test.sh installs the symlink `/workspace/.gt → $AOS_TESTS_DIR/.gt`
#     so the grader's existing `Path("/workspace/.gt")` / `workspace/"gt"`
#     references resolve correctly.
#
# Path layout when test.sh / solve.sh runs:
#   /workspace/             ← task root (was /app)
#   /workspace/.gt          ← symlink to $AOS_TESTS_DIR/.gt (installed by shim)
#   /workspace/gt           ← symlink to /workspace/.gt (so /workspace/tmp/gt resolves)
#   /workspace/tmp          ← symlink to /workspace (was /tmp_workspace)
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    python3 \\
    python3-pip \\
    ca-certificates \\
    curl \\
    && rm -rf /var/lib/apt/lists/*

# Python packages needed by the WildClawBench grader at verify-time.
# `openai` is imported by ~34 graders (LLM-as-judge); `Pillow` by image-based
# pre-checks even in tasks that fall back to text-only judging.
# Reward-time env (set by StreamVerifier / OracleAgent): OPENROUTER_API_KEY /
# OPENROUTER_BASE_URL / JUDGE_MODEL (default openai/gpt-5.4).
RUN pip install --no-cache-dir \\
        -i https://pypi.tuna.tsinghua.edu.cn/simple \\
        openai Pillow

WORKDIR /workspace

# Workspace fixtures (initial input only — no ground truth).
{copy_workspace}

# Skills referenced by this task — markdown skill descriptions.
{copy_skills}
"""


_TEST_SH = """\
#!/bin/bash
# WildClawBench task verifier — runs the Python grader and writes reward.txt.
# Reward = overall_score in [0.0, 1.0].
#
# Resolves env from BOTH naming conventions:
#   - harbor single-task mode:  AOS_REWARD_DIR=/logs/verifier, AOS_TESTS_DIR=/tests
#   - AOS-Bench stream mode:    __AOS_REWARD_DIR / __AOS_TESTS_DIR injected by
#                               aos_bench.task_construction.assemble.reward_rewrite
# So this script works standalone OR inside a merged stream image.

set -e
__AOS_REWARD_DIR="${__AOS_REWARD_DIR:-${AOS_REWARD_DIR:-/logs/verifier}}"
__AOS_TESTS_DIR="${__AOS_TESTS_DIR:-${AOS_TESTS_DIR:-/tests}}"
mkdir -p "$__AOS_REWARD_DIR"

# Temporal-isolation shim: ground truth was uploaded into $AOS_TESTS_DIR/.gt
# at verify time (NOT baked into the agent image). The grader's hardcoded
# /workspace/.gt and /workspace/tmp/gt paths resolve via these symlinks.
# Re-install on every run so parallel re-points after a different task
# overwrites them.
# NOTE: wildclaw verify must be SEQUENTIAL because of this symlink contention.
_gt="${__AOS_TESTS_DIR}/.gt"
if [ -d "$_gt" ]; then
    ln -sfn "$_gt" /workspace/.gt
    ln -sfn /workspace/.gt /workspace/gt
fi
# /workspace/tmp must always exist (legacy /tmp_workspace alias). Created in
# image build, but recreate defensively in case the agent removed it.
[ -e /workspace/tmp ] || ln -sfn /workspace /workspace/tmp

cd /workspace
score=$(python3 "$__AOS_TESTS_DIR/test_outputs.py" 2>&1 | tail -1)
echo "$score" > "$__AOS_REWARD_DIR"/reward.txt
"""


_TEST_PY_HARNESS = """\
\"\"\"Harness around the original WildClawBench `grade()` function.

Loads the function below and prints its `overall_score` to stdout so test.sh
can capture it as the reward.
\"\"\"

import json
import sys


# === BEGIN ORIGINAL WildClawBench grade() ===
{grade_body}
# === END ORIGINAL WildClawBench grade() ===


if __name__ == "__main__":
    try:
        scores = grade()
        if not isinstance(scores, dict):
            print("0.0")
            sys.exit(0)
        overall = scores.get("overall_score", 0.0)
        try:
            overall = float(overall)
        except (TypeError, ValueError):
            overall = 0.0
        print(f"{{overall:.4f}}")
        # Also dump full breakdown to stderr for debugging.
        print(json.dumps(scores, ensure_ascii=False, indent=2), file=sys.stderr)
    except Exception as e:
        print("0.0")
        print(f"grader error: {{e}}", file=sys.stderr)
"""


def _emit_task(
    task: ParsedTask,
    out_root: Path,
    workspace_root: Path | None,
    skills_root: Path,
    *,
    allow_missing_fixtures: bool = False,
) -> tuple[bool, str]:
    """Write one harbor-format task dir. Return (ok, reason_if_not_ok).

    If `workspace_root` is given but the task's fixtures are missing AND
    `allow_missing_fixtures=False`, the half-emitted dir is cleaned up before
    returning.
    """
    out = out_root / task.task_id
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    def _bail(reason: str) -> tuple[bool, str]:
        # Don't leave a half-populated task dir behind.
        if out.exists():
            shutil.rmtree(out, ignore_errors=True)
        return False, reason

    # task.toml
    timeout_min = max(5, task.timeout_seconds // 60)
    difficulty = "medium"  # WildClawBench doesn't expose difficulty per task
    domain = task.category.split("_", 1)[1].lower().replace("_", "-") \
        if "_" in task.category else task.category.lower()
    toml = textwrap.dedent(f"""\
        # Auto-converted from WildClawBench by convert_wildclawbench.py
        # Source: {_safe_rel(task.source_md)}

        [metadata]
        category = "{task.category}"
        domain = "{domain}"
        difficulty = "{difficulty}"
        expert_time_estimate_min = {timeout_min}
        original_name = {repr(task.name)}
        original_workspace_path = "{task.workspace_path}"
        """)
    (out / "task.toml").write_text(toml)

    # instruction.md (path-rewritten so all agent-visible paths sit under /workspace/)
    (out / "instruction.md").write_text(_rewrite_paths(task.prompt) + "\n")

    # tests/ (also path-rewrite the grader body — graders hardcode /tmp_workspace/...)
    (out / "tests").mkdir()
    (out / "tests" / "test.sh").write_text(_TEST_SH)
    (out / "tests" / "test.sh").chmod(0o755)
    grade_python_rewritten = _rewrite_paths(task.grade_python)
    test_py = _TEST_PY_HARNESS.format(grade_body=grade_python_rewritten)
    (out / "tests" / "test_outputs.py").write_text(test_py)

    # environment/
    env_dir = out / "environment"
    env_dir.mkdir()

    # Per-task gt: ships under tests/.gt/ (host-side), NOT baked into image.
    # harbor uploads <task>/tests/ → /tests/<task_id>/ at verify-/oracle-time,
    # so gt arrives at /tests/<task_id>/.gt/. test.sh installs the
    # /workspace/.gt → $AOS_TESTS_DIR/.gt symlink so graders still resolve
    # their existing /workspace/.gt paths.
    copy_workspace_lines: list[str] = []
    if workspace_root and task.workspace_path:
        ws = workspace_root.parent / task.workspace_path
        ws_exec = ws / "exec"
        ws_gt = ws / "gt"
        if ws_exec.is_dir():
            target = env_dir / "_workspace_exec"
            shutil.copytree(ws_exec, target)
            copy_workspace_lines.append("COPY _workspace_exec/ /workspace/")
        if ws_gt.is_dir():
            # NEW location: ground truth ships with BOTH tests/ (so the grader
            # finds it after StreamVerifier upload) AND solution/ (so the
            # solver finds it after OracleAgent upload). Same content, two
            # copies — they're tiny JSON/text files. This way the oracle phase
            # never needs tests/ (aligning with harbor single-task contract).
            for gt_subdir in ((out / "tests" / ".gt"), (out / "solution" / ".gt")):
                if gt_subdir.exists():
                    shutil.rmtree(gt_subdir)
                shutil.copytree(ws_gt, gt_subdir)
        # Install /workspace/tmp → /workspace symlink at build time (graders
        # reference /workspace/tmp/... even when no gt is present, and the
        # shim in test.sh recreates it defensively if removed).
        if ws_exec.is_dir() or ws_gt.is_dir():
            copy_workspace_lines.append("RUN ln -sfn /workspace /workspace/tmp")
        if not copy_workspace_lines and not ws_gt.is_dir():
            if not allow_missing_fixtures:
                return _bail(f"no fixtures found at {ws}")
    if not copy_workspace_lines:
        copy_workspace_lines = ["# (no workspace fixtures referenced)"]

    copy_skills_lines: list[str] = []
    if task.skills and skills_root.is_dir():
        kept_skills = []
        for skill in task.skills:
            sk = skills_root / skill
            if sk.is_dir():
                target = env_dir / "_skills" / skill
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(sk, target)
                kept_skills.append(skill)
        if kept_skills:
            copy_skills_lines.append("RUN mkdir -p /workspace/skills")
            copy_skills_lines.append("COPY _skills/ /workspace/skills/")
    if not copy_skills_lines:
        copy_skills_lines = ["# (no skills referenced)"]

    df = _DOCKERFILE_TEMPLATE.format(
        source_md_rel=_safe_rel(task.source_md),
        copy_workspace="\n".join(copy_workspace_lines),
        copy_skills="\n".join(copy_skills_lines),
    )
    (env_dir / "Dockerfile").write_text(df)

    # solution/solve.sh — hand-written oracle for this task, if one exists.
    solver_body = _SOLVERS.get(task.task_id)
    if solver_body:
        sol_dir = out / "solution"
        sol_dir.mkdir()
        (sol_dir / "solve.sh").write_text(solver_body)
        (sol_dir / "solve.sh").chmod(0o755)

    return True, ""


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=_REPO_ROOT / "data" / "harbor" / "wildclawbench",
        help="WildClawBench source repo root",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_REPO_ROOT / "data" / "harbor" / "wildclawbench-converted",
        help="Output directory for harbor-format tasks",
    )
    parser.add_argument(
        "--workspace-dir",
        type=Path,
        default=None,
        help="Path to WildClawBench `workspace/` (HF-downloaded fixtures). "
        "If absent, tasks needing fixtures are skipped.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output dir if it already exists.",
    )
    parser.add_argument(
        "--allow-missing-fixtures",
        action="store_true",
        help="Emit task dirs even if their workspace fixtures are absent. "
        "The image will build but `grade()` will return 0 at runtime.",
    )
    args = parser.parse_args(argv)

    src = args.src.resolve()
    out = args.out.resolve()
    tasks_dir = src / "tasks"
    skills_dir = src / "skills"
    if not tasks_dir.is_dir():
        raise SystemExit(f"missing {tasks_dir}")

    if out.exists():
        if not args.force:
            raise SystemExit(f"{out} exists; pass --force to overwrite")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    parsed: list[ParsedTask] = []
    md_files: list[Path] = []
    for cat_dir in sorted(tasks_dir.iterdir()):
        if not cat_dir.is_dir():
            continue
        for md in sorted(cat_dir.iterdir()):
            if md.suffix == ".md" and not md.name.startswith("task0_template"):
                md_files.append(md)

    print(f"[parse] {len(md_files)} task .md file(s) found")
    for md in md_files:
        t = _parse_md(md)
        if t is not None:
            parsed.append(t)
    print(f"[parse] {len(parsed)} parsed successfully")

    skipped: list[tuple[str, str, Path]] = []  # (task_id, reason, source_md)
    written = 0
    for t in parsed:
        if t.task_id in _REQUIRES_YT_DLP_OR_LARGE_BLOBS:
            skipped.append((t.task_id, "needs yt-dlp / ModelScope blob", t.source_md))
            continue
        ok, reason = _emit_task(
            t, out, args.workspace_dir, skills_dir,
            allow_missing_fixtures=args.allow_missing_fixtures,
        )
        if ok:
            written += 1
        else:
            skipped.append((t.task_id, reason, t.source_md))

    # Park failed/skipped tasks in `build_failed/` so they're auditable instead
    # of disappearing silently. We just stash the source .md and a reason note;
    # no harbor 4-files (those are what failed to produce in the first place).
    if skipped:
        failed_root = out / "build_failed"
        failed_root.mkdir(exist_ok=True)
        for tid, reason, src_md in skipped:
            d = failed_root / tid
            d.mkdir(exist_ok=True)
            shutil.copy2(src_md, d / "source.md")
            (d / "_FAILED_REASON.md").write_text(
                f"# {tid}\n\nReason: {reason}\n\n"
                f"Original source: `{_safe_rel(src_md)}`\n"
            )
        # Index file for quick scanning.
        index_lines = ["# WildClawBench tasks not converted to harbor format", ""]
        for tid, reason, _ in sorted(skipped):
            index_lines.append(f"- **{tid}** — {reason}")
        (failed_root / "INDEX.md").write_text("\n".join(index_lines) + "\n")

    print(f"[done] wrote {written} harbor task(s) to {out}")
    if skipped:
        print(f"[skip] {len(skipped)} task(s) parked under {out / 'build_failed'}:")
        for tid, reason, _ in skipped:
            print(f"  - {tid}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
