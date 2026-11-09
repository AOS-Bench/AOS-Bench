#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_4_efficient_search.
#
# Rubric (LLM judge):
#   - Wrong final answer        → 0
#   - Right final answer (3.12 / PR #92517) → 0.2 base, then:
#       2 searches → +0.8 (total 1.0)
#       3 searches → +0.4 (total 0.6)
#       4 searches → +0.0 (total 0.2)
# We claim exactly 2 searches and follow the prescribed output format.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
Total number of searches: 2

Search 1: "pathlib.Path.walk added Python version site:docs.python.org"
Finding: The official pathlib documentation marks `Path.walk()` as "Added in version 3.12", confirming Python 3.12 as the version of introduction.

Search 2: "cpython pathlib Path.walk pull request site:github.com/python/cpython"
Finding: GitHub returns CPython PR #92517 ("gh-90385: Add pathlib.Path.walk()" by Stanley) as the merged PR that added the method.

Final Answer:

First introduced in version: Python 3.12

Corresponding PR: #92517

Evidence Chain:

Evidence 1: Python 3 docs — `pathlib — Object-oriented filesystem paths` — the `Path.walk()` entry carries the "Added in version 3.12" annotation. URL: https://docs.python.org/3/library/pathlib.html#pathlib.Path.walk

Evidence 2: CPython PR #92517 on GitHub — "gh-90385: Add pathlib.Path.walk()" merged into the main branch and shipped in CPython 3.12. URL: https://github.com/python/cpython/pull/92517

Reasoning:
The official Python documentation is canonical for "in which version was X added" — it explicitly tags `Path.walk()` as added in 3.12. Cross-referencing CPython's GitHub for the implementing PR yields PR #92517 (linked from issue gh-90385), which was merged into the main branch and shipped with the 3.12 release. Together these two pieces — the docs annotation and the merged PR — form a complete and consistent evidence chain that uniquely identifies the version (3.12) and the PR number (#92517), with no further searches required.
EOF

echo "[oracle] 04_task_4_efficient_search — answer: Python 3.12 / PR #92517 in 2 searches → expected 1.0"
