#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_9_artwork_search.
#
# Rubric (LLM judge): score 1 iff predicted location matches GT
#   "Museum of Art Pudong, Shanghai（浦东美术馆）"
# Anything else → 0.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# Where to view this painting (mid-July 2025)

Based on visual analysis of the supplied painting and a search of
contemporaneous (mid-2025) exhibition listings in major museums, the
painting is currently being shown at:

## Final answer

**Museum of Art Pudong, Shanghai（浦东美术馆）**

- Location: 浦东美术馆 (Museum of Art Pudong, MAP), 1 Binjiang Avenue,
  Pudong New Area, Shanghai, China
- Status as of mid-July 2025: on display in a special exhibition at the
  Museum of Art Pudong (浦东美术馆)
EOF

echo "[oracle] 04_task_9_artwork_search — answer: Museum of Art Pudong, Shanghai (浦东美术馆)"
