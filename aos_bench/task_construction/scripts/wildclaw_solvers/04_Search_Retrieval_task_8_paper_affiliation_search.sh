#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_8_paper_affiliation_search.
#
# Rubric (LLM judge): score 1 iff BOTH counts and ALL paper titles exactly
# match GT. Any error → 0.
# GT counts: SJTU=4, FDU=0
# GT SJTU paper list:
#   - FixTalk: Taming Identity Leakage for High-Quality Talking Head Generation in Extreme Cases
#   - Learning Streaming Video Representation via Multitask Training
#   - Knowledge Distillation for Learned Image Compression
#   - ROAR: Reducing Inversion Error in Generative Image Watermarking
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# ICCV 2025 Oral Papers — SJTU and FDU first-affiliation counts

After collecting the ICCV 2025 Oral track paper list and inspecting the
first author's primary affiliation for each paper, the counts are:

## SJTU (Shanghai Jiao Tong University): 4

1. FixTalk: Taming Identity Leakage for High-Quality Talking Head Generation in Extreme Cases
2. Learning Streaming Video Representation via Multitask Training
3. Knowledge Distillation for Learned Image Compression
4. ROAR: Reducing Inversion Error in Generative Image Watermarking

## FDU (Fudan University): 0

No ICCV 2025 Oral paper has FDU listed as the first affiliation of its
first author.

## Summary

- SJTU: **4**
- FDU: **0**
EOF

echo "[oracle] 04_task_8_paper_affiliation_search — SJTU=4 (4 titles) / FDU=0"
