#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_7_openmmlab_contributors.
#
# The grader expects:
#   /workspace/tmp/results/repos.txt          — one repo name per line
#   /workspace/tmp/results/contributors.jsonl — one JSON object per line
# It compares against expected_repos.txt + expected_contributors.jsonl in
# the gt dir. Verbatim copy of gt files maximises every metric (repo
# exact match, schema, full match, exact match).
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
    "expected_repos.txt":        "repos.txt",
    "expected_contributors.jsonl": "contributors.jsonl",
}
for src_name, dst_name in mapping.items():
    src = GT / src_name
    if not src.is_file():
        print(f"[oracle] WARN missing gt file: {src}", file=sys.stderr)
        continue
    shutil.copyfile(src, RES / dst_name)
    print(f"[oracle] copied {src_name} -> results/{dst_name}")
PY

echo "[oracle] 01_task_7_openmmlab_contributors — copied repos.txt + contributors.jsonl from gt"
