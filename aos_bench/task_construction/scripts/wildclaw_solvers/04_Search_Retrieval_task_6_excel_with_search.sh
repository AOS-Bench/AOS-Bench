#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_6_excel_with_search.
#
# Rubric (LLM judge): score 1 iff BOTH "Target airport" and "Final answer"
# match GT. 0.5 if only one matches. 0 otherwise.
# GT: Target airport: Jack McNamara Field (LocID: CEC) / Final answer: 1783
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# NPIAS 2023–2027 — California Regional/CS Airport Analysis

## Methodology
1. Opened `NPIAS-2023-2027-Appendix-A.xlsx`, sheet **CA**.
2. Filtered rows where `Role (FY23) == "Regional"` AND `Svc Lvl (FY23) == "CS"`.
3. Among those rows, picked the airport with the maximum `Enplaned (CY21)`.
4. Looked up the FAA primary-airport threshold (10,000 enplanements per year)
   and subtracted the CY 2022 enplanement count from 10,000 to get the gap.

## Result

Target airport: Jack McNamara Field (LocID: CEC)

Final answer: 1783
EOF

echo "[oracle] 04_task_6_excel_with_search — answer: Jack McNamara Field (CEC) / 1783"
