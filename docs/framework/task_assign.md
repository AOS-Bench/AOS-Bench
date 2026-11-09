# Task Assignment: v0.3 Universal Dispatcher

> **Goal**: deliver per-task instructions and arrival timing to the agent
> **without** any of that information being readable from inside the
> agent's container before its `arrive_wall_time`. This document
> describes the v0.3 architecture (universal HTTP MCP dispatcher,
> separate from the agent container) and how it replaces the v0.1
> in-container pool.
>
> v0.2 (in-container sidecar + non-root agent + per-task workspace
> staging) was implemented but superseded by v0.3 because (a) sudoers
> narrowing kept blocking legitimate task installs, and (b) running the
> pool in the agent's container makes the agent → schedule attack
> surface chmod-only, which is too brittle.

---

## 1. Motivation

### 1.1 What we are protecting against

AOS-Bench is a *streaming* benchmark: tasks arrive at staggered
wall-clock times within one trial. The point of the stream / serial /
continual settings is to see whether an orchestrator agent can
**react** to new work instead of pre-planning a known batch. If the
agent can read the full schedule (task IDs + instruction text +
arrival times) at t=0, the experiment collapses into the burst
setting and we get no signal.

Threat model (in priority order):

1. **Schedule disclosure**: agent reads future task IDs, instruction
   text, or arrival times before `arrive_wall_time`.
2. **Pool internal state disclosure**: agent reads `events.jsonl` or
   the full `tasks.json` and learns exactly when peers were claimed,
   completed, etc., bypassing the MCP API surface.
3. **Pool source code disclosure**: agent greps the pool server's
   Python source and infers state-file paths or event semantics.
4. **Workspace inference**: agent walks `/workspace/` and guesses
   what future tasks are about from baked-in fixture filenames
   (`code.png`, `trunc.db`, `papers.tar`).

We **fully defeat** threats 1–3 in v0.3. We **accept** threat 4 (see
§1.3 below).

### 1.2 Why the v0.1 design leaks

The v0.1 pool runs **inside the agent container** and persists state
to local files:

| # | Path | Exposes | Leaks because |
|---|---|---|---|
| 1 | `/sandbox/aos/schedule.json` | full N-task schedule + instruction text + arrival times + serial flag | world-readable file in agent's own container |
| 2 | `/sandbox/aos/state/{tasks.json,events.jsonl}` | live state + history | same |
| 3 | `/opt/aos_bench/pool/{pool,server}.py` | pool source ⇒ state path conventions | uploaded so the MCP server can run as a child of the agent process |
| 4 | `/workspace/<task_id>/...` | per-task data baked at image build | needed for the task to be solvable; harder to fix |

Containers run as **root by default**, so `chmod` cannot meaningfully
isolate the agent from these files within a single UID. v0.2 tried to
fix this with `useradd aos_agent` + sudoers narrowing + per-task
workspace staging via a sidecar. The implementation worked but the
sudoers list became a never-ending whitelist (every task that needed
`mkdir /opt/foo`, `apt-get install` of an unusual package, or a
`pip install --user` plumbing trick required a new exception), and
the staging move-then-chown step added 1–3 s of latency to each
`todo_next` for skill-heavy tasks.

### 1.3 Workspace baking is acceptable

We assume an agent **cannot meaningfully infer future task content**
from `/workspace/` filenames. This holds because:

- Stream tasks are merged into a flat `/workspace/` (e.g. the
  `tb2-swe-10-42` stream puts `code.png`, `trunc.db`, `deps/illum1.pov`
  side by side). The agent sees the file set at t=0 but does **not**
  know which files belong to which task or when each task arrives.
- Task instructions are NOT baked in.
  `aos_bench.task_construction.assemble.assemble` writes
  `tasks/<task_id>/instruction.md` to the host stream directory but
  the merged Dockerfile **does not COPY** that file into the image.
  This can be checked with `grep instruction
  data/stream/<version>/*/environment/Dockerfile`, which returns nothing.
