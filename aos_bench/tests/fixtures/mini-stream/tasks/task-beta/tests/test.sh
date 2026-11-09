#!/bin/bash
set -u

expected="hello beta"
target="beta.txt"

if [ -f "$target" ] && [ "$(cat "$target")" = "$expected" ]; then
  echo 1 > "$AOS_VERIFY_OUT/reward.txt"
  echo "[task-beta] PASS: $target matches"
  exit 0
else
  echo 0 > "$AOS_VERIFY_OUT/reward.txt"
  echo "[task-beta] FAIL: $target missing or wrong content"
  ls -la "$(pwd)"
  [ -f "$target" ] && cat "$target"
  exit 1
fi
