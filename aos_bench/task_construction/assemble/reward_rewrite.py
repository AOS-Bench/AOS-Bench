"""Rewrite tests/test.sh so that:

1. Reward + ctrf paths route to `$AOS_REWARD_DIR/...` (per-task verifier
   output), with a fallback to the global `/logs/verifier/...` so the script
   still runs when invoked outside the AOS-Bench harness.
2. References to `/tests/...` (the harbor convention) are made resilient by
   computing the script's own directory at the top, so the same test.sh can
   run when invoked as `/tests/test.sh` or as `/tasks/<id>/tests/test.sh`.

The rewrite is conservative: we only touch known-fixed paths to avoid messing
up bash heredocs / regex literals.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import tokenize

from .path_namespace import TaskSlot

_REWARD_PATHS = (
    "/logs/verifier/reward.txt",
    "/logs/verifier/reward.json",
    "/logs/verifier/ctrf.json",
    "/logs/verifier/stdout.txt",
)

_HEADER_TEMPLATE = """\
# === aos-bench: stream verifier shim (added by task_construction) ===
# Make this test.sh runnable both under harbor's /tests mount and under
# AOS-Bench's per-task /tasks/<id>/tests/ mount.
__AOS_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# Prefer the env supplied by harbor's StreamVerifier (sets AOS_TESTS_DIR per
# arrival) when present — it points at the per-task upload dir even when
# this test.sh is sourced through a wrapper that loses __AOS_TESTS_DIR.
if [ -n "${AOS_TESTS_DIR:-}" ]; then
  __AOS_TESTS_DIR="$AOS_TESTS_DIR"
fi
# Tests dir basename in stream mode is the task_id, which often contains
# dashes (e.g. `llm-inference-batching-scheduler`). Python can't load such
# a path as a package, so relative imports (`from .cost_model import …`)
# in test_outputs.py fail. Create an underscore-only symlink alias and
# point pytest at it so the package is loadable.
__AOS_TESTS_BASE="$(basename "$__AOS_TESTS_DIR")"
__AOS_TESTS_SAFE_NAME="$(echo "$__AOS_TESTS_BASE" | tr '-' '_')"
if [ "$__AOS_TESTS_SAFE_NAME" != "$__AOS_TESTS_BASE" ]; then
  __AOS_TESTS_PARENT="$(dirname "$__AOS_TESTS_DIR")"
  __AOS_TESTS_SAFE="$__AOS_TESTS_PARENT/$__AOS_TESTS_SAFE_NAME"
  if [ ! -e "$__AOS_TESTS_SAFE" ]; then
    ln -sfn "$__AOS_TESTS_DIR" "$__AOS_TESTS_SAFE" 2>/dev/null || true
  fi
  # Point downstream commands at the safe alias so relative imports work.
  [ -e "$__AOS_TESTS_SAFE" ] && __AOS_TESTS_DIR="$__AOS_TESTS_SAFE"
fi
__AOS_REWARD_DIR="${AOS_REWARD_DIR:-/logs/verifier}"
mkdir -p "$__AOS_REWARD_DIR"
# Put the tests dir on sys.path. We run pytest with `--import-mode=importlib`
# (needed so a task_id containing dashes is still loadable), but that mode does
# NOT add the test file's own directory to sys.path the way the default
# `prepend` mode does. A grader that imports a helper shipped alongside it —
# e.g. skillsbench-seismic-phase-picking's `from evaluate_picks import ...` —
# then dies with ModuleNotFoundError during collection and scores 0 even though
# the helper is present. Prepending here keeps importlib mode and the sibling
# import working together.
PYTHONPATH="$__AOS_TESTS_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
# (Old `/tests` symlink was removed: races between concurrent test.sh in
# StreamVerifier parallel mode. We rewrite hardcoded `/tests/<file>` refs
# below to `$__AOS_TESTS_DIR/<file>` instead.)
# Stub out runtime `curl https://astral.sh/uv/...install.sh | sh` so test.sh
# scripts that bootstrap uv don't hang on flaky outbound network. uv is
# pre-installed in /usr/local/bin via the merged Dockerfile (pip install uv).
# Provide $HOME/.local/bin/{uv,uvx,env} so `source $HOME/.local/bin/env` and
# subsequent `uvx ...` calls Just Work.
if command -v uv >/dev/null 2>&1 && [ ! -e "$HOME/.local/bin/uv" ]; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$(command -v uv)" "$HOME/.local/bin/uv"
  ln -sf "$(command -v uvx)" "$HOME/.local/bin/uvx" 2>/dev/null || true
  : > "$HOME/.local/bin/env"  # empty env stub so `source` is a no-op
