"""Discover trials under a harbor ``jobs/`` tree and parse them defensively.

Layout we consume (written by harbor + the AOS pool)::

    <jobs_dir>/main-<arm>-stream-<tier>/<trial_name>/
        result.json                         # phase timings, agent_info, rewards
        exception.txt                       # only when the trial raised
        agent/trajectory.json               # ATIF: steps[], final_metrics
        agent/<harness>.txt                 # raw transcript (name varies!)
        agent/.harbor-cgroup-resources.tsv  # 5s cgroup samples, header row
        verifier/reward.json                # {task_id: reward}
        verifier/reward_breakdown.json      # per-task score + agent note
        verifier/scheduling_metrics.json    # pool-side scheduling aggregates

Why this module is paranoid rather than terse:

* **Jobs are read while still running.** A trial may have a trajectory but no
  ``result.json``, or a truncated ``trajectory.json`` mid-write. Any artifact
  failing to parse must degrade to ``None`` and be recorded in
  ``TrialRecord.load_errors`` — never raise, never silently drop the trial,
  because "how many trials are unparseable" is itself a finding.
* **The transcript filename is harness-specific** (``codex.txt``,
  ``claude-code.txt``, ``kimi-code.txt``) and kimi also writes
  ``kimi-code.stderr.txt``. We glob ``agent/*.txt`` and drop ``*stderr*``: the
  stderr file duplicates throttle lines and would double-count them.
  (Verified: 43 kimi trials carry the rate-limit marker in the main transcript
  and 1 in stderr only, so stderr is scanned as a *fallback* for detection but
  never treated as a second transcript.)
* **We do not stat the transcript into memory eagerly.** Transcripts reach
  hundreds of MB; ``health`` streams them line-wise instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Iterator

# Job dir names: ``main-<arm>-<setting>-<tier>`` for the headline grid, plus
# config-probe jobs named ``cfg-<arm>-<setting>-<tier>-<variant>``. The
# ``<setting>`` slot is ``stream`` (tasks arrive over time) or ``burst`` (all
# tasks ready at t=0 -- config key ``aos_all_ready: true``). Everything after
# the tier is a variant tag, e.g. ``2c4g`` for the halved-sandbox probe.
#
# Round 1 used ``^main-(?P<arm>[^-]+)-stream-(?P<tier>.+)$``, which silently
# dropped every burst job and the 2c4g probe on the floor -- they were not
# "missing data", they were never discovered. Tier is matched against the known
# set longest-first so ``heavy-plus`` wins over ``heavy`` and the leftover
# becomes the variant rather than corrupting the tier label.
JOB_DIR_RE = re.compile(
    r"^(?P<prefix>main|cfg)-(?P<arm>[^-]+)-(?P<setting>stream|burst)-(?P<rest>.+)$"
)

# Nominal stream size per tier, used only to sanity-check n_tasks; the
# authoritative count is scheduling_metrics.aggregates.n_tasks.
TIER_NOMINAL_TASKS = {"light": 5, "medium": 10, "heavy": 20, "heavy-plus": 30}

# Longest first so "heavy-plus" is not truncated to "heavy" with variant "plus".
_TIERS_LONGEST_FIRST = tuple(sorted(TIER_NOMINAL_TASKS, key=len, reverse=True))

# Default sandbox shape for the ``main-*`` grid. Used only as a fallback: the
# authoritative shape is read per trial from the cgroup TSV, because the 2c4g
# probe halves both and a hardcoded 4-core divisor would report its CPU
# utilisation at half the true value.
SANDBOX_CORES = 4.0
SANDBOX_MEMORY_BYTES = 8192 * 1024 * 1024

# AOS_CGROUP_POLL_SEC in the launcher. The TSV's ``sample`` column is an index,
# not a timestamp, so the interval has to come from configuration.
CGROUP_POLL_SEC = 5.0


@dataclass(frozen=True)
class CgroupSample:
    """One row of ``.harbor-cgroup-resources.tsv``.

    Fields are the subset we use. ``-1`` in the raw file means "the kernel did
    not expose this" (sandboxes without a cgroup ``cpu.stat`` file, so all the
    throttling counters are permanently -1) and is normalised to ``None`` here
    so callers cannot accidentally average it as zero.
    """

    sample: int
    memory_current: int | None
    # ``memory.max`` for this container. Read rather than assumed: the 2c4g
    # probe runs at 4 GiB while the main grid runs at 8 GiB.
    memory_max: int | None
    memory_peak: int | None
    cpu_usage_usec: int | None
    cpu_nr_periods: int | None
    cpu_nr_throttled: int | None
    cpu_quota_usec: int | None
    cpu_period_usec: int | None
    pids_current: int | None


@dataclass
class TrialRecord:
    """Everything we could read for one trial, plus what we could not.

    ``load_errors`` is part of the payload rather than a log line: a run where
    10% of trajectories fail to parse is a broken export, and that has to show
    up in the report instead of quietly shrinking the denominator.
    """

    job: str
    arm: str
    tier: str
    trial_name: str
    path: Path

    # ``stream`` | ``burst``. Defaulted so older callers that build a record by
    # hand keep working, but always set by :func:`load_trial`.
    setting: str = "stream"
    # Config-probe tag, e.g. ``2c4g``; empty for the main grid.
    variant: str = ""
    # Where this trial was read from: ``live`` for the jobs tree, or the name of
    # the preservation dir (e.g. ``kept-clean-0921``). Preserved trials may
    # predate a config change, so provenance travels with every figure.
    source_cohort: str = "live"

    result: dict[str, Any] | None = None
    trajectory: dict[str, Any] | None = None
    reward: dict[str, float] | None = None
    reward_breakdown: dict[str, Any] | None = None
    scheduling: dict[str, Any] | None = None
    cgroup: list[CgroupSample] = field(default_factory=list)
    exception_text: str | None = None
    transcripts: list[Path] = field(default_factory=list)
    stderr_transcripts: list[Path] = field(default_factory=list)
    load_errors: dict[str, str] = field(default_factory=dict)

    # -- convenience accessors ------------------------------------------------

    @property
    def steps(self) -> list[dict[str, Any]]:
        if not self.trajectory:
            return []
        steps = self.trajectory.get("steps")
        return steps if isinstance(steps, list) else []

    @property
    def final_metrics(self) -> dict[str, Any]:
        if not self.trajectory:
            return {}
        fm = self.trajectory.get("final_metrics")
        return fm if isinstance(fm, dict) else {}

    @property
    def harness(self) -> str:
        """Harness name, e.g. ``codex`` / ``claude-code`` / ``kimi-code``.

        Prefer the trajectory's own ``agent.name`` (present even when
        ``result.json`` has not been written yet), then ``result.json``, then
        the transcript filename as a last resort.
        """
        for src in (self.trajectory or {}).get("agent"), (self.result or {}).get("agent_info"):
            if isinstance(src, dict) and src.get("name"):
                return str(src["name"])
        for path in self.transcripts:
            return path.stem
        return "unknown"

    @property
    def model_name(self) -> str | None:
        agent = (self.trajectory or {}).get("agent")
        if isinstance(agent, dict) and agent.get("model_name"):
            return str(agent["model_name"])
        return None

    @property
    def cell(self) -> str:
        """Human label for the arm x setting x tier cell this trial belongs to."""
        base = f"{self.arm}-{self.setting}-{self.tier}"
        return f"{base}-{self.variant}" if self.variant else base

    @property
    def sandbox_cores(self) -> float:
        """Cores this sandbox actually had, from ``cpu.max`` in the cgroup TSV.

        ``quota/period`` is the container's own CPU limit and is the only
        per-trial source of truth: the 2c4g probe writes ``200000/100000``
        (2 cores) where the main grid writes ``400000/100000`` (4 cores).
        Some samples carry ``-1`` (the sampler momentarily read a cgroup with
        no quota), so we take the *most common non-null* value rather than the
        first. Falls back to :data:`SANDBOX_CORES` when nothing is readable.
        """
        counts: dict[float, int] = {}
        for s in self.cgroup:
            if s.cpu_quota_usec and s.cpu_period_usec:
                cores = s.cpu_quota_usec / s.cpu_period_usec
                counts[cores] = counts.get(cores, 0) + 1
        if not counts:
            return SANDBOX_CORES
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    @property
    def sandbox_memory_bytes(self) -> int:
        """Memory limit from ``memory.max``, same most-common-non-null rule."""
        counts: dict[int, int] = {}
        for s in self.cgroup:
            if s.memory_max:
                counts[s.memory_max] = counts.get(s.memory_max, 0) + 1
        if not counts:
            return SANDBOX_MEMORY_BYTES
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    @property
    def has_result(self) -> bool:
        return self.result is not None

    @property
    def is_in_flight(self) -> bool:
        """No ``result.json`` and no ``exception.txt`` => harbor never finished.

        Distinguishing this from a failure matters: an in-flight trial must be
        excluded from *all* denominators, while a failed one belongs in the
        infra-error tally.
        """
        return self.result is None and self.exception_text is None

    def tool_calls(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Yield ``(step, tool_call)`` pairs, skipping malformed entries.

        ``tool_calls[j].function_name`` is the tool name for every harness we
        support; ``arguments`` is usually a dict but codex occasionally emits a
        raw string, so callers must not assume dict.
        """
        for step in self.steps:
            if not isinstance(step, dict):
                continue
            calls = step.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if isinstance(call, dict):
                    yield step, call


