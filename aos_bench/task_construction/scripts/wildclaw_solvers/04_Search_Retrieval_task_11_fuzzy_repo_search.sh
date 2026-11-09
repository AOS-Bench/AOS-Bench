#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_11_fuzzy_repo_search.
#
# Rubric (LLM judge, score 0/1): identify llama.cpp by Georgi Gerganov
# (ggerganov). Wrong answers (Ollama, llamafile, LocalAI, ...) score 0.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# Open-source LLM-on-laptop project — answer

Based on the clues (C/C++ implementation, animal-of-South-America repo name,
same author behind a lightweight speech-recognition inference tool, custom
quantization format widely adopted, >60k GitHub stars in 2023–2024), the
project is unambiguously:

## Final answer

**llama.cpp** — https://github.com/ggerganov/llama.cpp

- Author / original creator: **Georgi Gerganov** (GitHub: `ggerganov`)
- Language: pure C/C++ with minimal dependencies
- Repository name references the *llama* (a camelid native to South America)
- Same author wrote `whisper.cpp`, the lightweight C/C++ port of OpenAI
  Whisper for speech recognition
- Pioneered the **GGML / GGUF** quantization file formats, which became the
  community-standard local-inference format
- Stars well above 60k by 2024

This matches every constraint in the question.
EOF

echo "[oracle] 04_task_11_fuzzy_repo_search — answer: llama.cpp by ggerganov / Georgi Gerganov"
