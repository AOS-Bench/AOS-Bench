# Live Eval Monitor

> Agents covered: **kimi-cli · kimi-code · claude-code · codex · claude-agent-sdk**

Trial labels use Harbor's authoritative per-trial `config.json`, not lossy
launcher aliases or canonical symlink names. The UI displays
`<harness-name>-<model-name>-<stream-name>` (for example,
`claude-code-claude-opus-5-light-5-scan-007`) and shows Harbor's random attempt ID in
a separate column. The internal `trial_id` remains unchanged for API routing
and symlink compatibility.

The Eval Monitor is a small FastAPI app that watches an in-progress harbor
job, renders each trial's agent trajectory as a foldable tree of lanes,
surfaces errors as they appear in the agent's session log, and shows host and
per-container CPU/mem so the operator can keep an eye on a long batch run
without `ssh + tail -f`.

Pipeline:

```
harbor job (one of five arms)
  kimi-cli:          agent/kimi-share/sessions/<md5>/<sess>/wire.jsonl
                     + subagents/<id>/wire.jsonl
  kimi-code:         agent/kimi-code-home/sessions/wd_<slug>_<sha>/session_<uuid>/
                       agents/<agentId>/wire.jsonl  (+ state.json)
  claude-code:       agent/sessions/projects/<p>/<sess>/*.jsonl
  codex:             agent/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl
  claude-agent-sdk:  agent/events.jsonl
  (+ verifier/reward.json · result.json · exception.txt, read by every tail)
    → aos_bench.monitor live tailer (one *SessionTail per trial)
      → arm-specific incremental parser → ONE canonical event shape
        → in-memory MonitorState (per harbor job)
          → FastAPI app on :4000
            → vanilla-JS dashboard (Overview / Trial Detail / Errors)
```

**One event shape, five front-ends to it.** kimi-cli and kimi-code both write
flat JSONL journals of typed protocol records, but of *different products with
different vocabularies* (see the kimi-code section below); Claude Code persists an Anthropic-style transcript (`assistant`/`user`
events with `content` blocks, `usage`, and `isSidechain`/`agentId` tags for
subagents); codex writes one `rollout-*.jsonl` per `codex exec`; our SDK harness
writes its own `Coordinator` event log. Each tail has its own parser, but **all
of them emit the same normalized event dicts** (`event_id` / `seq` /
`timestamp` / `kind` / `phase` / `step_id` / `payload` / `metrics` / `raw_ref` /
`extra`). That is why the whole dashboard (agent-activity lanes, spawn arrows,
token counters, the dispatcher task panel, the error panel) renders a new arm
with **zero frontend changes**.

Each live parser mirrors its arm's offline converter, so the live view and the
post-mortem trace agree:

| arm | tail | live parser | mirrors |
|---|---|---|---|
| kimi-cli | `tail/kimi.py` | `tail/parser.py` | `aos_bench/trace/kimi_cli.py` |
| kimi-code | `tail/kimi_code.py` | `tail/kimi_code_parser.py` | `aos_bench/trace/kimi_code.py` (**by import**, not by copy) |
| claude-code | `tail/claude.py` | `tail/claude_parser.py` | harbor `agents/installed/claude_code.py` |
| codex | `tail/codex.py` | `tail/codex_parser.py` | harbor `agents/installed/codex.py` |
| claude-agent-sdk | `tail/agent_events.py` | (inline) | `aos_bench/harness/claude_code/core/coordinator.py` |

**How the right tail is chosen.** `discover.py` walks `jobs_dir` (recursively,
capped at `_MAX_TRIAL_DEPTH = 6`) for per-trial dirs (marked by an `agent/`
mount directly inside), and `_classify_trial_agent` types each one by on-disk
layout, in this order:

1. `agent/**/events.jsonl` → **claude-agent-sdk**. First, because the SDK also
   creates an *empty* `agent/sessions/<role>/projects/` from its
   `CLAUDE_CONFIG_DIR` and must not be mistaken for claude-code.
2. `agent/**/projects/**/*.jsonl` (excluding `subagents/`) → **claude-code**. A
   real transcript file is required, not just the directory.
