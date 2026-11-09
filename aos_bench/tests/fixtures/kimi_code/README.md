# `kimi-code` ground-truth fixtures

Provenance record for the `kimi-code` arm (blueprint: `docs/tasks/kimi_code_integration.md`).
Everything here is *observed output*, not hand-written: either a byte-real copy of an upstream
committed fixture, or the on-disk product of a real `kimi -p` run against the **published**
CLI. Downstream lanes (trace reader, monitor parser/tail, adapter tests) should trust these
files over any claim derived from reading the upstream TypeScript sources.

**CLI under test:** `@moonshot-ai/kimi-code@0.36.1`, installed from npm into an isolated prefix
(`npm install --prefix <scratch> @moonshot-ai/kimi-code@0.36.1`), entrypoint
`dist/main.mjs`, `engines.node >= 22.19.0`, probed on node v24.14.1 / npm 11.11.0.
`kimi --version` → `0.36.1`.

**No model API was ever called.** Every live capture ran against a local
OpenAI-protocol stub on `127.0.0.1` wired up through `KIMI_MODEL_NAME` /
`KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL` / `KIMI_MODEL_PROVIDER_TYPE=openai`. The stub
scripts the assistant turns (a `Bash` call, an `Agent` call, an `AgentSwarm` call, …) so each
capture exercises one structural case deliberately. Assistant text therefore reads
`reply N. done.` — that is the stub talking, not a model.
Common env for every capture: `KIMI_CODE_HOME=<scratch>/kc-home*`,
`KIMI_CODE_NO_AUTO_UPDATE=1`, `KIMI_DISABLE_CRON=1`, no credentials on the shell for any real
provider.

Nothing key-like is present: the journals carry no API key, no base URL, no port, and no
device id (`llm.request` records only `provider`, `model`, `modelAlias`, hashes and counts).
Absolute paths under `/home/...` are kept verbatim — they are part of the record shapes
(`state.json.agents.<id>.homedir`, `session_index.jsonl.sessionDir`) that the readers parse.

## Layout

```
vis_sample/sessions/       byte-real copy of the upstream committed vis fixtures
live_probe/solo/           real run: one Bash tool call, single agent
live_probe/subagent/       real run: foreground `Agent` call → agents/agent-0
live_probe/swarm/          real run: `AgentSwarm` with 2 items → agent-0 + agent-1
live_probe/background/     real run: `Agent` with run_in_background → tasks/<id>{,.json}
live_probe/mcp_v2_no_tools/ real run: user-layer mcp.json, DEFAULT (v2) engine — no MCP tools
                            NOTE: a STUB artifact, not product behaviour. Against the real
                            FastMCP aos_pool the v2 engine DOES register the tools (verified
                            live 2026-08-18). Kept only as a no-mcp-record parser fixture.
live_probe/mcp_legacy/      real run: same mcp.json, KIMI_CODE_LEGACY_FLAG=1 — MCP tools present
live_probe/tools/elide_wire.py  the shrink script described below
```

Each `live_probe/<case>/` mirrors a real `$KIMI_CODE_HOME` subtree plus the two process
streams:

* `sessions/wd_<slug>_<sha256[:12]>/session_<uuid>/agents/<agentId>/wire.jsonl`
* `sessions/.../state.json`, and for the background case
  `agents/main/tasks/<taskId>.json` + `agents/main/tasks/<taskId>/output.log`
* `session_index.jsonl` — the home-level discovery index (`{sessionId, sessionDir, workDir}`)
* `stdout.jsonl` — exactly what `--output-format stream-json` wrote to fd 1
* `stderr.txt` — exactly what the process wrote to fd 2
* `mcp.json` (MCP cases only) — the user-layer config that produced the run

The per-session `logs/kimi-code.log` was dropped (diagnostic noise, no record shapes).

## Real-run vs upstream-fixture, and which engine wrote what

`kimi-code` ships **two** agent engines, and they write **different record vocabularies into
the same directory layout**:

| | default engine (agent-core-v2) | legacy engine (`KIMI_CODE_LEGACY_FLAG=1`) |
| --- | --- | --- |
| `metadata.protocol_version` | `1.5` | `1.4` |
| session/profile records | `profile.bind`, `plugin.session_start` | `config.update` ×2, `tools.set_active_tools` |
| turn close | `turn.ended` | *(none observed)* |
| `state.json` timestamps | epoch **ms** ints, plus `id` / `version: 2` / `cwd` / `archived` / `lastTurnReason` | **ISO strings**, plus `workDir` / `lastPrompt`, no `id` / `version` / `cwd` |

