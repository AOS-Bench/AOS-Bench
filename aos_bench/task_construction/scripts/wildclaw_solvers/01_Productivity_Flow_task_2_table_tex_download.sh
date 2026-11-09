#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_2_table_tex_download.
#
# The grader compares pred files /workspace/tmp/results/{N}.tex against
# gt files {N}.tex (sorted by integer stem). Strategy: copy every {N}.tex
# from the gt dir verbatim into /workspace/tmp/results/.
#
# Hard caveat: gt is shipped via $AOS_GT_DIR (orchestrator) or the .gt
# sibling of this script. If LFS pointer files weren't pulled, the .tex
# files will be tiny pointer blobs and the grader returns 0 — solver
# logic is still correct.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
GT="${AOS_GT_DIR:-$_SCRIPT_DIR/.gt}"

WS=/workspace/tmp
RES="$WS/results"
mkdir -p "$RES"

GT="$GT" python3 - <<'PY'
import os
import re
import shutil
from pathlib import Path

GT = Path(os.environ["GT"])
RES = Path("/workspace/tmp/results")

n = 0
for src in sorted(
    [p for p in GT.glob("*.tex") if p.stem.isdigit()],
    key=lambda p: int(p.stem),
):
    dst = RES / src.name
    shutil.copyfile(src, dst)
    n += 1
print(f"[oracle] copied {n} .tex files from {GT} to {RES}")
PY

echo "[oracle] 01_task_2_table_tex_download — wrote N.tex files into results/"
