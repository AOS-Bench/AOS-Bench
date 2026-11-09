"""Prompt builders for the solo agent (engine-agnostic — no SDK imports)."""

from __future__ import annotations

# Kept for the Coordinator's bookkeeping (it imports LEAD_NAME); the solo agent
# has no lead, but the Coordinator is shared infrastructure.
LEAD_NAME = "team-lead"


def resource_env_section(cpus: int, mem_mb: int, *, shared: bool) -> str:
    """A concrete CPU/RAM-awareness blurb so an agent can plan its resource use.

    ``shared=True`` warns that the budget is split across concurrent teammates.
    """
    gb = f"{mem_mb / 1024:.0f} GB" if mem_mb else "a limited amount of"
    cores = str(cpus) if cpus else "a limited number of"
    j = cpus or "N"
    shared_note = (
        " You SHARE this budget with the other teammates running in the SAME "
        "container — it is not all yours, so stay modest with parallelism."
        if shared
        else ""
    )
    return (
        f"You run inside a resource-limited sandbox container with **{cores} CPU "
        f"cores / {gb} RAM**.{shared_note} Plan your work around it: use the "
        f"cores when it helps (e.g. `make -j{j}`, `pytest -n`, batching "
        "independent steps), but don't oversubscribe — too many concurrent heavy "
        "processes thrash and slow everything down, and memory-heavy steps can be "
        "OOM-killed. If unsure of current headroom, check with `nproc`, "
        "`free -m`, and `uptime`."
    )


SOLO_DONE_SENTINEL = "ALL_DONE"


def solo_system_prompt(cpus: int = 0, mem_mb: int = 0) -> str:
    """Single autonomous agent — NO lead, NO team, NO delegation. It claims its
    own task from the pool and does it itself. Used for the no-team baseline."""
    return f"""\
# You are a single autonomous agent in an "agent OS" sandbox

There is **no team and no lead**. You do everything yourself: claim the task,
solve it, report it.

## Resource environment
{resource_env_section(cpus, mem_mb, shared=False)}

## Tools
  - `aos_pool` is your task interface (how you receive work + report results):
      mcp__aos_pool__todo_next(timeout_sec)  — claim the next ready task
      mcp__aos_pool__todo_status()           — counts; check `stream_done`
      mcp__aos_pool__todo_update(task_id, "completed"|"failed", note)
  - Your normal tools: Bash, Read, Write, Edit, Glob, Grep. You run as a
    non-root user; `/workspace/` is yours. Use `sudo apt-get/pip/npm install`
    if a task needs packages.

## The loop you must run
1. Call `todo_next` to claim a task -> {{task_id, instruction}} or
   {{stream_done: true}}.
2. Do the task **end-to-end yourself** in /workspace. Work carefully and verify
   your output before reporting.
3. Report it exactly once with `todo_update(task_id, "completed"|"failed", note)`.
4. Repeat. When `todo_next` returns {{stream_done: true}} AND you have reported
   every task you claimed, reply with exactly `{SOLO_DONE_SENTINEL}` and make NO
   further tool calls.

Begin now: call `todo_status`, then `todo_next` to claim your task.
"""


def solo_first_message(stream_preamble: str) -> str:
    return (
        stream_preamble.strip()
        + "\n\n---\n\nYou are a single agent with no team. Start now: call "
        "`todo_next` to claim your task and solve it yourself, then "
        f"`todo_update` to report. Reply `{SOLO_DONE_SENTINEL}` when the stream "
        "is done."
    )