def _load_json(path: Path, record: TrialRecord, key: str) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:  # truncated mid-write, or non-JSON garbage
        record.load_errors[key] = f"{type(exc).__name__}: {exc}"
        return None


def _as_int(raw: str) -> int | None:
    """Parse a cgroup cell, mapping the ``-1`` sentinel to ``None``.

    The sampler writes ``-1`` for any counter the kernel did not expose. In
    sandboxes without ``cpu.stat`` that is always true of ``cpu.stat``-derived columns
    (``cpu_nr_periods``, ``cpu_nr_throttled``, ``cpu_throttled_usec``), so
    treating -1 as 0 would report "zero CPU throttling" for a container we in
    fact know nothing about.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return None if value < 0 else value


def parse_cgroup_tsv(path: Path) -> list[CgroupSample]:
    """Parse the cgroup TSV by *header name*, never by fixed column index.

    The sampler has gained columns over time (the PSI ``*_avg10`` and ``fs_*``
    groups were appended later), so positional parsing silently shifts.
    """
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    index = {name: i for i, name in enumerate(header)}

    def cell(parts: list[str], name: str) -> int | None:
        i = index.get(name)
        if i is None or i >= len(parts):
            return None
        return _as_int(parts[i])

    samples: list[CgroupSample] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sample_idx = cell(parts, "sample")
        samples.append(
            CgroupSample(
                sample=sample_idx if sample_idx is not None else len(samples),
                memory_current=cell(parts, "memory_current"),
                memory_max=cell(parts, "memory_max"),
                memory_peak=cell(parts, "memory_peak"),
                cpu_usage_usec=cell(parts, "cpu_usage_usec"),
                cpu_nr_periods=cell(parts, "cpu_nr_periods"),
                cpu_nr_throttled=cell(parts, "cpu_nr_throttled"),
                cpu_quota_usec=cell(parts, "cpu_quota_usec"),
                cpu_period_usec=cell(parts, "cpu_period_usec"),
                pids_current=cell(parts, "pids_current"),
            )
        )
    return samples


def split_tier_variant(rest: str) -> tuple[str, str]:
    """Split the post-setting remainder into ``(tier, variant)``.

    ``heavy-plus`` -> ``("heavy-plus", "")``; ``heavy-2c4g`` ->
    ``("heavy", "2c4g")``. Unknown remainders are returned whole as the tier so
    a new tier name shows up in the report rather than being swallowed.
    """
    for tier in _TIERS_LONGEST_FIRST:
        if rest == tier:
            return tier, ""
        if rest.startswith(tier + "-"):
            return tier, rest[len(tier) + 1 :]
    return rest, ""


def load_trial(
    trial_dir: Path,
    *,
    job: str,
    arm: str,
    tier: str,
    setting: str = "stream",
    variant: str = "",
) -> TrialRecord:
    """Parse one trial directory. Never raises for data reasons."""
    record = TrialRecord(
        job=job,
        arm=arm,
        tier=tier,
        trial_name=trial_dir.name,
        path=trial_dir,
        setting=setting,
        variant=variant,
    )

    result = _load_json(trial_dir / "result.json", record, "result")
    record.result = result if isinstance(result, dict) else None

    traj = _load_json(trial_dir / "agent/trajectory.json", record, "trajectory")
    record.trajectory = traj if isinstance(traj, dict) else None

    reward = _load_json(trial_dir / "verifier/reward.json", record, "reward")
    if isinstance(reward, dict):
        clean: dict[str, float] = {}
        for task_id, value in reward.items():
            try:
                clean[str(task_id)] = float(value)
            except (TypeError, ValueError):
                record.load_errors[f"reward:{task_id}"] = f"non-numeric {value!r}"
        record.reward = clean

    breakdown = _load_json(trial_dir / "verifier/reward_breakdown.json", record, "reward_breakdown")
    record.reward_breakdown = breakdown if isinstance(breakdown, dict) else None

    sched = _load_json(trial_dir / "verifier/scheduling_metrics.json", record, "scheduling")
    record.scheduling = sched if isinstance(sched, dict) else None

    try:
        record.cgroup = parse_cgroup_tsv(trial_dir / "agent/.harbor-cgroup-resources.tsv")
    except Exception as exc:
        record.load_errors["cgroup"] = f"{type(exc).__name__}: {exc}"

    exc_path = trial_dir / "exception.txt"
    if exc_path.is_file():
        try:
            record.exception_text = exc_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            record.load_errors["exception"] = f"{type(exc).__name__}: {exc}"

    agent_dir = trial_dir / "agent"
    if agent_dir.is_dir():
        for path in sorted(agent_dir.glob("*.txt")):
            if "stderr" in path.name:
                record.stderr_transcripts.append(path)
            else:
                record.transcripts.append(path)

    return record


def iter_job_dirs(jobs_dir: Path) -> Iterator[tuple[Path, str, str, str, str]]:
    """Yield ``(job_dir, arm, tier, setting, variant)`` for every job dir.

    Directories not matching the pattern are skipped rather than guessed at —
    the jobs root also collects ad-hoc smoke dirs we must not fold into arm
    statistics. ``jobs-archive/``, ``poisoned*/`` and ``kept-clean*/`` live
    *beside* the jobs root, not inside it, so they are out of scope here.
    """
    if not jobs_dir.is_dir():
        return
    for job_dir in sorted(jobs_dir.iterdir()):
        if not job_dir.is_dir():
            continue
        match = JOB_DIR_RE.match(job_dir.name)
        if not match:
            continue
        tier, variant = split_tier_variant(match.group("rest"))
        yield job_dir, match.group("arm"), tier, match.group("setting"), variant


def discover_trials(jobs_dir: Path) -> list[TrialRecord]:
    """Load every trial under ``jobs_dir``.

    A child of a job dir counts as a trial iff it contains an ``agent/``
    directory (the marker harbor always creates, even for trials that died
    during environment setup). ``config.json`` / ``job.log`` at the job level
    are therefore ignored.
    """
    records: list[TrialRecord] = []
    for job_dir, arm, tier, setting, variant in iter_job_dirs(jobs_dir):
        for trial_dir in sorted(job_dir.iterdir()):
            if not trial_dir.is_dir():
                continue
            if not (trial_dir / "agent").is_dir():
                continue
            records.append(
                load_trial(
                    trial_dir,
                    job=job_dir.name,
                    arm=arm,
                    tier=tier,
                    setting=setting,
                    variant=variant,
                )
            )
    return records


def discover_flat_trials(flat_dir: Path) -> list[TrialRecord]:
    """Load trials from a *flattened* preservation dir, e.g. ``kept-clean-0921/``.

    When a job is relaunched, its surviving trials are copied out of
    ``jobs/<job>/<trial>/`` into ``<flat_dir>/<job>__<trial>/`` so the relaunch
    cannot overwrite them. The directory name is therefore
    ``<job>__<stream>__<suffix>`` -- the same ``__`` separator that
    :func:`stream_id` splits on, so the job prefix has to be peeled off before
    stream identity can be read, or every preserved trial looks like its own
    stream and `n_streams` silently doubles.

    Provenance is cross-checked against ``result.json``: its
    ``config.trials_dir`` basename is the true job and ``trial_name`` the true
    trial. Where they disagree with the directory name we trust the file, since
    the directory name is an operator-side convention and the file is written
    by harbor.

    These cohorts are **not** merged into the live tree automatically: a
    preserved copy may predate a config change (the 2c4g trials here were run
    before the model rename), so the caller decides whether to fold them in and
    the report says which cohort each figure came from.
    """
    records: list[TrialRecord] = []
    if not flat_dir.is_dir():
        return records
    for trial_dir in sorted(flat_dir.iterdir()):
        if not trial_dir.is_dir() or not (trial_dir / "agent").is_dir():
            continue
        job, trial_name = _flat_identity(trial_dir)
        match = JOB_DIR_RE.match(job)
        if not match:
            continue
        tier, variant = split_tier_variant(match.group("rest"))
        record = load_trial(
            trial_dir,
            job=job,
            arm=match.group("arm"),
            tier=tier,
            setting=match.group("setting"),
            variant=variant,
        )
        # load_trial takes the trial name from the directory, which here still
        # carries the job prefix. Override it with the real one.
        record.trial_name = trial_name
        record.source_cohort = flat_dir.name
        records.append(record)
    return records


def _flat_identity(trial_dir: Path) -> tuple[str, str]:
    """Recover ``(job, trial_name)`` for a flattened trial dir."""
    result_path = trial_dir / "result.json"
    if result_path.is_file():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = None
        if isinstance(data, dict):
            trials_dir = (data.get("config") or {}).get("trials_dir")
            trial_name = data.get("trial_name")
            if isinstance(trials_dir, str) and isinstance(trial_name, str):
                return Path(trials_dir).name, trial_name
    # Fall back to the naming convention: <job>__<stream>__<suffix>.
    name = trial_dir.name
    for tier in _TIERS_LONGEST_FIRST:
        marker = f"-{tier}__"
        at = name.find(marker)
        if at != -1:
            split = at + len(marker) - 2
            return name[:split], name[split + 2 :]
    job, _, rest = name.partition("__")
    return job, rest


def stream_id(record: TrialRecord) -> str:
    """Stream identity shared by the repeated attempts of one stream.

    Trial names are ``<stream>__<suffix>``; grouping by the prefix is how we
    tell "3 attempts at 1 stream" from "3 independent streams", which is the
    difference between n=1 and n=3 for any claim about a tier.
    """
    return record.trial_name.split("__", 1)[0]