fi
# Replace the astral.sh install pipeline with a no-op when uv exists.
__aos_install_uv_noop() { command -v uv >/dev/null 2>&1; }
# === end shim ===

"""


def _has_shim(text: str) -> bool:
    return "aos-bench: stream verifier shim" in text


_SH_SHIM_END = "# === end shim ==="


def _rewrite_test_sh_body(text: str) -> str:
    """Apply test-path rewrites without injecting a second shim.

    Older assembled overlays already carry the verifier preamble, but can
    retain a legacy ``cd /tests`` in their body.  Re-applying only the body
    transformations upgrades those overlays while preserving the preamble.
    """
    rewritten: list[str] = []
    for line in text.splitlines(keepends=True):
        new = line
        for p in _REWARD_PATHS:
            new = new.replace(p, p.replace("/logs/verifier", '"$__AOS_REWARD_DIR"'))
        new = re.sub(
            r"(?<![\w/])/tests/([\w.\-]+)(?![/\w.])",
            r'"$__AOS_TESTS_DIR/\1"',
            new,
        )
        # `cd /tests` followed by a relative pytest target must use the
        # arrival-specific mount. `/tests` is merely the shared parent in a
        # stream, so it does not contain `test_outputs.py`.
        new = re.sub(
            r"(?<![\w/])cd\s+['\"]?/tests['\"]?\b",
            'cd "$__AOS_TESTS_DIR"',
            new,
        )
        # Archives containing verifier-only fixtures must be unpacked beside
        # the current task's uploaded tests.  Extracting into the shared
        # ``/tests`` parent makes subsequent rewritten lookups miss the file
        # (and can collide with other arrivals).
        new = re.sub(
            r"(?P<prefix>\s-C\s+)(?:['\"])?/tests(?:['\"])?(?P<suffix>\s|$)",
            r'\g<prefix>"$__AOS_TESTS_DIR"\g<suffix>',
            new,
        )
        # Do not nest the guard when upgrading an overlay that already got
        # this rewrite in a previous assembly pass.
        if "command -v uv >/dev/null 2>&1 ||" not in new:
            new = re.sub(
                r"(curl\s+[^\n|;]*astral\.sh/uv/[^\n|;]*install\.sh[^\n|;]*\|\s*sh)",
                r"command -v uv >/dev/null 2>&1 || (\1)",
                new,
            )
        if re.search(r"\bpytest\s+(?:-|\$|\")", new) and "--import-mode" not in new:
            new = re.sub(
                r"\bpytest(\s+)(?=-|\$|\")",
                r"pytest --import-mode=importlib\1",
                new,
                count=1,
            )
        rewritten.append(new)
    return "".join(rewritten)


def rewrite_test_sh(text: str, slot: TaskSlot) -> str:
    """Inject the shim and rewrite reward paths."""
    if _has_shim(text):
        # The original early return made generated overlays permanently stale:
        # they could not receive later body-only rewrites such as `cd /tests`
        # → `cd "$__AOS_TESTS_DIR"`.
        shim_end = text.find(_SH_SHIM_END)
        if shim_end < 0:
            return text
        shim_end += len(_SH_SHIM_END)
        return text[:shim_end] + _rewrite_test_sh_body(text[shim_end:])

    lines = text.splitlines()
    if not lines:
        return _HEADER_TEMPLATE

    # Find the position to insert the shim: after shebang, after `set -e`-style
    # lines and after the canary comment block.
    insert_at = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if i == 0 and s.startswith("#!"):
            insert_at = 1
            continue
        if s.startswith("#") or s == "":
            insert_at = i + 1
            continue
        if re.match(r"^\s*set\s+(-[a-zA-Z]+|-o\s+\w+)", s):
            insert_at = i + 1
            continue
        break

    head = lines[:insert_at]
    tail = lines[insert_at:]

    return (
        "\n".join(head)
        + "\n"
        + _HEADER_TEMPLATE
        + _rewrite_test_sh_body("\n".join(tail) + "\n")
    )


_AOS_TESTS_PY_HELPER = """\
# === aos-bench: stream tests-dir shim ===
# Single-task harbor mounts the task's tests/ at /tests; stream mode uploads
# each task's files to /tests/<task_id>/ and exposes that via AOS_TESTS_DIR.
# Rewrite hardcoded "/tests" and "/tests/<file>" string literals so they
# resolve in both modes.
import os as _aos_os
def __aos_tests_dir__():
    return _aos_os.environ.get('AOS_TESTS_DIR', '/tests')
