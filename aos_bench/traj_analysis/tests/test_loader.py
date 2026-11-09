"""Parsing contract tests.

These pin the schema assumptions that, if wrong, produce a report that is
plausible and wrong rather than one that crashes:
  * the ``-1`` cgroup sentinel must become ``None``, not ``0``;
  * the TSV must be parsed by header name so appended columns cannot shift it;
  * an in-flight or truncated trial must load without raising.
"""

from __future__ import annotations

from pathlib import Path

from aos_bench.traj_analysis import loader
from aos_bench.traj_analysis.tests import fixtures


def test_discovers_only_main_stream_jobs(tmp_path: Path) -> None:
    fixtures.write_trial(tmp_path, "main-codex-stream-light", "v1-light-05-aaa__X", steps=[])
    fixtures.write_trial(tmp_path, "scratch-smoke", "whatever__Y", steps=[])
    (tmp_path / "main-codex-stream-light" / "config.json").write_text("{}", encoding="utf-8")

    records = loader.discover_trials(tmp_path)
    assert [r.trial_name for r in records] == ["v1-light-05-aaa__X"]
    assert records[0].arm == "codex"
    assert records[0].tier == "light"


def test_tier_with_dash_is_parsed(tmp_path: Path) -> None:
    fixtures.write_trial(tmp_path, "main-codex-stream-heavy-plus", "v1-hplus-30-a__Z", steps=[])
    record = loader.discover_trials(tmp_path)[0]
    assert record.tier == "heavy-plus"
    assert record.arm == "codex"


def test_cgroup_minus_one_becomes_none_not_zero(tmp_path: Path) -> None:
    rows = [
        fixtures.cgroup_row(0, memory_current=100, memory_peak=110, cpu_usage_usec=0, pids_current=5),
        fixtures.cgroup_row(1, memory_current=200, memory_peak=210, cpu_usage_usec=1_000_000, pids_current=7),
    ]
    fixtures.write_trial(
        tmp_path, "main-codex-stream-light", "v1-light-05-aaa__X", steps=[], cgroup_rows=rows
    )
    record = loader.discover_trials(tmp_path)[0]
    assert len(record.cgroup) == 2
    # The whole point: cpu.stat columns are unavailable, not zero.
    assert record.cgroup[0].cpu_nr_periods is None
    assert record.cgroup[0].cpu_nr_throttled is None
    assert record.cgroup[1].cpu_usage_usec == 1_000_000
    assert record.cgroup[1].pids_current == 7


def test_cgroup_parsed_by_header_not_position(tmp_path: Path) -> None:
    """Prepending a column must not shift the parsed values."""
    header = "extra_new_col\t" + fixtures.CGROUP_HEADER
    row = "999\t" + fixtures.cgroup_row(
        0, memory_current=4242, memory_peak=4343, cpu_usage_usec=7, pids_current=3
    )
    trial = fixtures.write_trial(tmp_path, "main-codex-stream-light", "a__X", steps=[])
    (trial / "agent/.harbor-cgroup-resources.tsv").write_text(header + "\n" + row + "\n", encoding="utf-8")
    samples = loader.parse_cgroup_tsv(trial / "agent/.harbor-cgroup-resources.tsv")
    assert samples[0].memory_current == 4242
    assert samples[0].memory_peak == 4343


def test_in_flight_trial_loads_without_result(tmp_path: Path) -> None:
    fixtures.write_trial(
        tmp_path,
        "main-kimi-stream-medium",
        "v1-medium-10-aaa__X",
        harness="kimi-code",
        steps=[fixtures.step(1, "2026-09-19T07:43:08.971000+00:00", tool="Bash", arguments={"command": "ls"})],
        write_result=False,
    )
    record = loader.discover_trials(tmp_path)[0]
    assert record.is_in_flight is True
    assert record.has_result is False
    # Harness still resolvable from the trajectory alone.
    assert record.harness == "kimi-code"
    assert len(record.steps) == 1


def test_truncated_trajectory_records_load_error(tmp_path: Path) -> None:
    fixtures.write_trial(
        tmp_path, "main-glm-stream-light", "v1-light-05-aaa__X", corrupt_trajectory=True
    )
    record = loader.discover_trials(tmp_path)[0]
    assert record.trajectory is None
    assert "trajectory" in record.load_errors
    assert record.steps == []


def test_stderr_transcript_is_segregated(tmp_path: Path) -> None:
    fixtures.write_trial(
        tmp_path,
        "main-kimi-stream-light",
        "a__X",
        harness="kimi-code",
        steps=[],
        transcript="hello",
        stderr_transcript="world",
    )
    record = loader.discover_trials(tmp_path)[0]
    assert [p.name for p in record.transcripts] == ["kimi-code.txt"]
    assert [p.name for p in record.stderr_transcripts] == ["kimi-code.stderr.txt"]


def test_non_numeric_reward_is_reported_not_crashed(tmp_path: Path) -> None:
    fixtures.write_trial(
        tmp_path,
        "main-cc-stream-light",
        "a__X",
        steps=[],
        rewards={"good": 1.0, "bad": None},  # type: ignore[dict-item]
    )
    record = loader.discover_trials(tmp_path)[0]
    assert record.reward == {"good": 1.0}
    assert any(k.startswith("reward:") for k in record.load_errors)


