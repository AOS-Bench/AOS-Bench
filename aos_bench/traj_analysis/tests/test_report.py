"""End-to-end report assembly.

Pins the two structural guarantees the writeup depends on: clean and throttled
cohorts never mix, and a cell backed by a single stream is labelled as such.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from aos_bench.traj_analysis import report
from aos_bench.traj_analysis.__main__ import main
from aos_bench.traj_analysis.tests import fixtures

HEALTHY = [
    fixtures.step(i, f"2026-09-19T03:00:{i % 60:02d}.000Z", tool="Bash", arguments={"command": "make -j4"})
    for i in range(1, 41)
]


def _build(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    sched = fixtures.scheduling_metrics(n_tasks=2, n_passed=2, makespan_sec=600.0, throughput=12.0)
    # two clean trials on two distinct streams, one throttled
    fixtures.write_trial(
        jobs, "main-cc-stream-medium", "v1-medium-10-S1__a", harness="claude-code",
        steps=HEALTHY, rewards={"x": 1.0, "y": 1.0}, transcript="all good", scheduling=sched,
    )
    fixtures.write_trial(
        jobs, "main-cc-stream-medium", "v1-medium-10-S2__b", harness="claude-code",
        steps=HEALTHY, rewards={"x": 1.0, "y": 0.0}, transcript="all good", scheduling=sched,
    )
    fixtures.write_trial(
        jobs, "main-cc-stream-medium", "v1-medium-10-S3__c", harness="claude-code",
        steps=HEALTHY[:20], rewards={"x": 0.0, "y": 0.0},
        transcript="触发平台限流(bucket=project_rpm,limit=100)", scheduling=sched,
    )
    # heavy-plus: three attempts at ONE stream -> single-stream warning
    for suffix in ("a", "b", "c"):
        fixtures.write_trial(
            jobs, "main-codex-stream-heavy-plus", f"v1-hplus-30-ONE__{suffix}",
            steps=HEALTHY, rewards={"x": 1.0}, transcript="fine", scheduling=sched,
        )
    # an in-flight trial that must stay out of every denominator
    fixtures.write_trial(
        jobs, "main-kimi-stream-medium", "v1-medium-10-S9__z", harness="kimi-code",
        steps=HEALTHY[:5], write_result=False,
    )
    return jobs


def test_cohorts_do_not_mix(tmp_path: Path) -> None:
    bundles = report.analyse(_build(tmp_path))
    summary = report.build_summary(bundles)
    cell = next(c for c in summary["cells"] if c["arm"] == "cc" and c["tier"] == "medium")
    assert cell["clean"]["n_trials"] == 2
    assert cell["clean"]["n_streams"] == 2
    assert abs(cell["clean"]["mean_reward"] - 0.75) < 1e-9
    assert cell["throttled"]["n_trials"] == 1
    assert cell["throttled"]["mean_reward"] == 0.0
    # in-flight excluded everywhere
    assert summary["totals"]["n_in_flight"] == 1
    kimi = next(c for c in summary["cells"] if c["arm"] == "kimi")
    assert kimi["clean"]["n_trials"] == 0


def test_single_stream_cell_is_labelled(tmp_path: Path) -> None:
    bundles = report.analyse(_build(tmp_path))
    summary = report.build_summary(bundles)
    cell = next(c for c in summary["cells"] if c["tier"] == "heavy-plus")
    assert cell["clean"]["n_trials"] == 3
    assert cell["clean"]["n_streams"] == 1
    assert "SINGLE stream" in cell["clean"]["confidence"]


def test_cli_writes_all_outputs(tmp_path: Path) -> None:
    jobs = _build(tmp_path)
    out = tmp_path / "analysis"
    assert main(["--jobs-dir", str(jobs), "--out", str(out), "--quiet"]) == 0

    for name in ("summary.json", "trials.csv", "findings.csv", "health.json", "report.md"):
        assert (out / name).is_file(), name
    # no temp files left behind
    assert not list(out.glob("*.tmp"))

    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["n_trial_dirs"] == 7
    assert summary["totals"]["n_throttled"] == 1
    assert summary["cpu_throttle_data_available"] is False

    with (out / "trials.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 7
    assert {r["status"] for r in rows} == {"clean", "throttled", "in_flight"}

    text = (out / "report.md").read_text(encoding="utf-8")
    assert "clean-trial only" in text
    assert "absent, as required under pool v0.3" in text
    assert "NOT LLM latency" in text


def test_cli_arm_filter(tmp_path: Path) -> None:
    jobs = _build(tmp_path)
    out = tmp_path / "analysis-cc"
    assert main(["--jobs-dir", str(jobs), "--out", str(out), "--arm", "cc", "--quiet"]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert {c["arm"] for c in summary["cells"]} == {"cc"}


def test_cli_missing_jobs_dir_is_exit_2(tmp_path: Path) -> None:
    assert main(["--jobs-dir", str(tmp_path / "nope"), "--out", str(tmp_path / "o")]) == 2


def test_jobs_dir_is_not_mutated(tmp_path: Path) -> None:
    """The live jobs tree is read-only by contract."""
    jobs = _build(tmp_path)
    before = {p.relative_to(jobs): p.stat().st_mtime_ns for p in jobs.rglob("*") if p.is_file()}
    main(["--jobs-dir", str(jobs), "--out", str(tmp_path / "out"), "--quiet"])
    after = {p.relative_to(jobs): p.stat().st_mtime_ns for p in jobs.rglob("*") if p.is_file()}
    assert before == after


# --- round 2: settings never merge, comparisons, export gap ----------------


def _ab_jobs(tmp_path: Path) -> Path:
    """Same two streams and task ids run under stream and under burst.

    Burst scores strictly higher on both shared tasks, so a correct paired
    comparison must report a positive delta with both ids moving up.
    """
    jobs = tmp_path / "jobs"
    sched = fixtures.stream_scheduling(
        tasks={
            "x": fixtures.task(arrived=0.0, claimed=1.0, terminal=100.0, reward=1.0),
            "y": fixtures.task(arrived=0.0, claimed=2.0, terminal=200.0, reward=0.0),
        },
        makespan_sec=300.0,
        concurrency=[[1.0, 2], [100.0, 1], [300.0, 0]],
        backlog=[[0.0, 1], [2.0, 0]],
        mean_concurrency=1.33,
        peak_concurrency=2,
    )
    for stream in ("S1", "S2"):
        fixtures.write_trial(
            jobs, "main-glm-stream-light", f"v1-light-05-{stream}__a", harness="claude-code",
            steps=HEALTHY, rewards={"x": 0.0, "y": 0.0}, transcript="fine", scheduling=sched,
        )
        fixtures.write_trial(
            jobs, "main-glm-burst-light", f"v1-light-05-{stream}__b", harness="claude-code",
            steps=HEALTHY, rewards={"x": 1.0, "y": 1.0}, transcript="fine", scheduling=sched,
        )
    return jobs


def test_stream_and_burst_are_separate_cells(tmp_path: Path) -> None:
    """Merging them would average away the comparison the round exists for."""
    summary = report.build_summary(report.analyse(_ab_jobs(tmp_path)))
    labels = {c["cell"] for c in summary["cells"]}
    assert {"glm-stream-light", "glm-burst-light"} <= labels
    stream = next(c for c in summary["cells"] if c["cell"] == "glm-stream-light")
    burst = next(c for c in summary["cells"] if c["cell"] == "glm-burst-light")
    assert stream["clean"]["mean_reward"] == 0.0
    assert burst["clean"]["mean_reward"] == 1.0


def test_paired_comparison_appears_in_summary(tmp_path: Path) -> None:
    summary = report.build_summary(report.analyse(_ab_jobs(tmp_path)))
    comp = next(c for c in summary["comparisons"] if c["label"] == "glm light: stream -> burst")
    assert comp["status"] == "ok"
    assert comp["reward"]["n_shared_task_ids"] == 2
    assert comp["reward"]["paired_delta"] == 1.0
    assert comp["reward"]["n_better_b"] == 2
    assert comp["metrics"]["mean_reward"]["n_shared_streams"] == 2


def test_comparison_with_an_empty_side_is_marked_unavailable(tmp_path: Path) -> None:
    """A not-yet-started variant must read UNAVAILABLE, never as a 0.0 delta.

    The 4c8g heavy arm has data; the 2c4g probe has none. The comparison must
    still appear (so the reader knows it was attempted) and must say plainly
    that one side is empty.
    """
    jobs = _ab_jobs(tmp_path)
    fixtures.write_trial(
        jobs, "main-glm-stream-heavy", "v1-heavy-20-H1__a", harness="claude-code",
        steps=HEALTHY, rewards={"x": 1.0}, transcript="fine",
    )
    summary = report.build_summary(report.analyse(jobs))
    comp = next(c for c in summary["comparisons"] if "2c4g" in c["label"])
    assert comp["status"].startswith("UNAVAILABLE")
    assert comp["reward"]["paired_delta"] is None


def test_comparison_is_omitted_when_neither_side_exists(tmp_path: Path) -> None:
    """No rows at all for an arm we never ran: nothing to report, not a zero."""
    summary = report.build_summary(report.analyse(_ab_jobs(tmp_path)))
    assert not [c for c in summary["comparisons"] if "2c4g" in c["label"]]


def test_export_gap_flags_scored_trials_with_no_transcript(tmp_path: Path) -> None:
    """Scored but no trajectory and no transcript => health is UNKNOWN.

    The throttle detector reads the transcript and nothing else, so such a
    trial cannot be called clean. It must be excluded from headlines and
    surfaced as a blind spot.
    """
    jobs = tmp_path / "jobs"
    trial_dir = fixtures.write_trial(
        jobs, "main-glm-stream-light", "v1-light-05-S1__a", harness="claude-code",
        steps=HEALTHY, rewards={"x": 1.0}, transcript="fine",
    )
    # Second trial: reward only, no trajectory, no transcript.
    gap_dir = fixtures.write_trial(
        jobs, "main-glm-stream-light", "v1-light-05-S2__b", harness="claude-code",
        rewards={"x": 0.0},
    )
    assert not (gap_dir / "agent/trajectory.json").exists()
    assert trial_dir.exists()

    summary = report.build_summary(report.analyse(jobs))
    gap = summary["export_gap"]
    assert gap["n_affected"] == 1
    assert gap["n_without_any_transcript"] == 1
    assert gap["arms_affected"] == ["glm"]
    # and it is kept out of the clean cohort
    cell = next(c for c in summary["cells"] if c["cell"] == "glm-stream-light")
    assert cell["clean"]["n_trials"] == 1
    assert "R2-0" in report.render_markdown(summary, report.analyse(jobs))


def test_trials_csv_carries_round2_columns(tmp_path: Path, ) -> None:
    jobs = _ab_jobs(tmp_path)
    bundles = report.analyse(jobs)
    summary = report.build_summary(bundles)
    out = tmp_path / "out"
    report.write_outputs(out, bundles, summary)
    rows = list(csv.DictReader((out / "trials.csv").open()))
    row = rows[0]
    for field in (
        "setting", "variant", "conc_twmean", "conc_starved_frac", "conc_shape",
        "queue_mean_wait", "order_verdict", "failure_bucket", "sandbox_cores",
    ):
        assert field in row, field
    assert {r["setting"] for r in rows} == {"stream", "burst"}


def test_markdown_renders_all_round2_sections(tmp_path: Path) -> None:
    summary = report.build_summary(report.analyse(_ab_jobs(tmp_path)))
    text = report.render_markdown(summary, report.analyse(_ab_jobs(tmp_path)))
    for heading in ("R2-A.", "R2-B.", "R2-C/D.", "R2-E."):
        assert heading in text
