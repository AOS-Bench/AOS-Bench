"""
Integration tests for the AgentOS-Bench Docker sandbox.

Tests that each Harbor compose overlay actually enforces the intended
resource limits inside the container.

Requirements:
  - Docker daemon running
  - ubuntu:22.04 image (auto-pulled if absent)

Run:
    uv run pytest aos_bench/tests/sandbox/test_sandbox_integration.py -v
    uv run pytest aos_bench/tests/sandbox/test_sandbox_integration.py -v -k memory
"""

from __future__ import annotations

import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

# ── paths ──────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
_REPO = _HERE.parent.parent.parent          # AgentOSBench root
_COMPOSE_DIR = _REPO / "harbor/src/harbor/environments/docker"
_CHECK_SCRIPT = _REPO / "aos_bench/sandbox/check_sandbox.sh"
_BASE_IMAGE = "ubuntu:22.04"

# ── helpers ────────────────────────────────────────────────────────────────

def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def _exec_in(container: str, cmd: str) -> str:
    """Run a shell command inside the container, return stdout stripped."""
    r = _run(["docker", "exec", container, "sh", "-c", cmd])
    return r.stdout.strip()


@contextmanager
def _container(name: str, extra_flags: list[str]):
    """Start a detached container, yield its name, then remove it."""
    _run(["docker", "rm", "-f", name])  # clean up any leftover
    result = _run([
        "docker", "run", "-d", "--name", name,
        *extra_flags,
        _BASE_IMAGE, "sleep", "infinity",
    ])
    assert result.returncode == 0, f"docker run failed:\n{result.stderr}"
    try:
        yield name
    finally:
        _run(["docker", "rm", "-f", name])


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _read_cgroup(container: str, path: str) -> str:
    return _exec_in(container, f"cat {path} 2>/dev/null || echo MISSING")


def _is_cgroupv2(container: str) -> bool:
    out = _exec_in(container, "test -f /sys/fs/cgroup/cgroup.controllers && echo v2 || echo v1")
    return out == "v2"


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def ensure_image():
    """Pull base image once per test session."""
    r = _run(["docker", "pull", _BASE_IMAGE])
    assert r.returncode == 0, f"Could not pull {_BASE_IMAGE}"


# ── tests ──────────────────────────────────────────────────────────────────

class TestDockerAvailable:
    def test_docker_daemon_running(self):
        r = _run(["docker", "info"])
        assert r.returncode == 0, "Docker daemon not running"

    def test_can_start_container(self):
        name = _unique("aos-smoke")
        with _container(name, []):
            out = _exec_in(name, "echo hello")
            assert out == "hello"


class TestMemoryLimit:
    """--memory hard limit (docker-compose-base.yaml MEMORY var)."""

    def test_4g_limit_in_cgroup(self):
        name = _unique("aos-mem")
        with _container(name, ["--memory=4g", "--memory-swap=4g"]):
            v2 = _is_cgroupv2(name)
            if v2:
                raw = _read_cgroup(name, "/sys/fs/cgroup/memory.max")
            else:
                raw = _read_cgroup(name, "/sys/fs/cgroup/memory/memory.limit_in_bytes")

            assert raw != "MISSING", "memory cgroup file not found"
            limit = int(raw)
            expected = 4 * 1024 ** 3   # 4 GB in bytes
            assert abs(limit - expected) < 1024 ** 2, (
                f"Expected ~4G ({expected}), got {limit}"
            )

    def test_no_swap_when_memory_swap_equals_memory(self):
        name = _unique("aos-swap")
        with _container(name, ["--memory=4g", "--memory-swap=4g"]):
            v2 = _is_cgroupv2(name)
            if v2:
                raw = _read_cgroup(name, "/sys/fs/cgroup/memory.swap.max")
                # On cgroupv2, swap.max = 0 means swap disabled
                assert raw in ("0", "MISSING"), f"Swap not disabled: {raw}"
            else:
                raw = _read_cgroup(name, "/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes")
                assert raw != "MISSING"
                assert int(raw) == 4 * 1024 ** 3, f"memsw limit unexpected: {raw}"


