"""Path rewriting for task text and scripts.

The source task's instruction frequently mentions absolute paths like
`/app/foo.py` (TB-2 default WORKDIR is /app). In merged streams, task-local
paths are moved into the shared `/workspace` root, so `/app/foo.py` becomes
`/workspace/foo.py`. If COPY/ADD destinations collide, later tasks get an
explicit renamed target and every reference to that source path follows it.

This is intentionally conservative: only known, dockerfile-derived prefixes
get touched. Anything else stays untouched (including paths under /etc, /usr,
or pure code references like `import foo` that don't resolve to the
filesystem).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .dockerfile_parse import ParsedDockerfile
from .path_namespace import TaskSlot


@dataclass
class PathRewriteRule:
    """Replace any occurrence of `from_prefix` (as a path root) with `to_prefix`."""

    from_prefix: str
    to_prefix: str

    def __post_init__(self) -> None:
        # Normalize: no trailing slash on prefixes.
        self.from_prefix = self.from_prefix.rstrip("/")
        self.to_prefix = self.to_prefix.rstrip("/")


# Match a path that ends at a "path-end" boundary (EOL, slash, whitespace,
# common punctuation). We deliberately do NOT use a path-start boundary
# because legitimate flag-stuck-path forms like `+L/app/foo` and `-I/app/inc`
# preceded by alphanumerics are correct paths to rewrite — same boundary
# class as paths embedded in `/usr/share/novnc/app/...` which would be a
# false positive. The latter case is rare; we filter such source tasks at
# pool-discovery time instead (see `_has_unbuildable_pattern`).
_PATH_BOUNDARY = r"(?=$|[/\s,;`'\"\)\]\}>])"
# Relative collision rules are riskier than absolute path rules because words
# like `data` can also be ordinary code identifiers. The negative lookbehind
# in the relative rewriter already screens out matches butted up against an
# alphanumeric token; on top of that we require the match to be followed by a
# real "path or arg ends here" terminator. End-of-line counts (we apply the
# regex with re.MULTILINE), as do shell argument separators (`\s`, `|`),
# command/option terminators (`;`, `,`, `)`, `]`, `}`, `>`), and the `:` used
# by tools like EMBOSS to name a record inside a fasta file.
_RELATIVE_PATH_BOUNDARY = r"(?=$|[/\s,;:|`'\"\)\]\}>])"


def _normalize_prefix(prefix: str) -> str | None:
    if not prefix:
        return None
    while prefix.startswith("./"):
        prefix = prefix[2:]
    prefix = prefix.rstrip("/")
    if not prefix.startswith("/"):
        return None
    if prefix == "/":
        return None
    return prefix


def _relative_collision_prefix(src_prefix: str, source_workdir: str) -> str | None:
    """Return the relative form of a renamed task-local path, if it has one."""
    source_workdir = source_workdir.rstrip("/") or "/"
    home_root = _home_root(source_workdir)
    src_prefix = src_prefix.rstrip("/")

    for root in (source_workdir, home_root):
        if not _is_under(src_prefix, root):
            continue
        tail = src_prefix[len(root) :].lstrip("/")
        if tail:
            return tail
    return None


def _is_under(path: str, root: str) -> bool:
    """True if `path` equals `root` or sits beneath it (treating both as canonical, no trailing slash)."""
    return path == root or path.startswith(root + "/")


def _home_root(workdir: str) -> str:
    """Top-level directory of an absolute path — '/app/john/src' → '/app'.

    The task's *home root* is the topmost directory it owns. Source tasks may
    start with `WORKDIR /app/john/src` (already inside a subdir), but `/app`
    is what setup.sh, tests/test_outputs.py and other helpers reference. We
    map the WHOLE home subtree (including ancestors of the first WORKDIR like
    `/app` and intermediate dirs like `/app/john`) into the per-task slot so
    the inner directory structure is preserved.
    """
    parts = workdir.lstrip("/").split("/")
    if not parts or not parts[0]:
        return "/"
    return "/" + parts[0]


def _match_shared(prefix: str, source_workdir: str, shared: list[str]) -> str | None:
    """If `prefix` falls under any shared root, return the corresponding target /workspace path.

    Shared roots are evaluated in two flavors:
      - absolute (e.g. "/app/data") — matches a literal prefix
      - relative (e.g. "data") — interpreted relative to source_workdir
    Returns the rewritten /workspace target (with the shared tail preserved).
    """
    for raw in shared:
        norm = raw.rstrip("/")
        if not norm:
            continue
        if norm.startswith("/"):
            shared_abs = norm
        else:
            shared_abs = f"{source_workdir.rstrip('/')}/{norm}"
        if not _is_under(prefix, shared_abs):
            continue
        # Tail is the part of prefix below shared_abs.
        tail = prefix[len(shared_abs) :]
        # The target is /workspace/<shared-name>[<tail>].
        shared_name = shared_abs.rstrip("/").split("/")[-1]
        return f"/workspace/{shared_name}{tail}"
    return None


def build_rules(
    *,
    slot: TaskSlot,
    dockerfile: ParsedDockerfile,
    extra_workspace_hints: list[str] | None = None,
    shared_prefixes: list[str] | None = None,
    collision_renames: dict[str, str] | None = None,
) -> list[PathRewriteRule]:
    """Derive path-prefix rewrites for one source task.

    Three kinds of mappings can apply to a given source-side path:

    1. **Shared** — if the path falls under one of `shared_prefixes`, it maps
       to `/workspace/<shared-name>/...`, so multiple tasks merge under a
       common dir.
    2. **Collision-rename** — if a final destination collides with another
       task's COPY destination at assembly time, `collision_renames` injects
       an explicit `from_prefix` → `to_prefix` rule that wins over the
       default by being identical-length but added first.
    3. **Shared workspace (default)** — anything else under the source home
       root maps into `slot.workspace_dir` (currently `/workspace`).
    """
    shared_prefixes = list(shared_prefixes or [])
    collision_renames = dict(collision_renames or {})

    home_root = _home_root(slot.source_workdir)

    rules: list[PathRewriteRule] = []
    seen: set[str] = set()

    def add(prefix_raw: str) -> None:
        prefix = _normalize_prefix(prefix_raw)
        if prefix is None:
            return
        if prefix in seen:
            return
        seen.add(prefix)

        # 1. Explicit collision rename overrides everything else.
        if prefix in collision_renames:
            rules.append(
                PathRewriteRule(from_prefix=prefix, to_prefix=collision_renames[prefix])
            )
            return

        # 2. Shared prefix → /workspace/<shared-name>/...
        shared_target = _match_shared(prefix, slot.source_workdir, shared_prefixes)
        if shared_target is not None:
            # Don't add a rule that targets exactly the same string (would be a no-op).
            if shared_target != prefix:
                rules.append(PathRewriteRule(from_prefix=prefix, to_prefix=shared_target))
            return

        # 3. Default namespacing under slot.workspace_dir.
        if _is_under(prefix, slot.workspace_dir):
            return  # already inside the namespace, no-op
        # Anchor on the task's home root (top-level of source_workdir), NOT
        # source_workdir itself — source_workdir may be `/app/john/src` while
        # the home is `/app`, and we want `/app/...` to map preserving inner
        # structure into `<workspace_dir>/...`.
        if _is_under(prefix, home_root):
            tail = prefix[len(home_root) :]
        elif _is_under(prefix, "/workspace"):
            tail = prefix[len("/workspace") :]
        else:
            # Foreign system path (e.g. /bin, /etc, /usr, /opt) — a COPY dest
            # like `/bin/` for installing a binary system-wide. Leave it alone
            # so we don't redirect uv/etc into the per-task namespace.
            return
        target = slot.workspace_dir + tail
        if target == prefix:
            return
        rules.append(PathRewriteRule(from_prefix=prefix, to_prefix=target))

    def resolve_against_source_cwd(path_raw: str, cwd: str) -> str:
        path = path_raw.strip().rstrip("/")
        while path.startswith("./"):
            path = path[2:]
        if path in {"", "."}:
            return cwd.rstrip("/") or "/"
        if path.startswith("/"):
            return path
        return f"{cwd.rstrip('/')}/{path}"

    def update_source_cwd(workdir_raw: str, cwd: str) -> str:
        workdir = workdir_raw.strip()
        if not workdir:
            return cwd
        if workdir.startswith("/"):
            return workdir.rstrip("/") or "/"
        return f"{cwd.rstrip('/')}/{workdir}".rstrip("/") or "/"

    # Always seed with the canonical roots so absolute references like
    # `/app/setup.sh` get rewritten even when no WORKDIR/COPY mentions them.
    # `home_root` (e.g. `/app`) is the *task home*; `source_workdir` (e.g.
    # `/app/john/src`) is wherever the first WORKDIR landed — both need rules
    # so `/app/foo` AND `/app/john/src/foo` both map correctly.
    add(home_root)
    add(slot.source_workdir)
    add("/workspace")
    # And every shared prefix anchor (absolute form).
    for raw in shared_prefixes:
        if raw.startswith("/"):
            add(raw)
        else:
            add(f"{slot.source_workdir.rstrip('/')}/{raw}")
    for src_prefix in collision_renames:
        add(src_prefix)
        rel_prefix = _relative_collision_prefix(src_prefix, slot.source_workdir)
        if rel_prefix:
            rules.append(
                PathRewriteRule(
                    from_prefix=rel_prefix,
                    to_prefix=collision_renames[src_prefix],
                )
            )

    source_cwd = slot.source_workdir
    for ins in dockerfile.instructions:
        if ins.op == "WORKDIR":
            source_cwd = update_source_cwd(ins.args, source_cwd)
            add(source_cwd)
        elif ins.op in ("COPY", "ADD"):
            tokens = ins.args.replace("\\\n", " ").split()
            while tokens and tokens[0].startswith("--"):
                tokens.pop(0)
            if len(tokens) >= 2:
                dst = tokens[-1]
                add(resolve_against_source_cwd(dst, source_cwd))

    if extra_workspace_hints:
        for hint in extra_workspace_hints:
            add(hint)

    # Sort by length (longest first) so longer prefixes match before their
    # shorter parents (avoid `/app` rewriting before `/app/personal-site`).
    rules.sort(key=lambda r: len(r.from_prefix), reverse=True)
    return rules


# Paths that look task-local (under /root, /home/*, /app etc.) but are
# actually shared infrastructure that survives the merge. Rewriting them
# under /workspace/<task>/ would break tools that hard-code the canonical
# location (e.g. SkillsBench solve.sh expects skills at /root/.claude/skills/).
_PRESERVED_SUBPATHS = (
    "/.claude/",  # /root/.claude/skills/, /home/$USER/.claude/...
)


def rewrite_text(text: str, rules: list[PathRewriteRule]) -> str:
    """Apply prefix-rewrites to `text` in a single pass.

    All rules are compiled into one alternation regex with longest prefix
    first; the substitution dispatches to the matching rule's target. This
    ensures we only rewrite once per character — sequential application
    would re-match `/workspace` against the already-rewritten output of an
    earlier `/workspace/<id>` substitution.

    A path-prefix only matches at a path boundary (start of token, after
    whitespace, after backtick, etc.) so we don't rewrite something like
    `/app2`.
    """
    if not rules:
        return text
    rules_sorted = sorted(rules, key=lambda r: len(r.from_prefix), reverse=True)
    absolute_rules = [r for r in rules_sorted if r.from_prefix.startswith("/")]
    relative_rules = [r for r in rules_sorted if not r.from_prefix.startswith("/")]

    by_abs_prefix = {r.from_prefix: r.to_prefix for r in absolute_rules}
    by_rel_prefix = {r.from_prefix: r.to_prefix for r in relative_rules}

    def _sub_abs(match: re.Match[str]) -> str:
        # Look at what immediately follows the matched prefix; if it's a
        # preserved subpath (e.g. /.claude/), leave the original alone.
        rest = text[match.end():]
        for sub in _PRESERVED_SUBPATHS:
            if rest.startswith(sub):
                return match.group(0)
        return by_abs_prefix[match.group(0)]

    out = text
    if absolute_rules:
        alternation = "|".join(re.escape(r.from_prefix) for r in absolute_rules)
        pattern = re.compile(f"(?:{alternation}){_PATH_BOUNDARY}")
        out = pattern.sub(_sub_abs, out)

    if relative_rules:
        # Relative rules are only generated for collision-renamed task-local
        # paths, e.g. source `skills/...` after `/app/skills` was renamed to
        # `/workspace/skills__task`. Require a non-path character before the
        # match so we don't rewrite substrings like `pddl-skills`. MULTILINE
        # lets `$` in the boundary match end-of-line, not just end-of-string,
        # which matters for multi-line shell scripts where the path is the
        # last token on a line.
        alternation = "|".join(re.escape(r.from_prefix) for r in relative_rules)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_.~/-])(?:{alternation}){_RELATIVE_PATH_BOUNDARY}",
            re.MULTILINE,
        )

        def _sub_rel(match: re.Match[str]) -> str:
            return by_rel_prefix[match.group(0)]

        out = pattern.sub(_sub_rel, out)

    return out


def rewrite_file(
    src_file: Path, dst_file: Path, rules: list[PathRewriteRule]
) -> None:
    text = src_file.read_text()
    rewritten = rewrite_text(text, rules)
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    dst_file.write_text(rewritten)
