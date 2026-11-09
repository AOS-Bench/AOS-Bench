"""Cross-task COPY/ADD collision detection.

When two source tasks COPY to the same final path (after `shared_prefixes`
rewriting), the second one's destination is auto-suffixed with
``__<task_id>`` so both files coexist in the merged image. The same suffix
is then surfaced through the path-rewrite rules so any reference inside the
task's Dockerfile, instruction, or tests follows the new location.

We only detect *exact-string* collisions on COPY destinations. Directory
trees that happen to overlap at a sub-file level are NOT inspected because
that would require materializing the source tree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from .dockerfile_parse import ParsedDockerfile
from .instruction_rewrite import build_rules, rewrite_text
from .path_namespace import TaskSlot


def _suffix_preserving_ext(path: str, suffix: str) -> str:
    """Insert ``__<suffix>`` before the file extension, preserving trailing ``/``.

    Naive ``f"{path}__{suffix}"`` breaks compilers/tools that infer file type
    from extension. For instance, gcc treats ``orig.c__foo`` as a linker
    script, not C source. By inserting before the last ``.`` we keep
    ``orig.c`` → ``orig__foo.c``.

    Rules:
    - Basenames without a dot (e.g. ``skills``) get suffix appended as before.
    - Hidden files (``.env``) keep the leading-dot as filename (suffix appended at end).
    - Multi-extension files (``file.tar.gz``) only split on the LAST dot.
    - Trailing ``/`` (directory form) is preserved.
    """
    trailing_slash = path.endswith("/")
    p = path.rstrip("/")
    last_slash = p.rfind("/")
    basename = p[last_slash + 1 :] if last_slash >= 0 else p
    parent = p[: last_slash + 1] if last_slash >= 0 else ""
    last_dot = basename.rfind(".")
    if last_dot > 0:
        new_basename = f"{basename[:last_dot]}__{suffix}{basename[last_dot:]}"
    else:
        new_basename = f"{basename}__{suffix}"
    out = parent + new_basename
    if trailing_slash:
        out += "/"
    return out


@dataclass
class Collision:
    final_path: str
    first_task: str
    losing_task: str
    losing_orig_dest: str
    losing_new_dest: str


def _abs_dest(dst: str, source_cwd: str) -> str:
    cleaned = dst.rstrip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in {".", ""}:
        return source_cwd.rstrip("/") or "/"
    if cleaned.startswith("/"):
        return cleaned
    return f"{source_cwd.rstrip('/')}/{cleaned}"


def _update_source_cwd(workdir: str, source_cwd: str) -> str:
    target = workdir.strip()
    if not target:
        return source_cwd
    if target.startswith("/"):
        return target.rstrip("/") or "/"
    return f"{source_cwd.rstrip('/')}/{target}".rstrip("/") or "/"


def _home_root(workdir: str) -> str:
    parts = workdir.lstrip("/").split("/")
    if not parts or not parts[0]:
        return "/"
    return "/" + parts[0]


def detect(
    parsed_slots: list[tuple[TaskSlot, ParsedDockerfile]],
    shared_prefixes: list[str],
) -> tuple[dict[str, dict[str, str]], list[Collision]]:
    """Return (collision_renames_per_task, list-of-collision-records).

    `collision_renames_per_task[task_id]` is a dict mapping a source-side
    absolute destination path to its disambiguated target path. The merged
    Dockerfile and per-task rewrites pick that mapping up via
    ``build_rules(... collision_renames=...)``.
    """
    renames_per_task: dict[str, dict[str, str]] = {}
    collisions: list[Collision] = []
    seen: dict[str, str] = {}  # final_path → first task_id

    for slot, df in parsed_slots:
        # Build the no-collision baseline rules to compute "what would the
        # final path be without any rename?"
        baseline = build_rules(
            slot=slot, dockerfile=df, shared_prefixes=shared_prefixes
        )

        def register_dest(dst_abs: str) -> None:
            final = rewrite_text(dst_abs, baseline)
            if final in seen and seen[final] != slot.task_id:
                # The losing task suffixes its final path with __task_id.
                # We register a rename keyed on the source-side absolute dest
                # so build_rules can apply it for ALL references (Dockerfile,
                # instruction, tests) consistently.
                new_final = _suffix_preserving_ext(final, slot.task_id)
                renames_per_task.setdefault(slot.task_id, {})[dst_abs] = new_final
                collisions.append(
                    Collision(
                        final_path=final,
                        first_task=seen[final],
                        losing_task=slot.task_id,
                        losing_orig_dest=dst_abs,
                        losing_new_dest=new_final,
                    )
                )
            else:
                seen[final] = slot.task_id

        # The merger synthesizes an extra COPY for environment/skills. Treat it
        # like a task-local /app/skills directory so generated Dockerfiles obey
        # the same shared-workspace collision policy as source COPY/ADD lines.
        if (slot.source_dir / "environment" / "skills").is_dir():
            register_dest(f"{_home_root(slot.source_workdir)}/skills")

        source_cwd = slot.source_workdir
        for ins in df.instructions:
            if ins.op == "WORKDIR":
                source_cwd = _update_source_cwd(ins.args, source_cwd)
                continue
            if ins.op not in ("COPY", "ADD"):
                continue
            tokens = ins.args.replace("\\\n", " ").split()
            while tokens and tokens[0].startswith("--"):
                tokens.pop(0)
            if len(tokens) < 2:
                continue
            *srcs, dst = tokens
            dst_abs = _abs_dest(dst, source_cwd)
            # When dst is a directory-form destination (./ , `.`, ends with /,
            # or resolves to the current WORKDIR), each src lands at
            # dst/basename(src). Register the file-level path so two tasks
            # COPY-ing a same-named file into the shared workspace collide
            # exactly on that file rather than on the whole dir. For dir
            # sources (`COPY skills/ ./`) the basename is the dir name; file
            # contents still collide as the dir tree merges.
            dst_is_dir_form = (
                dst.endswith("/")
                or dst in (".", "./")
                or dst_abs == source_cwd.rstrip("/")
            )
            for s in srcs:
                if s.startswith(("/", "http://", "https://")):
                    register_dest(dst_abs)
                    continue
                src_basename = PurePosixPath(s.rstrip("/")).name
                if dst_is_dir_form and src_basename:
                    register_dest(f"{dst_abs.rstrip('/')}/{src_basename}")
                else:
                    register_dest(dst_abs)

    return renames_per_task, collisions
