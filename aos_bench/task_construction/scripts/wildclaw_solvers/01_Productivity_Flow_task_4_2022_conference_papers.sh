#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_4_2022_conference_papers.
#
# The grader expects:
#   /workspace/tmp/results/2022.tsv          — copy of gt.tsv
#   /workspace/tmp/results/{title}_v{N}.tex  — one per gt tex file
# The 'output_dir_clean' check penalises any extra file in results/, so we
# copy exactly the allowed names: 2022.tsv plus every *.tex from gt.
#
# Hard caveat: gt is shipped via $AOS_GT_DIR (orchestrator) or the .gt
# sibling of this script. Without git lfs pull, .tex files may be LFS
# pointer blobs and tex_exact_match_ratio will be 0.
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

# 1) The TSV — gt ships a complete, sorted, header-correct copy.
src_tsv = GT / "gt.tsv"
if src_tsv.is_file():
    shutil.copyfile(src_tsv, RES / "2022.tsv")

# 2) Every .tex from gt verbatim. Filenames already encode {title}_v{N}.tex.
n_tex = 0
for tex in GT.glob("*.tex"):
    shutil.copyfile(tex, RES / tex.name)
    n_tex += 1
print(f"[oracle] copied 2022.tsv + {n_tex} .tex files from {GT}")
PY

echo "[oracle] 01_task_4_2022_conference_papers — wrote 2022.tsv + per-version .tex files"
