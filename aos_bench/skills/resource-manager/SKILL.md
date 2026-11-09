---
name: resource-manager
description: Observe the real resource limits of this container and manage a queue of independent tasks against them. Use when work arrives from a pool or queue over time, when deciding how many things to run at once, when sizing build/test parallelism, or when a long-running job must finish inside a time budget.
---

# Resource manager

You are running inside a resource-capped container and pulling work from a queue.
This skill tells you how to find out what you actually have, and how to keep the
container busy without overcommitting it.

It gives you **methods, not numbers**. Every figure below must be measured in the
container you are in — values differ per run, and a hardcoded parallelism is
usually wrong.

## 1. Read the real limits — `nproc` lies

`nproc`, `/proc/cpuinfo`, `os.cpu_count()` and `multiprocessing.cpu_count()` all
report the **host's** CPUs, not your cgroup's quota. In this environment they
routinely report 64 while the container is allowed 4. Sizing `make -j` or a worker
pool from them oversubscribes by ~16x, which makes everything slower and can
trigger OOM.

Read the cgroup instead:

```bash
# cgroup v2 (what this container uses)
cat /sys/fs/cgroup/cpu.max       # "400000 100000" => 400000/100000 = 4 CPUs
cat /sys/fs/cgroup/memory.max    # bytes, or "max" if unlimited
cat /sys/fs/cgroup/memory.current

# cgroup v1 fallback
cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us   # / cpu.cfs_period_us
cat /sys/fs/cgroup/memory/memory.limit_in_bytes
```

One-liner for CPU budget:

```bash
awk '{ if ($1=="max") print "unlimited"; else printf "%.2f\n", $1/$2 }' /sys/fs/cgroup/cpu.max
```

Do this **once at the start** and reuse the answer. State the numbers you found
before you act on them, so your reasoning is auditable.

If `cpu.max` says `max` (unlimited), fall back to `nproc` but treat it as an upper
bound you probably should not saturate.

## 2. Keep work in flight — the queue will not push

Tasks are handed out by a pool that you must **pull** from: nothing arrives unless
you ask. The failure mode this skill exists to prevent is finishing one task,
never asking again, and idling with a full backlog.

Rules:

- Ask for work again **as soon as you have spare capacity**, not after everything
  in hand is finished.
- Keep asking until the pool explicitly tells you the stream is done. A timeout or
  an empty answer means "nothing ready *yet*" — more may arrive later. Only the
  pool's terminal signal means stop.
- Before you conclude you are finished, ask once more. If you have not received a
  terminal signal, you are not finished.

How many to hold at once (**work-in-progress**, WIP) is a judgement call, not a
constant. Reason from what you measured in step 1 and what the tasks look like:

- A task that is mostly waiting (network, a model call, a long download) costs
  almost no CPU — several can overlap freely.
- A task that saturates a core (compiling, encoding, training) needs a share of
  your CPU budget. Overlapping more of these than you have CPUs makes each slower
  without finishing more.
- Memory is the hard wall: exceeding `memory.max` gets the process **killed**, not
  slowed. Leave headroom.

Start conservative, watch, then raise WIP if you are leaving capacity unused.

## 3. Profile each task before choosing its parallelism

Look at what a task actually needs before deciding how to run it:

```bash
du -sh <workdir>                   # input size
ls <workdir>                       # source tree? dataset? single file?
nvidia-smi 2>/dev/null || true     # GPU present at all?
```

Then size the tool, using your measured CPU budget `N`:

- Builds: `make -jN`, `cmake --build . -jN`, `cargo build -jN`, `pytest -n N`.
  Not `-j64`, and not bare `-j` (which is unbounded).
- Memory-heavy steps: reduce parallelism below `N` rather than risking the OOM
  killer. One surviving job beats four killed ones.
- Single-threaded work: running `N` of those concurrently is the way to use `N`
  CPUs; running one with `-jN` does nothing.

## 4. Watch the container, not your assumptions

Check occasionally rather than trusting your plan:

```bash
cat /sys/fs/cgroup/memory.current                      # vs memory.max
cat /sys/fs/cgroup/cpu.stat                            # usage_usec; may be absent
uptime                                                 # load average vs CPU budget
ps -eo pid,pcpu,pmem,rss,comm --sort=-pcpu | head      # who is actually spending
```

Interpretation:

- `memory.current` approaching `memory.max` → shed parallelism now.
- Load average well below your CPU budget while a backlog exists → you are
  under-committed; take more work.
- `cpu.stat` may be missing throttling counters in this environment. If
  `nr_throttled` is absent or `-1`, it means **not measurable**, not "not
  throttled" — use utilisation and wall-clock progress instead.

## 5. Respect the clock

There is a wall-clock budget for the whole run. Near the end, prefer finishing and
reporting what is nearly done over starting something new. A task reported as done
but unverified is worth less than one actually completed — report outcomes
truthfully, including failures.

## Anti-patterns

| Don't | Why |
|---|---|
| Size parallelism from `nproc` | Reports host CPUs (64 vs an actual 4) |
| `make -j` with no number | Unbounded; spawns a job per target |
| Do one task, then stop asking | Queue is pull-based; backlog sits idle |
| Treat a poll timeout as "stream over" | Means "none ready yet"; wait for the terminal signal |
| Raise WIP until memory is full | Exceeding `memory.max` kills processes |
| Read a `-1` throttle counter as "fine" | `-1` means unavailable |
| Report work as complete without checking | Self-reported success is independently verified |
