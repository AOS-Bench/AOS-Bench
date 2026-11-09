"""Minimal Dockerfile parser for the subset of instructions we need to merge.

Supports: FROM, ENV, WORKDIR, USER, RUN, COPY, ADD. Comments and blank lines
are preserved. Backslash line-continuations are joined.

Two BuildKit-isms the parser handles explicitly (both seen in the corpus):

1. **Inline comments inside a `\\`-continued instruction** — Docker allows
   ``# comment`` lines inside a multi-line ``RUN``; they're passed to the
   shell verbatim and ignored. Our parser used to terminate the instruction
   at the first non-``\\`` line, which broke tasks like
   ``fix-visual-stability`` whose multi-line apt-get install has comments
   between package lines.

2. **Heredoc form** ``RUN <<'EOF'`` (``RUN <<EOF`` / ``<<-EOF`` etc.) — the
   instruction body extends until a sentinel line equal to ``EOF``.
   Triggered by ``organize-messy-files`` (100 arXiv URLs) and
   ``azure-bgp-oscillation-route-leak`` (Python heredoc).

We do NOT support: ARG, multi-stage builds, ONBUILD, HEALTHCHECK, ENTRYPOINT,
CMD, VOLUME, EXPOSE, SHELL, STOPSIGNAL, LABEL — they would need real semantics
inside the merged image. If we encounter them we keep them verbatim per task,
prefixed by a comment so the merge author can review.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_SUPPORTED = {"FROM", "ENV", "WORKDIR", "USER", "RUN", "COPY", "ADD"}
_PASS_THROUGH = {"LABEL", "EXPOSE", "ENTRYPOINT", "CMD", "VOLUME", "STOPSIGNAL"}

# BuildKit heredoc: `<<DELIM`, `<<-DELIM`, `<<'DELIM'`, `<<"DELIM"`. Captures the delim.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")


@dataclass
class Instruction:
    op: str  # FROM / ENV / RUN / COPY / WORKDIR / USER / ADD / LABEL / ...
    args: str  # everything after the op, with line-continuations joined
    raw: str  # original lines (for fall-through / pass-through cases)


@dataclass
class ParsedDockerfile:
    instructions: list[Instruction] = field(default_factory=list)
    leading_comments: list[str] = field(default_factory=list)

    @property
    def from_image(self) -> str | None:
        for ins in self.instructions:
            if ins.op == "FROM":
                # FROM image[:tag] [AS stage] — take the image[:tag] part
                return ins.args.split(" AS ")[0].strip()
        return None

    @property
    def workdir(self) -> str | None:
        """The LAST WORKDIR set in the file (i.e. where `tests/test.sh` would land if cwd-sensitive)."""
        last: str | None = None
        for ins in self.instructions:
            if ins.op == "WORKDIR":
                last = ins.args.strip()
        return last

    @property
    def first_workdir(self) -> str | None:
        """The FIRST WORKDIR — i.e. the source task's `home` root, used for path-rewrite anchoring."""
        for ins in self.instructions:
            if ins.op == "WORKDIR":
                return ins.args.strip()
        return None

    @property
    def is_multistage(self) -> bool:
        """True if the Dockerfile has ≥2 FROM lines (a multi-stage build)."""
        return sum(1 for ins in self.instructions if ins.op == "FROM") >= 2