3. `agent/**/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl` → **codex**.
4. `agent/**/kimi-code-home/` *(a directory)* → **kimi-code**.
5. `agent/**/kimi-share/` → **kimi-cli**.

**There is no default arm.** An unrecognised trial classifies as `None` and the
discoverer *defers*: it will be re-examined on the next scan (every
`scan_interval_s`, default 2 s) rather than being locked onto a tail that can
never find its session files. The tail dict is keyed exhaustively.

Branches 4 and 5 key on a *directory*, not a journal file, deliberately: both
homes are created by the adapter before the CLI writes anything, so a trial that
died during install is still typed and its `exception.txt` still reaches the
Errors panel. The two markers cannot collide (`kimi-code-home` vs `kimi-share`).

**Subagent attribution differs by arm.**

- **kimi-cli** spawns each child into its own `subagents/<id>/wire.jsonl`, so
  `KimiSessionTail` watches that directory and reads per-child sidecar metadata.
- **kimi-code** splits agents *physically* too, but one level deeper and with the
  parent link out-of-band: `agents/<agentId>/wire.jsonl` plus a session-level
  `state.json` holding `agents.<id>.{parentAgentId, labels}`. There is no
  `isSidechain` flag inside records.
- **claude-code** interleaves subagent turns into the *same* transcript with
  `isSidechain: true` and a distinct `agentId`; `ClaudeSessionTail` keeps one
  parser per `agentId` and routes each record, synthesizing the child run +
  spawn marker on first sight.
- **codex** gives each `spawn_agent` worker its own `rollout-*.jsonl`; the
  child's `session_meta.id` equals the `agent_id` the root's spawn call
  returned, which is what re-attaches the lane to the right parent step.
- **claude-agent-sdk** records `spawn_worker` / `send_message` / `deliver`
  events directly in `events.jsonl`.

In every arm the frontend builds lanes and spawn arrows purely from
`run.parent_run_id`, so a missed spawn marker costs an arrow, never a lane.

**Workflow-tool fan-out is tracked for both arms.** In the dynamic-workflow arm
the agent under test drives Claude Code's `Workflow` tool, and every `agent()`
call in the orchestration script runs as a headless agent with its own
transcript at
`<session-uuid>/subagents/workflows/wf_<runid>/agent-<agentId>.jsonl`. Those
records are ordinary CC transcripts (`isSidechain`+`agentId`), so they render as
normal subagent lanes with full tool-call timelines.

`WorkflowFanoutTracker` (`tail/workflow.py`) owns this and is embedded in the
`_tick` of **both** `ClaudeSessionTail` and `AgentEventsTail`. The SDK arm needs
it just as much as the native one: an SDK-harness agent can drive the `Workflow`
tool too, but `events.jsonl` only records the single `who` that made the call.
Without the tracker, an 8-agent Workflow shows up as **one** lane with 8 tool
calls instead of 8 lanes with all of their tool calls. The two arms also nest `CLAUDE_CONFIG_DIR`
differently (`agent/sessions/projects` vs `agent/sessions/<role>/projects`),
which is why the tracker scans by glob rather than a fixed path.

Two extra files complete the picture:

- `wf_<runid>/journal.jsonl` — `{type: started|result, agentId}`. The only
  signal that an agent *finished*; without it every workflow lane would sit at
  "running" forever. It is bookkeeping, never decoded as a transcript.
- `<session-uuid>/workflows/wf_<runid>.json` — the sidecar carrying
  `workflowName`, `phases`, and per-agent `label`/`phaseTitle`/`model`/`state`.
  The `label` is whatever the script passed to `agent()`. In a stream trial
  that is the **task id** (`wf vulnerable-secret`) or the worker slot
  (`wf worker-3`), and with more than one phase it is prefixed
  (`wf Execute/worker-1`). It is written only when the whole Workflow
  *completes* (verified by probe: absent for the first 60s of an 80s run), so
  the tracker treats it as best-effort enrichment: lanes appear immediately
  labelled from the agent's own prompt, then get relabelled in place once the
  sidecar lands. Agents group under a `run_wfrun_<wf_id>` lane, so a lead that
  launches one Workflow per batch of arrivals gets one group per batch.