def __aos_tests__(name):
    return __aos_tests_dir__() + '/' + name
# === end shim ===
"""

_PY_SHIM_END = "# === end shim ===\n"

_AOS_TESTS_JS_HELPER = """\
// === aos-bench: stream tests-dir shim ===
const __aos_tests__ = (name) => (process.env.AOS_TESTS_DIR || '/tests') + '/' + name;
// === end shim ===
"""


def _has_py_shim(text: str) -> bool:
    return "aos-bench: stream tests-dir shim" in text


def rewrite_tests_helper_py(text: str) -> str:
    """For non-test.sh Python tests files, make `/tests` lookups task-local.

    Both direct file literals (``"/tests/input.txt"``) and the tests root
    itself (``Path("/tests")`` / ``os.path.join("/tests", ...)``) occur in
    task fixtures.  StreamVerifier deliberately mounts each task at
    ``/tests/<task_id>`` and supplies that path through ``AOS_TESTS_DIR``;
    retaining a global `/tests/*` symlink is unsafe when tasks run in
    parallel.  Therefore both forms must be rewritten here.

    Existing generated overlays may contain the older shim, which only had
    ``__aos_tests__``.  Upgrade those files in place and remain byte-stable on
    a second pass.
    """
    file_pattern = re.compile(r'(["\'])/tests/([\w.\-]+)\1')
    root_pattern = re.compile(r'(["\'])/tests\1')
    if not file_pattern.search(text) and not root_pattern.search(text):
        return text

    if not _has_py_shim(text):
        shim = _AOS_TESTS_PY_HELPER
        body = text
    else:
        end = text.find(_PY_SHIM_END)
        if end < 0:
            # The marker is present but malformed.  Adding a complete helper
            # is safer than attempting to edit an unknown partial preamble.
            shim = _AOS_TESTS_PY_HELPER
            body = text
        else:
            end += len(_PY_SHIM_END)
            shim = text[:end]
            body = text[end:]
            # Upgrade the exact previous helper without touching the fallback
            # literal inside its implementation.
            old_helper = """\
import os as _aos_os
def __aos_tests__(name):
    return _aos_os.environ.get('AOS_TESTS_DIR', '/tests') + '/' + name
"""
            new_helper = """\
import os as _aos_os
def __aos_tests_dir__():
    return _aos_os.environ.get('AOS_TESTS_DIR', '/tests')
def __aos_tests__(name):
    return __aos_tests_dir__() + '/' + name