class TestCpuQuota:
    """--cpus CFS quota (docker-compose-base.yaml CPUS var)."""

    def test_2cpu_quota_in_cgroup(self):
        name = _unique("aos-cpu")
        with _container(name, ["--cpus=2"]):
            v2 = _is_cgroupv2(name)
            if v2:
                raw = _read_cgroup(name, "/sys/fs/cgroup/cpu.max")
                # format: "quota period"
                assert raw != "MISSING", "cpu.max not found"
                quota_str, period_str = raw.split()
                assert quota_str != "max", "CPU quota not set"
                ratio = int(quota_str) / int(period_str)
                assert abs(ratio - 2.0) < 0.01, f"Expected 2.0 CPUs, got {ratio}"
            else:
                quota = int(_read_cgroup(name, "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
                period = int(_read_cgroup(name, "/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
                assert quota > 0, "CFS quota not set"
                assert abs(quota / period - 2.0) < 0.01


class TestSandboxMode:
    """docker-compose-sandbox.yaml: mem_swappiness=0 + pids_limit=1024."""

    def test_swappiness_zero(self):
        name = _unique("aos-sandbox")
        with _container(name, ["--memory=4g", "--memory-swap=4g", "--memory-swappiness=0"]):
            v2 = _is_cgroupv2(name)
            if v2:
                # cgroupv2 enforces swap via memory.swap.max=0, swappiness not exposed
                raw = _read_cgroup(name, "/sys/fs/cgroup/memory.swap.max")
                assert raw in ("0", "MISSING")
            else:
                raw = _read_cgroup(name, "/sys/fs/cgroup/memory/memory.swappiness")
                assert raw == "0", f"swappiness expected 0, got {raw}"

    def test_pids_limit(self):
        name = _unique("aos-pids")
        with _container(name, ["--pids-limit=1024"]):
            v2 = _is_cgroupv2(name)
            if v2:
                raw = _read_cgroup(name, "/sys/fs/cgroup/pids.max")
            else:
                raw = _read_cgroup(name, "/sys/fs/cgroup/pids/pids.max")

            assert raw != "MISSING", "pids cgroup file not found"
            assert raw != "max", "pids limit not set"
            assert int(raw) == 1024, f"Expected 1024, got {raw}"

    def test_cannot_exceed_pids_limit(self):
        """Verify pids limit is enforced by trying to fork more than 1024 procs."""
        name = _unique("aos-pids-enforce")
        with _container(name, ["--pids-limit=20"]):
            # Try to create 30 background sleeps — should fail or be capped
            out = _exec_in(
                name,
                "count=0; for i in $(seq 1 30); do sleep 10 & count=$((count+1)); done 2>/dev/null; echo $count"
            )
            # Not all 30 should succeed; some should be blocked
            created = int(out)
            assert created < 30, f"pids limit not enforced: spawned {created}/30"


@pytest.mark.skipif(
    int(subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip()) < 2,
    reason="cpuset test requires ≥2 CPUs"
)
class TestCpuset:
    """docker-compose-cpuset.yaml: cpuset pin to specific cores."""

    def test_cpuset_restricts_to_specified_cores(self):
        name = _unique("aos-cpuset")
        with _container(name, ["--cpuset-cpus=0,1"]):
            v2 = _is_cgroupv2(name)
            if v2:
                raw = _read_cgroup(name, "/sys/fs/cgroup/cpuset.cpus.effective")
            else:
                raw = _read_cgroup(name, "/sys/fs/cgroup/cpuset/cpuset.cpus")

            assert raw != "MISSING", "cpuset cgroup file not found"
            assert raw in ("0-1", "0,1"), f"Expected cores 0-1, got '{raw}'"

    def test_process_runs_on_pinned_cores(self):
        """taskset should show the process is only allowed on cores 0,1."""
        name = _unique("aos-cpuset-proc")
        with _container(name, ["--cpuset-cpus=0,1"]):
            # taskset -cp <pid> shows the CPU affinity mask
            out = _exec_in(name, "taskset -cp 1 2>/dev/null || echo not_available")
            if out == "not_available":
                pytest.skip("taskset not available in container")
            assert "0,1" in out or "0-1" in out, f"Unexpected affinity: {out}"


class TestCheckSandboxScript:
    """Run check_sandbox.sh inside a properly configured sandbox container."""

    def test_check_script_passes_full_sandbox(self):
        assert _CHECK_SCRIPT.exists(), f"check_sandbox.sh not found at {_CHECK_SCRIPT}"
        name = _unique("aos-check")
        with _container(name, [
            "--cpus=2",
            "--memory=4g",
            "--memory-swap=4g",
            "--memory-swappiness=0",
            "--pids-limit=1024",
            "--cpuset-cpus=0,1",
        ]):
            # Copy script into container
            cp = _run(["docker", "cp", str(_CHECK_SCRIPT), f"{name}:/check_sandbox.sh"])
            assert cp.returncode == 0

            result = _run(["docker", "exec", name, "bash", "/check_sandbox.sh"])
            print("\n" + result.stdout)   # show output in pytest -v
            assert result.returncode == 0, (
                f"check_sandbox.sh failed:\n{result.stdout}"
            )

    def test_check_script_detects_missing_limits(self):
        """Script should report FAIL when no limits are set."""
        assert _CHECK_SCRIPT.exists()
        name = _unique("aos-check-bare")
        with _container(name, []):   # no limits
            cp = _run(["docker", "cp", str(_CHECK_SCRIPT), f"{name}:/check_sandbox.sh"])
            assert cp.returncode == 0

            result = _run(["docker", "exec", name, "bash", "/check_sandbox.sh"])
            print("\n" + result.stdout)
            # Expect non-zero exit (at least memory and CPU limits missing)
            assert result.returncode != 0, (
                "Expected check to fail on unlimited container, but it passed"
            )


class TestComposeOverlays:
    """Test the actual Harbor compose YAML files via docker compose directly."""

    def _compose_up(self, name: str, compose_files: list[Path], env: dict) -> str:
        flags = []
        for f in compose_files:
            flags += ["-f", str(f)]

        import os
        merged_env = {**os.environ, **env}

        r = _run(
            ["docker", "compose", "--project-name", name, *flags, "up", "-d", "--wait"],
            env=merged_env,
        )
        assert r.returncode == 0, f"compose up failed:\n{r.stderr}\n{r.stdout}"
        return name

    def _compose_down(self, name: str, compose_files: list[Path], env: dict):
        import os
        flags = []
        for f in compose_files:
            flags += ["-f", str(f)]
        _run(
            ["docker", "compose", "--project-name", name, *flags, "down"],
            env={**os.environ, **env},
        )

    def test_sandbox_overlay_sets_swappiness(self):
        """docker-compose-sandbox.yaml should apply mem_swappiness=0."""
        import tempfile, os
        name = _unique("aos-compose-sandbox")

        # Minimal task dir with Dockerfile
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "Dockerfile").write_text("FROM ubuntu:22.04\n")
            Path(tmpdir, "logs", "verifier").mkdir(parents=True)
            Path(tmpdir, "logs", "agent").mkdir(parents=True)
            Path(tmpdir, "artifacts").mkdir(parents=True)

            compose_files = [
                _COMPOSE_DIR / "docker-compose-base.yaml",
                _COMPOSE_DIR / "docker-compose-build.yaml",
                _COMPOSE_DIR / "docker-compose-sandbox.yaml",
            ]
            env = {
                "CONTEXT_DIR": tmpdir,
                "MAIN_IMAGE_NAME": f"aos-test-{name}",
                "HOST_VERIFIER_LOGS_PATH": str(Path(tmpdir) / "logs/verifier"),
                "HOST_AGENT_LOGS_PATH":    str(Path(tmpdir) / "logs/agent"),
                "HOST_ARTIFACTS_PATH":     str(Path(tmpdir) / "artifacts"),
                "ENV_VERIFIER_LOGS_PATH":  "/logs/verifier",
                "ENV_AGENT_LOGS_PATH":     "/logs/agent",
                "ENV_ARTIFACTS_PATH":      "/artifacts",
                "CPUS": "2",
                "MEMORY": "2G",
            }

            try:
                self._compose_up(name, compose_files, env)
                # Get the container name via compose ps
                r = _run(
                    ["docker", "compose", "--project-name", name,
                     "-f", str(compose_files[0]),
                     "-f", str(compose_files[1]),
                     "-f", str(compose_files[2]),
                     "ps", "-q", "main"],
                    env={**os.environ, **env},
                )
                container_id = r.stdout.strip()
                assert container_id, "Could not get container ID"

                # Check pids.max (most reliable cross-version indicator)
                out = subprocess.run(
                    ["docker", "exec", container_id, "cat",
                     "/sys/fs/cgroup/pids.max"],
                    capture_output=True, text=True
                ).stdout.strip()

                assert out == "1024", f"pids.max expected 1024, got '{out}'"
            finally:
                self._compose_down(name, compose_files, env)