## The kimi-code arm

`kimi-code` is `@moonshot-ai/kimi-code` (TypeScript, npm-pinned, node ≥ 22),
a **different product** from the Python `kimi-cli`, not a new version of it.
harbor's adapter is `harbor/src/harbor/agents/installed/kimi_code.py`.

```
adapter sets KIMI_CODE_HOME=/logs/agent/kimi-code-home   (bind-mounted from <trial>/agent)
  kimi --output-format stream-json -p "<STREAM_PREAMBLE>"
    ├─ fd 1 → /logs/agent/kimi-code.txt          (stream-json; no timestamps/usage — NOT the trace source)
    ├─ fd 2 → /logs/agent/kimi-code.stderr.txt   (the ONLY place run failures appear)
    └─ $KIMI_CODE_HOME/
         session_index.jsonl                      {sessionId, sessionDir, workDir}
         sessions/wd_<slug>_<sha256[:12]>/session_<uuid>/
           state.json                             agent tree + parent links + labels
           agents/main/wire.jsonl                 root journal      → run_root_001
           agents/main/tasks/<taskId>.json        background-task sidecars
           agents/agent-0/wire.jsonl              one journal per subagent → run_sub_agent-0
             ↓
    KimiCodeSessionTail (poll)  →  IncrementalKimiCodeParser  →  the same normalized events
```

Arm-specific things worth knowing when reading the code:

- **Disk, never stdout.** `--output-format stream-json` carries no timestamps,
  no token usage, no subagent attribution and no final `result` line, so both
  the live monitor and the post-run AgentTrace read `wire.jsonl`.
- **The parser mirrors the trace module by import.** The vocabulary is ~60
  record types wide, so `kimi_code_parser.py` imports `map_wire_kind` and the
  payload/phase/metrics builders from `aos_bench/trace/kimi_code.py` instead of
  restating them. The two therefore cannot drift. The only deliberate
  differences are the two kind downgrades the dashboard cannot render
  (`reasoning` → `message`, `background_task` → `status`) and spawn dedup.
- **Timestamps are epoch milliseconds** and are *optional* on a record,
  divided by 1000, with the last known value carried forward. The first line is
  `metadata` and carries `created_at` instead of `time`.
- **Tokens come from `step.end` only.** `usage.record` repeats the identical
  `TokenUsage` object at turn scope; counting both would double the lane header.
- **`event.step` restarts at 1 every turn**, so it is not the step counter;
  the parser keeps a running per-run counter and exposes the native value as
  `native_step`.
- **Subagent attribution is exact on disk**: a foreground `Agent` result begins
  `agent_id: <id>`, `AgentSwarm` results carry
  `<subagent agent_id="agent-0" item="alpha" …>`, and a background delegation
  journals `task.started` naming both `parentToolCallId` and `agentId`. No time
  heuristics are needed; spawn events are emitted immediately with
  `child_run_id: None` and back-filled in place once the id arrives.
- **Two engines, one layout.** The default (v2) engine writes
  `protocol_version 1.5` (`profile.bind`, `turn.ended`, `task.*`); the legacy
  engine (`--ak legacy_engine=true` → `KIMI_CODE_LEGACY_FLAG=1`) writes `1.4`
  with `config.update` + `tools.set_active_tools` and an ISO-string
  `state.json`. Every reader here tolerates both, because the legacy engine is
  the fallback if the default engine does not register the dispatcher's MCP
  tools.
- **No `notification` kind.** kimi-code has no notification wire record
  (notifications reach the model as injected prompt text), so the shared
  `notification_severe` error rule has no source on this arm. The other four
  rules all fire, because failed tool results carry `payload.error = True`.
- **Installed from npm, not from source.** The arm installs from **pinned npm**
  (`npm i -g @moonshot-ai/kimi-code@<version>`, adapter default `0.36.1`,
  overridable with `--ak version=`); no kimi-code source checkout is needed at
  runtime. The fixtures the tests need are committed under
  `aos_bench/tests/fixtures/kimi_code/`.