def test_stream_id_groups_attempts(tmp_path: Path) -> None:
    for suffix in ("aaa", "bbb", "ccc"):
        fixtures.write_trial(tmp_path, "main-codex-stream-light", f"v1-light-05-S1__{suffix}", steps=[])
    records = loader.discover_trials(tmp_path)
    assert {loader.stream_id(r) for r in records} == {"v1-light-05-S1"}


# --- round 2: burst jobs, config probes, per-trial sandbox shape ------------


def test_burst_jobs_are_discovered_with_setting_label(tmp_path: Path) -> None:
    """Round 1's regex matched only ``-stream-`` and dropped every burst job.

    That is not "missing data" -- the jobs were never opened. This pins the
    setting slot so a burst arm cannot silently vanish from the report again.
    """
    fixtures.write_trial(tmp_path, "main-glm-burst-light", "v1-light-05-a__X", steps=[])
    fixtures.write_trial(tmp_path, "main-glm-stream-light", "v1-light-05-a__Y", steps=[])
    records = loader.discover_trials(tmp_path)
    by_setting = {r.setting: r for r in records}
    assert set(by_setting) == {"burst", "stream"}
    assert by_setting["burst"].tier == "light"
    assert by_setting["burst"].arm == "glm"


def test_config_probe_job_yields_tier_and_variant(tmp_path: Path) -> None:
    fixtures.write_trial(tmp_path, "cfg-glm-stream-heavy-2c4g", "v1-heavy-20-a__X", steps=[])
    (record,) = loader.discover_trials(tmp_path)
    assert (record.tier, record.variant) == ("heavy", "2c4g")
    assert record.cell == "glm-stream-heavy-2c4g"


def test_heavy_plus_is_not_split_into_heavy_plus_variant() -> None:
    """``heavy-plus`` is a tier, not ``heavy`` with variant ``plus``."""
    assert loader.split_tier_variant("heavy-plus") == ("heavy-plus", "")
    assert loader.split_tier_variant("heavy-2c4g") == ("heavy", "2c4g")
    assert loader.split_tier_variant("heavy-plus-2c4g") == ("heavy-plus", "2c4g")


def test_unknown_tier_is_kept_whole_not_swallowed() -> None:
    assert loader.split_tier_variant("colossal") == ("colossal", "")


def test_sandbox_shape_is_read_per_trial_not_assumed(tmp_path: Path) -> None:
    """The 2c4g probe must report 2 cores / 4 GiB, not the 4c8g grid default.

    Hardcoding 4 cores would halve every utilisation figure for this job and
    make the smaller sandbox look *less* loaded than the bigger one.
    """
    rows = [
        fixtures.cgroup_row(
            i, memory_current=1 << 30, memory_peak=1 << 30,
            cpu_usage_usec=i * 1_000_000, pids_current=10,
            cpu_quota_usec=200000, memory_max=4294967296,
        )
        for i in range(3)
    ]
    trial_dir = fixtures.write_trial(
        tmp_path, "cfg-glm-stream-heavy-2c4g", "a__X", steps=[], cgroup_rows=rows
    )
    record = loader.load_trial(
        trial_dir, job="cfg-glm-stream-heavy-2c4g", arm="glm", tier="heavy", variant="2c4g"
    )
    assert record.sandbox_cores == 2.0
    assert record.sandbox_memory_bytes == 4294967296


def test_sandbox_shape_ignores_minus_one_samples(tmp_path: Path) -> None:
    """Some samples carry -1 for cpu.max; the modal real value must win."""
    rows = [
        fixtures.cgroup_row(
            0, memory_current=1 << 30, memory_peak=1 << 30, cpu_usage_usec=0,
            pids_current=1, cpu_quota_usec=-1, memory_max=8589934592,
        ),
        fixtures.cgroup_row(
            1, memory_current=1 << 30, memory_peak=1 << 30, cpu_usage_usec=1,
            pids_current=1, cpu_quota_usec=400000, memory_max=8589934592,
        ),
        fixtures.cgroup_row(
            2, memory_current=1 << 30, memory_peak=1 << 30, cpu_usage_usec=2,
            pids_current=1, cpu_quota_usec=400000, memory_max=8589934592,
        ),
    ]
    trial_dir = fixtures.write_trial(
        tmp_path, "main-glm-stream-heavy", "a__X", steps=[], cgroup_rows=rows
    )
    record = loader.load_trial(trial_dir, job="main-glm-stream-heavy", arm="glm", tier="heavy")
    assert record.sandbox_cores == 4.0


def test_sandbox_shape_falls_back_to_grid_default(tmp_path: Path) -> None:
    trial_dir = fixtures.write_trial(tmp_path, "main-glm-stream-heavy", "a__X", steps=[])
    record = loader.load_trial(trial_dir, job="main-glm-stream-heavy", arm="glm", tier="heavy")
    assert record.sandbox_cores == loader.SANDBOX_CORES
    assert record.sandbox_memory_bytes == loader.SANDBOX_MEMORY_BYTES
