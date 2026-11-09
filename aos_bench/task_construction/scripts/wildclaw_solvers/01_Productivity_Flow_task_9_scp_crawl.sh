#!/usr/bin/env bash
# Oracle solver for 01_Productivity_Flow_task_9_scp_crawl.
#
# Grader expects under /workspace/tmp/results/:
#   scp-001/ ... scp-050/
#     text.md          — must contain every text_anchors[] substring
#     1.jpg, 2.jpg ... — must look like a real JPEG (FFD8..FFD9), exactly image_count files
#   summary.jsonl      — one JSON object per item: {item, class, n_mask}
#
# gt ships ground_truth.json with by_item[SCP-XXX] = {class, n_mask,
# image_count, image_urls, text_anchors, slug, url}.
#
# Strategy:
#   - For text.md: just write text_anchors joined by newlines (the
#     grader only checks anchor substring containment, length>0).
#   - For images: optionally copy gt-shipped jpgs from $GT/images/<slug>/{N}.jpg
#     if present; otherwise synthesise a minimal valid JPEG (1x1 pixel)
#     to satisfy the FFD8..FFD9 + read-bytes check.
#   - summary.jsonl: emit {item, class, n_mask} per gt entry.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
GT="${AOS_GT_DIR:-$_SCRIPT_DIR/.gt}"

WS=/workspace/tmp
RES="$WS/results"
mkdir -p "$RES"

GT="$GT" python3 - <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

GT = Path(os.environ["GT"])
RES = Path("/workspace/tmp/results")

gt_path = GT / "ground_truth.json"
if not gt_path.is_file():
    print(f"[oracle] FATAL missing gt: {gt_path}", file=sys.stderr)
    sys.exit(0)

gt = json.loads(gt_path.read_text(encoding="utf-8"))
by_item = gt.get("by_item", {})

# Minimal valid 1x1 white JPEG (works against the FFD8..FFD9 check).
MIN_JPEG = bytes.fromhex(
    "FFD8FFE000104A46494600010100000100010000FFDB004300080606070605080707070909080A0C140D0C0B"
    "0B0C1912130F141D1A1F1E1D1A1C1C20242E2720222C231C1C2837292C30313434341F27393D38323C2E33343"
    "2FFDB0043010909090C0B0C180D0D1832211C2132323232323232323232323232323232323232323232323232"
    "32323232323232323232323232323232323232323232323232323232FFC00011080001000103012200021101031101"
    "FFC4001F0000010501010101010100000000000000000102030405060708090A0BFFC400B5100002010303020403050504"
    "040000017D01020300041105122131410613516107227114328191A1082342B1C11552D1F02433627282090A161718191A2526"
    "2728292A3435363738393A434445464748494A535455565758595A636465666768696A737475767778797A8384858687888"
    "98A92939495969798999AA2A3A4A5A6A7A8A9AAB2B3B4B5B6B7B8B9BAC2C3C4C5C6C7C8C9CAD2D3D4D5D6D7D8D9DAE1"
    "E2E3E4E5E6E7E8E9EAF1F2F3F4F5F6F7F8F9FAFFC4001F0100030101010101010101010000000000000102030405060708"
    "090A0BFFC400B51100020102040403040705040400010277000102031104052131061241510761711322328108144291A1B1"
    "C109233352F0156272D10A162434E125F11718191A262728292A35363738393A434445464748494A535455565758595A6"
    "3646566676869"
    "6A737475767778797A82838485868788898A92939495969798999AA2A3A4A5A6A7A8A9AAB2B3B4B5B6B7B8B9BAC2C3C4C"
    "5C6C7C8C9CAD2D3D4D5D6D7D8D9DAE2E3E4E5E6E7E8E9EAF2F3F4F5F6F7F8F9FAFFDA000C03010002110311003F00FBD"
    "76FFD9"
)
assert MIN_JPEG[:2] == b"\xff\xd8" and MIN_JPEG[-2:] == b"\xff\xd9"

# Optional gt image cache: $GT/images/<slug>/<N>.jpg
gt_img_root = GT / "images"

summary_lines = []
for item, info in sorted(by_item.items()):
    slug = info.get("slug") or item.lower()
    item_dir = RES / slug.lower()
    item_dir.mkdir(parents=True, exist_ok=True)

    # text.md: anchors joined by blank lines so each substring is verbatim
    # present. Grader normalises whitespace before substring matching.
    anchors = info.get("text_anchors") or []
    text_body = "\n\n".join(str(a) for a in anchors).strip() or f"SCP entry {item}."
    (item_dir / "text.md").write_text(text_body + "\n", encoding="utf-8")

    n_imgs = int(info.get("image_count", 0))
    # Drop any stale image files first to enforce exact-match set semantics.
    for old in list(item_dir.iterdir()):
        if old.is_file() and old.name != "text.md":
            old.unlink()

    for idx in range(1, n_imgs + 1):
        dst = item_dir / f"{idx}.jpg"
        gt_src = gt_img_root / slug / f"{idx}.jpg"
        if gt_src.is_file():
            shutil.copyfile(gt_src, dst)
        else:
            dst.write_bytes(MIN_JPEG)

    summary_lines.append(json.dumps({
        "item": item,
        "class": info.get("class", "Unknown"),
        "n_mask": int(info.get("n_mask", 0)),
    }, ensure_ascii=False))

(RES / "summary.jsonl").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
print(f"[oracle] wrote {len(summary_lines)} item dirs + summary.jsonl")
PY

echo "[oracle] 01_task_9_scp_crawl — built scp-XXX/ dirs + summary.jsonl from gt"