Smoke launcher: `scripts/sandbox/docker_kimi_code_smoke.sh` (requires the
`KIMI_MODEL_*` trio in the caller's env, because kimi-code ignores `KIMI_API_KEY`
and related variables in print mode, and sets an explicit `--agent-timeout-sec`, because
`kimi -p` does not reliably self-terminate).

## Launch via harbor

```bash
harbor run \
    --agent kimi-code --ak version=0.36.1 \
    --path ../data/stream/<stream> --task-type stream \
    --env docker --aos-pool-v03 \
    --stream-monitor \
    --stream-monitor-port 4000
```

harbor spawns `python -m aos_bench.monitor --jobs-dir <job-dir> --port 4000`
as a subprocess and prints the dashboard URL. The subprocess is terminated when
the job ends. Stdout/stderr land in `<job-dir>/.monitor.log`.

Flags:

| Flag | Default | Meaning |
|---|---|---|
| `--stream-monitor` / `--no-stream-monitor` | off | Enable the monitor sidecar |
| `--stream-monitor-port INT` | 4000 | Dashboard port |
| `--stream-monitor-bind ADDR` | 127.0.0.1 | Bind address |

## Launch standalone (replay / catch-up)

```bash
python -m aos_bench.monitor \
    --jobs-dir <path-to-jobs-dir> \
    --port 4000
```

Works on a finished run too, which is useful for inspecting an old job without
re-running it. Arm classification is identical either way, so a finished
kimi-code job renders the same lanes it would have rendered live.

## Architecture

```
aos_bench.monitor (single asyncio event loop)
├─ TrialDiscoverer       — scans jobs_dir for new per-trial dirs (poll); classifies arm + picks tail
├─ KimiSessionTail       — kimi-cli: tails root + subagents wire.jsonl, watches subagents/
│  └─ IncrementalWireParser        — same normalization as aos_bench/trace/kimi_cli.py
├─ KimiCodeSessionTail   — kimi-code: re-resolves the newest session dir + rescans agents/* each tick
│  └─ IncrementalKimiCodeParser    — imports its kind map from aos_bench/trace/kimi_code.py
├─ ClaudeSessionTail     — claude-code: tails sessions/projects/<sess>/*.jsonl, routes by agentId/isSidechain
│  └─ IncrementalClaudeParser — mirrors harbor's claude_code offline parser; emits the SAME event shape
├─ CodexSessionTail      — codex: one _FileTail per rollout-*.jsonl, child keyed by thread id
│  └─ IncrementalCodexParser  — normalizes Responses/tool/spawn/token events
├─ AgentEventsTail       — claude-agent-sdk harness: agent/events.jsonl
├─ ErrorDetector         — 5 rules over normalized events (gateway 404, tool errors, severe notifications, ...)
├─ HostResourceSampler   — psutil.cpu_percent + virtual_memory every 2s
├─ TrialContainerSampler — docker stats + cgroup probe + docker top per trial
│  ├─ persists .monitor-resources.jsonl (CPU/memory/swap/network/block I/O/PIDs/PSI/filesystem)
│  └─ persists .monitor-processes.jsonl (PID/PPID/CPU/RSS/run attribution)
├─ MonitorState          — in-memory dataclasses; pub-sub via asyncio.Queue per SSE subscriber
└─ FastAPI app           — REST + SSE + static /index.html
```

All five tails are duck-typed to the same contract (keyword-only
`(trial_id, trial_dir, state, error_detector, interval_s)` plus async
`start()` / `stop()`), and each carries its own copy of the finalization triad
(`exception.txt` → `crashed`, `verifier/reward.{txt,json}`, `result.json` →
`completed`/`failed`). There is no shared mixin; the triad is duplicated on
purpose so a change to one arm cannot silently alter another.

Per-container samples include Docker CPU/memory/network/block I/O/PIDs plus
cgroup v2 CPU time and throttling, memory/swap/events, I/O counters, PID
controller counters, PSI pressure, and workspace filesystem usage. They are
persisted beside the trial so replay after monitor restart keeps the full
curve. Process snapshots and per-process peaks are persisted separately.
Claude Code sidechains share a runtime process and are marked
`shared_runtime`; exact per-sidechain CPU/memory requires runtime support or a
separate process/cgroup per child run.