def parse_dockerfile(text: str) -> ParsedDockerfile:
    lines = text.splitlines()
    parsed = ParsedDockerfile()

    i = 0
    leading = True
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if leading:
                parsed.leading_comments.append(line)
            else:
                parsed.instructions.append(
                    Instruction(op="#", args=stripped.lstrip("#").strip(), raw=line)
                )
            i += 1
            continue

        # Found first non-comment line: leading section ends.
        leading = False

        # Identify the op so we can trigger BuildKit heredoc handling on RUN.
        head = line.lstrip()
        op_tok, _, _ = head.partition(" ")
        op = op_tok.upper()

        # Collect line + continuations.
        accumulator = [line]

        # ── BuildKit heredoc form: `RUN <<DELIM ...` ─────────────────────
        # Detect a heredoc on the first line (BuildKit allows `RUN`, `COPY`,
        # `ADD` heredocs but `RUN` is the common case in our corpus). The
        # body extends until a line equal to the (unquoted) delimiter.
        if op in ("RUN", "COPY", "ADD"):
            heredoc_match = _HEREDOC_RE.search(line)
            if heredoc_match:
                delim = heredoc_match.group(1)
                # Consume everything up to and including the closing delimiter line.
                while i + 1 < len(lines):
                    i += 1
                    accumulator.append(lines[i])
                    # Closing line: literal delimiter (BuildKit allows
                    # leading whitespace only when `<<-DELIM` was used; we
                    # match either to be permissive).
                    if lines[i].strip() == delim:
                        break
                # `accumulator` now spans the whole heredoc; fall through to
                # the standard arg-extraction below.
            else:
                # Standard `\`-continuation joining, but tolerate inline
                # `# comment` lines inside the continuation (Docker allows
                # these — the shell ignores them, but our old logic
                # terminated at the first non-`\` line).
                while accumulator[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                    i += 1
                    accumulator.append(lines[i])
                    # If the just-appended line is a pure comment (after
                    # leading whitespace), the `\`-continuation chain logically
                    # extends across it: synthesize a `\` ending so the loop
                    # condition keeps reading the next line.
                    nxt = lines[i].lstrip()
                    if nxt.startswith("#"):
                        # Pretend this comment line ended with `\` so the
                        # outer loop keeps consuming continuation lines.
                        accumulator[-1] = lines[i].rstrip() + " \\"
        else:
            # Non-RUN ops keep the original strict `\`-continuation logic
            # (no inline comments expected in FROM/ENV/etc).
            while accumulator[-1].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                accumulator.append(lines[i])

        joined = "\n".join(accumulator)
        # The op is the first whitespace-separated token of the first line.
        head = accumulator[0].lstrip()
        op_tok, _, rest = head.partition(" ")
        op = op_tok.upper()

        # Re-assemble args: everything after op on the first line + the rest of the continuation lines.
        first_line_rest = rest
        rest_lines = accumulator[1:]
        if rest_lines:
            args = first_line_rest + "\n" + "\n".join(rest_lines)
        else:
            args = first_line_rest

        parsed.instructions.append(Instruction(op=op, args=args, raw=joined))
        i += 1

    return parsed


def split_apt_install(run_args: str) -> tuple[list[str], str | None]:
    """If the RUN line is a single `apt-get install` (with optional update), pull the package list out.

    Returns (packages, leftover_run) where leftover_run is None if the RUN line
    is purely an apt install (and an optional update / clean), otherwise the
    original run_args unchanged.
    """
    # Strip inline `# comment` lines BEFORE flattening: the parser preserves
    # them verbatim inside the args (so `# Chromium dependencies` ends up
    # between package names), but here we want to recover the actual package
    # token list. Without this, words like `Chromium`, `Playwright`, `(needed`,
    # `deps)` end up promoted to "apt packages" and break `apt-get install`.
    cleaned_lines: list[str] = []
    for ln in run_args.splitlines():
        # Drop pure-comment lines (after leading whitespace + optional `\`).
        # We strip a trailing `\` first so a comment-line synthesized by the
        # parser's continuation-fixup is recognized.
        s = ln.rstrip()
        if s.endswith("\\"):
            s = s[:-1].rstrip()
        if s.lstrip().startswith("#"):
            continue
        cleaned_lines.append(ln)
    flat = "\n".join(cleaned_lines).replace("\\\n", " ")
    flat = " ".join(flat.split())  # collapse whitespace

    # Crude pattern: starts with `apt-get update [&&]` and contains
    # `apt-get install -y <pkgs>` and ends with optional cleanup.
    has_update = "apt-get update" in flat
    install_idx = flat.find("apt-get install")
    if install_idx < 0:
        return ([], run_args)

    after_install = flat[install_idx + len("apt-get install") :].lstrip()
    # Skip flags like `-y`, `--no-install-recommends`.
    tokens = shlex.split(after_install, posix=True)
    pkgs: list[str] = []
    other_after_install = ""
    for j, tok in enumerate(tokens):
        if tok.startswith("-"):
            continue
        if tok in {"&&", ";", "||"}:
            other_after_install = " ".join(tokens[j:])
            break
        # Stop on tokens that don't look like Debian package names or version-pinned packages
        if "/" in tok or tok.startswith("$") or "=" in tok and tok.split("=")[0].strip() == "":
            other_after_install = " ".join(tokens[j:])
            break
        pkgs.append(tok)

    # If anything after the package list is non-trivial (other than apt cleanup), refuse to split.
    cleanup_only = other_after_install.replace("&&", " ").replace(";", " ").strip()
    cleanup_tokens = cleanup_only.split()
    is_cleanup = all(
        t in {"rm", "-rf", "/var/lib/apt/lists/*", "/var/cache/apt/archives/*", "apt-get", "clean", "autoremove", "-y"}
        for t in cleanup_tokens
    )
    before_install = flat[:install_idx].strip().rstrip("&&").strip()
    update_only = before_install in {"", "apt-get update"}
    if not (has_update or before_install == "") or not is_cleanup or not update_only:
        return ([], run_args)

    return (pkgs, None)


def to_text(parsed: ParsedDockerfile) -> str:
    out: list[str] = []
    out.extend(parsed.leading_comments)
    for ins in parsed.instructions:
        if ins.op == "#":
            out.append(ins.raw)
        elif ins.op in _SUPPORTED or ins.op in _PASS_THROUGH:
            out.append(ins.raw)
        else:
            out.append(f"# [aos: unsupported instruction `{ins.op}`, kept verbatim]")
            out.append(ins.raw)
    return "\n".join(out) + "\n"


def load_dockerfile(path: Path) -> ParsedDockerfile:
    return parse_dockerfile(path.read_text())
