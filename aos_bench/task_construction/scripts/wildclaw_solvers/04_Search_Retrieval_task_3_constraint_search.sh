#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_3_constraint_search.
#
# Rubric (from grader judge_prompt, score scale 0 / 0.5 / 1):
#   1.0  — agent identifies NO phone meets all constraints AND recommends
#          at least 2 of: Xiaomi 14 Ultra / Vivo X100 Ultra / Honor Magic 6
#          至臻版 / OPPO Find X7 Ultra
#   0.5  — agent identifies no full match BUT no near-match recommendations
#   0    — agent claims some phone matches all constraints
#
# Grader reads /workspace/tmp/results/results.md and ships it to gpt-5.4 judge.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# 手机推荐 — 调研结果

## 结论
经过逐项核对你给出的 7 个条件（中国厂商 / 骁龙 8 Gen 3 / 1 英寸主摄 /
卫星通信 / 2024 发布 / 512 GB 内存 / 5400 mAh 以上电池），**目前市面上
不存在一款手机同时满足全部 7 项要求**。最大的卡点是"骁龙 8 Gen 3 + 1
英寸主摄 + 卫星通信"这个三选项基本互斥：

- 同时具备 **1 英寸主摄 + 骁龙 8 Gen 3** 的旗舰大多没把卫星通信做进去；
- 把 **卫星通信 + 骁龙 8 Gen 3** 都做了的（如 Honor Magic 6 至臻版、
  Mate 60 系列），主摄并没有走 1 英寸 Sony IMX989 这一档；
- 国产 1 英寸主摄机型在 2024 中前期多用骁龙 8 Gen 2 或天玑，并非 8 Gen 3。

## 接近条件的几款近似机型
以下机型至少满足 5/7 条核心要求，是最合理的替代候选：

### 1. Xiaomi 14 Ultra（小米 14 Ultra）
- ✅ 中国厂商、✅ 骁龙 8 Gen 3、✅ 1 英寸主摄（Sony LYT-900）、
  ✅ 2024 年发布、✅ 512 GB 可选
- ❌ 不支持卫星通信
- ⚠️ 电池 5300 mAh（差 100 mAh）

### 2. Vivo X100 Ultra
- ✅ 中国厂商、✅ 1 英寸主摄（Sony LYT-900）、✅ 2024 年发布、
  ✅ 512 GB 可选、✅ 5500 mAh 电池
- ❌ SoC 是天玑 9300，而非骁龙 8 Gen 3
- ❌ 不支持卫星通信

### 3. Honor Magic 6 至臻版（荣耀 Magic 6 至臻版）
- ✅ 中国厂商、✅ 骁龙 8 Gen 3、✅ 卫星通信、✅ 2024 年发布、
  ✅ 512 GB 可选、✅ 5600 mAh 电池
- ❌ 主摄是 1/1.3" 传感器（H9800），不是 1 英寸

### 4. OPPO Find X7 Ultra
- ✅ 中国厂商、✅ 骁龙 8 Gen 3、✅ 1 英寸主摄（LYT-900）、
  ✅ 2024 年发布、✅ 512 GB 可选
- ❌ 不支持卫星通信（国际版没有，部分行货款也无）
- ⚠️ 电池 5000 mAh（不达 5400 要求）

## 建议
如果"卫星通信"是硬指标 → 选 **Honor Magic 6 至臻版**，主摄稍弱；
如果"1 英寸主摄"是硬指标 → 选 **Xiaomi 14 Ultra** 或 **OPPO Find X7
Ultra**，放弃卫星通信；
如果都想兼顾 → 等 2024 H2 / 2025 的下一代旗舰。
EOF

echo "[oracle] 04_task_3_constraint_search — wrote results.md"
echo "[oracle]   strategy: declare no full match + recommend 4 near-matches"
echo "[oracle]   judge expects ≥2 of {Xiaomi 14 Ultra, Vivo X100 Ultra, Magic 6 至臻版, Find X7 Ultra}"
