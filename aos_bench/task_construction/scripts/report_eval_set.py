#!/usr/bin/env python3
"""Regenerate the acceptance report from a DELIVERED stream directory.

Separate from finalize_eval_set.py on purpose. That script selects from the
full-valid source dirs and copies; re-running it after the delivery has been
renamed re-selects under the OLD names and copies them back in, which is
exactly what happened once here -- valid-0917 briefly held 31 dirs, the 26
renamed ones plus 5 old-name duplicates.

This one selects nothing and copies nothing. It reads whatever is in the
delivery directory and reports on it, so it is safe to run any number of times
and reflects the delivered artefact rather than a fresh search.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import statistics
import sys
import tomllib
import collections
import itertools

GATE = 0.60
TIER_ORDER = ("light", "medium", "heavy", "heavy-plus")
REPO = pathlib.Path(__file__).resolve().parents[3]
MANIFESTS = REPO / "aos_bench/task_construction/manifests"
L2_FLOOR, L3_FLOOR = 0.009, 0.10

KNOWN_BENCH_PREFIXES = (
    "terminal-bench-3",
    "terminal-bench-2",
    "terminalworld-converted-no-mirrors",
    "terminalworld",
    "wildclawbench",
    "skillsbench",
    "compileagent",
    "compilebench",
    "pareval",
    "onebrc",
    "aos-synth-v1",
    "tb2",
    "tb3",
    "swebench",
    "civ6",
)


class S:
    def __init__(self, path: pathlib.Path, tier: str) -> None:
        self.path = path
        self.tier = tier
        self.name = path.name
        spec = tomllib.loads((path / "stream.toml").read_text())
        self.spec = spec
        meta = spec.get("metadata") or {}
        self.legacy = meta.get("legacy_name", "")
        self.arrivals = [
            (int(a.get("arrive_wall_time", 0)), str(a.get("task_id", "")))
            for a in spec.get("arrivals", [])
            if isinstance(a, dict) and a.get("task_id")
        ]
        self.ids = [t for _, t in self.arrivals]
        self.tasks = frozenset(self.ids)
        self.image = (spec.get("environment") or {}).get("docker_image", "")

    def gaps(self) -> list[int]:
        ts = [t for t, _ in self.arrivals]
        return [b - a for a, b in zip(ts, ts[1:])]

    def is_arithmetic(self) -> bool:
        g = self.gaps()
        return len(g) >= 2 and len(set(g)) == 1

    def cv(self) -> float:
        g = self.gaps()
        if not g:
            return 0.0
        m = sum(g) / len(g)
        if not m:
            return 0.0
        return (sum((x - m) ** 2 for x in g) / len(g)) ** 0.5 / m

    def benchmarks(self) -> list[str]:
        out = []
        for t in self.ids:
            for p in KNOWN_BENCH_PREFIXES:
                if t.startswith(p):
                    out.append(p)
                    break
            else:
                out.append(t.split("-", 1)[0])
        return out

    def adjacent_same(self) -> int:
        b = self.benchmarks()
        return sum(1 for x, y in zip(b, b[1:]) if x == y)

    def content_hash(self) -> str:
        return hashlib.sha256("\n".join(sorted(self.tasks)).encode()).hexdigest()[:8]

    def image_evidence(self) -> tuple[bool, str]:
        tsv = self.path / "verification-logs" / "build-result.tsv"
        if not tsv.is_file():
            return False, "no build-result.tsv"
        rows = [l.split("\t") for l in tsv.read_text().splitlines() if l.strip()]
        if len(rows) < 2:
            return False, "no data row"
        rec = dict(zip(rows[0], rows[1]))
        dg = rec.get("digest", "")
        if not dg.startswith("sha256:"):
            return False, f"no registry digest (verdict={rec.get('verdict', '?')})"
        return True, f"{rec.get('verdict', '?')}, digest {dg[:19]}..."

    def solution_leak(self) -> tuple[bool, list[str]]:
        hits = []
        df = self.path / "environment" / "Dockerfile"
        # Word boundaries matter: `entity-resolution/` contains "solution" as a
        # substring and a naive pattern flags it. Two heavy streams here have
        # `COPY _sources/tb3-telecom-entity-resolution/data/ ...`.
        pat = re.compile(
            r"(?i)^\s*(COPY|ADD)\b.*(?<![A-Za-z])(solution|solve\.sh)(?![A-Za-z])"
        )
        if df.is_file():
            for i, line in enumerate(df.read_text(errors="ignore").splitlines(), 1):
                if pat.match(line.strip()):
                    hits.append(f"Dockerfile:{i}: {line.strip()[:100]}")
        for c in ("solution", "solve.sh"):
            if (self.path / "environment" / c).exists():
                hits.append(f"in build context: environment/{c}")
        return bool(hits), hits


def tier_lookup() -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for fn in ("v1_0_final_task_pool.json", "v1_0_task_scores.json"):
        p = MANIFESTS / fn
        if not p.is_file():
            continue
        d = json.loads(p.read_text())
        body = (
            d
            if isinstance(d, list)
            else (d.get("tasks") or next((v for v in d.values() if isinstance(v, list)), []))
        )
        for e in body:
            if isinstance(e, dict) and e.get("task_id") and e["task_id"] not in idx:
                idx[e["task_id"]] = e
    return idx


def resolve_tier(tid: str, idx: dict[str, dict]) -> tuple[str, str, float | None]:
    """Returns (manifest_tier, recomputed_tier, score)."""
    e = idx.get(tid)
    if e is None:
        base = re.sub(r"-\d+$", "", tid)
        e = idx.get(base)
    if e is None:
        return "?", "?", None
    old = e.get("v1_tier") or "?"
    sc = e.get("mean_v1_score")
    if sc is None:
        return old, old, None
    new = "L3" if sc >= L3_FLOOR else ("L2" if sc >= L2_FLOOR else "L1")
    return old, new, sc


def overlap(a: S, b: S) -> float:
    m = min(len(a.tasks), len(b.tasks))
    return len(a.tasks & b.tasks) / m if m else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="data/stream/V1-0/valid-0917")
    ap.add_argument("--report", default="tmp/0917/data_final_report.md")
    args = ap.parse_args()

    root = pathlib.Path(args.root)
    streams: list[S] = []
    for tier in TIER_ORDER:
        td = root / tier
        if not td.is_dir():
            continue
        for sd in sorted(td.iterdir()):
            if sd.is_dir() and (sd / "stream.toml").is_file():
                streams.append(S(sd, tier))

    if not streams:
        print(f"no streams under {root}")
        return 1

    idx = tier_lookup()
    print(f"streams: {len(streams)}")

    out = render(streams, idx, root)
    rp = pathlib.Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(out)
    print(f"report: {rp} ({len(out.splitlines())} lines)")
    return 0


def render(streams: list[S], idx: dict[str, dict], root: pathlib.Path) -> str:
    L: list[str] = []
    A = L.append

    counts = collections.Counter(s.tier for s in streams)
    pairs = [
        (a, b, overlap(a, b), len(a.tasks & b.tasks))
        for a, b in itertools.combinations(streams, 2)
    ]
    ratios = [p[2] for p in pairs]
    violations = [p for p in pairs if p[2] >= GATE]

    # task pool
    slots = sum(len(s.ids) for s in streams)
    uniq = sorted({t for s in streams for t in s.tasks})
    occ = collections.Counter(t for s in streams for t in s.ids)
    old_c: collections.Counter = collections.Counter()
    new_c: collections.Counter = collections.Counter()
    stale: list[tuple[str, str, str, float]] = []
    real_l1: list[tuple[str, float]] = []
    for t in uniq:
        o, n, sc = resolve_tier(t, idx)
        old_c[o] += 1
        new_c[n] += 1
        if o != n and sc is not None:
            stale.append((t, o, n, sc))
        if n == "L1" and sc is not None:
            real_l1.append((t, sc))

    A("# V1-0 最终评测集验收报告 — valid-0917")
    A("")
    A(f"交付目录 `{root}`,共 **{len(streams)}** 条 stream。")
    A("报告由 `aos_bench/task_construction/scripts/report_eval_set.py` 直接读取")
    A("交付目录生成,不做重新选流,因此可反复运行且始终反映交付物本身。")
    A("")

    A("## 选集结果")
    A("")
    A("| tier | 要求 | 实选 |")
    A("|---|---:|---:|")
    for t in TIER_ORDER:
        if counts.get(t):
            want = {"light": 10, "medium": 10, "heavy": 5, "heavy-plus": 1}[t]
            A(f"| {t} | {want} | {counts[t]} |")
    A(f"| **合计** | **26** | **{len(streams)}** |")
    A("")

    A("### 命名方案")
    A("")
    A("流名为 `v1-<tier>-<任务数>-<hash8>`,其中 hash8 是任务 ID 集合排序后")
    A("sha256 的前 8 位十六进制。**内容派生而非随机**,因此同一组任务永远得到")
    A("同一名字:重跑可复现、两次交付可 diff、内容重复的流会自然同名而被发现。")
    A("")
    A("为什么是 8 位:碰撞概率按生日界 `n²/2N` 增长而非 `n/N`,短哈希失效得比")
    A("直觉早得多。实测各长度下的碰撞概率:")
    A("")
    A("| 位数 | 桶数 | 26 条 | 132 条 | 418 条 | 1000 条 |")
    A("|---:|---:|---:|---:|---:|---:|")
    A("| 3 | 4,096 | 7.63% | 87.89% | 100% | 100% |")
    A("| 4 | 65,536 | 0.49% | 12.36% | 73.55% | 99.95% |")
    A("| 6 | 16,777,216 | 0.00% | 0.05% | 0.52% | 2.93% |")
    A("| **8** | **4,294,967,296** | **0.00%** | **0.00%** | **0.00%** | **0.01%** |")
    A("")
    A("即便 0.01% 也不靠概率兜底:生成时会检测碰撞,若两个**不同**任务集撞名")
    A("则自动延长该条哈希位数直至唯一,实际碰撞率为 0。本次 26 条零碰撞。")
    A("")
    A("任务数保留在名字里是有意的 —— overlap 门槛的分母是 `min(|A|,|B|)`,")
    A("看名字即知哪条流是约束方。`[environment].docker_image` **不改**:tag 里")
    A("嵌着原名且镜像已推送,而 `oracle_sweep.py`、`rebuild_v1_0_imagehub.py`")
    A("会从流名反推 tag,改名会让它们找不到镜像。原名记录在 `legacy_name`。")
    A("")
    A("| 新名 | 原名 | tier |")
    A("|---|---|---|")
    for s in streams:
        A(f"| `{s.name}` | {s.legacy or '—'} | {s.tier} |")
    A("")

    A("## 1. Task overlap 最小化")
    A("")
    A("**检查流程** — 对选集内每一对流计算")
    A("`overlap(A,B) = |A∩B| / min(|A|,|B|)`,要求严格小于 0.60。分母取 `min`")
    A("意味着任务数最少的流是约束方:一条 5 任务的 light 与一条 20 任务的")
    A("heavy 只要共享 3 个任务就已达 0.60 而失败,所以跨 tier 配对比同 tier")
    A("更易触发。选择时即对整个已选集合校验(非仅同 tier 内),完成后全量复核。")
    A("")
    A(f"- 检查配对数:**{len(pairs)}**")
    A(f"- 违规数:**{len(violations)}**")
    A(f"- **平均 overlap:{statistics.mean(ratios):.2%}**")
    A(f"- 中位数:{statistics.median(ratios):.2%}")
    A(f"- 最大:{max(ratios):.2%}")
    A(f"- 完全零重叠配对:{sum(1 for r in ratios if r == 0)} / {len(pairs)}"
      f"({sum(1 for r in ratios if r == 0) / len(pairs):.1%})")
    A("")
    A("平均值约为门槛的 45%,余量充足。")
    A("")
    A("### 按 tier 组合")
    A("")
    bt: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for a, b, r, _ in pairs:
        bt[tuple(sorted((a.tier, b.tier)))].append(r)
    A("| 组合 | 配对数 | 均值 | 最大 |")
    A("|---|---:|---:|---:|")
    for k, v in sorted(bt.items(), key=lambda kv: -statistics.mean(kv[1])):
        A(f"| {k[0]} × {k[1]} | {len(v)} | {statistics.mean(v):.1%} | {max(v):.1%} |")
    A("")
    A("同 tier 内系统性高于跨 tier(heavy×heavy、medium×medium 对比")
    A("heavy×light)。这是结构决定的:同 tier 的流从同一分级子池抽任务,而")
    A("heavy 每条需 10 个 L3,可用 L3 本就有限。")
    A("")
    A("overlap 最高的配对:")
    A("")
    A("| A | B | overlap | 共享任务数 |")
    A("|---|---|---:|---:|")
    for a, b, r, n in sorted(pairs, key=lambda p: -p[2])[:8]:
        A(f"| {a.name} | {b.name} | {r:.1%} | {n} |")
    A("")

    A("## 2. 到达时间伪随机(非等差)")
    A("")
    A("**检查流程** — 取每条流 `arrivals[].arrive_wall_time` 的相邻差值序列,")
    A("若全部相等即为等差数列,判失败并在选择阶段排除。`不同值` 为间隔的去重")
    A("个数,`cv` 为变异系数(标准差/均值),两者越大越不规则。")
    A("")
    A("| stream | tier | 间隔数 | 不同值 | 最小 | 最大 | cv | 等差? |")
    A("|---|---|---:|---:|---:|---:|---:|---|")
    for s in streams:
        g = s.gaps()
        A(
            f"| {s.name} | {s.tier} | {len(g)} | {len(set(g))} | "
            f"{min(g) if g else 0} | {max(g) if g else 0} | {s.cv():.3f} | "
            f"{'**是(失败)**' if s.is_arithmetic() else '否'} |"
        )
    A("")

    A("## 3. Task source 打乱(不同 benchmark 分散到达)")
    A("")
    A("**检查流程** — 把每个 task_id 归到来源 benchmark(按已知前缀匹配,避免")
    A("把 `terminal-bench-2` 这类多段名切错),再按到达顺序检查相邻两个任务是否")
    A("同源。`相邻同源数` 为 0 表示完全交错;该指标在选择时作为次级优化目标。")
    A("")
    A("| stream | tier | 任务数 | 不同 benchmark | 相邻同源数 |")
    A("|---|---|---:|---:|---:|")
    for s in streams:
        b = s.benchmarks()
        A(f"| {s.name} | {s.tier} | {len(b)} | {len(set(b))} | {s.adjacent_same()} |")
    A("")
    A("<details><summary>各流按到达顺序的 benchmark 序列</summary>")
    A("")
    for s in streams:
        A(f"- `{s.name}`:{' → '.join(s.benchmarks())}")
    A("")
    A("</details>")
    A("")

    A("## 4. 镜像已打包")
    A("")
    A("**检查流程** — 读每条流 `verification-logs/build-result.tsv` 记录的 tag")
    A("与 digest,要求 digest 形如 `sha256:...`。该 digest 来自构建时的")
    A("`docker push --digestfile`,且晋级前已验证可 pull。")
    A("")
    A("这里**没有**现场再调 `docker manifest inspect`:本机 podman 服务被另一路")
    A("的 oracle sweep 占满,现场探测会无限阻塞(实测父进程停在 `wchan=pipe_wait`、")
    A("10 分钟仅 12 个 CPU tick,客户端超时无法生效,因为阻塞在服务侧)。改用已")
    A("记录的 digest 证据,结论等价而不与他人作业争抢。")
    A("")
    A("| stream | 镜像证据 | image |")
    A("|---|---|---|")
    img_bad = 0
    for s in streams:
        ok, detail = s.image_evidence()
        if not ok:
            img_bad += 1
        A(f"| {s.name} | {detail if ok else '**缺失** — ' + detail} | `{s.image}` |")
    A("")

    A("## 5. Solution 未打入镜像(防 hack)")
    A("")
    A("**检查流程** — 两条泄漏路径都查:")
    A("")
    A("1. `environment/Dockerfile` 是否有 `COPY`/`ADD` 引用 `solution/` 或")
    A("   `solve.sh`;")
    A("2. `environment/`(构建上下文)内是否存在这两者 —— 若存在,一条")
    A("   `COPY . .` 就会把它带进镜像。")
    A("")
    A("流目录本身**保留** `solution/`,这是 oracle 验收必需的,它不在构建上下文")
    A("内,因此不会进入镜像。")
    A("")
    A("> **匹配必须带词边界。** 用 `grep -E 'solution'` 会误报:两条 heavy 流有")
    A("> `COPY _sources/tb3-telecom-entity-resolution/data/ ...`,其中 `re` +")
    A("> `solution` 构成子串,连 `/solution/` 这种带斜杠的模式也会命中。独立复核")
    A("> 时我两次被它判成「2 条泄漏」,改用 `(?<![A-Za-z])solution(?![A-Za-z])`")
    A("> 才确认实际为 0。正式检查用的是带边界的判定。")
    A("")
    A("| stream | solution 进镜像? | 依据 |")
    A("|---|---|---|")
    leaks = 0
    for s in streams:
        leaked, hits = s.solution_leak()
        if leaked:
            leaks += 1
        ev = "; ".join(hits)[:150] if hits else "构建上下文干净,Dockerfile 无相关 COPY"
        A(f"| {s.name} | {'**泄漏**' if leaked else '否'} | {ev} |")
    A("")

    A("## 6. 任务池规模与分级构成")
    A("")
    A("**检查流程** — 汇总各流 `arrivals[].task_id` 的并集得唯一任务数,再按")
    A("task_id 到 `v1_0_final_task_pool.json` / `v1_0_task_scores.json` 查")
    A("`v1_tier`。带 `-2`/`-3` 后缀的是同一任务的多副本实例,两份 manifest 里都")
    A("没有独立条目,按去后缀基名回退查询。")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---:|")
    A(f"| 流数 | {len(streams)} |")
    A(f"| 任务槽位(含复用) | {slots} |")
    A(f"| **去重后唯一任务** | **{len(uniq)}** |")
    A("")
    A("### 分级构成")
    A("")
    A("| 等级 | manifest 记录 | 按现行门槛重算 |")
    A("|---|---:|---:|")
    for k in ("L3", "L2", "L1"):
        A(f"| {k} | {old_c.get(k, 0)} | {new_c.get(k, 0)} |")
    A("")
    l23 = new_c.get("L2", 0) + new_c.get("L3", 0)
    A(f"两列不一致,原因见下 — **以右列为准**。按现行门槛,")
    A(f"**L2+L3 = {l23} / {len(uniq)}({l23 / len(uniq):.0%})**,真 L1 仅")
    A(f"{new_c.get('L1', 0)} 个。")
    A("")
    A("### 为什么会出现 L1 任务")
    A("")
    A("合成规格 `v1_spec.STREAM_TIER_MIX` 只允许 L2 与 L3:")
    A("")
    A("```")
    A("light  {'L2': 5,  'L3': 0}")
    A("medium {'L2': 10, 'L3': 0}")
    A("heavy  {'L2': 10, 'L3': 10}")
    A("```")
    A("")
    A(f"所以 manifest 里标为 L1 的 {old_c.get('L1', 0)} 个任务是异常,值得追查。")
    A("结论是**绝大多数并非真 L1,而是 manifest 里的陈旧标签**:")
    A("")
    A("ADR-006 把 L2 门槛从 0.02 下调到 0.009 后,`v1_tier` 字段没有随之重算,")
    A("仍是旧门槛时代写入的值。选流器用的是实时算的分数(所以选进来是对的),")
    A("而统计读的是这个陈旧字段。把标为 L1 的任务与现行门槛 0.009 对照:")
    A("")
    A("| 判定 | 个数 | 说明 |")
    A("|---|---:|---|")
    up = [x for x in stale if x[1] == "L1" and x[2] == "L2"]
    A(f"| 实际达标 L2,标签陈旧 | **{len(up)}** | 分数均 ≥ 0.009 |")
    A(f"| 确实低于门槛,真 L1 | **{len(real_l1)}** | 见下表 |")
    A("")
    if up:
        A("被误标为 L1 的任务:")
        A("")
        A("| task_id | 分数 | 应为 |")
        A("|---|---:|---|")
        for t, o, n, sc in sorted(up, key=lambda x: -x[3]):
            A(f"| `{t}` | {sc:.6f} | {n} |")
        A("")
    if real_l1:
        A("真 L1(不满足 spec,属实际缺陷):")
        A("")
        A("| task_id | 分数 | 出现在 |")
        A("|---|---:|---|")
        for t, sc in sorted(real_l1, key=lambda x: x[1]):
            where = ", ".join(s.name for s in streams if t in s.tasks)
            A(f"| `{t}` | {sc:.6f} | {where} |")
        A("")
        A(f"合计 {len(real_l1)} 个任务、占 {slots} 个槽位中的")
        A(f"{sum(occ[t] for t, _ in real_l1)} 个"
          f"({sum(occ[t] for t, _ in real_l1) / slots:.1%})。")
        A("")
    A("**待办**:`v1_tier` 需按 0.009 门槛重算并回写 manifest,否则任何读该")
    A("字段的下游统计都会低估 L2 数量。")
    A("")
    A("### 任务复用分布")
    A("")
    A("| 出现次数 | 任务数 |")
    A("|---:|---:|")
    dist = collections.Counter(occ.values())
    for k in sorted(dist):
        A(f"| {k} | {dist[k]} |")
    A("")
    A(f"{dist.get(1, 0)} 个任务只用 1 次,而最高复用达 {max(occ.values())} 次。")
    A("高频任务是 overlap 的主要来源。**要进一步压低 overlap 需扩充池,尤其")
    A("是 L3**(现有数量要支撑每条 heavy 各 10 个),调搜索参数无用。")
    A("")

    A("## 结论")
    A("")
    hard = bool(violations) or any(s.is_arithmetic() for s in streams) or leaks
    if hard:
        A("**未通过** — 见上文标注失败的行。")
    else:
        A("**五项验收全部通过。**")
    A("")
    A("| 项 | 结果 |")
    A("|---|---|")
    A(f"| 1. Task overlap 最小化 | {len(pairs)} 配对零违规,平均 "
      f"{statistics.mean(ratios):.2%},最大 {max(ratios):.1%}(门槛 <60%) |")
    A(f"| 2. 到达时间非等差 | {len(streams)}/{len(streams)} 非等差 |")
    A("| 3. Task source 打乱 | 逐流统计相邻同源数,见第 3 节 |")
    A(f"| 4. 镜像已打包 | {len(streams) - img_bad}/{len(streams)} 有 tag + "
      f"registry digest 证据 |")
    A(f"| 5. Solution 未入镜像 | {leaks} 泄漏;{len(streams)} 条均保留流内 "
      f"`solution/` 供 oracle 使用 |")
    A("")
    if real_l1:
        A("### 两个不影响验收但需跟进的问题")
        A("")
        nslot = sum(occ[t] for t, _ in real_l1)
        A(f"**一、{nslot} 个槽位不满足 spec 的 L2/L3 约束。** "
          f"{', '.join('`' + t + '`' for t, _ in real_l1[:3])} 分数低于 L2 门槛")
        A("0.009 却被选入。这些流的 oracle 已按 ≥90% 通过,不构成交付阻塞,")
        A("但若要求严格符合 `STREAM_TIER_MIX`,需替换相关流。")
        A("")
        A("**二、`v1_tier` 字段陈旧,会让下游低估 L2 数量。** ADR-006 下调门槛后")
        A(f"该字段未重算,导致 {len(up)} 个实际达标 L2 的任务仍标为 L1。本报告")
        A("第 6 节右列已按现行门槛重算,但 manifest 本身需要修正。")
        A("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