- Reward / verifier code (`tests/test.sh`) is also not baked: it is
  uploaded by `StreamVerifier._upload_tasks_tree` only after the
  agent has finished.

If a task type produces filenames so distinctive that they would tip
off the agent (e.g. a task literally named
`README_solve_this_pls.md`), that is a task-construction bug, not a
framework bug: fix it in `task_construction/`.

### 1.4 Why v0.3 over v0.2

v0.2 (in-container pool sidecar with non-root agent) and v0.3
(out-of-container HTTP dispatcher) both close threats 1–3. v0.3 wins
on:

| Property | v0.2 | v0.3 |
|---|---|---|
| Agent runtime user | non-root (`aos_agent`) | root (unchanged) |
| Pool source on agent FS | yes, at 0700 root | **no**: separate container |
| Schedule on agent FS | env var only (in pool's env) | **never on agent FS** |
| Sudoers maintenance | required & growing | not needed |
| Per-task workspace staging | required (chown latency) | not needed |
| Image rebuild needed | yes (useradd + sudoers + dirs) | minimal (just stop COPYing instruction.md if you ever did) |
| Portability to other hosts | container-only, but useradd UID conflicts on some bases | container-only, no UID assumptions |
| Multi-stream concurrent trials | one pool process per trial | one dispatcher per **job**, multiplexed by stream_id |

The price of v0.3: one extra container per job + an HTTP MCP transport
the agent CLI must support. Both kimi-cli (via FastMCP `MCPConfig`
with `transport: "http"`) and opencode (via `StreamableHTTPClientTransport`
in `@modelcontextprotocol/sdk`) support HTTP MCP natively, so the
client-side cost is zero.

---

## 2. v0.3 Architecture

**One dispatcher container per *job*** (not per trial). It serves every
trial in the job, multiplexing by `stream_id`. Lifecycle is bound to
the job, not to any individual trial's compose stack.

The diagrams and commands below write the job network as `aos-net`;
Harbor names it `aos-net-<job_id>` so that concurrent jobs on one host
do not collide (see §3.7 and D9).

```
   harbor                   ┌─ aos-bench/dispatcher:v1 ──────────────┐
   (host process)           │  uid=0, :8765                          │
       │                    │   /admin/*       (Bearer-gated)        │
       │  job start         │   /<sid_1>/mcp   (anon, agent-facing)  │
       ├──► docker run -d ──┤   /<sid_2>/mcp                         │
       │   aos_dispatcher   │   /<sid_3>/mcp   ...                   │
       │   on aos-net       │   /healthz       (anon)                │
       │                    │                                        │
       │  per trial:        │  in-memory:                            │
       │  docker exec       │   pools: dict[stream_id → TaskPool]    │
       │   <main> curl ◄────┤   <ExitStack with N session_managers>  │
       │   /admin/register  │                                        │
       │   /admin/freeze    │  bind-mounted /logs/dispatcher/<sid>/  │
       │   /admin/dump      │   events.jsonl + tasks.json on flush   │
       │   /admin/flush     │                                        │
       │   /admin/unregister│                                        │
       │                    └────────────────────▲───────────────────┘
       │                                         │ HTTP MCP
       │                                         │ DNS: aos_dispatcher
       │                          ┌──────────────┴──────────────┐
       │  per trial:              │                             │
       │  docker compose          │ trial[1]    trial[2]    ... │
       │  up -d                   │ main        main            │
       │  (trial joins            │ (sweep      (sweep          │
       │   aos-net externally)    │  image)      image)         │
       │                          │                             │
       │                          │ all on aos-net (external)   │
       │                          └─────────────────────────────┘
       │  job end
       └──► docker stop aos_dispatcher
            (SIGTERM → flush any remaining pools → exit)
```

### 2.1 Components

1. **Dispatcher container** (`aos-bench/dispatcher:v1`)
   - **One per harbor job**, regardless of trial count.
     50 concurrent trials in a job = 1 dispatcher container, not 50.
   - Held in memory: `dict[stream_id → TaskPool]` with N entries,
     one per active trial.
   - Listens on a single HTTP port `:8765`:
     - `/<stream_id>/mcp`: MCP streamable-http, anonymous (the
       agent reaches it). `stream_id` carries a per-trial UUID
       suffix (see §2.3) so two trials cannot collide.
     - `/admin/*`: Bearer-token gated. `ADMIN_TOKEN` is a fresh
       per-**job** random secret known to harbor (in its process
       env) and the dispatcher (in its container env); it never
       enters any trial container.
     - `/healthz`: anonymous, exposes nothing, used by docker
       HEALTHCHECK + harbor's "wait for dispatcher ready" loop.
   - On `/admin/flush` (per-trial) or SIGTERM (job end), pools'
     events + final state are written to a host-mounted directory
     (`HOST_DISPATCHER_LOGS_PATH`), one subdirectory per `<stream_id>`.
   - **Not published on the host**: no `ports:` mapping. Trials
     reach it on `aos-net`; harbor reaches it by execing `curl`
     inside any of the running `main` containers.

2. **Agent container** (`aos-bench/<stream>:sweep`, per-stream image)
   - **Unchanged from v0.1** in every other way: workspace baked,
     agent runs as root, no useradd, no sudoers.
   - Removed: COPY of `aos_bench/pool/` source (was 0700 root in v0.2,
     now simply absent).
   - Removed: schedule.json upload by harbor.
   - **Joined to `aos-net` as an external network** (compose
     `networks.aos-net.external: true`) so DNS name `aos_dispatcher`
     resolves automatically. Network is created once at job start,
     destroyed at job end.
   - MCP entry uses `streamable-http` transport.
   - **Instruction text NOT baked**: `task_construction/assemble`
     writes `tasks/<task_id>/instruction.md` to the *host* stream
     directory, but the merged Dockerfile does NOT COPY it into the
     image.

3. **Harbor** (`harbor/src/harbor/job.py` + `trial/aos_pool.py`)
   - **At job start** (before any trial spawns):
     1. Generate `AOS_DISPATCHER_ADMIN_TOKEN` if not pre-set
     2. `docker network create aos-net` (idempotent)
     3. `docker run -d --name aos-disp-<job_id> --network aos-net
        --network-alias aos_dispatcher -e ADMIN_TOKEN=...
        -v <host-logs>:/logs/dispatcher aos-bench/dispatcher:v1`
     4. Poll `docker exec aos-disp-<job_id> curl /healthz` until 200
   - **At trial setup**: `docker exec <main> curl -X POST
     -H 'Authorization: Bearer $TOKEN'
     http://aos_dispatcher:8765/admin/register -d <schedule>`.
     `stream_id = f"{task.name}__{uuid8}"` for uniqueness.
   - **When the agent's execution phase ends**: `POST
     /admin/freeze?stream_id=<sid>` closes the stream, then Harbor
     stops the agent's processes (§3.3).
   - **Before verification**: `GET /admin/dump?stream_id=<sid>`
     captures per-task lifecycle into `<trial>/dispatcher_state.json`
     for the verifier's strict pass-rate; see
     [`verification.md` §3.4](verification.md#34-reward-aggregation-lenient-vs-strict-pass-rate).
   - **At trial cleanup**: `/admin/flush?stream_id=<sid>` and
     `/admin/unregister?stream_id=<sid>`.
     The dispatcher writes that one stream's events.jsonl + tasks.json
     to disk; other trials keep going.
   - **At job end**: `docker stop -t 30 aos-disp-<job_id>` (SIGTERM
     gives the dispatcher 30s to flush any straggler pools);
     `docker network rm aos-net`.

### 2.2 Why one dispatcher per job (not per trial)

| Property | Per-trial (rejected) | Per-job (current) |
|---|---|---|
| Containers @ 50 concurrent trials | 50 dispatchers | 1 dispatcher |
| RSS @ 50 concurrent | ~50 × 50 MB = 2.5 GB | ~80 MB |
| Startup overhead | ~2 s × every trial (healthcheck) | one-time at job start |
| Operational view | 50 `docker logs`, scattered | one log, full job timeline |
| Stream_id collision | Impossible by scope | Impossible by UUID suffix |
| Cross-trial state leak | Physically impossible | Token-gated; even on token leak, `/admin/status` is filtered to not return instruction text |
| Single point of failure | One trial dies per dispatcher OOM | All running trials die per dispatcher OOM |
| Mitigation for SPOF | n/a | `restart: unless-stopped`; memory ceiling `O(events × streams)` <100 MB realistic |
| Harbor changes | One compose overlay | Job-level lifecycle hooks (start + stop) + external network creation |

The OOM blast-radius concern is the only real argument for per-trial.
A trivial calculation: at 50 streams × 100 events/stream × 200 bytes =
1 MB events buffer total. The dispatcher process itself is ~50 MB.
There is no realistic OOM scenario; harbor's docker `--memory=512m`
on the dispatcher gives 10× headroom. With `--restart unless-stopped`
and 30s SIGTERM grace, recovery is automatic.

The operational win (one log, one container to monitor, 2.5 GB
memory savings) is decisive in favor of per-job.

### 2.3 stream_id collision-avoidance

Since one dispatcher sees every trial's `register()`, harbor
generates:

    stream_id = re.sub(r"[^A-Za-z0-9_.-]", "-", task.name) + "__" + uuid4().hex[:8]

The UUID suffix is fresh per `_stream_id_for()` call. Re-attempts
(`n_attempt=2`) get distinct ids. Two parallel trials on the same
StreamTask get distinct ids. 8 hex chars = 4.3 × 10⁹ space; collision
probability across a 64-trial sweep is < 10⁻¹².

### 2.4 Why this is portable

The whole moving piece is one dispatcher container + one external
docker network + trial compose stacks attaching to that network.
To run on a fresh machine:

```bash
# Dispatcher image, built once locally from the repository root:
docker build -t aos-bench/dispatcher:v1 -f aos_bench/pool/Dockerfile.dispatcher .
# Task images for the released workloads are on Docker Hub
# (zhuerle/aosbench); Harbor pulls the one pinned in each stream.toml.
export AOS_DISPATCHER_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export HOST_DISPATCHER_LOGS_PATH=/path/to/your/dispatcher/logs
harbor run --aos-pool-v03 ...
```

No host-side TCP forwarding, no `/dev/kvm`, no UID conventions, no
host-resolved sockets, no host port mappings. The `--aos-pool-v03`
flag tells harbor's `Job._setup` to:
1. Create the `aos-net` external network (idempotent).
2. Start the `aos-disp-<job_id>` dispatcher container, attached to
   `aos-net` with DNS alias `aos_dispatcher`, bind-mounting host
   log dir, env carries `ADMIN_TOKEN`.
3. Wait for `/healthz` to pass before spawning any trial.
4. Set `HARBOR_AOS_POOL_V03=1` in `os.environ` so the
   `DockerEnvironment` factory appends the
   `docker-compose-aos-dispatcher.yaml` overlay, which adds
   `networks.aos-net.external: true` to each trial's compose stack.

At job end (success, failure, or interrupted), `Job._teardown`
sends SIGTERM to the dispatcher (30s grace for flush) and removes
the `aos-net` network.

---

## 3. Detailed design

### 3.1 Dispatcher process layout

`aos_bench/pool/dispatcher.py`. Single process, single uvicorn:

```python
# pseudo-code
app = Starlette(routes=[
    Route("/admin/register",   register,   methods=["POST"]),
    Route("/admin/flush",      flush,      methods=["POST"]),
    Route("/admin/freeze",     freeze,     methods=["POST"]),
    Route("/admin/unregister", unregister, methods=["POST"]),
    Route("/admin/status",     status,     methods=["GET"]),
    Route("/admin/dump",       dump,       methods=["GET"]),
    Route("/healthz",          healthz,    methods=["GET"]),
])

# In-memory state.
pools: dict[str, TaskPool] = {}

def register(req):
    check_admin_token(req)
    body = req.json()
    pool = TaskPool(in_memory=True, ...)
    pool.load_schedule(body["schedule"])
    pool.start()
    pools[body["stream_id"]] = pool
    # Mount FastMCP streamable-http subapp under /<sid>.
    sub_app = mcp.streamable_http_app()
    app.routes.append(Mount(f"/{body['stream_id']}", app=sub_app))
```

The `TaskPool` class is the one in `aos_bench/pool/pool.py` with
`in_memory=True` + `flush()`. The dispatcher is a thin multi-stream
wrapper + HTTP transport.

Admin auth is a constant-time bearer token check
(`secrets.compare_digest`). Healthz is anonymous so docker
HEALTHCHECK and compose `depends_on: service_healthy` can use it.

### 3.2 MCP transport

We use FastMCP's built-in `streamable_http_app()`. Per stream:

```python
def build_mcp_for(pool: TaskPool) -> FastMCP:
    mcp = FastMCP(name="aos_pool")
    register_tools(mcp, pool)   # same factory used by server.py
    return mcp

# At registration time:
mcp = build_mcp_for(pool)
sub_app = mcp.streamable_http_app()
app.router.routes.append(Mount(f"/{stream_id}", app=sub_app))
app.router.routes = app.routes  # force router rebuild
```

Agent-side config:

```jsonc
{
  "url": "http://aos_dispatcher:8765/{stream_id}/mcp",
  "transport": "streamable-http"
}
```

Client support:
- kimi-cli: FastMCP `MCPConfig` with `transport: "http"` (which
  fastmcp internally routes to streamable-http).
- opencode: `StreamableHTTPClientTransport` from
  `@modelcontextprotocol/sdk`, registered as `type: "remote"`.

Harbor's existing `MCPServerConfig` uses `transport="streamable-http"`,
which the kimi-cli + opencode harbor adapters convert to
`{"url": ...}` (kimi) or `{"type": "remote", "url": ...}` (opencode).

### 3.3 Lifecycle

```
T = -10s (job start)      harbor Job._setup:
                            docker network create aos-net (idempotent)
                            docker run -d --name aos-disp-<job_id>
                              --network aos-net
                              --network-alias aos_dispatcher
                              --restart unless-stopped
                              --memory 512m
                              -e ADMIN_TOKEN=<random>
                              -v <host-logs>:/logs/dispatcher
                              aos-bench/dispatcher:v1
                            poll /healthz until 200

T = -3s  (trial spawn)    harbor brings up trial compose:
                            networks.aos-net.external: true
                            (main joins the existing aos-net)
                          NB: no per-trial dispatcher service. Trial
                          compose stack only has `main`.

T = -1s  (trial setup)    harbor _setup_aos_pool_v03:
                            stream_id = "<task.name>__<uuid8>"
                            docker exec main curl -X POST
                              -H "Authorization: Bearer $TOKEN"
                              http://aos_dispatcher:8765/admin/register
                              -d {stream_id, schedule, ...}
                          dispatcher creates TaskPool, mounts /<sid>/mcp,
                          pushes session_manager.run() to AsyncExitStack

T =  0s  (agent.run)      agent makes first todo_status / todo_next call
                          → HTTP MCP to aos_dispatcher:8765/<sid>/mcp
                          → routed to TaskPool[sid].todo_next()
                          (other trials' pools untouched)

T =  Ks  (execution ends) harbor Trial._end_agent_execution:
                            docker exec main curl -X POST
                              /admin/freeze?stream_id=<sid>
                            stop every process the agent started
                            write <trial>/agent_stop.json
                          then GET /admin/dump?stream_id=<sid>
                          → <trial>/dispatcher_state.json
                          then StreamVerifier

T =  Ks' (trial done)     harbor trial cleanup:
                            docker exec main curl /admin/flush?stream_id=<sid>
                            docker exec main curl /admin/unregister?stream_id=<sid>
                          dispatcher flushes only this stream;
                          other trials' streams keep running.
                          Trial compose `down` removes main only;
                          aos-net + dispatcher untouched.

T = end  (job teardown)   harbor Job._teardown:
                            docker stop -t 30 aos-disp-<job_id>
                              (SIGTERM → flush any straggler streams
                               whose trials died without unregister)
                            docker network rm aos-net
```

The critical ordering: dispatcher must be healthy **before** any
trial spawns (otherwise the trial's first `register` call fails),
and **last** to die in cleanup (so any in-flight `flush` from a
crashing trial's harbor cleanup still has a server to talk to).

#### Freezing a stream when execution ends

`POST /admin/freeze?stream_id=<sid>` (Bearer-gated, optional `reason`
query parameter, default `execution_limit`) closes one stream. After
it, the pool promotes no further arrivals, `todo_next` returns the
`<FINISH>` sentinel without claiming anything, and `todo_update` is
rejected and logged as a `rejected_after_freeze` event. The call is
idempotent and leaves other streams untouched.

Harbor calls it whenever the agent's execution phase ends (normal
exit, failure or time limit) and before verification. It then stops
every process the agent started and writes the outcome (whether the
dispatcher was frozen, how many processes were stopped) to
`agent_stop.json` in the trial directory. A time limit only cancels
the host-side exec call, so without this step background sub-agents
could keep claiming tasks, reporting completions and writing the
workspace while the verifier grades.

### 3.4 Token-based isolation (instead of two networks)

An alternative design uses two networks (one for MCP, one for admin)
so the agent literally cannot reach the admin port. The shipped design
uses one network + bearer token because:

- Two-network setups need careful alias bookkeeping per trial,
  painful at 50 concurrent trials.
- `secrets.compare_digest` makes the token check timing-safe.
- `ADMIN_TOKEN` lives only in harbor's process env + the dispatcher
  container's env. No trial container ever sees it, so there is no
  shell-environment path from agent to token. The threat is "agent
  brute-forces a 32-character urlsafe token", which is 256-bit
  unfeasible.
- Defense in depth: `/admin/status` returns counts + flags only,
  never instruction text. Even if the token leaked, blast radius is
  reading status, not reading instructions.

### 3.5 Per-job vs per-trial isolation (security re-analysis)

Per-job dispatcher means one process holds N concurrent trials'
state. Reasons this remains safe:

1. **stream_id is a capability**. `/<sid>/mcp` is keyed on the
   8-hex UUID suffix. An attacker who somehow learned another
   trial's full `stream_id` would still hit a TaskPool whose
   schedule + instructions are only relevant to that other trial,
   and would not learn anything they didn't already know
   (the trial's own agent must have leaked the id to the attacker
   in the first place).
2. **Cross-trial probing requires guessing the UUID**. 32-bit suffix
   × at most 50 concurrent trials = `2^32 / 50 ≈ 10^8` attempts to
   land on a live `stream_id`. No realistic agent does that.
3. **No shared filesystem between trials**. Per-trial host log
   dirs (`<sid>/events.jsonl`) are dispatcher-side; agents never
   see anything but their own `/workspace/` and the MCP endpoint.

### 3.6 What gets removed from the agent image

`aos_bench/task_construction/assemble/dockerfile_merge.py`:

- v0.2 boilerplate (useradd `aos_agent`, sudoers narrowing,
  `/aos_internal/sources/` staging dirs, `/run`/socat install) is
  not needed by v0.3.
- No `COPY tasks/.../instruction.md` may appear in the merged
  Dockerfile.

`harbor/src/harbor/trial/aos_pool.py`:

- The v0.1 path (upload `pool.py`/`server.py` to `/opt/aos_bench/pool/`
  + write schedule.json) and the v0.2 path (sidecar via UDS) remain
  available behind their flags.
- The `STREAM_PREAMBLE` non-root prompt fragment ("you run as
  aos_agent…") is not used, since v0.3 keeps the agent as root.

`harbor/src/harbor/agents/installed/base.py`:

- The `runtime_user` attribute and `exec_as_agent` branching on it stay
  (v0.2 uses them) but are no-op for v0.3.

### 3.7 Failure modes

| Failure | Behavior |
|---|---|
| Dispatcher container OOM / crash | All running trials' next MCP call returns 5xx → MCP error. With `--restart unless-stopped`, dispatcher restarts in ~1s but loses in-memory state; affected trials fail their MCP calls and harbor records trial-failed. Mitigation: dispatcher RSS is dominated by `O(events × streams)` which caps at ~5 MB realistic, ~512 MB hard limit gives 100× headroom. |
| Network blip between trial and dispatcher | MCP HTTP retry; FastMCP and the kimi/opencode clients handle reconnect at the streamable-http layer. |
| Token mismatch | 403 from `/admin/*`. Harbor logs and aborts trial setup with a clear message. |
| Agent tries to call `/admin/*` | 401 (no Bearer header) or 403 (wrong token). Pool state untouched. |
| Trial killed mid-run (SIGKILL) | Trial's compose `down` removes `main`; the trial's `register`ed pool stays in the dispatcher until the job's final SIGTERM, at which point it gets flushed to disk anyway. Harbor's per-trial finalizer best-effort calls `/admin/flush` before the kill. |
| Job killed mid-sweep | Dispatcher's SIGTERM grace (30s) flushes every active pool. The job network may leak if `docker network rm` doesn't run; idempotent re-create on next job recovers. |
| Two parallel `harbor run --aos-pool-v03` jobs on same host | Each uses its own dispatcher container name (`aos-disp-<job_id>`). A single shared `aos-net` would make both resolve `aos_dispatcher`, a DNS collision, so each job gets its own network `aos-net-<job_id>`. |

### 3.8 Wall control (single-layer, outside the pool)

> **Rule of thumb**: there is exactly one wall-clock budget for a trial,
> and it is enforced outside the pool, by Harbor's agent timeout
> (`--agent-timeout-sec`). The pool does NOT enforce a wall.

A design with two timeout layers is unreliable:

1. **Pool `max_wall_sec`** (e.g. `max_steps × 60`): when `_tick_locked`
   crosses this value, the pool marks remaining non-terminal tasks as
   `TIMEOUT` and flips `stream_done = True`. The agent learns about it
   on the next `todo_next` call.
2. **Outer timeout**: Harbor's agent timeout, or an external
   `timeout(1)` wrapper around the harbor process.

The pool's TIMEOUT signal is **only delivered when the agent polls**
`todo_next`. Agents stuck in a long LLM call, a sub-agent loop, or an
external network operation never see the signal, so without a single
outer wall such trials run several times past their pool
`max_wall_sec` until the outer timeout ends them; the pool-layer
TIMEOUT does nothing.

**Design:**

- `harbor.trial.aos_pool._build_schedule` sets `max_wall_sec = 2**31-1`
  (~68 years). The pool TIMEOUT path is effectively dead code; the
  field is preserved so unit tests with explicit small values still
  work.
- When the outer wall fires, Harbor freezes the stream and stops the
  agent's processes (§3.3), so nothing the agent left running can
  change task states during verification.
- An external `timeout --kill-after=30 $WALL` around the harbor
  process can still serve as a hard backstop: SIGTERM at `$WALL`, 30 s
  to flush, then SIGKILL.

**Implications for agents:**

- The `stream_done: true` signal in `todo_next` is not correlated
  with a wall budget: it only fires on the natural `all_completed`
  path (every arrival fired and every task `todo_update`d to terminal)
  or after a freeze. Agents should not assume a long-running trial
  will be wall-limited by the pool.
- If an agent does want to know "how long do I have?", expose it via
  the launcher (e.g. set a `$TRIAL_WALL_SECONDS` env via `--ae`); the
  pool doesn't know.

**JSON note:** the schedule travels over MCP HTTP; `math.inf` is not
standard JSON, so we use the finite sentinel `2**31-1` instead of
`Infinity`.

---

## 4. Validation matrix

Run after an image rebuild:

| Test | Command (inside agent container) | Expected |
|---|---|---|
| 1. Schedule files absent | `find / -path '*sandbox/aos*'` | empty |
| 2. Pool source absent | `ls /opt/aos_bench 2>&1`, `find / -name 'pool.py' -path '*aos_bench*'` | not found |
| 3. Workspace has no instruction.md | `find /workspace -name 'instruction.md'` | empty |
| 4. Admin endpoint requires token | `curl -m 2 http://aos_dispatcher:8765/admin/status` | HTTP 401 |
| 5. Admin endpoint rejects wrong token | `curl -m 2 -H 'Authorization: Bearer wrong' http://aos_dispatcher:8765/admin/status` | HTTP 403 |
| 6. MCP port reachable | `curl -m 2 http://aos_dispatcher:8765/<sid>/mcp` (POST initialize) | 200 with `Mcp-Session-Id` header |
| 7. Token env var absent in agent's env | `printenv \| grep -i ADMIN_TOKEN` | empty |
| 8. Schedule env var absent in agent's env | `printenv \| grep -i AOS_SCHEDULE` | empty |
| 9. Cross-trial probing | `curl /<other_trials_stream_id>/mcp` (any random UUID) | 404 if not registered; if random hits a registered sid, attacker only learns it exists, not its instructions |
| 10. Concurrent 50-trial sweep | `docker stats aos-disp-<job_id>` mid-sweep | RSS < 200 MB, single process serves all |
| 11. Reward parity vs v0.1 | re-run the same stream and agent under v0.1 and v0.3 | within run-to-run noise (~±1.0 / 10) |

Tests 1, 2, 3, 4, 5, 7, 8 are the hard "did the leak close" checks.

---

## 5. Open issues

1. **Single dispatcher SPOF**: one OOM kills all parallel trials in
   the job. Mitigation: dispatcher is tiny (no LLM calls, no model
   weights), memory ceiling is `O(events × streams)` ~ <100 MB even
   with 50 trials. If this becomes a real problem, shard by
   `hash(stream_id) % K` dispatchers.
2. **Streamable-HTTP keepalive on flaky networks**: not relevant
   inside a docker-bridge; mention only for cross-host deployments.
3. **events.jsonl ordering across trials**: each trial's events live
   in their own `<sid>/events.jsonl`, so timeline analysis joining
   trials needs the dispatcher's wall-clock to write `recorded_at`.
   The pool already does this.
4. **State recovery after dispatcher restart**: not implemented in
   v0.3. If dispatcher crashes mid-trial, the trial fails. A
   write-ahead log per stream would be needed.

---

## 6. Design decisions

- D1: **Universal dispatcher container** (not per-stream). One image,
  multiplex by stream_id.
- D2: **Workspace stays baked** in agent image. Instruction.md NOT
  baked.
- D3: **HTTP MCP** (FastMCP streamable-http). Supported by kimi-cli
  (FastMCP backend) and opencode (StreamableHTTPClient).
- D4: **/workspace layout unchanged** from v0.1 (flat merged
  workspace; agent sees fixture filenames at t=0).
- D5: **Agent runs as root**, in-container, full sudo. No useradd,
  no sudoers narrowing.
- D6: **v0.1 in-container pool retained only behind its flag**; v0.3
  is the supported path.
- D7: **One dispatcher per harbor job**, not per trial. Lifecycle
  managed by `Job._setup` / `Job._teardown`, not by trial compose.
  Stream_id collisions handled by per-trial UUID suffix.
- D8: **Single-port dispatcher with bearer-token admin**, not
  two-port two-network. `ADMIN_TOKEN` only lives in harbor's env
  and the dispatcher container's env; never enters a trial.
- D9: **Per-job docker network `aos-net-<job_id>`**, not a global
  `aos-net`. Allows multiple concurrent v0.3 jobs on one host.
