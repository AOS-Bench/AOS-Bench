"""Host-level resource sampler. V0 is intentionally toy.

We sample whole-host CPU + memory via psutil and push samples to MonitorState.
For per-container attribution we use ``docker stats`` (see
``TrialContainerSampler`` below), since the docker proxy on this host means
container PIDs aren't visible from the host via psutil.

Per-trial samples are also persisted to ``<trial_dir>/.monitor-resources.jsonl``
so a fresh monitor pointed at a completed trial can replay the full curve.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path

import psutil

from aos_bench.monitor.state import (
    ContainerSample,
    MonitorState,
    ProcessInfo,
    ProcessSnapshot,
    ResourceSample,
)


class HostResourceSampler:
    def __init__(self, *, state: MonitorState, interval_s: float = 2.0) -> None:
        self.state = state
        self.interval_s = max(0.5, float(interval_s))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            # Prime psutil.cpu_percent so the first reading isn't 0.0.
            psutil.cpu_percent(interval=None)
            self._task = asyncio.create_task(self._loop(), name="resource-sampler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample_once()
            except Exception:
                # Resource sampling must not crash the monitor.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    def _sample_once(self) -> None:
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        sample = ResourceSample(
            ts=time.time(),
            cpu_percent=float(cpu),
            mem_percent=float(vm.percent),
            mem_used_mb=int(vm.used / 1024 / 1024),
        )
        self.state.add_resource_sample(sample)


# ── per-trial container sampler ─────────────────────────────────────────


# Parse strings like "256MiB", "1.5GiB", "42.91MiB / 16GiB".
_SIZE_PAT = re.compile(r"([\d.]+)\s*([a-zA-Z]+)")
_UNIT_MB = {
    "b": 1 / (1024 * 1024),
    "kb": 1 / 1024,
    "kib": 1 / 1024,
    "mb": 1.0,
    "mib": 1.0,
    "gb": 1024.0,
    "gib": 1024.0,
    "tb": 1024.0 * 1024,
    "tib": 1024.0 * 1024,
}


def _parse_size_mb(s: str) -> float:
    """Convert a ``docker stats``-style size string to MiB. Returns 0 if unparseable."""
    if not s:
        return 0.0
    m = _SIZE_PAT.search(s)
    if not m:
        return 0.0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0.0
    unit = m.group(2).lower()
    return n * _UNIT_MB.get(unit, 1.0)


def _parse_percent(s: str) -> float:
    if not s:
        return 0.0
    try:
        return float(s.rstrip("%").strip())
    except ValueError:
        return 0.0


def _parse_optional_number(value: str | None, *, integer: bool = False):
    """Parse one cgroup scalar, treating ``max``/negative/missing as unknown."""
    if value is None or value in {"", "max"}:
        return None
    try:
        parsed = int(value) if integer else float(value)
    except ValueError:
        return None
    return None if parsed < 0 else parsed


def _read_latest_cgroup_resources(path: Path) -> dict[str, str]:
    """Read the latest complete row from Harbor's extended cgroup TSV.

    Only the header and final 64 KiB are read, so polling cost stays constant
    for multi-hour trials. A concurrently-written partial final row is ignored.
    """
    try:
        with path.open("rb") as handle:
            header_raw = handle.readline()
            header_end = handle.tell()
            handle.seek(0, 2)
            end = handle.tell()
            if end <= header_end:
                return {}
            start = max(header_end, end - 65536)
            handle.seek(start)
            tail = handle.read()
    except OSError:
        return {}
    try:
        header = header_raw.decode("utf-8", "replace").rstrip("\r\n").split("\t")
        text = tail.decode("utf-8", "replace")
    except (AttributeError, UnicodeError):
        return {}
    lines = text.splitlines()
    if start > header_end and lines:
        lines = lines[1:]
    for line in reversed(lines):
        values = line.split("\t")
        if len(values) == len(header):
            return dict(zip(header, values, strict=True))
    return {}


def _container_sample_from_dict(data: dict[str, object]) -> ContainerSample:
    """Replay current and legacy resource JSONL without schema migrations."""
    required = {
        "ts": float(data.get("ts", 0)),
        "cpu_percent": float(data.get("cpu_percent", 0)),
        "mem_used_mb": float(data.get("mem_used_mb", 0)),
        "mem_limit_mb": float(data.get("mem_limit_mb", 0)),
        "mem_percent": float(data.get("mem_percent", 0)),
        "net_rx_mb": float(data.get("net_rx_mb", 0)),
        "net_tx_mb": float(data.get("net_tx_mb", 0)),
    }
    known = ContainerSample.__dataclass_fields__.keys()
    extras = {
        key: value
        for key, value in data.items()
        if key in known and key not in required
    }
    return ContainerSample(**required, **extras)


def _parse_ps_output(output: str) -> list[ProcessInfo]:
    """Parse ``ps -eo pid,ppid,pcpu,rss,etimes,comm,args`` output.

    ``args`` is intentionally the final, unsplit field. Malformed rows and the
    sampler's own short-lived ``ps`` process are ignored.
    """
    processes: list[ProcessInfo] = []
    for raw_line in output.splitlines():
        parts = raw_line.strip().split(None, 6)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            cpu_percent = float(parts[2])
            rss_mb = float(parts[3]) / 1024.0
            elapsed_s = int(parts[4])
        except ValueError:
            continue
        command = parts[5]
        args = parts[6] if len(parts) > 6 else command
        if command == "ps" and "-eo" in args:
            continue
        processes.append(
            ProcessInfo(
                pid=pid,
                ppid=ppid,
                cpu_percent=cpu_percent,
                mem_rss_mb=rss_mb,
                elapsed_s=elapsed_s,
                command=command,
                args=args,
            )
        )
    return processes


class TrialContainerSampler:
    """Periodically call ``docker stats <container>`` and push samples.

    Container name discovery: harbor names trial containers
    ``<trial_id_lowercased>-main-1``. We look it up by name on each tick (it
    may not exist yet during early trial setup; we just skip until it shows
    up, and stop on its own when the container goes away).
    """

    def __init__(
        self,
        *,
        trial_id: str,
        state: MonitorState,
        interval_s: float = 3.0,
        trial_dir: Path | None = None,
        agent_kind: str | None = None,
    ) -> None:
        self.trial_id = trial_id
        self.state = state
        self.interval_s = max(1.0, float(interval_s))
        self.trial_dir = Path(trial_dir) if trial_dir is not None else None
        self.agent_kind = agent_kind or "unknown"
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Cache the resolved container name once we find it; saves one
        # docker-ps call per tick.
        self._container_name: str | None = None
        self._previous_cgroup_cpu: tuple[int, int] | None = None

    @property
    def expected_container_pattern(self) -> str:
        # Public monitor IDs may be qualified by a symlink-view prefix such as
        # ``claude-glm52--stream--native__hash``. Docker Compose uses Harbor's
        # native trial directory name, so resolve the link before matching.
        native_trial_id = self.trial_id
        if self.trial_dir is not None:
            try:
                native_trial_id = self.trial_dir.resolve().name
            except OSError:
                native_trial_id = self.trial_dir.name
        return f"{native_trial_id.lower()}-main"

    @property
    def samples_log_path(self) -> Path | None:
        """Per-trial JSONL log of historical samples. Lives inside the trial
        dir so it travels with the run and survives monitor restarts."""
        if self.trial_dir is None:
            return None
        return self.trial_dir / ".monitor-resources.jsonl"

    @property
    def processes_log_path(self) -> Path | None:
        """Process snapshots persisted separately from container curves."""
        if self.trial_dir is None:
            return None
        return self.trial_dir / ".monitor-processes.jsonl"

    async def start(self) -> None:
        # First, preload any previously persisted samples into state so a
        # restart of the monitor (or pointing at a completed trial) shows
        # the full historical curve immediately.
        self._preload_existing_samples()
        self._preload_existing_processes()
        if self._task is None and shutil.which("docker"):
            self._task = asyncio.create_task(self._loop(), name=f"docker-stats-{self.trial_id}")

    def _preload_existing_samples(self) -> None:
        path = self.samples_log_path
        if path is None or not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    sample = _container_sample_from_dict(d)
                except (TypeError, ValueError):
                    continue
                self.state.add_trial_resource_sample(self.trial_id, sample)
        except OSError:
            return

    def _persist_sample(self, sample: ContainerSample) -> None:
        path = self.samples_log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            # Persistence is best-effort; in-memory state is still updated.
            pass

    def _preload_existing_processes(self) -> None:
        path = self.processes_log_path
        if path is None or not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    data = json.loads(line)
                    processes = [ProcessInfo(**row) for row in data.get("processes", [])]
                    snapshot = ProcessSnapshot(
                        ts=float(data["ts"]),
                        container_name=str(data.get("container_name", "")),
                        attribution_mode=str(data.get("attribution_mode", "unknown")),
                        processes=processes,
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
                self.state.add_trial_process_snapshot(self.trial_id, snapshot)
        except OSError:
            return

    def _persist_process_snapshot(self, snapshot: ProcessSnapshot) -> None:
        path = self.processes_log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot.to_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        missing_after_terminal = 0
        while not self._stop.is_set():
            try:
                sampled = await self._sample_once()
            except Exception:
                sampled = False
            if sampled:
                missing_after_terminal = 0
            else:
                trial = self.state.trials.get(self.trial_id)
                if trial is not None and trial.status in {
                    "completed", "failed", "killed", "crashed"
                }:
                    missing_after_terminal += 1
                    # A replayed/finished trial has no live container. Avoid
                    # issuing docker ps forever (63 replays previously meant
                    # 63 process launches every three seconds).
                    if missing_after_terminal >= 2:
                        return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    async def _resolve_container_name(self) -> str | None:
        if self._container_name is not None:
            return self._container_name
        # docker ps --filter "name=<pattern>" --format "{{.Names}}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps",
                "--filter", f"name={self.expected_container_pattern}",
                "--format", "{{.Names}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            # The host Docker proxy adds several seconds of RPC latency even
            # for a tiny `ps` command. Keep this above the observed ~5s floor;
            # sampling is best-effort and runs outside the agent container.
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except (asyncio.TimeoutError, OSError):
            return None
        names = [n for n in out.decode("utf-8", "replace").splitlines() if n.strip()]
        if not names:
            return None
        # Prefer the -main-1 suffix if multiple match.
        for n in names:
            if n.endswith("-main-1"):
                self._container_name = n
                return n
        self._container_name = names[0]
        return self._container_name

    async def _sample_once(self) -> bool:
        name = await self._resolve_container_name()
        if name is None:
            return False
        # docker stats --no-stream --format "{{json .}}" <name>
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "stats", "--no-stream",
                "--format", "{{json .}}",
                name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except (asyncio.TimeoutError, OSError):
            return False
        line = out.decode("utf-8", "replace").strip()
        if not line:
            # Container went away — forget the cached name so next tick re-resolves.
            self._container_name = None
            return False
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            return False
        # docker stats fields (varies by version):
        #   CPUPerc, MemUsage ("42.91MiB / 16GiB"), MemPerc, NetIO ("25.9kB / 47kB"), Name
        cpu = _parse_percent(row.get("CPUPerc", ""))
        mem_use_raw, _sep, mem_lim_raw = row.get("MemUsage", "").partition("/")
        mem_use_mb = _parse_size_mb(mem_use_raw)
        mem_lim_mb = _parse_size_mb(mem_lim_raw)
        mem_pct = _parse_percent(row.get("MemPerc", ""))
        net_rx_raw, _sep, net_tx_raw = row.get("NetIO", "").partition("/")
        net_rx_mb = _parse_size_mb(net_rx_raw)
        net_tx_mb = _parse_size_mb(net_tx_raw)
        block_read_raw, _sep, block_write_raw = row.get("BlockIO", "").partition("/")
        block_read_mb = _parse_size_mb(block_read_raw)
        block_write_mb = _parse_size_mb(block_write_raw)
        try:
            pids = int(row.get("PIDs", 0))
        except (TypeError, ValueError):
            pids = None
        sample = ContainerSample(
            ts=time.time(),
            cpu_percent=cpu,
            mem_used_mb=mem_use_mb,
            mem_limit_mb=mem_lim_mb,
            mem_percent=mem_pct,
            net_rx_mb=net_rx_mb,
            net_tx_mb=net_tx_mb,
            block_read_mb=block_read_mb,
            block_write_mb=block_write_mb,
            pids=pids,
        )
        self._merge_cgroup_resources(sample)
        self.state.add_trial_resource_sample(self.trial_id, sample)
        self._persist_sample(sample)
        process_snapshot = await self._sample_processes(name, sample.ts)
        if process_snapshot is not None:
            self.state.add_trial_process_snapshot(self.trial_id, process_snapshot)
            self._persist_process_snapshot(process_snapshot)
        return True

    @property
    def cgroup_resources_path(self) -> Path | None:
        if self.trial_dir is None:
            return None
        return self.trial_dir / "agent" / ".harbor-cgroup-resources.tsv"

    def _merge_cgroup_resources(self, sample: ContainerSample) -> None:
        """Merge Harbor's controller-level telemetry into a Docker sample."""
        path = self.cgroup_resources_path
        if path is None:
            return
        row = _read_latest_cgroup_resources(path)
        if not row:
            return

        def integer(key: str):
            return _parse_optional_number(row.get(key), integer=True)

        def number(key: str):
            return _parse_optional_number(row.get(key))

        def mib(key: str):
            value = number(key)
            return None if value is None else value / 1024 / 1024

        def kib_to_mib(key: str):
            value = number(key)
            return None if value is None else value / 1024

        sample.cgroup_mem_current_mb = mib("memory_current")
        sample.cgroup_mem_limit_mb = mib("memory_max")
        sample.memory_peak_mb = mib("memory_peak")
        sample.swap_used_mb = mib("swap_current")
        sample.swap_limit_mb = mib("swap_max")
        sample.mem_events_low = integer("mem_low")
        sample.mem_events_high = integer("mem_high")
        sample.mem_events_max = integer("mem_max")
        sample.mem_events_oom = integer("mem_oom")
        sample.mem_events_oom_kill = integer("mem_oom_kill")
        sample.mem_events_oom_group_kill = integer("mem_oom_group_kill")
        usage_usec = number("cpu_usage_usec")
        user_usec = number("cpu_user_usec")
        system_usec = number("cpu_system_usec")
        throttled_usec = number("cpu_throttled_usec")
        sample.cpu_usage_s = None if usage_usec is None else usage_usec / 1_000_000
        sample.cpu_user_s = None if user_usec is None else user_usec / 1_000_000
        sample.cpu_system_s = None if system_usec is None else system_usec / 1_000_000
        sample.cpu_nr_periods = integer("cpu_nr_periods")
        sample.cpu_nr_throttled = integer("cpu_nr_throttled")
        sample.cpu_throttled_s = (
            None if throttled_usec is None else throttled_usec / 1_000_000
        )
        if sample.cpu_nr_periods is not None and sample.cpu_nr_throttled is not None:
            current = (sample.cpu_nr_periods, sample.cpu_nr_throttled)
            if self._previous_cgroup_cpu is not None:
                delta_periods = current[0] - self._previous_cgroup_cpu[0]
                delta_throttled = current[1] - self._previous_cgroup_cpu[1]
                if delta_periods > 0 and delta_throttled >= 0:
                    sample.cpu_throttled_period_percent = (
                        100.0 * delta_throttled / delta_periods
                    )
            self._previous_cgroup_cpu = current
        sample.cpu_quota_us = integer("cpu_quota_usec")
        sample.cpu_period_us = integer("cpu_period_usec")
        sample.io_read_mb = mib("io_rbytes")
        sample.io_write_mb = mib("io_wbytes")
        sample.io_read_ops = integer("io_rios")
        sample.io_write_ops = integer("io_wios")
        sample.pids_current = integer("pids_current")
        sample.pids_limit = integer("pids_max")
        sample.pids_events_max = integer("pids_events_max")
        sample.cpu_pressure_some_avg10 = number("cpu_some_avg10")
        sample.cpu_pressure_full_avg10 = number("cpu_full_avg10")
        sample.memory_pressure_some_avg10 = number("memory_some_avg10")
        sample.memory_pressure_full_avg10 = number("memory_full_avg10")
        sample.io_pressure_some_avg10 = number("io_some_avg10")
        sample.io_pressure_full_avg10 = number("io_full_avg10")
        sample.fs_size_mb = kib_to_mib("fs_size_kb")
        sample.fs_used_mb = kib_to_mib("fs_used_kb")
        sample.fs_available_mb = kib_to_mib("fs_available_kb")
        sample.fs_used_percent = number("fs_used_percent")

    async def _sample_processes(
        self, container_name: str, ts: float
    ) -> ProcessSnapshot | None:
        """Capture a process table and attach conservative run attribution."""
        commands = (
            # Primary path: Docker's top API is fast through the host proxy
            # and does not require procps to be installed in the image.
            (
                8.0,
                "docker", "top", container_name,
                "-eo", "pid,ppid,pcpu,rss,etimes,comm,args",
            ),
            # Fallback: namespace-local PIDs when docker top is unavailable.
            (
                15.0,
                "docker", "exec", container_name, "ps", "-eo",
                "pid=,ppid=,pcpu=,rss=,etimes=,comm=,args=",
            ),
        )
        processes: list[ProcessInfo] = []
        for timeout_s, *command in commands:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_s
                )
            except (asyncio.TimeoutError, OSError):
                continue
            if proc.returncode != 0:
                continue
            processes = _parse_ps_output(out.decode("utf-8", "replace"))
            if processes:
                break
        if not processes:
            return None
        mode = self._attribute_processes(processes)
        # Bound each snapshot. Sorting keeps the most resource-relevant rows
        # if a pathological task creates thousands of processes.
        processes.sort(key=lambda p: (p.mem_rss_mb, p.cpu_percent), reverse=True)
        return ProcessSnapshot(
            ts=ts,
            container_name=container_name,
            attribution_mode=mode,
            processes=processes[:256],
        )

    def _attribute_processes(self, processes: list[ProcessInfo]) -> str:
        """Best-effort PID/run linkage without inventing false precision.

        Codex/Kimi child commands may contain a native run identifier; when
        present, match it and propagate the run to descendants through PPID.
        Claude Code sidechains normally share one Node runtime, so those rows
        are explicitly marked ``shared_runtime`` rather than assigned to an
        arbitrary subagent.
        """
        trial = self.state.trials.get(self.trial_id)
        runs = trial.runs if trial is not None else {}
        for process in processes:
            args = process.args.lower()
            for run_id in runs:
                native_id = run_id.removeprefix("run_sub_").lower()
                if len(native_id) >= 8 and native_id in args:
                    process.run_id = run_id
                    process.attribution = "native_id"
                    break
            if (
                process.run_id is None
                and self.agent_kind == "claude-code"
                and ("claude" in args or process.command.lower() in {"node", "claude"})
            ):
                process.attribution = "shared_runtime"

        by_pid = {process.pid: process for process in processes}
        changed = True
        while changed:
            changed = False
            for process in processes:
                if process.run_id is not None:
                    continue
                parent = by_pid.get(process.ppid)
                if parent is not None and parent.run_id is not None:
                    process.run_id = parent.run_id
                    process.attribution = "pid_tree"
                    changed = True

        if self.agent_kind == "claude-code":
            return "shared_runtime"
        if any(process.run_id for process in processes):
            return "native_id+pid_tree"
        return "process_only"
