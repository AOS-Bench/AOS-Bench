#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_6_calendar_scheduling.
#
# The grader expects under /workspace/tmp/results/:
#   scheduled.ics      — gt's optimal_scheduled.ics (verbatim)
#   unscheduled.json   — gt's optimal_unscheduled.json
#   decision_log.md    — gt's optimal_decision_log.md
# The grader independently re-derives the optimal solution from the same
# inputs, so a verbatim copy of gt's optimal_* files satisfies every hard
# constraint and yields optimality_ratio == 1.0.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
GT="${AOS_GT_DIR:-$_SCRIPT_DIR/.gt}"

WS=/workspace/tmp
RES="$WS/results"
mkdir -p "$RES"

GT="$GT" python3 - <<'PY'
import os
import shutil
import sys
from pathlib import Path

GT = Path(os.environ["GT"])
RES = Path("/workspace/tmp/results")

mapping = {
    "optimal_scheduled.ics":   "scheduled.ics",
    "optimal_unscheduled.json": "unscheduled.json",
    "optimal_decision_log.md":  "decision_log.md",
}

for src_name, dst_name in mapping.items():
    src = GT / src_name
    if not src.is_file():
        print(f"[oracle] WARN missing gt file: {src}", file=sys.stderr)
        continue
    shutil.copyfile(src, RES / dst_name)
    print(f"[oracle] copied {src_name} -> results/{dst_name}")
PY

echo "[oracle] 01_task_6_calendar_scheduling — copied optimal_* gt files into results/"
