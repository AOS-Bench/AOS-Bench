#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_3_bibtex.
#
# Reads ground truth from $AOS_GT_DIR (set by the stream orchestrator to
# /solution/tasks/<id>/.gt — gt is shipped INSIDE solution/, NOT tests/, so
# the oracle phase doesn't need /tests/. Falls back to script-local .gt for
# direct invocation outside the orchestrator.
#
# Hard caveat: the grader hashes /workspace/tmp/*.pdf inputs and verifies
# they match the SHAs recorded in gt_manifest.json. On a host where the
# input PDFs are still git-LFS POINTER files (not pulled), every SHA will
# mismatch and grader returns overall_score=0 even though this solver is
# logically correct. To actually score on this task, `git lfs pull` the
# wildclaw fixtures first.
set -euo pipefail

# Resolve gt dir: prefer orchestrator-supplied AOS_GT_DIR, fall back to
# the .gt sibling of this script (works for direct `bash solve.sh` too).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
GT="${AOS_GT_DIR:-$_SCRIPT_DIR/.gt}"

WS=/workspace/tmp
RES=$WS/results
mkdir -p "$RES/renamed_papers" "$RES/bibtex"

GT="$GT" python3 - <<'PY'
import hashlib
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path

WS = Path("/workspace/tmp")
GT = Path(os.environ["GT"])
RES = WS / "results"

def sanitize_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    title = re.sub(r'[\/\\:*?"<>|]', "-", title)
    title = re.sub(r"\s+", " ", title).strip()
    title = title.rstrip(".").strip()
    return f"{title}.pdf"

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

gt = json.loads((GT / "gt_manifest.json").read_text(encoding="utf-8"))

input_pdfs = [p for p in WS.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
sha_to_path = {sha256_file(p): p for p in input_pdfs}

manifest_out = []
for entry in gt:
    arxiv_id = entry["arxiv_id"]
    title = entry["title"]
    figure_count = entry["figure_count"]
    sanitized = entry.get("sanitized_filename") or sanitize_title(title)

    input_path = sha_to_path.get(entry.get("input_sha256"))
    if input_path is None:
        cand = entry.get("original_input_filename")
        if cand and (WS / cand).exists():
            input_path = WS / cand
    if input_path is None:
        continue

    dst = RES / "renamed_papers" / sanitized
    shutil.copyfile(input_path, dst)

    src_bib = GT / f"{arxiv_id}.bib"
    if src_bib.is_file():
        shutil.copyfile(src_bib, RES / "bibtex" / f"{arxiv_id}.bib")

    manifest_out.append({
        "original_filename": input_path.name,
        "arxiv_id": arxiv_id,
        "title": title,
        "renamed_filename": sanitized,
        "figure_count": figure_count,
    })

(RES / "paper_manifest.json").write_text(json.dumps(manifest_out, indent=2, ensure_ascii=False))
print(f"[oracle] wrote {len(manifest_out)} manifest entries from gt at {GT}")
PY

echo "[oracle] 01_task_3_bibtex — wrote paper_manifest.json + renamed_papers/ + bibtex/"
echo "[oracle]   note: byte-identical hard-constraint requires real PDF inputs;"
echo "[oracle]         on hosts without 'git lfs pull' the grader will return 0."