* `live_probe/*` (except `mcp_legacy`) — **default v2 engine**, `protocol_version 1.5`. This is
  what an AOS-Bench trial will actually produce.
* `live_probe/mcp_legacy/` — **legacy engine**, `protocol_version 1.4`.
* `vis_sample/` — byte-real upstream fixtures, `protocol_version **1.1**`, and shaped like the
  **legacy** engine (`config.update` + `tools.set_active_tools`, ISO-string `state.json`).
  They are authoritative for *older* journals a migration path must still read, **not** for
  what the shipped default engine writes. Do not derive the v1 kind map from them alone.

Upstream source of `vis_sample/`:
`aos_bench/harness/kimi-code/apps/vis/server/test/fixtures/sessions/` (git-ignored local
clone), copied unmodified.

## The one transformation applied (and where it is not)

`live_probe/solo/` is **fully byte-real**, including the two very large records:
`llm.tools_snapshot.tools` (all 25 built-in tool JSON-Schemas, ~70 KB) and
`profile.bind.systemPrompt` (~24 KB). Use it whenever the exact shape of those two matters.

Every other `live_probe/<case>/wire.jsonl` was passed through
`live_probe/tools/elide_wire.py`, which replaces **only** those two field values with an
`"<elided: … >"` marker and re-serializes each line compactly. Record order, record types and
every other key/value are unchanged; the sibling `hash` fields are left intact so the elision
is obvious. Without it the tree would be ~800 KB of duplicated tool schemas.

`state.json`, `session_index.jsonl`, `stdout.jsonl`, `stderr.txt`, the task sidecars and all of
`vis_sample/` are untouched.

## Observed `.type` histograms

`jq -r .type <wire.jsonl> | sort | uniq -c`, with `context.append_loop_event` broken out by
`.event.type`:

| fixture | pv | record types | loop events |
| --- | --- | --- | --- |
| `solo/…/main` | 1.5 | `context.append_loop_event`×7, `context.append_message`×2, `llm.request`×2, `llm.tools_snapshot`×1, `metadata`×1, `permission.set_mode`×1, `plugin.session_start`×1, `profile.bind`×1, `turn.ended`×1, `turn.prompt`×1, `usage.record`×2 | `step.begin`×2, `step.end`×2, `content.part`×1, `tool.call`×1, `tool.result`×1 |
| `subagent/…/main` | 1.5 | same as `solo/…/main` | same as `solo/…/main` |
| `subagent/…/agent-0` | 1.5 | `context.append_loop_event`×6, `context.append_message`×3, `llm.request`×2, `llm.tools_snapshot`×1, `metadata`×1, `permission.set_mode`×1, `profile.bind`×1, `turn.ended`×2, `turn.prompt`×2, `usage.record`×2 | `step.begin`×2, `step.end`×2, `content.part`×2 |
| `swarm/…/main` | 1.5 | as `solo/…/main` **plus** `swarm_mode.enter`×1, `swarm_mode.exit`×1 | as `solo/…/main` |
| `swarm/…/agent-0`, `…/agent-1` | 1.5 | as `subagent/…/agent-0` | `step.begin`×2, `step.end`×2, `content.part`×2 |
| `background/…/main` | 1.5 | `context.append_loop_event`×10, `context.append_message`×3, `llm.request`×3, `llm.tools_snapshot`×1, `metadata`×1, `permission.set_mode`×1, `plugin.session_start`×1, `profile.bind`×1, **`task.started`×1**, **`task.terminated`×1**, `turn.ended`×2, `turn.prompt`×2, `usage.record`×3 | `step.begin`×3, `step.end`×3, `content.part`×2, `tool.call`×1, `tool.result`×1 |
| `background/…/agent-0` | 1.5 | as `subagent/…/agent-0` | `step.begin`×2, `step.end`×2, `content.part`×2 |
| `mcp_v2_no_tools/…/main` | 1.5 | `context.append_loop_event`×3, `context.append_message`×2, `llm.request`×1, `llm.tools_snapshot`×1, `metadata`×1, `permission.set_mode`×1, `plugin.session_start`×1, `profile.bind`×1, `turn.ended`×1, `turn.prompt`×1, `usage.record`×1 — **no `mcp.*` record** | `step.begin`×1, `step.end`×1, `content.part`×1 |
| `mcp_legacy/…/main` | 1.4 | `config.update`×2, `context.append_loop_event`×3, `context.append_message`×2, `llm.request`×1, `llm.tools_snapshot`×1, **`mcp.tools_discovered`×1**, `metadata`×1, `permission.set_mode`×1, `tools.set_active_tools`×1, `turn.prompt`×1, `usage.record`×1 | `step.begin`×1, `step.end`×1, `content.part`×1 |
| `vis_sample/sample-main/main` | 1.1 | `config.update`×1, `context.append_loop_event`×3, `context.append_message`×1, `metadata`×1, `permission.set_mode`×1, `tools.set_active_tools`×1, `turn.prompt`×1, `usage.record`×1 | `step.begin`×1, `step.end`×1, `content.part`×1 |
| `vis_sample/sample-main/agent-0` | 1.1 | `config.update`×1, `metadata`×1 | — |
| `vis_sample/sample-compaction/main` | 1.1 | `config.update`×1, `context.append_message`×3, `context.apply_compaction`×1, `metadata`×1 | — |

