#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_5_wikipedia_biography.
#
# The grader expects /workspace/tmp/results/{name}.md for every gt file,
# normalises whitespace + drops [N] reference markers + leading 生平/传记
# headers, then compares verbatim. The 'no_extra_files' check penalises
# any *.md outside the expected set. Strategy: mirror gt verbatim.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
GT="${AOS_GT_DIR:-$_SCRIPT_DIR/.gt}"

WS=/workspace/tmp
RES="$WS/results"
mkdir -p "$RES"

GT="$GT" python3 - <<'PY'
import os
import shutil
from pathlib import Path

GT = Path(os.environ["GT"])
RES = Path("/workspace/tmp/results")

n = 0
for src in GT.iterdir():
    if src.is_file() and src.suffix.lower() == ".md":
        shutil.copyfile(src, RES / src.name)
        n += 1
print(f"[oracle] copied {n} biography .md files from {GT}")
PY

echo "[oracle] 01_task_5_wikipedia_biography — mirrored gt biography files into results/"
