"""Copy a source task's environment/ into the stream's _sources/<task_id>/,
applying path rewrites to scripts (so e.g. setup.sh's hardcoded /app/foo
references follow the same /workspace/<task_id>/foo redirection as the
merged Dockerfile and the per-task instruction).

Heuristic for "is this file a script we should rewrite?"

1. Extension match against ``_SCRIPT_EXTS`` — covers the common cases.
2. Otherwise, peek at the first two bytes; if they are ``#!`` we treat it
   as a script regardless of name (Makefiles without an extension would
   miss this — they're rare in our task inputs).
3. Everything else (binaries, data fixtures, .pdf, .qcow2, etc.) is copied
   byte-identically.

Errors reading a file are non-fatal: we fall back to byte-copy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .dockerfile_merge import _swap_url_mirrors
from .instruction_rewrite import PathRewriteRule, rewrite_text


_SCRIPT_EXTS = {
    ".sh",
    ".bash",
    ".zsh",
    ".py",
    ".pl",
    ".rb",
    ".R",
    # JavaScript family — graders sometimes ship Node helper scripts
    # (e.g. threejs-structure-parser's gen_ground_truth.mjs) with
    # hardcoded /root/... paths that must follow the same redirection
    # as the merged Dockerfile.
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".mts",
    ".cts",
}

# Common script-ish text formats where /app/... references could appear.
# We apply the same path rewrites (paths inside e.g. requirements.txt are
# typically package versions, not filesystem paths, but we play it safe by
# only matching at path boundaries — see instruction_rewrite._PATH_BOUNDARY).
_TEXT_EXTS_OPT_IN = {
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
}


def _has_shebang(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return False


def is_rewritable(p: Path) -> bool:
    if p.suffix in _SCRIPT_EXTS:
        return True
    if p.suffix in _TEXT_EXTS_OPT_IN:
        return True
    if not p.suffix and _has_shebang(p):
        return True
    return False


def copy_with_rewrite(
    src_root: Path,
    dst_root: Path,
    rules: list[PathRewriteRule],
) -> list[Path]:
    """Recursive copy from ``src_root`` to ``dst_root`` with optional rewriting.

    Returns the list of files that had their content rewritten (for logging /
    auditing).
    """
    if dst_root.exists():
        # Caller is expected to have cleaned this out; we don't shutil.rmtree
        # here to avoid surprising deletions.
        raise FileExistsError(f"{dst_root} already exists")
    rewritten: list[Path] = []
    for src in src_root.rglob("*"):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if is_rewritable(src):
            try:
                text = src.read_text()
            except (UnicodeDecodeError, OSError):
                shutil.copy2(src, dst)
                continue
            new_text = _swap_url_mirrors(rewrite_text(text, rules))
            dst.write_text(new_text)
            shutil.copystat(src, dst)
            if new_text != text:
                rewritten.append(rel)
        else:
            shutil.copy2(src, dst)
    return rewritten
