#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_8_real_image_category.
#
# Inputs: /workspace/tmp/images.tar contains 100 images.
# Grader: reads gt/filename_to_class.json mapping {filename: class_dir},
# checks each image was placed under /workspace/tmp/results/{class_dir}/.
# Strategy: extract images.tar, then route each file into the class dir
# specified by the gt mapping (verbatim — even if the gt mapping is
# semantically wrong for some file, we follow it to maximise score).
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
GT="${AOS_GT_DIR:-$_SCRIPT_DIR/.gt}"

WS=/workspace/tmp
RES="$WS/results"
TAR="$WS/images.tar"
EXTRACT_DIR="$WS/.oracle_extract"

mkdir -p "$RES" "$EXTRACT_DIR"

if [ ! -f "$TAR" ]; then
    echo "[oracle] WARN: $TAR not found — cannot extract images" >&2
fi

# Extract the tarball into a scratch dir; flatten layout below.
tar -xf "$TAR" -C "$EXTRACT_DIR" 2>/dev/null || true

GT="$GT" EXTRACT_DIR="$EXTRACT_DIR" python3 - <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

GT = Path(os.environ["GT"])
EXTRACT_DIR = Path(os.environ["EXTRACT_DIR"])
RES = Path("/workspace/tmp/results")

gt_map_path = GT / "filename_to_class.json"
if not gt_map_path.is_file():
    print(f"[oracle] FATAL: missing {gt_map_path}", file=sys.stderr)
    sys.exit(0)

mapping = json.loads(gt_map_path.read_text(encoding="utf-8"))

# Build a basename -> path index of everything we extracted.
name_to_path = {}
for p in EXTRACT_DIR.rglob("*"):
    if p.is_file():
        name_to_path.setdefault(p.name, p)

# Pre-create the 5 expected class dirs.
class_dirs = sorted(set(mapping.values()))
for c in class_dirs:
    (RES / c).mkdir(parents=True, exist_ok=True)

placed = 0
missing = []
for fname, cls in mapping.items():
    src = name_to_path.get(fname)
    if src is None:
        missing.append(fname)
        continue
    shutil.copyfile(src, RES / cls / fname)
    placed += 1

print(f"[oracle] placed {placed}/{len(mapping)} files; missing={len(missing)}")
if missing[:5]:
    print(f"[oracle] sample missing: {missing[:5]}", file=sys.stderr)
PY

# Tidy scratch dir.
rm -rf "$EXTRACT_DIR"

echo "[oracle] 01_task_8_real_image_category — extracted images.tar and placed per gt mapping"