## API

```
GET  /                             → static dashboard
GET  /api/health
GET  /api/jobs                     → { jobs_dir, scan/tail/resource intervals, started_at }
GET  /api/trials                   → [ {trial_id, agent, status, agent_count, llm_calls, tool_calls, last_event_age_s, reward, errors_count} ]
GET  /api/trials/{id}              → { trial_id, agent, status, runs: [ {run_id, role, agent_name, status, events_tail, n_*} ], reward }
GET  /api/trials/{id}/resources    → persisted per-container samples
GET  /api/trials/{id}/processes    → latest process table + trial peak CPU/RSS rows
GET  /api/trials/{id}/tasks        → dispatcher task lifecycle (claim/complete per task)
GET  /api/resources                → { latest: ResourceSample, samples: [...] }
GET  /api/errors                   → [ ErrorRecord ]
GET  /api/events                   → SSE; { type: trial_discovered | trial_update | run_added | error | resource, payload | trial_id }
```

The `/tasks` panel needs nothing arm-specific: `tasks.py._strip_mcp_prefix`
already turns `mcp__aos_pool__todo_next` into `todo_next`, and `_result_text`
understands the handful of `return_value` shapes the parsers emit (the kimi /
kimi-code dict `{"output": …}`, and claude's envelope and flattened strings). A
new arm should pick the dict shape and the panel works with zero changes.

## Error rules

See `aos_bench/monitor/errors.py`. Currently:

| rule_id | trigger |
|---|---|
| `event_marked_error`     | event payload `error: true` or `kind == "error"` |
| `status_failed_count`    | status payload `token_usage.failed` (or `.errors`) > 0 |
| `tool_result_error_text` | tool result text matches `\b(exception\|traceback\|fatal)\b` |
| `gateway_404`            | tool result body contains `anthropic/v1/chat/completions` (a request routed to the wrong gateway path) |
| `notification_severe`    | `kind == "notification"` with `severity ∈ {warning, error}` |

Plus the per-tail finalization rule `harbor_setup_crash`, raised straight from
`exception.txt` rather than from an event.

## Remaining scope

- ModelTrajectory LLM proxy
- opencode (the only stream arm with no monitor tail)
- resource-threshold alerts
- active task interruption on error
- exact per-subagent CPU/memory attribution

## Verifying it works

```bash
# Against a finished run (E2E smoke):
python -m aos_bench.monitor \
    --jobs-dir <path-to-jobs-dir> \
    --port 4101 &

curl -s localhost:4101/api/trials | jq '. | length'      # one entry per trial dir
curl -s localhost:4101/api/errors | jq '. | length'
curl -s localhost:4101/api/trials/<trial_id> \
    | jq '{agent_count, llm_calls, tool_calls, reward, n_runs: (.runs | length)}'

# Every arm answers the same endpoints; `agent` names which one it is:
curl -s localhost:4101/api/trials | jq '.[] | {trial_id, agent, agent_count}'

# kimi-code specifically: lanes come from the per-agent journals, so the run
# count should match the agent directories on disk:
find <trial>/agent/kimi-code-home -path '*/agents/*/wire.jsonl' | wc -l
curl -s localhost:4101/api/trials/<trial> | jq '[.runs[].run_id]'
# expect run_root_001 + one run_sub_<agentId> per agent-* directory
```

Unit tests:

```bash
harbor/.venv/bin/pytest aos_bench/monitor/tests/ -v
# includes kimi-cli, kimi-code, Claude Code, Codex, SDK, discovery, server,
# workflow fan-out and resource paths
```

Per-arm files: `test_parser` / `test_discover` / `test_finalize` (kimi-cli) ·
`test_kimi_code_parser` / `test_kimi_code_tail` (kimi-code) ·
`test_claude_parser` / `test_claude_tail` · `test_codex_parser` /
`test_codex_tail` · `test_agent_events_tail`.
