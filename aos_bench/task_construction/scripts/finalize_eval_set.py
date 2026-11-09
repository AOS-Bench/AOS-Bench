#!/usr/bin/env python3
"""Finish the valid-0917 set: copy the selection and verify it, offline.

Split out of select_final_eval_set.py because the live registry probe is not
usable on this host right now: another agent's oracle sweep has the podman
socket saturated, so `docker manifest inspect` blocks indefinitely (the parent
sat in wchan=pipe_wait with 12 CPU ticks over 10+ minutes, and the subprocess
timeout never fired because the hang is in the podman service, not the client).

So criterion 4 is verified from RECORDED evidence instead of a live call:
verification-logs/build-result.tsv holds the tag and the registry digest that
was read back from `docker push --digestfile` and, at promotion time, proven
pullable. That is the same fact the probe would re-establish, minus the
contention. Where the digest is missing the stream is reported as unverified
rather than passed.

Everything else is computed locally from stream.toml and the build context.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import shutil
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "aos_bench/task_construction/scripts"))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "sel", REPO / "aos_bench/task_construction/scripts/select_final_eval_set.py"
)
sel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sel)

TIER_V2 = REPO / "data/stream/V1-0/tier-v2"


def image_evidence(stream: sel.Stream) -> tuple[bool, str]:
    """Criterion 4 from recorded build evidence."""
    tsv = stream.path / "verification-logs" / "build-result.tsv"
    if not tsv.is_file():
        return False, "no build-result.tsv"
    rows = [
        line.split("\t")
        for line in tsv.read_text().splitlines()
        if line.strip()
    ]
    if len(rows) < 2:
        return False, "build-result.tsv has no data row"
    header, data = rows[0], rows[1]
    rec = dict(zip(header, data))
    verdict = rec.get("verdict", "")
    tag = rec.get("tag", "")
    digest = rec.get("digest", "")
    if not digest.startswith("sha256:"):
        return False, f"no registry digest recorded (verdict={verdict or '?'})"
    if tag and stream.image and tag != stream.image:
        return False, f"tag mismatch: stream.toml={stream.image} tsv={tag}"
    return True, f"{verdict}, digest {digest[:19]}..."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--light", type=int, default=10)
    ap.add_argument("--medium", type=int, default=10)
    ap.add_argument("--heavy", type=int, default=5)
    ap.add_argument("--heavy-plus", type=int, default=1)
    ap.add_argument("--out", default="data/stream/V1-0/valid-0917")
    ap.add_argument("--report", default="tmp/0917/data_final_report.md")
    ap.add_argument("--seed", type=int, default=917)
    ap.add_argument("--tries", type=int, default=25)
    ap.add_argument("--extra-source", action="append", default=["build-valid"])
    ap.add_argument(
        "--require-oracle",
        action="append",
        default=["heavy-plus-30-01", "heavy-plus-30-04"],
    )
    args = ap.parse_args()

    want = {
        "light": args.light,
        "medium": args.medium,
        "heavy": args.heavy,
        "heavy-plus": args.heavy_plus,
    }

    pool = sel.load_pool(tuple(args.extra_source))
    allowed = set(args.require_oracle)
    fv = sel.load_pool()
    for tier in sel.TIERS:
        fv_names = {s.name for s in fv.get(tier, [])}
        pool[tier] = [
            s for s in pool[tier] if s.name in fv_names or s.name in allowed
        ]

    print("pool:")
    for t in sel.TIERS:
        usable = [
            s
            for s in pool[t]
            if not s.is_arithmetic()
            and s.image
            and not s.solution_in_build_context()[0]
        ]
        print(f"  {t:11s} {len(pool[t]):3d} total, {len(usable):3d} eligible")

    chosen, metrics = sel.select(pool, want, args.seed, args.tries)
    if not chosen:
        print("no feasible selection")
        return 1

    counts: dict[str, int] = {}
    for s in chosen:
        counts[s.tier] = counts.get(s.tier, 0) + 1
    print("\nselected:", {k: counts.get(k, 0) for k in sel.TIERS if want.get(k)})

    # ---- copy -----------------------------------------------------------
    out = pathlib.Path(args.out)
    for s in chosen:
        dst = out / s.tier / s.name
        if dst.exists() and (dst / "stream.toml").is_file():
            continue  # already copied by the interrupted run
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(s.path, dst, symlinks=True)
        print(f"  copied {s.tier}/{s.name}")

    # Remove anything in out/ that is not part of this selection, so a
    # re-run with a different seed cannot leave a stale stream behind.
    keep = {(s.tier, s.name) for s in chosen}
    for tier_dir in out.iterdir() if out.is_dir() else []:
        if not tier_dir.is_dir():
            continue
        for sd in tier_dir.iterdir():
            if sd.is_dir() and (tier_dir.name, sd.name) not in keep:
                print(f"  removing stale {tier_dir.name}/{sd.name}")
                shutil.rmtree(sd)

    # ---- verify ---------------------------------------------------------
    pairs = [
        (a.name, b.name, sel.overlap(a, b), len(a.tasks & b.tasks))
        for i, a in enumerate(chosen)
        for b in chosen[i + 1 :]
    ]
    worst = max(pairs, key=lambda p: p[2]) if pairs else None
    results = {
        "overlap": {
            "pairs": len(pairs),
            "violations": len([p for p in pairs if p[2] >= sel.GATE]),
            "max_ratio": worst[2] if worst else 0.0,
            "max_pair": f"{worst[0]} / {worst[1]}" if worst else "",
            "shared_task_pairs": metrics.get("shared_task_pairs"),
            "top_pairs": sorted(pairs, key=lambda p: -p[2])[:8],
        },
        "arrivals": [
            {
                "stream": s.name,
                "tier": s.tier,
                **{k: (round(v, 3) if isinstance(v, float) else v)
                   for k, v in s.gap_stats().items()},
                "arithmetic": s.is_arithmetic(),
                "gaps": s.gaps(),
            }
            for s in chosen
        ],
        "benchmarks": [
            {
                "stream": s.name,
                "tier": s.tier,
                "n_tasks": len(s.benchmarks()),
                "distinct_benchmarks": len(set(s.benchmarks())),
                "adjacent_same": s.adjacent_same_benchmark(),
                "sequence": " → ".join(s.benchmarks()),
            }
            for s in chosen
        ],
        "images": [],
        "solution": [],
    }

    for s in chosen:
        ok, detail = image_evidence(s)
        results["images"].append(
            {"stream": s.name, "image": s.image, "ok": ok, "detail": detail}
        )
    for s in chosen:
        leaked, hits = s.solution_in_build_context()
        results["solution"].append(
            {"stream": s.name, "leaked": leaked, "hits": hits}
        )

    report = pathlib.Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render(chosen, results, want, args))
    print(f"\nreport: {report}")

    bad_img = [r for r in results["images"] if not r["ok"]]
    print(f"overlap violations: {results['overlap']['violations']}")
    print(f"max overlap: {results['overlap']['max_ratio']:.1%}")
    print(f"arithmetic arrivals: {sum(1 for r in results['arrivals'] if r['arithmetic'])}")
    print(f"solution leaks: {sum(1 for r in results['solution'] if r['leaked'])}")
    print(f"image evidence missing: {len(bad_img)}")
    for r in bad_img:
        print(f"  {r['stream']}: {r['detail']}")
    return 0


def render(chosen, results, want, args) -> str:
    ov = results["overlap"]
    L: list[str] = []
    A = L.append
    A("# V1-0 最终评测集验收报告 — valid-0917")
    A("")
    A(f"生成脚本 `aos_bench/task_construction/scripts/finalize_eval_set.py`,")
    A(f"seed {args.seed},{args.tries} 次重启搜索,输出 `{args.out}`。")
    A("")
    A("## 选集结果")
    A("")
    counts: dict[str, int] = {}
    for s in chosen:
        counts[s.tier] = counts.get(s.tier, 0) + 1
    A("| tier | 要求 | 实选 |")
    A("|---|---:|---:|")
    for t in sel.TIERS:
        if want.get(t):
            A(f"| {t} | {want[t]} | {counts.get(t, 0)} |")
    A(f"| **合计** | **{sum(want.values())}** | **{len(chosen)}** |")
    A("")
    for t in sel.TIERS:
        names = sorted(s.name for s in chosen if s.tier == t)
        if names:
            A(f"- **{t}** — {', '.join(names)}")
    A("")
    A("候选池来自 `tier-v2/full-valid`、`full-valid-0915-new`、")
    A("`full-valid-diverse0915` 三个目录去重后的并集。heavy-plus 是例外:")
    A("三个 full-valid 目录都没有 heavy-plus,而 `build-valid/heavy-plus`")
    A("下有两条已跑到 oracle 通过率 27/30 = 90%(`heavy-plus-30-01`、")
    A("`heavy-plus-30-04`),按 >=90% 口径达标,故以显式白名单方式纳入候选。")
    A("")

    A("## 1. Task overlap 最小化")
    A("")
    A("**检查流程** — 对选集内每一对流计算")
    A("`overlap(A,B) = |A∩B| / min(|A|,|B|)`,要求严格小于 0.60。分母取")
    A("`min` 意味着任务数最少的那条流是约束方: 一条 5 任务的 light 与一条")
    A("20 任务的 heavy 只要共享 3 个任务就已达到 0.60 而失败,所以跨 tier")
    A("的配对比同 tier 更容易触发。选择器在搜索时即对整个已选集合做校验")
    A("(不是只在同 tier 内),完成后再全量复核一次。")
    A("")
    A("在满足门槛的可行解中,优先选取**共享任务对总数最小**的一组作为")
    A("最小化目标。")
    A("")
    A(f"- 检查配对数: **{ov['pairs']}**")
    A(f"- 违规数: **{ov['violations']}**")
    A(f"- 最大 overlap: **{ov['max_ratio']:.1%}**(`{ov['max_pair']}`)")
    A(f"- 全集共享任务对总数: **{ov['shared_task_pairs']}**")
    A("")
    A("overlap 最高的若干配对:")
    A("")
    A("| A | B | overlap | 共享任务数 |")
    A("|---|---|---:|---:|")
    for a, b, r, n in ov["top_pairs"]:
        A(f"| {a} | {b} | {r:.1%} | {n} |")
    A("")

    A("## 2. 到达时间伪随机(非等差)")
    A("")
    A("**检查流程** — 取每条流 `arrivals[].arrive_wall_time` 的相邻差值")
    A("序列,若全部相等即为等差数列,直接判失败并在选择阶段就排除。报告中")
    A("的 `distinct` 为不同间隔值的个数,`cv` 为间隔的变异系数")
    A("(标准差/均值),两者越大说明越不规则。")
    A("")
    A("| stream | tier | 间隔数 | 不同值 | 最小 | 最大 | cv | 等差? |")
    A("|---|---|---:|---:|---:|---:|---:|---|")
    for r in results["arrivals"]:
        A(
            f"| {r['stream']} | {r['tier']} | {r['n']} | {r['distinct']} | "
            f"{r['min']} | {r['max']} | {r['cv']} | "
            f"{'**是(失败)**' if r['arithmetic'] else '否'} |"
        )
    A("")

    A("## 3. Task source 打乱(不同 benchmark 分散到达)")
    A("")
    A("**检查流程** — 把每个 task_id 归到它的来源 benchmark(按已知前缀")
    A("匹配,避免把 `terminal-bench-2` 这类多段名切错),然后按到达顺序检查")
    A("相邻两个任务是否同源。`相邻同源数` 为 0 表示完全交错,没有任何两个")
    A("相邻任务出自同一 benchmark;该指标在选择时作为次级优化目标。")
    A("")
    A("| stream | tier | 任务数 | 不同 benchmark | 相邻同源数 |")
    A("|---|---|---:|---:|---:|")
    for r in results["benchmarks"]:
        A(
            f"| {r['stream']} | {r['tier']} | {r['n_tasks']} | "
            f"{r['distinct_benchmarks']} | {r['adjacent_same']} |"
        )
    A("")
    A("<details><summary>各流按到达顺序的 benchmark 序列</summary>")
    A("")
    for r in results["benchmarks"]:
        A(f"- `{r['stream']}`:{r['sequence']}")
    A("")
    A("</details>")
    A("")

    A("## 4. 镜像已打包")
    A("")
    A("**检查流程** — 读每条流 `verification-logs/build-result.tsv` 里记录的")
    A("tag 与 digest,并与 `stream.toml` 的 `[environment].docker_image` 比对,")
    A("要求三者一致且 digest 形如 `sha256:...`。该 digest 来自构建时的")
    A("`docker push --digestfile`,且在晋级前已验证过可 pull。")
    A("")
    A("这里**没有**现场再调一次 `docker manifest inspect`: 本机 podman 服务")
    A("此刻被另一路的 oracle sweep 占满,现场探测会无限阻塞(实测父进程停在")
    A("`wchan=pipe_wait`、10 分钟仅 12 个 CPU tick,且客户端超时无法生效,")
    A("因为阻塞发生在 podman 服务侧)。改用已记录的 digest 证据,结论等价而")
    A("不与他人作业争抢。")
    A("")
    A("| stream | 镜像证据 | image |")
    A("|---|---|---|")
    for r in results["images"]:
        mark = r["detail"] if r["ok"] else f"**缺失** — {r['detail']}"
        A(f"| {r['stream']} | {mark} | `{r['image']}` |")
    A("")

    A("## 5. Solution 未打入镜像(防 hack)")
    A("")
    A("**检查流程** — 两条泄漏路径都查:")
    A("")
    A("1. `environment/Dockerfile` 中是否有 `COPY`/`ADD` 指令引用")
    A("   `solution/` 或 `solve.sh`;")
    A("2. `environment/` 目录(即构建上下文)内是否存在 `solution/` 或")
    A("   `solve.sh` — 若存在,一条 `COPY . .` 就会把它带进镜像。")
    A("")
    A("流目录本身**保留** `solution/`,这是 oracle 验收必需的,它不在构建")
    A("上下文内,因此不会进入镜像。")
    A("")
    A("| stream | solution 进镜像? | 依据 |")
    A("|---|---|---|")
    for r in results["solution"]:
        ev = "; ".join(r["hits"])[:150] if r["hits"] else "构建上下文干净,Dockerfile 无相关 COPY"
        A(f"| {r['stream']} | {'**泄漏**' if r['leaked'] else '否'} | {ev} |")
    A("")

    hard = (
        ov["violations"] > 0
        or any(r["arithmetic"] for r in results["arrivals"])
        or any(r["leaked"] for r in results["solution"])
    )
    missing_img = [r for r in results["images"] if not r["ok"]]
    A("## 结论")
    A("")
    if hard:
        A("**未通过** — 见上文标注失败的行。")
    elif missing_img:
        A(f"**四项通过,镜像证据有 {len(missing_img)} 条缺失**(见第 4 节),")
        A("其余 overlap、到达时间、source 打乱、solution 隔离四项全部通过。")
    else:
        A("**五项全部通过。**")
    A("")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