"""
            if "__aos_tests_dir__" not in shim:
                if old_helper in shim:
                    shim = shim.replace(old_helper, new_helper, 1)
                else:
                    # Keep compatibility with any hand-edited old shim:
                    # its __aos_tests__ helper still works, while the root
                    # helper is added before the end marker.
                    shim = shim.replace(
                        _PY_SHIM_END,
                        "def __aos_tests_dir__():\n"
                        "    return _aos_os.environ.get('AOS_TESTS_DIR', '/tests')\n"
                        + _PY_SHIM_END,
                        1,
                    )

    # Specific `/tests/<file>` replacements must happen first, otherwise a
    # root substitution could consume the string used by the more precise
    # pattern.  Apply substitutions to the body only so the helper's fallback
    # `'/tests'` remains intact.
    body = file_pattern.sub(r'__aos_tests__("\2")', body)
    body = root_pattern.sub("__aos_tests_dir__()", body)

    # ``from __future__`` imports must follow only the module docstring,
    # comments, and blank lines.  Older overlays prepended this helper at byte
    # zero, which made otherwise valid graders fail during collection with:
    #   SyntaxError: from __future__ imports must occur at the beginning
    # Keep the helper after the last future import whenever one is present.
    future_imports = list(
        re.finditer(r"(?m)^from __future__ import [^\n]+(?:\n|$)", body)
    )
    if future_imports:
        insert_at = future_imports[-1].end()
        return body[:insert_at] + "\n" + shim + body[insert_at:]
    return shim + body


def rewrite_wildclaw_judge_budget(text: str, slot: TaskSlot | str) -> str:
    """Give WildClaw LLM graders enough room to return complete JSON.

    Several visual graders were authored with 128/256/512 output-token
    budgets.  Reasoning-capable OpenAI-compatible models can consume most of
    that allowance—and in practice even an intermediate 1024-token repair—
    before emitting the requested JSON, leaving a truncated response that the
    unchanged grader then treats as a zero.  Raising the
    transport budget does not alter the rubric, thresholds, expected values,
    or JSON parsing semantics; it only prevents otherwise valid responses
    from being cut off by the API.

    Keep this scoped to WildClaw test Python so unrelated task resource
    limits remain byte-for-byte unchanged.
    """
    task_id = slot if isinstance(slot, str) else slot.task_id
    if not task_id.startswith("wildclawbench-"):
        return text

    # Work from Python tokens rather than a plain regular expression.  WCB
    # graders contain explanatory comments and prompt strings mentioning
    # ``max_tokens=256``; changing those would be needless source drift.  The
    # token pattern below updates only an actual assignment/default/keyword
    # argument whose name is ``max_tokens`` and whose literal is one of the
    # known truncation-prone budgets.
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (IndentationError, tokenize.TokenError):
        # Assembly should not make an already malformed source less
        # diagnosable.  Leave it unchanged and let the normal syntax gate
        # report the original problem.
        return text

    line_offsets = [0]
    for line in text.splitlines(keepends=True):
        line_offsets.append(line_offsets[-1] + len(line))

    replacements: list[tuple[int, int]] = []
    for index in range(len(tokens) - 2):
        name, equals, value = tokens[index : index + 3]
        if (
            name.type == tokenize.NAME
            and name.string == "max_tokens"
            and equals.type == tokenize.OP
            and equals.string == "="
            and value.type == tokenize.NUMBER
            and value.string in {"128", "256", "512", "1024"}
        ):
            start = line_offsets[value.start[0] - 1] + value.start[1]
            end = line_offsets[value.end[0] - 1] + value.end[1]
            replacements.append((start, end))

    for start, end in reversed(replacements):
        text = text[:start] + "4096" + text[end:]

    # WildClaw visual graders historically selected JUDGE_MODEL for requests
    # containing image_url parts.  The default text judge is intentionally
    # text-only, while the launcher supplies AOS_JUDGE_VISION_MODEL for these
    # requests.  Prefer that explicit visual model (and retain the old value as
    # a fallback) so multimodal requests do not fail with UpstreamParamError.
    text = text.replace(
        'model=os.environ.get("JUDGE_MODEL", "openai/gpt-5.4")',
        'model=os.environ.get("AOS_JUDGE_VISION_MODEL", '
        'os.environ.get("JUDGE_MODEL", "openai/gpt-5.4"))',
    )
    text = text.replace(
        "model=os.environ.get('JUDGE_MODEL', 'openai/gpt-5.4')",
        "model=os.environ.get('AOS_JUDGE_VISION_MODEL', "
        "os.environ.get('JUDGE_MODEL', 'openai/gpt-5.4'))",
    )
    return text


def rewrite_tests_helper_js(text: str) -> str:
    """For non-test.sh JS/MJS tests files: rewrite hardcoded `/tests/<file>`
    string literals so they use ``AOS_TESTS_DIR`` env when set.

    Idempotent: skips if the shim is already injected.
    """
    if "aos-bench: stream tests-dir shim" in text:
        return text
    pattern = re.compile(r'(["\'`])/tests/([\w.\-]+)\1')
    if not pattern.search(text):
        return text
    new = pattern.sub(r'__aos_tests__("\2")', text)
    return _AOS_TESTS_JS_HELPER + new


def rewrite_file(src_file: Path, dst_file: Path, slot: TaskSlot) -> None:
    text = src_file.read_text()
    rewritten = rewrite_test_sh(text, slot)
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    dst_file.write_text(rewritten)
    # Preserve executable bit if source had one.
    if src_file.stat().st_mode & 0o111:
        dst_file.chmod(dst_file.stat().st_mode | 0o755)
