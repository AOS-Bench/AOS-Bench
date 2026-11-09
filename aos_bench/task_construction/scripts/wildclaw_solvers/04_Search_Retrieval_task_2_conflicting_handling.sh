#!/usr/bin/env bash
# Oracle solver for 04_Search_Retrieval_task_2_conflicting_handling.
#
# Rubric (LLM judge): score 1 iff the final reasoning conclusion is "3 years"
# (matching China's general civil-claim statute of limitations under PRC Civil
# Code Article 188). Any other final answer scores 0.
set -euo pipefail

OUT=/workspace/tmp/results
mkdir -p "$OUT"

cat > "$OUT/results.md" <<'EOF'
# 诉讼时效分析 / Statute of Limitations Analysis

## 案件事实概要 (Facts)
- 中国 A 公司向中国 B 公司销售体育器材，约定收货后 30 日付款；
- B 公司收货后未付款；
- 当年 8 月，B 公司通过电子邮件确认仍欠 A 公司货款 50 万元，并承诺尽快支付；
- 此后双方多轮催收/协商，债务始终未清偿；
- 当年 9 月，A 公司律师准备起诉追讨货款。

## 法律依据 (Applicable Law)
《中华人民共和国民法典》第 188 条规定：
> 向人民法院请求保护民事权利的诉讼时效期间为**三年**。法律另有规定的，依照其规定。

第 195 条规定：权利人向义务人提出履行请求、义务人同意履行义务等情形，
**诉讼时效中断**，从中断时起重新计算。

## 推理过程 (Reasoning)
1. 本案为普通买卖合同之债，无《民法典》或单行法的特别短期/长期时效规定，
   适用一般诉讼时效——即 3 年。
2. 8 月 B 公司邮件确认欠款 50 万元并承诺尽快支付，构成《民法典》第 195 条
   "义务人同意履行义务"，触发诉讼时效中断；时效自该确认之日起重新计算 3 年。
3. 此后多轮催收/协商，每次有效催告/确认均可再次中断时效。
4. 9 月起诉时距最近一次中断尚不足 3 年，时效未届满，A 公司起诉于法有据。

## 最终结论 / Final Answer
**The applicable statute of limitations is 3 years.**
诉讼时效为 **3 年**（自最近一次中断之日起重新起算 3 年）。
EOF

echo "[oracle] 04_task_2_conflicting_handling — final answer: 3 years (PRC Civil Code Art. 188)"