Union of every record type observed anywhere in this tree — the only types for which a live
payload shape is on record:

`metadata`, `profile.bind`, `config.update`, `tools.set_active_tools`, `permission.set_mode`,
`plugin.session_start`, `turn.prompt`, `turn.ended`, `context.append_message`,
`context.append_loop_event` (`step.begin`, `step.end`, `content.part`, `tool.call`,
`tool.result`), `llm.tools_snapshot`, `llm.request`, `usage.record`, `task.started`,
`task.terminated`, `swarm_mode.enter`, `swarm_mode.exit`, `mcp.tools_discovered`,
`context.apply_compaction`.

Nothing here exercises `turn.steer`, `turn.cancel`, `token_counting.*`, `full_compaction.*`,
`interaction.request` / `interaction.resolved`, `permission.record_approval_result`,
`plan_mode.*`, `plan.revision`, `goal.*`, `forked`, `context.clear`, `context.undo`,
`interruptionReminder.recorded`, `content.part` with `part.type == "think"`, or
`context.append_message` with `role` ∈ {`assistant`, `tool`, `system`} — treat any handling of
those as source-derived, not evidence-backed.

## Facts these fixtures pin down

* Session path shape: `<home>/sessions/wd_<slug(basename)>_<sha256(workdir)[:12]>/session_<uuid>/agents/<agentId>/wire.jsonl`;
  root agent id is `main`, children are `agent-0`, `agent-1`, … monotonic per session.
* `metadata` is line 1 and carries `created_at` (epoch **ms**) and no `time`; every other
  record carries `time` (epoch **ms**).
* `event.step` restarts at 1 **per turn** (see `subagent/…/agent-0`: `turnId "0"` step 1, then
  `turnId "1"` step 1) — it is not a per-agent monotonic counter.
* `turn.ended.turnId` is a **number**; loop events' `turnId` is a **string**.
* Agent parentage lives in `state.json` → `agents.<id>` as
  `{homedir, type: "main"|"sub", parentAgentId, labels:{parentAgentId, swarmItem?}}`. `agents`
  key order is **not** sorted (`swarm/state.json` lists `agent-1` before `agent-0`).
* Foreground `Agent` results name the child explicitly in
  `tool.result.result.output`: `"agent_id: agent-0\nactual_subagent_type: coder\nstatus: completed\n\n[summary]…"`.
  `AgentSwarm` results use
  `<subagent agent_id="agent-0" item="alpha" outcome="completed">…</subagent>`.
* Background delegation writes `task.started` / `task.terminated` whose `info` block carries
  `parentToolCallId` **and** `agentId` — an exact tool-call → child-agent join, no heuristic.
* `stderr.txt` in `solo/` contains `README.txt` — the `Bash` tool's combined output is echoed
  onto the CLI's **fd 2** while fd 1 stays clean JSONL. Do not merge the streams if you intend
  to parse fd 1.
* `stdout.jsonl` opens with `{"role":"meta","type":"system.version",…}` and closes with
  `{"role":"meta","type":"session.resume_hint","session_id":…}` — the session id is
  recoverable from stdout alone.
