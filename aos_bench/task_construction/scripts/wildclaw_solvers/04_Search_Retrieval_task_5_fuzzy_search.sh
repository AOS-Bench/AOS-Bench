#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_5_fuzzy_search.
#
# Rubric (LLM judge): score 1 iff the identified paper matches GT
#   "Visual-RFT: Visual Reinforcement Fine-Tuning"
# (an early 2025 work transferring DeepSeek-R1-style RL to vision; first
# author surname "Liu"; >2k GitHub stars). Anything else → 0.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# 2025 paper — DeepSeek-R1-style approach for visual perception

## Final answer

**Visual-RFT: Visual Reinforcement Fine-Tuning**

- First author: **Ziyu Liu** (surname Liu, matches the constraint)
- Year: 2025 (early-2025 release, one of the earliest works to transfer
  the DeepSeek-R1 / GRPO-based RL recipe to visual perception tasks such
  as detection, grounding, and classification)
- arXiv: https://arxiv.org/abs/2503.01785
- GitHub repository: https://github.com/Liuziyu77/Visual-RFT
- Star count: well above 2k (matches the >2k stars constraint)

The paper introduces verifiable rewards (e.g. IoU-based rewards for
grounding, exact-match rewards for classification) and applies GRPO to
vision-language models, mirroring DeepSeek-R1's reinforcement-learning
recipe for reasoning. This satisfies every clue: 2025, first-author Liu,
DeepSeek-R1-style approach to visual perception, >2k stars.
EOF

echo "[oracle] 04_task_5_fuzzy_search — answer: Visual-RFT: Visual Reinforcement Fine-Tuning (Liu et al., 2025)"
