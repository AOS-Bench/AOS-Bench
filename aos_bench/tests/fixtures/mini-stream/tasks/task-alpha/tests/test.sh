#!/bin/bash
set -u
# Verifier runs with cwd=<workspace>, AOS_VERIFY_OUT pointing at a tmp dir.

expected="hello alpha"
target="alpha.txt"

if [ -f "$target" ] && [ "$(cat "$target")" = "$expected" ]; then
  echo 1 > "$AOS_VERIFY_OUT/reward.txt"
  echo "[task-alpha] PASS: $target matches"
  exit 0
else
  echo 0 > "$AOS_VERIFY_OUT/reward.txt"
  echo "[task-alpha] FAIL: $target missing or wrong content"
  ls -la "$(pwd)"
  [ -f "$target" ] && cat "$target"
  exit 1
fi
