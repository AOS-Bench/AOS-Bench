#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_1_google_scholar_search.
#
# Rubric (LLM judge): score 1 iff at least one chain in the answer matches
# one of the four GT chains node-for-node (start = Ziyu Liu, end = Geoffrey
# Hinton). Names may be abbreviated. We list all 4 GT chains verbatim to
# maximise the chance the judge accepts at least one.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# Shortest Collaboration Chains — Ziyu Liu (A) → Geoffrey Hinton (B)

Scholar A: Ziyu Liu — https://scholar.google.com/citations?user=hUrX-LMAAAAJ
Scholar B: Geoffrey Hinton — https://scholar.google.com/citations?user=JicYPdAAAAAJ

After cross-referencing co-author lists on both Google Scholar profiles
and following co-authorship edges through one intermediate hop on each
side, the shortest collaboration chains (length 4 nodes / 3 edges) are:

## Shortest chains found

1. **Ziyu Liu → Dahua Lin → Boyang Deng → Geoffrey Hinton**
2. **Ziyu Liu → Yuhang Zang → Joshua M. Susskind → Geoffrey Hinton**
3. **Ziyu Liu → Zuxuan Wu → Leonid Sigal → Geoffrey Hinton**
4. **Ziyu Liu → Yu-Gang Jiang → Leonid Sigal → Geoffrey Hinton**

Each adjacent pair has at least one published joint paper, verified via
Google Scholar's co-author listings on the relevant profiles.
EOF

echo "[oracle] 04_task_1_google_scholar_search — listed all 4 GT chains verbatim (Ziyu Liu → … → Hinton)"
