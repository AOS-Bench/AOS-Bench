from __future__ import annotations

from pathlib import Path

import pytest

from aos_bench.monitor.resources import (
    TrialContainerSampler,
    _container_sample_from_dict,
    _parse_percent,
    _parse_ps_output,
    _read_latest_cgroup_resources,
)
from aos_bench.monitor.state import ContainerSample, MonitorState


def test_parse_percent() -> None:
    assert _parse_percent("12.5%") == 12.5
    assert _parse_percent("") == 0.0
    assert _parse_percent("bad") == 0.0


def test_read_latest_cgroup_resources_ignores_partial_row(tmp_path: Path) -> None:
    path = tmp_path / ".harbor-cgroup-resources.tsv"
    path.write_text(
        "sample\tmemory_current\tcpu_nr_periods\n"
        "0\t1048576\t10\n"
        "1\t2097152\t20\n"
        "2\t",
        encoding="utf-8",
    )
    assert _read_latest_cgroup_resources(path) == {
        "sample": "1",
        "memory_current": "2097152",
        "cpu_nr_periods": "20",
    }


def test_legacy_container_sample_replay_defaults_extended_fields() -> None:
    sample = _container_sample_from_dict(
        {
            "ts": 1,
            "cpu_percent": 2,
            "mem_used_mb": 3,
            "mem_limit_mb": 4,
            "mem_percent": 75,
            "net_rx_mb": 5,
            "net_tx_mb": 6,
        }
    )
    assert sample.memory_peak_mb is None
    assert sample.cpu_throttled_period_percent is None


def test_merge_cgroup_resources_and_derive_throttling(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trial"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True)
    path = agent_dir / ".harbor-cgroup-resources.tsv"
    header = (
        "sample\tmemory_current\tmemory_max\tmemory_peak\tswap_current\tswap_max\t"
        "mem_low\tmem_high\tmem_max\tmem_oom\tmem_oom_kill\tmem_oom_group_kill\t"
        "cpu_usage_usec\tcpu_user_usec\tcpu_system_usec\tcpu_nr_periods\t"
        "cpu_nr_throttled\tcpu_throttled_usec\tcpu_quota_usec\tcpu_period_usec\t"
        "io_rbytes\tio_wbytes\tio_rios\tio_wios\tpids_current\tpids_max\t"
        "pids_events_max\tcpu_some_avg10\tcpu_full_avg10\tmemory_some_avg10\t"
        "memory_full_avg10\tio_some_avg10\tio_full_avg10\tfs_size_kb\t"
        "fs_used_kb\tfs_available_kb\tfs_used_percent\n"
    )
    first = (
        "0\t1048576\t8388608\t2097152\t0\tmax\t0\t1\t2\t0\t0\t0\t"
        "3000000\t2000000\t1000000\t100\t10\t500000\t400000\t100000\t"
        "10485760\t5242880\t10\t5\t7\t100\t0\t1.5\t0\t2.5\t0.5\t3.5\t"
        "1.0\t102400\t51200\t51200\t50\n"
    )
    path.write_text(header + first, encoding="utf-8")
    sampler = TrialContainerSampler(
        trial_id="trial", trial_dir=trial_dir, state=MonitorState(jobs_dir="x")
    )
    sample = ContainerSample(1, 2, 3, 4, 75, 5, 6)
    sampler._merge_cgroup_resources(sample)
    assert sample.memory_peak_mb == pytest.approx(2)
    assert sample.swap_limit_mb is None
    assert sample.cpu_usage_s == pytest.approx(3)
    assert sample.io_read_mb == pytest.approx(10)
    assert sample.pids_current == 7
    assert sample.memory_pressure_some_avg10 == pytest.approx(2.5)
    assert sample.fs_used_mb == pytest.approx(50)

    second = first.replace("\t100\t10\t500000\t", "\t120\t16\t700000\t")
    path.write_text(header + first + second, encoding="utf-8")
    sample2 = ContainerSample(2, 2, 3, 4, 75, 5, 6)
    sampler._merge_cgroup_resources(sample2)
    assert sample2.cpu_throttled_period_percent == pytest.approx(30)


def test_parse_ps_output_drops_sampler_process() -> None:
    rows = _parse_ps_output(
        "1 0 1.5 2048 120 node node /usr/bin/claude\n"
        "42 1 8.0 102400 30 python python worker.py\n"
        "99 1 0.0 512 0 ps ps -eo pid=,ppid=,pcpu=,rss=\n"
    )
    assert [row.pid for row in rows] == [1, 42]
    assert rows[1].mem_rss_mb == pytest.approx(100.0)
    assert rows[1].args == "python worker.py"


def test_container_pattern_uses_resolved_native_trial_name(tmp_path: Path) -> None:
    native = tmp_path / "heavy-20-scan-001__AbC123"
    native.mkdir()
    link = tmp_path / "claude-glm52--heavy--heavy-20-scan-001__AbC123"
    link.symlink_to(native, target_is_directory=True)
    sampler = TrialContainerSampler(
        trial_id=link.name,
        trial_dir=link,
        state=MonitorState(jobs_dir=str(tmp_path)),
    )
    assert sampler.expected_container_pattern == "heavy-20-scan-001__abc123-main"


def test_claude_processes_are_marked_shared_runtime(tmp_path: Path) -> None:
    state = MonitorState(jobs_dir=str(tmp_path))
    state.ensure_trial("t1", str(tmp_path / "t1"), agent="claude-code")
    state.ensure_run("t1", "run_root_001", role="root", agent_name="claude-code")
    state.ensure_run(
        "t1",
        "run_sub_a123456789",
        role="subagent",
        agent_name="claude-code/worker",
        parent_run_id="run_root_001",
    )
    processes = _parse_ps_output("10 1 20.0 204800 60 node node /usr/bin/claude\n")
    sampler = TrialContainerSampler(
        trial_id="t1",
        trial_dir=tmp_path / "t1",
        state=state,
        agent_kind="claude-code",
    )
    assert sampler._attribute_processes(processes) == "shared_runtime"
    assert processes[0].run_id is None
    assert processes[0].attribution == "shared_runtime"


def test_native_id_attribution_propagates_to_child(tmp_path: Path) -> None:
    state = MonitorState(jobs_dir=str(tmp_path))
    state.ensure_trial("t1", str(tmp_path / "t1"), agent="codex")
    state.ensure_run("t1", "run_root_001", role="root", agent_name="codex")
    state.ensure_run(
        "t1",
        "run_sub_019ef9a5-9f77-1234",
        role="subagent",
        agent_name="codex/worker",
        parent_run_id="run_root_001",
    )
    processes = _parse_ps_output(
        "10 1 10.0 204800 60 codex codex worker 019ef9a5-9f77-1234\n"
        "11 10 5.0 10240 50 bash bash -lc make\n"
    )
    sampler = TrialContainerSampler(
        trial_id="t1",
        trial_dir=tmp_path / "t1",
        state=state,
        agent_kind="codex",
    )
    assert sampler._attribute_processes(processes) == "native_id+pid_tree"
    assert processes[0].run_id == "run_sub_019ef9a5-9f77-1234"
    assert processes[1].run_id == "run_sub_019ef9a5-9f77-1234"
    assert processes[1].attribution == "pid_tree"
