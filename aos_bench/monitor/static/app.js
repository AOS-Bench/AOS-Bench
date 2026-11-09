// AOSBench Eval Monitor — vanilla JS dashboard (V0).
// No build step; talks to /api/* endpoints, listens to /api/events SSE.

const state = {
  jobs_dir: null,
  trials: new Map(),          // trial_id -> summary
  trialDetailCache: new Map(), // trial_id -> last fetched detail
  trialResourceCache: new Map(), // trial_id -> [ContainerSample]
  trialProcessCache: new Map(),  // trial_id -> latest process snapshot document
  trialTasksCache: new Map(),    // trial_id -> {trial_started_at, tasks: [...]}
  errors: [],
  resourceLatest: null,
  activeTab: "overview",
  selectedTrialId: null,
  detailRequestSeq: 0,
  expandedRuns: new Set(),
  // Time-axis zoom/pan: viewWindow = {t0, t1} in wall-clock seconds.
  // fullTimeRange tracks the data's natural range so "Reset Zoom" returns there.
  viewWindow: null,
  fullTimeRange: null,
  // Each trial gets its own viewWindow that persists across re-renders.
  trialViewWindows: new Map(),
};

// ── tab switching ──
document.querySelectorAll("nav button").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    const tabName = btn.dataset.tab;
    document.getElementById("tab-" + tabName).classList.add("active");
    state.activeTab = tabName;
    if (tabName === "trial" && state.selectedTrialId) loadTrialDetail(state.selectedTrialId);
    if (tabName === "errors") renderErrors();
  });
});

document.getElementById("trial-select").addEventListener("change", e => {
  state.selectedTrialId = e.target.value;
  loadTrialDetail(state.selectedTrialId);
});
document.getElementById("trial-refresh").addEventListener("click", () => {
  if (state.selectedTrialId) loadTrialDetail(state.selectedTrialId);
});

// ── initial loads ──
async function bootstrap() {
  try {
    const jobs = await fetchJSON("/api/jobs");
    state.jobs_dir = jobs.jobs_dir;
    document.getElementById("jobs-dir").textContent = "jobs_dir: " + jobs.jobs_dir;
  } catch (e) { /* ignore */ }
  await refreshTrials();
  await refreshErrors();
  await refreshResources();
  startSSE();
}

async function refreshTrials() {
  try {
    const trials = await fetchJSON("/api/trials");
    state.trials.clear();
    for (const t of trials) state.trials.set(t.trial_id, t);
    renderTrialsTable();
    renderTrialSelect();
    renderKPIs();
  } catch (e) { console.warn("refreshTrials", e); }
}

async function refreshErrors() {
  try {
    state.errors = await fetchJSON("/api/errors");
    renderErrors();
  } catch (e) { console.warn("refreshErrors", e); }
}

async function refreshResources() {
  try {
    const r = await fetchJSON("/api/resources");
    state.resourceLatest = r.latest;
    renderKPIs();
  } catch (e) { console.warn("refreshResources", e); }
}

async function loadTrialDetail(trialId) {
  const requestSeq = ++state.detailRequestSeq;
  if (state.detailAbortController) state.detailAbortController.abort();
  const controller = new AbortController();
  state.detailAbortController = controller;
  const detailRoot = document.getElementById("trial-detail");
  if (!state.trialDetailCache.has(trialId)) {
    detailRoot.innerHTML = '<div class="empty">Loading trajectory…</div>';
  }
  try {
    const [detail, res, tasksDoc, processDoc] = await Promise.all([
      fetchJSON("/api/trials/" + encodeURIComponent(trialId), { signal: controller.signal }),
      fetchJSON("/api/trials/" + encodeURIComponent(trialId) + "/resources", { signal: controller.signal }).catch(() => ({ samples: [] })),
      fetchJSON("/api/trials/" + encodeURIComponent(trialId) + "/tasks", { signal: controller.signal }).catch(() => ({ tasks: [] })),
      fetchJSON("/api/trials/" + encodeURIComponent(trialId) + "/processes", { signal: controller.signal }).catch(() => ({ sample_count: 0, latest: null, peaks: [] })),
    ]);
    // A large, older request may finish after the user has selected another
    // trial. Never let that stale response overwrite the current selection.
    if (requestSeq !== state.detailRequestSeq || trialId !== state.selectedTrialId) return;
    state.trialDetailCache.set(trialId, detail);
    state.trialResourceCache.set(trialId, res.samples || []);
    state.trialTasksCache.set(trialId, tasksDoc);
    state.trialProcessCache.set(trialId, processDoc);
    // Skip the (full DOM teardown) re-render when nothing observable
    // changed — completed trials otherwise re-render on every SSE tick,
    // which is what made the page twitch and lose scroll position.
    const sig = JSON.stringify([
      trialId,
      detail.status,
      (detail.runs || []).map(r => [r.run_id, r.n_events, r.status]),
      (res.samples || []).length,
      ((tasksDoc && tasksDoc.tasks) || []).length,
      processDoc.sample_count || 0,
      processDoc.latest?.ts || null,
    ]);
    if (sig === state._lastDetailSig) return;
    state._lastDetailSig = sig;
    renderTrialDetail(detail);
  } catch (e) {
    if (e && e.name === "AbortError") return;
    if (requestSeq !== state.detailRequestSeq || trialId !== state.selectedTrialId) return;
    document.getElementById("trial-detail").textContent = "error: " + e;
  }
}

// ── rendering ──
function renderKPIs() {
  const trials = Array.from(state.trials.values());
  document.getElementById("kpi-trials").textContent = trials.length;
  document.getElementById("kpi-running").textContent = trials.filter(t => t.status === "running").length;
  document.getElementById("kpi-completed").textContent = trials.filter(t => t.status === "completed").length;
  document.getElementById("kpi-errors").textContent = state.errors.length;
  if (state.resourceLatest) {
    const cpu = Number(state.resourceLatest.cpu_percent);
    const mem = Number(state.resourceLatest.mem_percent);
    const cpuText = Number.isFinite(cpu) ? cpu.toFixed(1) : "–";
    const memText = Number.isFinite(mem) ? mem.toFixed(1) : "–";
    document.getElementById("kpi-cpu").textContent = cpuText;
    document.getElementById("kpi-mem").textContent = memText;
    document.getElementById("host-resource-status").textContent =
      `Host CPU ${cpuText}% · Mem ${memText}%`;
  }
}

// ── grouping (wandb-style project/group) ──
//
// Trials get grouped into a two-level tree by parsing trial_dir for a
// ``/monitor_view/<project>/<group>/…`` layout. Operators use a curated
// ``monitor_view/`` symlink dir to bucket runs by experiment phase + agent
// (see scripts/local/* docs). Trials outside that prefix fall into
// ``(ungrouped)/(ungrouped)`` so the table always renders something.
const UNGROUPED = "(ungrouped)";

function extractGroup(trial) {
  if (trial.setting_name) {
    return { project: "settings", group: trial.setting_name };
  }
  const trialDir = trial.trial_dir;
  const trialId = trial.trial_id;
  const marker = "/monitor_view/";
  const idx = (trialDir || "").indexOf(marker);
  if (idx >= 0) {
    const tail = trialDir.slice(idx + marker.length);
    const parts = tail.split("/");
    return {
      project: parts[0] || UNGROUPED,
      group: parts[1] || UNGROUPED,
    };
  }
  // Canonical/replay views use flat symlink names:
  //   <setting>--<stream>--<native-trial-id>
  // Fall back to the stable setting prefix so claude-glm52, claude-opus48,
  // codex-gpt55, etc. each get their own group without requiring a special
  // directory layout.
  const id = String(trialId || "");
  const sep = id.indexOf("--");
  if (sep > 0) return { project: "settings", group: id.slice(0, sep) };
  // A direct Harbor invocation usually exposes only the native trial id
  // (e.g. light-5-scan-007__hash), while its parent job directory carries
  // the setting: claude-glm52-light-5-scan-007. Recover that prefix too.
  const pathParts = String(trialDir || "").split("/").filter(Boolean);
  for (let i = pathParts.length - 2; i >= 0; i--) {
    const match = pathParts[i].match(/^(.+?)-(?:light|medium|heavy)-\d+-/);
    if (match) return { project: "settings", group: match[1] };
  }
  return { project: UNGROUPED, group: UNGROUPED };
}

// State for which groups are collapsed (persist across re-renders).
const collapsedGroups = new Set();
function groupKey(project, group) { return `${project}/${group}`; }

function renderTrialsTable() {
  const tbody = document.querySelector("#trials-table tbody");
  tbody.innerHTML = "";

  // Group trials by (project, group), then sort within each group by trial_id.
  const groups = new Map(); // "project/group" -> { project, group, trials: [] }
  for (const t of state.trials.values()) {
    const { project, group } = extractGroup(t);
    const key = groupKey(project, group);
    if (!groups.has(key)) groups.set(key, { project, group, trials: [] });
    groups.get(key).trials.push(t);
  }
  for (const g of groups.values()) {
    g.trials.sort((a, b) => a.trial_id.localeCompare(b.trial_id));
  }

  const sortedGroups = Array.from(groups.values()).sort((a, b) => {
    if (a.project !== b.project) return a.project.localeCompare(b.project);
    return a.group.localeCompare(b.group);
  });

  // Number of <td> columns in the body row — must match index.html <thead>.
  const COLSPAN = 10;

  let lastProject = null;
  for (const g of sortedGroups) {
    // Aggregate stats for the group header.
    const n = g.trials.length;
    const rewards = g.trials.map(t => t.reward).filter(r => r != null);
    const wins = rewards.filter(r => r > 0).length;
    const avg = rewards.length > 0 ? rewards.reduce((s, r) => s + r, 0) / rewards.length : null;
    const running = g.trials.filter(t => t.status === "running").length;
    const completed = g.trials.filter(t => t.status === "completed").length;

    // First level — project (phase) divider, rendered once per project.
    if (g.project !== lastProject) {
      const ptr = document.createElement("tr");
      ptr.className = "group-l1";
      ptr.innerHTML = `<td colspan="${COLSPAN}"><span class="group-l1-label">${escape(g.project)}</span></td>`;
      tbody.appendChild(ptr);
      lastProject = g.project;
    }

    // Second level — group (agent) header, collapsible.
    const key = groupKey(g.project, g.group);
    const collapsed = collapsedGroups.has(key);
    const gtr = document.createElement("tr");
    gtr.className = "group-l2";
    gtr.dataset.groupKey = key;
    const avgStr = avg == null ? "–" : avg.toFixed(3);
    const rewardSummary = rewards.length === 0
      ? "no rewards"
      : `${wins}/${rewards.length} >0 · avg ${avgStr}`;
    const caret = collapsed ? "▶" : "▼";
    gtr.innerHTML = `<td colspan="${COLSPAN}">
      <span class="group-caret">${caret}</span>
      <span class="group-l2-label">${escape(g.group)}</span>
      <span class="group-stats">${n} trials · ${running} running · ${completed} done · ${rewardSummary}</span>
    </td>`;
    gtr.addEventListener("click", () => {
      if (collapsedGroups.has(key)) collapsedGroups.delete(key);
      else collapsedGroups.add(key);
      renderTrialsTable();
    });
    tbody.appendChild(gtr);

    if (collapsed) continue;

    for (const t of g.trials) {
      const tr = document.createElement("tr");
      tr.dataset.trialId = t.trial_id;
      tr.className = "group-l2-row";
      tr.innerHTML = `
        <td title="${escape(t.trial_id)}">${escape(trialDisplayName(t))}</td>
        <td>${escape(t.attempt_id || "–")}</td>
        <td>${escape(t.agent || "–")}</td>
        <td class="status-${t.status}">${escape(t.status)}</td>
        <td>${t.agent_count}</td>
        <td>${t.llm_calls}</td>
        <td>${t.tool_calls}</td>
        <td>${t.last_event_age_s == null ? "–" : t.last_event_age_s.toFixed(0) + "s"}</td>
        <td>${t.reward == null ? "–" : t.reward.toFixed(2)}</td>
        <td>${t.errors_count || 0}</td>
      `;
      tr.addEventListener("click", () => {
        state.selectedTrialId = t.trial_id;
        document.getElementById("trial-select").value = t.trial_id;
        document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
        document.querySelector('nav button[data-tab="trial"]').classList.add("active");
        document.querySelectorAll(".tab").forEach(tab => tab.classList.remove("active"));
        document.getElementById("tab-trial").classList.add("active");
        state.activeTab = "trial";
        loadTrialDetail(t.trial_id);
      });
      tbody.appendChild(tr);
    }
  }
}

function renderTrialSelect() {
  const sel = document.getElementById("trial-select");
  const prev = sel.value;
  sel.innerHTML = "";
  for (const t of Array.from(state.trials.values()).sort((a, b) => a.trial_id.localeCompare(b.trial_id))) {
    const opt = document.createElement("option");
    opt.value = t.trial_id;
    const attempt = t.attempt_id ? ` — attempt ${t.attempt_id}` : "";
    opt.textContent = trialDisplayName(t) + attempt + " — " + t.status;
    sel.appendChild(opt);
  }
  if (prev && state.trials.has(prev)) sel.value = prev;
  else if (sel.options.length > 0 && !state.selectedTrialId) state.selectedTrialId = sel.options[0].value;
  if (state.selectedTrialId) sel.value = state.selectedTrialId;
}

function renderTrialDetail(detail) {
  const root = document.getElementById("trial-detail");
  const meta = document.getElementById("trial-meta");
  // Measure BEFORE wiping: reading clientWidth after innerHTML="" forces a
  // reflow on the emptied container — the document height collapses and the
  // browser clamps the scroll position to the top mid-render.
  const measuredW = root.clientWidth;
  const scrollYBefore = window.scrollY;
  root.innerHTML = "";
  if (!detail || !detail.runs || detail.runs.length === 0) {
    // Arm-agnostic: every tail waits on a different file (kimi-cli/kimi-code
    // wire.jsonl, claude-code / codex *.jsonl, the SDK harness events.jsonl),
    // and the trial is typed before any of them exists.
    root.innerHTML = '<div class="empty">(no runs yet — waiting for the agent to write its session log)</div>';
    meta.textContent = "";
    return;
  }
  // ── 1. Build run tree (root + spawn order) ──
  const runsByParent = new Map();
  for (const r of detail.runs) {
    const key = r.parent_run_id || "_root";
    if (!runsByParent.has(key)) runsByParent.set(key, []);
    runsByParent.get(key).push(r);
  }
  for (const arr of runsByParent.values()) arr.sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
  const lanes = [];
  function walk(parentId, depth) {
    for (const r of runsByParent.get(parentId) || []) {
      lanes.push({ run: r, depth });
      walk(r.run_id, depth + 1);
    }
  }
  walk("_root", 0);
  if (lanes.length === 0) {
    root.innerHTML = '<div class="empty">(no root run discovered)</div>';
    return;
  }

  // ── 2. Compute FULL time range across all data sources ──
  const allTs = [];
  for (const { run } of lanes) {
    if (typeof run.started_at === "number") allTs.push(run.started_at);
    if (typeof run.last_event_at === "number") allTs.push(run.last_event_at);
    for (const ev of (run.events_tail || [])) {
      if (typeof ev.timestamp === "number") allTs.push(ev.timestamp);
    }
  }
  const tasksDoc = state.trialTasksCache.get(detail.trial_id);
  if (tasksDoc && tasksDoc.trial_started_at) {
    const ts0 = tasksDoc.trial_started_at;
    for (const t of (tasksDoc.tasks || [])) {
      for (const k of ["at_sec", "ready_at", "claimed_at", "completed_at"]) {
        const v = t[k];
        if (typeof v === "number") allTs.push(ts0 + v);
      }
    }
  }
  const resSamples = state.trialResourceCache.get(detail.trial_id) || [];
  const processDoc = state.trialProcessCache.get(detail.trial_id) || {};
  for (const s of resSamples) {
    if (typeof s.ts === "number") allTs.push(s.ts);
  }
  if (allTs.length === 0) {
    root.innerHTML = '<div class="empty">(events have no timestamps)</div>';
    return;
  }
  const fullT0 = Math.min(...allTs);
  const fullT1 = Math.max(...allTs);

  // ── 3. Resolve viewWindow (zoom/pan state) ──
  // Per-trial view window so switching trials doesn't reset zoom.
  let view = state.trialViewWindows.get(detail.trial_id);
  if (!view) {
    view = { t0: fullT0, t1: fullT1 };
    state.trialViewWindows.set(detail.trial_id, view);
  } else {
    // Trial may still be running — fullT0/fullT1 can grow each tick.
    // Clamp the user's view INSIDE the data range, but otherwise leave
    // it alone. Earlier code did `view.t0 = Math.min(view.t0, fullT0)`
    // which mutated the stored object on every rerender and silently
    // dragged t0 back toward the trial start, making pan look like
    // single-edge resize after the first frame.
    if (view.t0 < fullT0) view.t0 = fullT0;
    if (view.t1 > fullT1) view.t1 = fullT1;
  }
  state.fullTimeRange = { t0: fullT0, t1: fullT1 };

  // ── 4. Layout dimensions ──
  const LANE_H = 36;
  const TASK_ROW_H = 22;
  const LABEL_W = 180;
  const PADDING_R = 30;
  const TOP_AXIS = 28;
  const BOTTOM = 28;
  const containerW = measuredW || 1200;
  const plotW = Math.max(400, containerW - LABEL_W - PADDING_R);

  // ── 5. Update meta header ──
  const durFull = Math.max(0.001, fullT1 - fullT0);
  const attempt = detail.attempt_id ? ` · attempt ${detail.attempt_id}` : "";
  meta.textContent = `${trialDisplayName(detail)}${attempt} · ${lanes.length} agent${lanes.length > 1 ? "s" : ""} · span ${formatDur(durFull)} · ${detail.runs.reduce((s, r) => s + r.n_events, 0)} events`;

  // ── 6. Container resource chart (above the panels) ──
  if (resSamples.length > 0) {
    root.appendChild(renderContainerChart(resSamples, {
      t0: view.t0, tEnd: view.t1, LABEL_W, plotW, containerW,
    }));
    root.appendChild(renderContainerStats(resSamples, view.t0, view.t1));
  } else {
    const notice = document.createElement("div");
    notice.className = "trial-resource-empty";
    notice.textContent =
      "No historical per-trial CPU/Mem samples. Current host usage is shown in the header.";
    root.appendChild(notice);
  }
  if (processDoc.latest) root.appendChild(renderProcessSnapshot(processDoc));

  // ── 7. Two big panels ──
  const panelsWrap = document.createElement("div");
  panelsWrap.className = "trial-panels";

  // Visible scrubber track at the TOP — shows the current view window's
  // position in the full trial timeline. Drag the thumb to pan, drag
  // its edges to resize (= zoom), click empty track to jump.
  const scrubber = renderScrubber({
    trialId: detail.trial_id, fullT0, fullT1, view, LABEL_W, plotW, containerW,
  });
  panelsWrap.appendChild(scrubber);

  const agentPanel = renderAgentPanel({
    lanes, runsByParent, viewT0: view.t0, viewT1: view.t1,
    LABEL_W, plotW, containerW, TOP_AXIS, LANE_H, BOTTOM,
  });
  panelsWrap.appendChild(agentPanel);

  if (tasksDoc && tasksDoc.tasks && tasksDoc.tasks.length > 0) {
    // Stable executor → color mapping from lane order (root stays neutral).
    const execColorFor = makeExecutorColorMap(lanes);
    const dispatcherPanel = renderDispatcherPanel({
      tasksDoc, viewT0: view.t0, viewT1: view.t1, execColorFor,
      LABEL_W, plotW, containerW, TOP_AXIS, ROW_H: TASK_ROW_H, BOTTOM,
      trialStatus: detail.status,
    });
    panelsWrap.appendChild(dispatcherPanel);
    const legend = renderExecutorLegend(tasksDoc, lanes, execColorFor);
    if (legend) panelsWrap.appendChild(legend);
    // Scheduling curves: backlog depth + realized concurrency + cumulative done.
    panelsWrap.appendChild(renderSchedulingChart({
      tasksDoc, viewT0: view.t0, viewT1: view.t1,
      LABEL_W, plotW, containerW,
    }));
  }

  // ── 8. Zoom/pan controls (below panels) ──
  const controls = document.createElement("div");
  controls.className = "zoom-controls";
  controls.innerHTML = `
    <button id="zoom-reset">Reset Zoom</button>
    <span class="zoom-info" id="zoom-info"></span>
    <span style="margin-left:auto;opacity:.7">scroll = zoom · drag svg = pan · drag scrubber thumb / edges</span>
  `;
  panelsWrap.appendChild(controls);

  root.appendChild(panelsWrap);
  // If the mid-render reflow clamped the page scroll, put the user back
  // where they were (re-renders fire on every SSE tick).
  if (window.scrollY !== scrollYBefore) window.scrollTo(0, scrollYBefore);
  updateZoomInfo(view, fullT0, fullT1);

  // ── 9. Wire up zoom/pan handlers to both panel SVGs ──
  const svgs = panelsWrap.querySelectorAll(".panel-svg");
  attachZoomPan(svgs, detail.trial_id, fullT0, fullT1, LABEL_W, plotW);
  document.getElementById("zoom-reset").addEventListener("click", () => {
    state.trialViewWindows.set(detail.trial_id, { t0: fullT0, t1: fullT1 });
    renderTrialDetail(state.trialDetailCache.get(detail.trial_id));
  });
}

function updateZoomInfo(view, fullT0, fullT1) {
  const el = document.getElementById("zoom-info");
  if (!el) return;
  const span = Math.max(0.001, view.t1 - view.t0);
  const fullSpan = Math.max(0.001, fullT1 - fullT0);
  const pct = (span / fullSpan) * 100;
  const leftOff = view.t0 - fullT0;
  el.textContent = `view: ${formatDur(span)} (${pct.toFixed(0)}% of full) · offset +${formatDur(leftOff)}`;
}

function renderProcessSnapshot(processDoc) {
  const latest = processDoc.latest;
  const wrap = document.createElement("div");
  wrap.className = "process-snapshot";
  const heading = document.createElement("div");
  heading.className = "process-snapshot-heading";
  const mode = latest.attribution_mode || "unknown";
  heading.textContent = `Process peaks · ${processDoc.peaks?.length || 0} rows · ${processDoc.sample_count || 0} samples · attribution=${mode}`;
  wrap.appendChild(heading);
  if (mode === "shared_runtime") {
    const note = document.createElement("div");
    note.className = "process-attribution-note";
    note.textContent = "Claude Code sidechains share the Claude runtime process; PID-level CPU/RSS is recorded, but exact per-sidechain attribution is not available.";
    wrap.appendChild(note);
  }
  const table = document.createElement("table");
  table.className = "process-table";
  table.innerHTML = "<thead><tr><th>PID</th><th>PPID</th><th>Peak CPU%</th><th>Peak RSS MiB</th><th>Run</th><th>Attribution</th><th>Command</th></tr></thead>";
  const tbody = document.createElement("tbody");
  const rows = [...(processDoc.peaks || latest.processes || [])]
    .sort((a, b) => ((b.peak_mem_rss_mb ?? b.mem_rss_mb) - (a.peak_mem_rss_mb ?? a.mem_rss_mb)) || ((b.peak_cpu_percent ?? b.cpu_percent) - (a.peak_cpu_percent ?? a.cpu_percent)))
    .slice(0, 20);
  for (const p of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${p.pid}</td><td>${p.ppid}</td><td>${Number(p.peak_cpu_percent ?? p.cpu_percent ?? 0).toFixed(1)}</td><td>${Number(p.peak_mem_rss_mb ?? p.mem_rss_mb ?? 0).toFixed(1)}</td><td>${escape(p.run_id || "–")}</td><td>${escape(p.attribution || "unattributed")}</td><td class="process-command" title="${escape(p.args || p.command || "")}">${escape(p.command || "")}</td>`;
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

// ── Panel 1: Agent Activity ─────────────────────────────────────────

function renderAgentPanel({ lanes, runsByParent, viewT0, viewT1, LABEL_W, plotW, containerW, TOP_AXIS, LANE_H, BOTTOM }) {
  const NS = "http://www.w3.org/2000/svg";
  const panel = document.createElement("div");
  panel.className = "panel agent-activity";
  const svgH = TOP_AXIS + lanes.length * LANE_H + BOTTOM;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "panel-svg agent-activity-svg");
  svg.setAttribute("viewBox", `0 0 ${containerW} ${svgH}`);
  svg.setAttribute("height", svgH);
  svg.dataset.labelW = LABEL_W;
  svg.dataset.plotW = plotW;

  const durView = Math.max(0.001, viewT1 - viewT0);
  const tScale = (t) => LABEL_W + ((t - viewT0) / durView) * plotW;
  const inView = (t) => typeof t === "number" && t >= viewT0 && t <= viewT1;

  // Time axis at top
  svg.appendChild(renderTimeAxis({ viewT0, viewT1, LABEL_W, plotW, baseY: TOP_AXIS - 8 }));

  // Lane index for spawn link lookups
  const laneIndex = new Map();
  lanes.forEach(({ run }, i) => laneIndex.set(run.run_id, i));

  // Draw parent→child spawn arrows for every subagent lane, anchored at
  // the subagent's started_at. This is independent of subagent_spawn
  // events (kimi-cli's root spawns via the `Agent` tool call, not via
  // SubagentEvent, so spawn events may not exist in the wire).
  lanes.forEach(({ run }, i) => {
    if (!run.parent_run_id || typeof run.started_at !== "number") return;
    const parentIdx = laneIndex.get(run.parent_run_id);
    if (parentIdx === undefined) return;
    if (!inView(run.started_at)) return;
    const x = tScale(run.started_at);
    const parentY = TOP_AXIS + parentIdx * LANE_H + LANE_H / 2;
    const childY = TOP_AXIS + i * LANE_H + LANE_H / 2;
    const d = `M ${x} ${parentY} L ${x} ${childY} L ${x + 10} ${childY}`;
    svg.appendChild(svgNS("path", { d, class: "spawn-link" }));
    // Small triangle at spawn point on parent lane to mark "spawned here"
    svg.appendChild(svgNS("polygon", {
      points: `${x - 4},${parentY - 4} ${x + 4},${parentY - 4} ${x},${parentY + 4}`,
      class: "ev ev-subagent-spawn",
    }));
  });

  lanes.forEach(({ run, depth }, i) => {
    const y = TOP_AXIS + i * LANE_H + LANE_H / 2;
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", "lane");
    // Lane background (hover)
    const bg = svgNS("rect", {
      class: "lane-bg",
      x: 0, y: TOP_AXIS + i * LANE_H, width: containerW, height: LANE_H,
    });
    g.appendChild(bg);
    // Lane baseline (only inside plot area)
    g.appendChild(svgNS("line", {
      x1: LABEL_W, y1: y, x2: LABEL_W + plotW, y2: y, class: "lane-baseline",
    }));

    // Track tool_call → x for matching tool_result spans, and
    // compaction_begin → x for matching compaction_end spans.
    const toolCallByCallId = new Map();
    const compactionBeginX = { last: null };

    // Pre-build step content index for this run.
    const stepIndex = new Map();
    for (const ev of (run.events_tail || [])) {
      const sid = ev.step_id;
      if (!sid) continue;
      let gi = stepIndex.get(sid);
      if (!gi) {
        gi = { think: [], text: [], tools: [], first_ts: null, step_n: null };
        stepIndex.set(sid, gi);
      }
      if (gi.first_ts === null && typeof ev.timestamp === "number") gi.first_ts = ev.timestamp;
      if (ev.kind === "step_begin") gi.step_n = ev.payload?.n;
      if (ev.kind === "message") {
        const pt = ev.payload?.type;
        if (pt === "think" && typeof ev.payload.think === "string") gi.think.push(ev.payload.think);
        else if (pt === "text" && typeof ev.payload.text === "string") gi.text.push(ev.payload.text);
      } else if (ev.kind === "tool_call") {
        gi.tools.push((ev.payload?.function?.name) || ev.payload?.name || "?");
      }
    }

    // Render events that fall inside the current view window. For very long
    // runs (5h+ ⇒ tens of thousands of events) we additionally cap rendered
    // nodes per lane so the DOM stays small. Structural events (spawn /
    // compaction / turn boundaries) and tool calls are always kept; status
    // updates get dropped first.
    const allEvts = run.events_tail || [];
    const PRIORITY_KINDS = new Set([
      "subagent_spawn", "compaction_begin", "compaction_end",
      "turn_begin", "turn_end", "step_begin",
      "tool_call", "tool_result",
      "message", "notification",
    ]);
    const MAX_NODES_PER_LANE = 600;
    let evs = allEvts.filter(ev => inView(ev.timestamp));
    if (evs.length > MAX_NODES_PER_LANE) {
      const prio = evs.filter(e => PRIORITY_KINDS.has(e.kind));
      evs = prio.length <= MAX_NODES_PER_LANE
        ? prio
        : prio.slice(-MAX_NODES_PER_LANE);
    }
    for (const ev of evs) {
      if (typeof ev.timestamp !== "number") continue;
      const x = tScale(ev.timestamp);
      const node = makeEventNode(ev, x, y, run.run_id, lanes, laneIndex, TOP_AXIS, LANE_H, toolCallByCallId, compactionBeginX, stepIndex);
      if (node) {
        if (Array.isArray(node)) node.forEach(n => g.appendChild(n));
        else g.appendChild(node);
      }
    }
    svg.appendChild(g);

    // Lane label rendered LAST so it sits over any bar that bleeds left of LABEL_W
    svg.appendChild(svgNS("rect", {
      x: 0, y: TOP_AXIS + i * LANE_H, width: LABEL_W - 2, height: LANE_H,
      class: "label-bg",
    }));
    // Truncate both label lines to the label gutter so long subagent task
    // descriptions never bleed past the 0s gridline; full text on hover.
    const labelX = 8 + depth * 12;
    const labelAvail = LABEL_W - labelX - 8;
    const fullName = run.agent_name || run.run_id;
    const lbl = svgText(labelX, y - 4, truncLabel(fullName, labelAvail, 6.8));
    lbl.setAttribute("class", "lane-label");
    lbl.appendChild(svgTitle(fullName));
    svg.appendChild(lbl);
    const subFull = `${run.role} · ${run.status} · llm=${run.n_llm_calls} tools=${run.n_tool_calls}${run.n_compactions ? ` cmp=${run.n_compactions}` : ""}`;
    const sub = svgText(labelX, y + 10, truncLabel(subFull, labelAvail, 5.7));
    sub.setAttribute("class", "lane-substatus");
    sub.setAttribute("font-size", "10");
    sub.appendChild(svgTitle(subFull));
    svg.appendChild(sub);
  });

  // Title bottom-right (inside the panel)
  const totalEvents = lanes.reduce((s, { run }) => s + run.n_events, 0);
  const titleSub = svgText(containerW - 12, svgH - 18, `${lanes.length} agents · ${totalEvents} events · step(yellow) tool(green) spawn(red) compaction(magenta) · click any node for details`);
  titleSub.setAttribute("class", "panel-title-br-sub");
  titleSub.setAttribute("text-anchor", "end");
  svg.appendChild(titleSub);
  const title = svgText(containerW - 12, svgH - 4, "Agent Activity");
  title.setAttribute("class", "panel-title-br");
  title.setAttribute("text-anchor", "end");
  svg.appendChild(title);

  panel.appendChild(svg);
  return panel;
}

// ── Panel 2: Task Dispatcher ────────────────────────────────────────

function renderDispatcherPanel({ tasksDoc, viewT0, viewT1, execColorFor, LABEL_W, plotW, containerW, TOP_AXIS, ROW_H, BOTTOM, trialStatus }) {
  const NS = "http://www.w3.org/2000/svg";
  const panel = document.createElement("div");
  panel.className = "panel task-dispatcher";
  const tasks = tasksDoc.tasks;
  const ts0 = tasksDoc.trial_started_at || viewT0;
  const svgH = TOP_AXIS + tasks.length * ROW_H + BOTTOM;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "panel-svg task-dispatcher-svg");
  svg.setAttribute("viewBox", `0 0 ${containerW} ${svgH}`);
  svg.setAttribute("height", svgH);
  svg.dataset.labelW = LABEL_W;
  svg.dataset.plotW = plotW;

  const durView = Math.max(0.001, viewT1 - viewT0);
  const xOfWall = (wall_ts) => LABEL_W + ((wall_ts - viewT0) / durView) * plotW;
  const xOfOff = (off) => xOfWall(ts0 + (off || 0));
  const PLOT_LEFT = LABEL_W;
  const PLOT_RIGHT = LABEL_W + plotW;
  const clampX = (x) => Math.max(PLOT_LEFT, Math.min(PLOT_RIGHT, x));

  // Time axis at top
  svg.appendChild(renderTimeAxis({ viewT0, viewT1, LABEL_W, plotW, baseY: TOP_AXIS - 8 }));

  tasks.forEach((task, i) => {
    const y = TOP_AXIS + i * ROW_H + ROW_H / 2;
    // Row background
    svg.appendChild(svgNS("rect", {
      class: "task-row-bg",
      x: 0, y: TOP_AXIS + i * ROW_H, width: containerW, height: ROW_H,
    }));

    const xDispatch = (typeof task.at_sec === "number") ? xOfOff(task.at_sec) : null;
    const xReady = (typeof task.ready_at === "number") ? xOfOff(task.ready_at) : xDispatch;
    const xClaim = (typeof task.claimed_at === "number") ? xOfOff(task.claimed_at) : null;
    const xComplete = (typeof task.completed_at === "number") ? xOfOff(task.completed_at) : null;

    // Queue bar (dispatch → claim): grey, clamped to plot
    if (xReady !== null && xClaim !== null) {
      const x0 = clampX(xReady), x1 = clampX(xClaim);
      if (x1 > x0) svg.appendChild(svgNS("rect", {
        class: "task-queue-bar", x: x0, y: y - 4, width: x1 - x0, height: 8,
      }));
    } else if (xReady !== null && xClaim === null && task.status !== "pending") {
      const x0 = clampX(xReady), x1 = PLOT_RIGHT;
      if (x1 > x0) svg.appendChild(svgNS("rect", {
        class: "task-queue-bar-open", x: x0, y: y - 3, width: x1 - x0, height: 6,
      }));
    }

    // Execution bar (claim → complete), colored by executor when attributed.
    if (xClaim !== null) {
      const failed = (task.status || "").toLowerCase().includes("fail") ||
                     (task.agent_reported_status || "").toLowerCase().includes("fail");
      const finished = xComplete !== null;
      const xEnd = finished ? xComplete : PLOT_RIGHT;
      const x0 = clampX(xClaim), x1 = clampX(xEnd);
      if (x1 > x0) {
        const cls = "task-exec-bar " + (finished ? (failed ? "failed" : "completed") : "in-flight");
        const bar = svgNS("rect", {
          class: cls, x: x0, y: y - 5, width: x1 - x0, height: 10,
        });
        const execColor = execColorFor && task.executor_run_id
          ? execColorFor(task.executor_run_id) : null;
        if (execColor) bar.style.fill = execColor;
        attachTaskTooltip(bar, task, finished ? "executed" : "executing");
        svg.appendChild(bar);
      }
    }

    // Markers — only render if inside view
    if (xDispatch !== null && xDispatch >= PLOT_LEFT && xDispatch <= PLOT_RIGHT) {
      const d = svgNS("polygon", {
        class: "task-marker dispatch",
        points: `${xDispatch},${y - 5} ${xDispatch + 4},${y} ${xDispatch},${y + 5} ${xDispatch - 4},${y}`,
      });
      attachTaskTooltip(d, task, "dispatch");
      svg.appendChild(d);
    }
    if (xClaim !== null && xClaim >= PLOT_LEFT && xClaim <= PLOT_RIGHT) {
      const c = svgNS("circle", { class: "task-marker claim", cx: xClaim, cy: y, r: 4 });
      attachTaskTooltip(c, task, "claim");
      svg.appendChild(c);
    }
    if (xComplete !== null && xComplete >= PLOT_LEFT && xComplete <= PLOT_RIGHT) {
      const failed = (task.status || "").toLowerCase().includes("fail") ||
                     (task.agent_reported_status || "").toLowerCase().includes("fail");
      const m = svgNS("rect", {
        class: "task-marker " + (failed ? "failed" : "completed"),
        x: xComplete - 4, y: y - 4, width: 8, height: 8,
      });
      attachTaskTooltip(m, task, failed ? "failed" : "completed");
      svg.appendChild(m);
    }

    // Label rendered LAST so it sits over bars
    svg.appendChild(svgNS("rect", {
      class: "label-bg",
      x: 0, y: TOP_AXIS + i * ROW_H, width: LABEL_W - 2, height: ROW_H,
    }));
    // Split label column: name on left (up to LABEL_W - 70), status on right.
    const STATUS_W = 66;
    const NAME_MAX_W = LABEL_W - STATUS_W - 12;
    const lbl = svgText(8, y + 4, shortTaskName(task.task_id, NAME_MAX_W));
    lbl.setAttribute("class", "task-label");
    lbl.setAttribute("font-size", "11");
    const lblTtl = document.createElementNS(NS, "title");
    lblTtl.textContent = task.task_id;
    lbl.appendChild(lblTtl);
    svg.appendChild(lbl);
    const statusText = renderTaskStatusText(task, trialStatus);
    if (statusText) {
      const st = svgText(LABEL_W - 6, y + 4, statusText.text);
      st.setAttribute("class", "task-status " + statusText.cls);
      st.setAttribute("text-anchor", "end");
      st.setAttribute("font-size", "10");
      svg.appendChild(st);
    }
  });

  // Title bottom-right. If the trial died, re-label in-flight tasks as
  // "abandoned" so the summary matches the per-row labels.
  const counts = countTaskStatuses(tasks);
  const trialDead = trialStatus === "failed" || trialStatus === "killed";
  const midLabel = trialDead
    ? `${counts.in_progress} abandoned`
    : `${counts.in_progress} in-flight`;
  const src = tasksDoc.source ? ` · src=${tasksDoc.source}` : "";
  const titleSub = svgText(containerW - 12, svgH - 18, `${tasks.length} tasks · ${counts.completed} done · ${midLabel} · ${counts.ready} ready · ${counts.pending} pending${src} · dispatch(♦) claim(●) complete(■)`);
  titleSub.setAttribute("class", "panel-title-br-sub");
  titleSub.setAttribute("text-anchor", "end");
  svg.appendChild(titleSub);
  const title = svgText(containerW - 12, svgH - 4, "Task Dispatcher");
  title.setAttribute("class", "panel-title-br");
  title.setAttribute("text-anchor", "end");
  svg.appendChild(title);

  panel.appendChild(svg);
  return panel;
}

function countTaskStatuses(tasks) {
  const c = { completed: 0, in_progress: 0, ready: 0, pending: 0, failed: 0 };
  for (const t of tasks) {
    const s = (t.status || "").toLowerCase();
    if (s in c) c[s] += 1;
    else if (s.includes("fail")) c.failed += 1;
  }
  return c;
}

// ── Executor coloring (Gantt bars colored by which agent ran the task) ──

const EXEC_PALETTE = [
  "#4fc3f7", "#ffb74d", "#ba68c8", "#81c784", "#f06292",
  "#a1887f", "#4db6ac", "#dce775", "#7986cb", "#ff8a65",
  "#90a4ae", "#aed581",
];

function makeExecutorColorMap(lanes) {
  // Root lane keeps the default (status-colored) fill — color only
  // subagent/workflow executors, by lane order so colors match the
  // Agent Activity panel ordering and stay stable across re-renders.
  const map = new Map();
  let i = 0;
  for (const { run } of lanes) {
    if (run.role === "root") continue;
    map.set(run.run_id, EXEC_PALETTE[i % EXEC_PALETTE.length]);
    i += 1;
  }
  return (runId) => map.get(runId) || null;
}

function renderExecutorLegend(tasksDoc, lanes, execColorFor) {
  const used = new Map(); // run_id -> executor label
  for (const t of (tasksDoc.tasks || [])) {
    if (t.executor_run_id && execColorFor(t.executor_run_id)) {
      used.set(t.executor_run_id, t.executor || t.executor_run_id);
    }
  }
  if (used.size === 0) return null;
  const wrap = document.createElement("div");
  wrap.className = "exec-legend";
  const head = document.createElement("span");
  head.className = "exec-legend-head";
  head.textContent = "executors:";
  wrap.appendChild(head);
  for (const [runId, label] of used) {
    const item = document.createElement("span");
    item.className = "exec-legend-item";
    const chip = document.createElement("span");
    chip.className = "exec-chip";
    chip.style.background = execColorFor(runId);
    item.appendChild(chip);
    item.appendChild(document.createTextNode(label));
    wrap.appendChild(item);
  }
  return wrap;
}

// ── Scheduling curves: backlog depth · realized concurrency · cumulative done ──

function buildStepSeries(deltas) {
  // deltas: [t, +1|-1][] → change points [t, runningValue][]
  deltas.sort((a, b) => a[0] - b[0]);
  const pts = [];
  let v = 0;
  for (const [t, d] of deltas) {
    v += d;
    if (pts.length && pts[pts.length - 1][0] === t) pts[pts.length - 1][1] = v;
    else pts.push([t, v]);
  }
  return pts;
}

function valueAt(pts, t) {
  let v = 0;
  for (const [pt, pv] of pts) {
    if (pt > t) break;
    v = pv;
  }
  return v;
}

function stepPolylinePoints(pts, viewT0, viewT1, xOf, yOf) {
  // Render a step function over [viewT0, viewT1]: start at the running value
  // entering the window, draw horizontal-then-vertical segments, extend the
  // last value to the right edge.
  if (!pts.length) return "";
  const out = [];
  let v = valueAt(pts, viewT0);
  out.push(`${xOf(viewT0).toFixed(1)},${yOf(v).toFixed(1)}`);
  for (const [t, nv] of pts) {
    if (t < viewT0 || t > viewT1) { if (t > viewT1) break; continue; }
    out.push(`${xOf(t).toFixed(1)},${yOf(v).toFixed(1)}`);   // horizontal to t
    out.push(`${xOf(t).toFixed(1)},${yOf(nv).toFixed(1)}`);  // vertical step
    v = nv;
  }
  out.push(`${xOf(viewT1).toFixed(1)},${yOf(v).toFixed(1)}`);
  return out.join(" ");
}

function renderSchedulingChart({ tasksDoc, viewT0, viewT1, LABEL_W, plotW, containerW }) {
  const H = 130;
  const TOP = 10;
  const BOTTOM = 22;
  const innerH = H - TOP - BOTTOM;
  const ts0 = tasksDoc.trial_started_at || viewT0;
  const tasks = tasksDoc.tasks || [];
  const TERMINAL = new Set(["completed", "failed", "timeout"]);

  const backlogDeltas = [];      // ready, not yet claimed
  const concurrencyDeltas = [];  // claimed, not yet terminal
  const doneDeltas = [];         // cumulative completed
  for (const t of tasks) {
    const arr = (typeof t.ready_at === "number") ? t.ready_at
      : (typeof t.at_sec === "number" ? t.at_sec : null);
    const claim = (typeof t.claimed_at === "number") ? t.claimed_at : null;
    const isTerminal = TERMINAL.has((t.status || "").toLowerCase());
    const term = (isTerminal && typeof t.completed_at === "number") ? t.completed_at : null;
    if (arr !== null) backlogDeltas.push([ts0 + arr, +1]);
    if (claim !== null) {
      backlogDeltas.push([ts0 + claim, -1]);
      concurrencyDeltas.push([ts0 + claim, +1]);
    }
    if (term !== null) {
      concurrencyDeltas.push([ts0 + term, -1]);
      if ((t.status || "").toLowerCase() === "completed") doneDeltas.push([ts0 + term, +1]);
    }
  }
  const backlog = buildStepSeries(backlogDeltas);
  const concurrency = buildStepSeries(concurrencyDeltas);
  const done = buildStepSeries(doneDeltas);

  const yMax = Math.max(
    1,
    ...backlog.map(p => p[1]),
    ...concurrency.map(p => p[1]),
    ...done.map(p => p[1]),
  );
  const durSec = Math.max(0.001, viewT1 - viewT0);
  const xOf = (ts) => LABEL_W + ((ts - viewT0) / durSec) * plotW;
  const yOf = (v) => TOP + innerH - (v / yMax) * innerH;

  const panel = document.createElement("div");
  panel.className = "panel sched-curves";
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "panel-svg sched-curves-svg");
  svg.setAttribute("viewBox", `0 0 ${containerW} ${H}`);
  svg.setAttribute("height", H);
  svg.dataset.labelW = LABEL_W;
  svg.dataset.plotW = plotW;

  // Integer y gridlines (at most ~5).
  const stepY = Math.max(1, Math.ceil(yMax / 5));
  for (let v = 0; v <= yMax; v += stepY) {
    const y = yOf(v);
    svg.appendChild(svgNS("line", {
      x1: LABEL_W, y1: y, x2: LABEL_W + plotW, y2: y, class: "chart-grid",
    }));
    const lbl = svgText(LABEL_W - 6, y + 3, String(v));
    lbl.setAttribute("text-anchor", "end");
    lbl.setAttribute("font-size", "10");
    lbl.setAttribute("class", "chart-axis");
    svg.appendChild(lbl);
  }

  const series = [
    { pts: backlog, key: "backlog", cls: "sched-line backlog", label: "backlog (unclaimed)" },
    { pts: concurrency, key: "concurrency", cls: "sched-line concurrency", label: "in-flight (claimed)" },
    { pts: done, key: "done", cls: "sched-line done", label: "done (cumulative)" },
  ];
  for (const s of series) {
    const ptsStr = stepPolylinePoints(s.pts, viewT0, viewT1, xOf, yOf);
    if (ptsStr) svg.appendChild(svgNS("polyline", { points: ptsStr, class: s.cls }));
  }

  // Lane label + color-coded legend: a line swatch in each series' own
  // stroke + a matching-color label, stacked under the panel name.
  const lbl = svgText(8, TOP + 10, "scheduling");
  lbl.setAttribute("class", "lane-label");
  svg.appendChild(lbl);
  series.forEach((s, i) => {
    const ly = TOP + 26 + i * 14;
    svg.appendChild(svgNS("line", {
      x1: 8, y1: ly - 3, x2: 26, y2: ly - 3, class: s.cls,
    }));
    const t = svgText(31, ly, s.label);
    t.setAttribute("class", `sched-legend ${s.key}`);
    t.setAttribute("font-size", "10");
    svg.appendChild(t);
  });

  // Current values badge (right edge of the view), colored per series.
  const badge = svgNS("text", {
    x: LABEL_W + plotW - 4, y: TOP + 12,
    "text-anchor": "end", "font-size": "10", class: "chart-latest",
  });
  const peakConc = Math.max(0, ...concurrency.map(p => p[1]));
  const badgeParts = [
    { text: `backlog ${valueAt(backlog, viewT1)}`, key: "backlog" },
    { text: ` · `, key: null },
    { text: `in-flight ${valueAt(concurrency, viewT1)} (peak ${peakConc})`, key: "concurrency" },
    { text: ` · `, key: null },
    { text: `done ${valueAt(done, viewT1)}`, key: "done" },
  ];
  for (const part of badgeParts) {
    const tspan = document.createElementNS(NS, "tspan");
    if (part.key) tspan.setAttribute("class", `sched-legend ${part.key}`);
    tspan.textContent = part.text;
    badge.appendChild(tspan);
  }
  svg.appendChild(badge);

  const title = svgText(containerW - 12, H - 6, "Scheduling Curves");
  title.setAttribute("class", "panel-title-br");
  title.setAttribute("text-anchor", "end");
  svg.appendChild(title);

  panel.appendChild(svg);
  return panel;
}

// ── Shared time axis ──

function renderTimeAxis({ viewT0, viewT1, LABEL_W, plotW, baseY }) {
  const NS = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(NS, "g");
  g.setAttribute("class", "axis");
  const durView = Math.max(0.001, viewT1 - viewT0);
  g.appendChild(svgNS("line", { x1: LABEL_W, y1: baseY, x2: LABEL_W + plotW, y2: baseY }));
  const tickCount = 8;
  for (let i = 0; i <= tickCount; i++) {
    const off = (i / tickCount) * durView;
    const x = LABEL_W + (i / tickCount) * plotW;
    g.appendChild(svgNS("line", { x1: x, y1: baseY, x2: x, y2: baseY + 4 }));
    const label = svgText(x, baseY - 4, formatDur(off));
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("font-size", "10");
    g.appendChild(label);
  }
  return g;
}

// ── Zoom + Pan handlers ──

function attachZoomPan(svgs, trialId, fullT0, fullT1, LABEL_W, plotW) {
  let rafPending = false;
  function scheduleRerender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      renderTrialDetail(state.trialDetailCache.get(trialId));
    });
  }

  svgs.forEach(svg => {
    const labelW = parseFloat(svg.dataset.labelW || LABEL_W);
    const pw = parseFloat(svg.dataset.plotW || plotW);

    // Wheel → zoom around mouse
    svg.addEventListener("wheel", e => {
      e.preventDefault();
      const view = state.trialViewWindows.get(trialId);
      if (!view) return;
      const rect = svg.getBoundingClientRect();
      // viewBox is containerW × svgH; SVG scales to fit rect. Translate
      // mouse x in pixels back to SVG userspace x.
      const svgX = (e.clientX - rect.left) * (rect.width ? svg.viewBox.baseVal.width / rect.width : 1);
      // If wheel fires outside plot area, ignore.
      if (svgX < labelW || svgX > labelW + pw) return;
      const durView = view.t1 - view.t0;
      const tAtCursor = view.t0 + ((svgX - labelW) / pw) * durView;
      // delta: positive = zoom out, negative = zoom in
      const factor = e.deltaY > 0 ? 1.2 : 1 / 1.2;
      const newDur = Math.max(1, Math.min(fullT1 - fullT0, durView * factor));
      // Keep cursor at same t
      const newT0 = tAtCursor - ((svgX - labelW) / pw) * newDur;
      const newT1 = newT0 + newDur;
      // Clamp to full range
      let clT0 = newT0, clT1 = newT1;
      if (clT0 < fullT0) { clT0 = fullT0; clT1 = clT0 + newDur; }
      if (clT1 > fullT1) { clT1 = fullT1; clT0 = clT1 - newDur; }
      if (clT0 < fullT0) clT0 = fullT0;
      state.trialViewWindows.set(trialId, { t0: clT0, t1: clT1 });
      scheduleRerender();
    }, { passive: false });

    // Drag → pan
    let dragStart = null;
    svg.addEventListener("mousedown", e => {
      if (e.button !== 0) return;
      const view = state.trialViewWindows.get(trialId);
      if (!view) return;
      dragStart = {
        clientX: e.clientX,
        viewT0: view.t0,
        viewT1: view.t1,
        rectWidth: svg.getBoundingClientRect().width,
      };
      svg.classList.add("dragging");
    });
    window.addEventListener("mousemove", e => {
      if (!dragStart) return;
      const view = state.trialViewWindows.get(trialId);
      if (!view) return;
      const durView = dragStart.viewT1 - dragStart.viewT0;
      // pixel delta → svg userspace delta → time delta
      const svgDeltaX = (e.clientX - dragStart.clientX) * (dragStart.rectWidth ? svg.viewBox.baseVal.width / dragStart.rectWidth : 1);
      const dt = -(svgDeltaX / pw) * durView;  // drag right = pan left in time
      let newT0 = dragStart.viewT0 + dt;
      let newT1 = dragStart.viewT1 + dt;
      // Clamp
      if (newT0 < fullT0) { newT0 = fullT0; newT1 = newT0 + durView; }
      if (newT1 > fullT1) { newT1 = fullT1; newT0 = newT1 - durView; }
      state.trialViewWindows.set(trialId, { t0: newT0, t1: newT1 });
      scheduleRerender();
    });
    window.addEventListener("mouseup", () => {
      if (dragStart) {
        dragStart = null;
        svg.classList.remove("dragging");
      }
    });
  });
}

// ── renderTaskLifecycle is no longer used (replaced by renderDispatcherPanel).
// Kept as a no-op so nothing crashes if some old caller still references it.
function renderTaskLifecycle() { return document.createElement("div"); }

// ── Scrubber: visible time-axis pan/zoom widget ──────────────────────────
// Renders a horizontal track below the panels representing the FULL trial
// timeline; the highlighted "thumb" shows the current view window. Drag the
// thumb body to pan, drag the left/right edges to resize (= zoom keeping
// the opposite edge anchored), click empty track to jump.
function renderScrubber({ trialId, fullT0, fullT1, view, LABEL_W, plotW, containerW }) {
  const NS = "http://www.w3.org/2000/svg";
  const TRACK_H = 22;
  const PADDING = 4;
  const wrap = document.createElement("div");
  wrap.className = "scrubber-wrap";

  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "scrubber-svg");
  svg.setAttribute("viewBox", `0 0 ${containerW} ${TRACK_H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(TRACK_H));

  // Track background — full trial timeline, aligned with the panels' plot
  // area (LABEL_W..LABEL_W+plotW) so a thumb x-position visually maps to
  // the same x in the panels above.
  svg.appendChild(svgNS("rect", {
    class: "scrubber-track",
    x: LABEL_W, y: PADDING,
    width: plotW, height: TRACK_H - 2 * PADDING,
  }));

  const fullDur = Math.max(0.001, fullT1 - fullT0);
  const t0Frac = (view.t0 - fullT0) / fullDur;
  const t1Frac = (view.t1 - fullT0) / fullDur;
  const thumbX = LABEL_W + t0Frac * plotW;
  const thumbW = Math.max(6, (t1Frac - t0Frac) * plotW);

  const thumb = svgNS("rect", {
    class: "scrubber-thumb",
    x: thumbX, y: PADDING,
    width: thumbW, height: TRACK_H - 2 * PADDING,
    rx: 2, ry: 2,
  });
  svg.appendChild(thumb);

  // Visual edge markers (thin vertical lines INSIDE the thumb at the
  // very edges) so the user sees where resize handles are. They're
  // pure decoration — actual hit-test is done in mousedown below.
  const edgeBarW = 2;
  const lEdge = svgNS("rect", {
    class: "scrubber-edge",
    x: thumbX, y: PADDING,
    width: edgeBarW, height: TRACK_H - 2 * PADDING,
  });
  const rEdge = svgNS("rect", {
    class: "scrubber-edge",
    x: thumbX + thumbW - edgeBarW, y: PADDING,
    width: edgeBarW, height: TRACK_H - 2 * PADDING,
  });
  svg.appendChild(lEdge);
  svg.appendChild(rEdge);

  wrap.appendChild(svg);

  // ── Interaction ────────────────────────────────────────────────────
  // Note: each rerender creates a fresh scrubber whose window listeners
  // get GC'd along with their closures once nothing references them.
  // We do NOT use AbortController here because aborting on rerender
  // would kill the in-flight drag's mousemove listener mid-gesture.
  // The rAF batching below caps add-listener pressure to ~60/sec,
  // and stale ones early-return because their `drag` closure is null.

  function pixelToTime(clientX) {
    const rect = svg.getBoundingClientRect();
    const svgX = (clientX - rect.left) * (rect.width ? containerW / rect.width : 1);
    const fracInPlot = (svgX - LABEL_W) / plotW;
    return fullT0 + Math.max(0, Math.min(1, fracInPlot)) * fullDur;
  }

  function pickModeFromX(svgX) {
    const leftEdge = thumbX;
    const rightEdge = thumbX + thumbW;
    if (svgX <= leftEdge || svgX >= rightEdge) {
      const RESIZE_OUTSIDE_PX = 3;
      if (svgX >= leftEdge - RESIZE_OUTSIDE_PX && svgX <= leftEdge) return "resize-l";
      if (svgX >= rightEdge && svgX <= rightEdge + RESIZE_OUTSIDE_PX) return "resize-r";
      return null;
    }
    const RESIZE_INSIDE_PX = 3;
    const MIN_PAN_WIDTH = 12;
    if (thumbW >= MIN_PAN_WIDTH + 2 * RESIZE_INSIDE_PX) {
      if (svgX < leftEdge + RESIZE_INSIDE_PX) return "resize-l";
      if (svgX > rightEdge - RESIZE_INSIDE_PX) return "resize-r";
    }
    return "pan";
  }

  // rAF-batched rerender so a fast mousemove doesn't sync-rerender 60+
  // times per frame.
  let rafPending = false;
  function rerender() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      renderTrialDetail(state.trialDetailCache.get(trialId));
    });
  }

  let drag = null;
  // Hover cursor (svg-local listener — dies with the SVG node).
  svg.addEventListener("mousemove", e => {
    if (drag) return;
    const rect = svg.getBoundingClientRect();
    const svgX = (e.clientX - rect.left) * (rect.width ? containerW / rect.width : 1);
    const mode = pickModeFromX(svgX);
    if (mode === "resize-l" || mode === "resize-r") svg.style.cursor = "ew-resize";
    else if (mode === "pan") svg.style.cursor = "grab";
    else svg.style.cursor = "";
  });

  svg.addEventListener("mousedown", e => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = svg.getBoundingClientRect();
    const svgX = (e.clientX - rect.left) * (rect.width ? containerW / rect.width : 1);
    const mode = pickModeFromX(svgX);
    const v = state.trialViewWindows.get(trialId);
    if (!v) return;

    if (mode === null) {
      const tClick = pixelToTime(e.clientX);
      const dur = v.t1 - v.t0;
      let nt0 = tClick - dur / 2;
      let nt1 = nt0 + dur;
      if (nt0 < fullT0) { nt0 = fullT0; nt1 = nt0 + dur; }
      if (nt1 > fullT1) { nt1 = fullT1; nt0 = nt1 - dur; }
      state.trialViewWindows.set(trialId, { t0: nt0, t1: nt1 });
      rerender();
      return;
    }

    drag = {
      mode,
      startX: e.clientX,
      startT0: v.t0,
      startT1: v.t1,
      // Snapshot rect.width once at mousedown; reuse for every move so
      // dx-to-userspace scale stays constant across rerenders that
      // might subtly resize the SVG element.
      rectWidth: svg.getBoundingClientRect().width,
    };
    svg.classList.add("dragging");
  });

  function onMove(e) {
    if (!drag) return;
    const dx = (e.clientX - drag.startX) * (drag.rectWidth ? containerW / drag.rectWidth : 1);
    const dt = (dx / plotW) * fullDur;
    const dur = drag.startT1 - drag.startT0;

    let nt0 = drag.startT0;
    let nt1 = drag.startT1;

    if (drag.mode === "pan") {
      // Lock dur and slide both edges by dt.
      nt0 = drag.startT0 + dt;
      nt1 = nt0 + dur;
      if (nt0 < fullT0) { nt0 = fullT0; nt1 = nt0 + dur; }
      if (nt1 > fullT1) { nt1 = fullT1; nt0 = nt1 - dur; }
    } else if (drag.mode === "resize-l") {
      nt0 = Math.max(fullT0, Math.min(drag.startT1 - 1, drag.startT0 + dt));
    } else if (drag.mode === "resize-r") {
      nt1 = Math.min(fullT1, Math.max(drag.startT0 + 1, drag.startT1 + dt));
    }
    state.trialViewWindows.set(trialId, { t0: nt0, t1: nt1 });
    rerender();
  }
  function endDrag() {
    if (drag) {
      drag = null;
      svg.classList.remove("dragging");
    }
  }
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", endDrag);

  return wrap;
}

function renderTaskStatusText(task, trialStatus) {
  const s = (task.status || "").toLowerCase();
  // Trial died mid-flight → tasks still "in_progress"/"claimed" are
  // actually orphans (agent killed before todo_update fired). Mark them
  // clearly so the dispatcher view doesn't pretend work is still happening.
  const trialDead = trialStatus === "failed" || trialStatus === "killed";
  if (trialDead && (s === "in_progress" || s === "claimed")) {
    return { text: "abandoned", cls: "abandoned" };
  }
  if (s === "completed") return { text: "completed", cls: "completed" };
  if (s.includes("fail")) return { text: "failed", cls: "failed" };
  if (s === "in_progress" || s === "claimed") return { text: "in flight", cls: "claimed" };
  if (s === "ready") return { text: "ready", cls: "ready" };
  if (s === "pending") return { text: "pending", cls: "pending" };
  return null;
}

function attachTaskTooltip(el, task, phase, xPx, trialStartedAt) {
  const NS = "http://www.w3.org/2000/svg";
  const ttl = document.createElementNS(NS, "title");
  const parts = [`${task.task_id} — ${phase}`];
  if (typeof task.at_sec === "number") parts.push(`dispatch: t+${task.at_sec.toFixed(0)}s`);
  if (typeof task.ready_at === "number") parts.push(`ready: t+${task.ready_at.toFixed(1)}s`);
  if (typeof task.claimed_at === "number") parts.push(`claim: t+${task.claimed_at.toFixed(1)}s`);
  if (typeof task.completed_at === "number") parts.push(`complete: t+${task.completed_at.toFixed(1)}s`);
  if (typeof task.completed_at === "number" && typeof task.ready_at === "number")
    parts.push(`turnaround: ${formatDur(task.completed_at - task.ready_at)}`);
  if (task.executor) parts.push(`executor: ${task.executor}`);
  if (task.agent_note) parts.push(`note: ${task.agent_note.slice(0, 160)}`);
  ttl.textContent = parts.join("\n");
  el.appendChild(ttl);
  el.style.cursor = "pointer";
  el.addEventListener("click", () => openTaskModal(task));
}

function openTaskModal(task) {
  const m = document.getElementById("event-modal");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");
  title.textContent = `task: ${task.task_id} · status=${task.status}`;
  body.textContent = JSON.stringify(task, null, 2);
  m.classList.remove("hidden");
}

function shortTaskName(id, maxWidthPx) {
  // ~6.5px/char at 11px monospace. Default to 90px fit if no width given.
  const maxChars = Math.max(8, Math.floor((maxWidthPx || 90) / 6.5));
  return id.length > maxChars ? id.slice(0, maxChars - 1) + "…" : id;
}

function renderContainerChart(samples, { t0, tEnd, LABEL_W, plotW, containerW }) {
  // ~120px tall chart with CPU% on one polyline and Mem% on another.
  const H = 120;
  const TOP = 8;
  const BOTTOM = 20;
  const innerH = H - TOP - BOTTOM;
  // CPU% can exceed 100 (multi-core), so scale to max(100, observed).
  const cpuMax = Math.max(100, ...samples.map(s => s.cpu_percent || 0));
  const memMax = 100;  // mem_percent always ≤ 100
  const durSec = Math.max(0.001, tEnd - t0);
  const xOf = (ts) => LABEL_W + ((ts - t0) / durSec) * plotW;

  const wrap = document.createElement("div");
  wrap.className = "container-chart-wrap";
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "container-chart");
  svg.setAttribute("viewBox", `0 0 ${containerW} ${H}`);
  svg.setAttribute("height", H);

  // Y-axis gridlines + labels (0 / 50 / 100 %)
  for (const pct of [0, 50, 100]) {
    const y = TOP + innerH - (pct / 100) * innerH;
    svg.appendChild(svgNS("line", {
      x1: LABEL_W, y1: y, x2: LABEL_W + plotW, y2: y, class: "chart-grid",
    }));
    const t = svgText(LABEL_W - 6, y + 3, `${pct}%`);
    t.setAttribute("text-anchor", "end");
    t.setAttribute("font-size", "10");
    t.setAttribute("class", "chart-axis");
    svg.appendChild(t);
  }
  // CPU > 100 marker
  if (cpuMax > 100) {
    const y = TOP + innerH - (cpuMax / cpuMax) * innerH;
    const t = svgText(LABEL_W + plotW + 4, y + 3, `cpu max ${cpuMax.toFixed(0)}%`);
    t.setAttribute("font-size", "9");
    t.setAttribute("class", "chart-axis");
    svg.appendChild(t);
  }

  // CPU polyline (orange)
  const cpuPts = samples.map(s => `${xOf(s.ts).toFixed(1)},${(TOP + innerH - ((s.cpu_percent || 0) / cpuMax) * innerH).toFixed(1)}`).join(" ");
  if (cpuPts) {
    svg.appendChild(svgNS("polyline", { points: cpuPts, class: "chart-line cpu" }));
  }
  // Mem polyline (cyan)
  const memPts = samples.map(s => `${xOf(s.ts).toFixed(1)},${(TOP + innerH - ((s.mem_percent || 0) / memMax) * innerH).toFixed(1)}`).join(" ");
  if (memPts) {
    svg.appendChild(svgNS("polyline", { points: memPts, class: "chart-line mem" }));
  }

  // Latest values badge (top right), colored to match the lines.
  const latest = samples[samples.length - 1];
  if (latest) {
    const badge = svgNS("text", {
      x: LABEL_W + plotW - 4, y: TOP + 12,
      "text-anchor": "end", "font-size": "10", class: "chart-latest",
    });
    const parts = [
      { text: `cpu ${latest.cpu_percent.toFixed(1)}%`, cls: "chart-legend cpu" },
      { text: "  ·  ", cls: null },
      { text: `mem ${latest.mem_used_mb.toFixed(0)} / ${latest.mem_limit_mb.toFixed(0)} MiB (${latest.mem_percent.toFixed(1)}%)`, cls: "chart-legend mem" },
    ];
    for (const part of parts) {
      const tspan = document.createElementNS(NS, "tspan");
      if (part.cls) tspan.setAttribute("class", part.cls);
      tspan.textContent = part.text;
      badge.appendChild(tspan);
    }
    svg.appendChild(badge);
  }

  // Lane label + color-coded line legend.
  const lbl = svgText(8, TOP + 12, "container");
  lbl.setAttribute("class", "lane-label");
  svg.appendChild(lbl);
  const legendDefs = [
    { cls: "chart-line cpu", textCls: "chart-legend cpu", label: "cpu %" },
    { cls: "chart-line mem", textCls: "chart-legend mem", label: "mem %" },
  ];
  legendDefs.forEach((d, i) => {
    const ly = TOP + 28 + i * 13;
    svg.appendChild(svgNS("line", { x1: 8, y1: ly - 3, x2: 26, y2: ly - 3, class: d.cls }));
    const t = svgText(31, ly, d.label);
    t.setAttribute("class", d.textCls);
    t.setAttribute("font-size", "10");
    svg.appendChild(t);
  });
  const sub = svgText(8, TOP + 28 + legendDefs.length * 13, `${samples.length} sample${samples.length > 1 ? "s" : ""}`);
  sub.setAttribute("class", "lane-substatus");
  sub.setAttribute("font-size", "10");
  svg.appendChild(sub);

  wrap.appendChild(svg);
  return wrap;
}

function renderContainerStats(samples, viewT0, viewT1) {
  // Aggregate Docker stats and cgroup v2 counters over the current window.
  // Saturation threshold: 350% (≥87.5% of 4-core budget). For other core
  // counts we still flag this, since most trials run cpus=4.
  const inWin = samples.filter(s =>
    typeof s.ts === "number" && s.ts >= viewT0 && s.ts <= viewT1
  );
  const wrap = document.createElement("div");
  wrap.className = "container-stats";
  if (inWin.length === 0) {
    wrap.innerHTML = '<span class="empty">(no samples in view)</span>';
    return wrap;
  }
  function pct(arr, p) {
    const a = arr.slice().sort((x, y) => x - y);
    const i = Math.min(a.length - 1, Math.floor(a.length * p));
    return a[i];
  }
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const values = (key) => inWin.map(s => s[key]).filter(finite);
  const latestValue = (key) => {
    for (let i = inWin.length - 1; i >= 0; i--) {
      if (finite(inWin[i][key])) return inWin[i][key];
    }
    return null;
  };
  const delta = (key) => {
    const a = values(key);
    return a.length >= 2 ? Math.max(0, a[a.length - 1] - a[0]) : null;
  };
  const cpu = inWin.map(s => s.cpu_percent || 0);
  const mem = inWin.map(s => s.mem_percent || 0);
  const memMb = inWin.map(s => s.mem_used_mb || 0);
  const memLimMb = inWin[inWin.length - 1].mem_limit_mb || 0;
  const cpuMean = cpu.reduce((a, b) => a + b, 0) / cpu.length;
  const memMean = mem.reduce((a, b) => a + b, 0) / mem.length;
  const cpuMed = pct(cpu, 0.5);
  const cpuP90 = pct(cpu, 0.9);
  const cpuP99 = pct(cpu, 0.99);
  const cpuMax = Math.max(...cpu);
  const memMax = Math.max(...mem);
  const memMaxMb = Math.max(...memMb);
  // Saturation: CPU% ≥ 350 (close to 4-core ceiling). Counts of samples and
  // duration over threshold.
  const SAT_THRESHOLD = 350;
  const satCount = cpu.filter(c => c >= SAT_THRESHOLD).length;
  const satPct = (satCount / cpu.length) * 100;
  // Estimate sample interval from medians of neighbor diffs.
  const tsSorted = inWin.map(s => s.ts).slice().sort((a, b) => a - b);
  let dt = 3;
  if (tsSorted.length > 5) {
    const diffs = [];
    for (let i = 1; i < tsSorted.length; i++) diffs.push(tsSorted[i] - tsSorted[i - 1]);
    diffs.sort((a, b) => a - b);
    dt = diffs[Math.floor(diffs.length / 2)] || 3;
  }
  const satDurSec = satCount * dt;

  function card(label, big, small, cls = "") {
    const c = document.createElement("div");
    c.className = "stat-card " + cls;
    const l = document.createElement("div");
    l.className = "stat-label";
    l.textContent = label;
    const v = document.createElement("div");
    v.className = "stat-value";
    v.textContent = big;
    c.appendChild(l);
    c.appendChild(v);
    if (small) {
      const s = document.createElement("div");
      s.className = "stat-sub";
      s.textContent = small;
      c.appendChild(s);
    }
    return c;
  }
  wrap.appendChild(card("CPU mean", `${cpuMean.toFixed(1)}%`, `median ${cpuMed.toFixed(1)}%`));
  wrap.appendChild(card("CPU p90", `${cpuP90.toFixed(0)}%`, `p99 ${cpuP99.toFixed(0)}%`));
  wrap.appendChild(card("CPU max", `${cpuMax.toFixed(0)}%`, ""));
  const satCls = satPct >= 10 ? "stat-warn" : "";
  wrap.appendChild(card(
    `CPU sat (≥${SAT_THRESHOLD}%)`,
    `${satPct.toFixed(1)}%`,
    `${formatDur(satDurSec)} cumulative`,
    satCls,
  ));
  wrap.appendChild(card("Mem peak", `${memMaxMb.toFixed(0)} MiB`, `${memMax.toFixed(1)}% of ${memLimMb.toFixed(0)}`));
  wrap.appendChild(card("Mem mean", `${(memMb.reduce((a, b) => a + b, 0) / memMb.length).toFixed(0)} MiB`, `${memMean.toFixed(1)}%`));
  const cgroupPeak = Math.max(0, ...values("memory_peak_mb"));
  const swapPeak = Math.max(0, ...values("swap_used_mb"));
  if (cgroupPeak || values("memory_peak_mb").length) {
    wrap.appendChild(card("cgroup mem peak", `${cgroupPeak.toFixed(0)} MiB`, `swap peak ${swapPeak.toFixed(1)} MiB`));
  }
  const cpuTime = delta("cpu_usage_s");
  const cpuUser = delta("cpu_user_s");
  const cpuSystem = delta("cpu_system_s");
  if (cpuTime !== null) {
    wrap.appendChild(card("CPU time Δ", formatDur(cpuTime), `user ${formatDur(cpuUser || 0)} · sys ${formatDur(cpuSystem || 0)}`));
  }
  const throttle = values("cpu_throttled_period_percent");
  const throttledTime = delta("cpu_throttled_s");
  if (throttle.length) {
    const throttleMax = Math.max(...throttle);
    wrap.appendChild(card(
      "CPU throttling",
      `${throttleMax.toFixed(1)}% max`,
      `${formatDur(throttledTime || 0)} throttled Δ`,
      throttleMax >= 10 ? "stat-warn" : "",
    ));
  }
  const ioRead = delta("io_read_mb");
  const ioWrite = delta("io_write_mb");
  if (ioRead !== null || ioWrite !== null) {
    wrap.appendChild(card("cgroup I/O Δ", `R ${(ioRead || 0).toFixed(1)} MiB`, `W ${(ioWrite || 0).toFixed(1)} MiB`));
  }
  const netRx = delta("net_rx_mb");
  const netTx = delta("net_tx_mb");
  wrap.appendChild(card("Network Δ", `RX ${(netRx || 0).toFixed(1)} MiB`, `TX ${(netTx || 0).toFixed(1)} MiB`));
  const pidValues = values("pids_current");
  if (pidValues.length) {
    const pidLimit = latestValue("pids_limit");
    const pidEvents = latestValue("pids_events_max") || 0;
    wrap.appendChild(card("PIDs", `${Math.max(...pidValues)} peak`, `${pidLimit === null ? "unlimited" : pidLimit + " limit"} · max events ${pidEvents}`));
  }
  const cpuPsi = Math.max(0, ...values("cpu_pressure_some_avg10"));
  const memPsi = Math.max(0, ...values("memory_pressure_some_avg10"));
  const ioPsi = Math.max(0, ...values("io_pressure_some_avg10"));
  if (values("cpu_pressure_some_avg10").length) {
    wrap.appendChild(card("PSI some avg10", `CPU ${cpuPsi.toFixed(2)}%`, `mem ${memPsi.toFixed(2)}% · I/O ${ioPsi.toFixed(2)}%`));
  }
  const fsUsed = latestValue("fs_used_mb");
  const fsSize = latestValue("fs_size_mb");
  const fsPct = latestValue("fs_used_percent");
  if (fsUsed !== null) {
    wrap.appendChild(card("Workspace FS", `${fsUsed.toFixed(0)} MiB`, `${(fsPct || 0).toFixed(1)}% of ${(fsSize || 0).toFixed(0)} MiB`));
  }
  const oom = latestValue("mem_events_oom") || 0;
  const oomKill = latestValue("mem_events_oom_kill") || 0;
  const memHigh = latestValue("mem_events_high") || 0;
  if (oom || oomKill || memHigh) {
    wrap.appendChild(card("Memory events", `OOM ${oom} · kill ${oomKill}`, `high ${memHigh}`, oom || oomKill ? "stat-warn" : ""));
  }
  wrap.appendChild(card("Samples", `${inWin.length}`, `over ${formatDur(Math.max(0.001, viewT1 - viewT0))}`));
  return wrap;
}

function renderContainerChart_legendNote() {
  // placeholder — keep file structure stable
}

function makeEventNode(ev, x, y, runId, lanes, laneIndex, TOP_AXIS, LANE_H, toolCallByCallId, compactionBeginX, stepIndex) {
  const NS = "http://www.w3.org/2000/svg";
  const kind = ev.kind || "other";
  const payload = ev.payload || {};
  let nodes = [];
  let primary = null;
  switch (kind) {
    case "step_begin": {
      primary = svgNS("circle", { cx: x, cy: y, r: 4, class: "ev ev-step-begin" });
      // Pin a richer tooltip + click handler — show the LLM response for THIS step.
      const stepData = stepIndex && stepIndex.get(ev.step_id);
      if (stepData) {
        const ttl = document.createElementNS(NS, "title");
        ttl.textContent = stepTooltipText(stepData, ev);
        primary.appendChild(ttl);
        primary.addEventListener("click", () => openStepModal(ev, runId, stepData));
        return [primary];   // override default click handler attached below
      }
      break;
    }
    case "tool_call": {
      primary = svgNS("circle", { cx: x, cy: y, r: 4, class: "ev ev-tool-call" });
      const callId = payload.id || payload.tool_call_id;
      if (callId) toolCallByCallId.set(callId, x);
      break;
    }
    case "tool_result": {
      primary = svgNS("circle", { cx: x, cy: y, r: 3, class: "ev ev-tool-result" });
      const callId = payload.id || payload.tool_call_id;
      const startX = callId && toolCallByCallId.get(callId);
      if (startX !== undefined) {
        const span = svgNS("line", { x1: startX, y1: y, x2: x, y2: y, class: "ev-tool-span" });
        nodes.push(span);
      }
      break;
    }
    case "subagent_spawn": {
      primary = svgNS("rect", { x: x - 3, y: y - 6, width: 6, height: 12, class: "ev ev-subagent-spawn" });
      // Branch link to child lane
      const childRunId = payload.child_run_id || ("run_sub_" + payload.task_id);
      const childIdx = laneIndex.get(childRunId);
      if (childIdx !== undefined) {
        const childY = TOP_AXIS + childIdx * LANE_H + LANE_H / 2;
        // L-shaped path from spawn point → down to child lane → right a bit
        const d = `M ${x} ${y} L ${x} ${childY} L ${x + 12} ${childY}`;
        nodes.push(svgNS("path", { d, class: "spawn-link" }));
      }
      break;
    }
    case "compaction_begin": {
      // Hold off rendering — wait for matching compaction_end so we draw
      // one span (= one actual compaction). Stash the x AND timestamp.
      if (compactionBeginX) {
        compactionBeginX.last = x;
        compactionBeginX.beginTs = ev.timestamp;
      }
      return null;
    }
    case "compaction_end": {
      // Draw a thin span band from begin → end + a single marker at begin.
      // If we never saw a begin (truncated tail), fall back to a marker at end.
      const x0 = compactionBeginX && compactionBeginX.last !== null ? compactionBeginX.last : x;
      const beginTs = compactionBeginX && compactionBeginX.beginTs !== undefined ? compactionBeginX.beginTs : null;
      if (compactionBeginX) {
        compactionBeginX.last = null;
        compactionBeginX.beginTs = null;
      }
      const width = Math.max(2, x - x0);
      const durMs = (beginTs != null && typeof ev.timestamp === "number") ? Math.round((ev.timestamp - beginTs) * 1000) : null;
      const tipText = `compaction · ${durMs != null ? durMs + "ms" : "?ms"} · context summarised`;
      const span = svgNS("rect", { x: x0, y: y - 7, width, height: 14, class: "ev-compaction-span" });
      const stitle = document.createElementNS(NS, "title"); stitle.textContent = tipText; span.appendChild(stitle);
      span.style.cursor = "pointer";
      span.addEventListener("click", () => openEventModal(ev, runId));
      nodes.push(span);
      primary = svgNS("rect", { x: x0 - 1, y: y - 8, width: 2, height: 16, class: "ev ev-compaction" });
      break;
    }
    case "status": {
      primary = svgNS("circle", { cx: x, cy: y + 8, r: 2, class: "ev ev-status" });
      break;
    }
    default: {
      primary = svgNS("circle", { cx: x, cy: y, r: 2, class: "ev ev-other" });
    }
  }
  if (primary) {
    // Attach click handler with the full event JSON.
    primary.addEventListener("click", () => openEventModal(ev, runId));
    // Tooltip via SVG <title>
    const ttl = document.createElementNS(NS, "title");
    ttl.textContent = `${kind} · ${formatTimestamp(ev.timestamp)}`;
    primary.appendChild(ttl);
    nodes.push(primary);
  }
  return nodes.length ? nodes : null;
}

function svgNS(tag, attrs) {
  const NS = "http://www.w3.org/2000/svg";
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function svgLine(x1, y1, x2, y2) {
  return svgNS("line", { x1, y1, x2, y2 });
}
function svgLineCls(x1, y1, x2, y2, cls) {
  return svgNS("line", { x1, y1, x2, y2, class: cls });
}
function svgText(x, y, text) {
  const el = svgNS("text", { x, y });
  el.textContent = text;
  return el;
}
function svgTitle(text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "title");
  el.textContent = text;
  return el;
}
function truncLabel(text, availPx, pxPerChar) {
  const s = String(text == null ? "" : text);
  const maxChars = Math.max(3, Math.floor(availPx / pxPerChar));
  return s.length > maxChars ? s.slice(0, maxChars - 1) + "…" : s;
}
function formatDur(sec) {
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}m${s}s`;
}
function formatTimestamp(ts) {
  if (typeof ts !== "number") return "?";
  return new Date(ts * 1000).toISOString().substr(11, 12);
}

function openEventModal(ev, runId) {
  const m = document.getElementById("event-modal");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");
  title.textContent = `[${runId}] ${ev.kind || "?"} · seq ${ev.seq ?? "?"} · ${formatTimestamp(ev.timestamp)}`;
  body.textContent = JSON.stringify(ev, null, 2);
  m.classList.remove("hidden");
}

function stepTooltipText(stepData, ev) {
  // Compact tooltip: step number + first 200 chars of think (or text fallback).
  const sn = stepData.step_n ?? "?";
  const tools = stepData.tools.length ? `  tools: ${stepData.tools.slice(0, 4).join(", ")}${stepData.tools.length > 4 ? "…" : ""}` : "";
  const think = (stepData.think.join(" ") || "").slice(0, 280);
  const text = (stepData.text.join(" ") || "").slice(0, 280);
  const snippet = think || text || "(no content)";
  return `step ${sn} · ${formatTimestamp(ev.timestamp)}${tools}\n\n${snippet}${snippet.length >= 280 ? "…" : ""}`;
}

function openStepModal(ev, runId, stepData) {
  const m = document.getElementById("event-modal");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");
  title.textContent = `[${runId}] step ${stepData.step_n ?? "?"} · ${formatTimestamp(ev.timestamp)} · ${stepData.tools.length} tool call${stepData.tools.length === 1 ? "" : "s"}`;
  const THINK_CAP = 1200;     // user said "thinking can be truncated"
  const TEXT_CAP = 3000;
  let parts = [];
  // Reasoning blocks
  if (stepData.think.length) {
    const allThink = stepData.think.join("\n\n────\n\n");
    const shown = allThink.length > THINK_CAP ? allThink.slice(0, THINK_CAP) + `\n\n… [truncated ${allThink.length - THINK_CAP} chars of thinking]` : allThink;
    parts.push(`▼ THINKING (${stepData.think.length} block${stepData.think.length === 1 ? "" : "s"})\n${shown}`);
  }
  // Assistant text output
  if (stepData.text.length) {
    const allText = stepData.text.join("");
    const shown = allText.length > TEXT_CAP ? allText.slice(0, TEXT_CAP) + `\n\n… [truncated ${allText.length - TEXT_CAP} chars]` : allText;
    parts.push(`▼ TEXT\n${shown}`);
  }
  // Tool calls fired by this step
  if (stepData.tools.length) {
    parts.push(`▼ TOOL CALLS (${stepData.tools.length})\n  ${stepData.tools.join("\n  ")}`);
  }
  if (parts.length === 0) parts.push("(no content captured for this step)");
  body.textContent = parts.join("\n\n");
  m.classList.remove("hidden");
}
function closeEventModal() {
  document.getElementById("event-modal").classList.add("hidden");
}
document.getElementById("modal-close").addEventListener("click", closeEventModal);
document.getElementById("event-modal").querySelector(".modal-backdrop").addEventListener("click", closeEventModal);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeEventModal(); });

// renderRunNode is no longer used by the trial detail view (replaced by the
// SVG timeline above) but kept for reference in case we want a fallback.
function renderRunNode(run, runsByParent) {
  const node = document.createElement("div");
  node.className = "run-node";
  const expanded = state.expandedRuns.has(run.run_id) || run.role === "root";
  if (expanded) state.expandedRuns.add(run.run_id);
  const header = document.createElement("div");
  header.className = "run-header" + (expanded ? "" : " collapsed");
  header.innerHTML = `
    <span class="twisty">${expanded ? "▼" : "▶"}</span>
    <span class="agent-name">${escape(run.agent_name || "?")}</span>
    <span class="role">[${escape(run.role)}]</span>
    <span class="status status-${run.status}">${escape(run.status)}</span>
    <span class="stats">
      events: ${run.n_events} · llm: ${run.n_llm_calls} · tools: ${run.n_tool_calls}
      · ctx_gen: ${run.n_compactions + 1} · in: ${run.prompt_tokens} · out: ${run.completion_tokens}
    </span>
  `;
  header.addEventListener("click", () => {
    if (state.expandedRuns.has(run.run_id)) state.expandedRuns.delete(run.run_id);
    else state.expandedRuns.add(run.run_id);
    renderTrialDetail(state.trialDetailCache.get(state.selectedTrialId));
  });
  node.appendChild(header);
  if (expanded) {
    const body = document.createElement("div");
    body.className = "run-body";
    // Tail of recent events
    for (const ev of (run.events_tail || []).slice(-50)) {
      const row = document.createElement("div");
      row.className = "event-row kind-" + (ev.kind || "");
      const ts = ev.timestamp ? new Date(ev.timestamp * 1000).toISOString().substr(11, 8) : "–";
      let body_text = "";
      if (ev.kind === "tool_call") body_text = (ev.payload?.function?.name) || (ev.payload?.name) || JSON.stringify(ev.payload).slice(0, 120);
      else if (ev.kind === "tool_result") body_text = "→ " + (JSON.stringify(ev.payload).slice(0, 120));
      else if (ev.kind === "step_begin") body_text = "step " + ev.payload?.n;
      else if (ev.kind === "subagent_spawn") body_text = "spawn " + (ev.payload?.task_id || "?");
      else if (ev.kind === "status") body_text = "tokens=" + (ev.payload?.context_tokens ?? "?");
      else if (ev.kind === "compaction_begin") body_text = "(compaction…)";
      else body_text = ev.payload ? JSON.stringify(ev.payload).slice(0, 120) : "";
      row.innerHTML = `
        <span class="ts">${escape(ts)}</span>
        <span class="kind">${escape(ev.kind || "")}</span>
        <span class="body">${escape(body_text)}</span>
      `;
      body.appendChild(row);
    }
    node.appendChild(body);
    // Children
    const children = (runsByParent.get(run.run_id) || []).sort((a, b) =>
      (a.started_at || 0) - (b.started_at || 0)
    );
    if (children.length) {
      const sub = document.createElement("div");
      sub.className = "subagent-children";
      for (const c of children) sub.appendChild(renderRunNode(c, runsByParent));
      node.appendChild(sub);
    }
  }
  return node;
}

function renderErrors() {
  const tbody = document.querySelector("#errors-table tbody");
  tbody.innerHTML = "";
  const recent = state.errors.slice(-200).reverse();
  for (const e of recent) {
    const tr = document.createElement("tr");
    tr.className = "errors-table";
    tr.innerHTML = `
      <td>${escape(shortTrialName(e.trial_id))}</td>
      <td>${escape(e.run_id || "")}</td>
      <td>${escape(new Date(e.ts * 1000).toISOString().substr(11, 19))}</td>
      <td class="rule-${escape(e.rule_id)}">${escape(e.rule_id)}</td>
      <td class="snippet">${escape(e.snippet || "")}</td>
    `;
    tbody.appendChild(tr);
  }
}

// ── SSE ──
function startSSE() {
  const es = new EventSource(apiURL("api/events"));
  const indicator = document.getElementById("connection-state");
  es.onopen = () => { indicator.className = "conn-live"; indicator.textContent = "live"; };
  es.onerror = () => { indicator.className = "conn-down"; indicator.textContent = "disconnected"; };
  es.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg);
  };
}

function handleEvent(msg) {
  if (msg.type === "trial_discovered" || msg.type === "trial_update" || msg.type === "run_added") {
    // Throttled — many trials × many SSE/sec would otherwise drown the
    // browser in /api/trials + /api/trials/<id> fetches.
    scheduleTrialsRefresh();
    if (state.activeTab === "trial" && msg.trial_id === state.selectedTrialId) {
      scheduleTrialDetailRefresh(msg.trial_id);
    }
  } else if (msg.type === "trial_resource") {
    // Append to local cache; re-render only if this trial is open.
    const buf = state.trialResourceCache.get(msg.trial_id) || [];
    buf.push(msg.payload);
    state.trialResourceCache.set(msg.trial_id, buf);
    if (state.activeTab === "trial" && msg.trial_id === state.selectedTrialId) {
      // Throttle — sampler can fire faster than the browser can re-render
      // a full timeline tree.
      scheduleTrialDetailRefresh(msg.trial_id);
    }
  } else if (msg.type === "trial_process") {
    state.trialProcessCache.set(msg.trial_id, {
      sample_count: (state.trialProcessCache.get(msg.trial_id)?.sample_count || 0) + 1,
      latest: msg.payload,
      peaks: state.trialProcessCache.get(msg.trial_id)?.peaks || [],
    });
    if (state.activeTab === "trial" && msg.trial_id === state.selectedTrialId) {
      scheduleTrialDetailRefresh(msg.trial_id);
    }
  } else if (msg.type === "error") {
    state.errors.push(msg.payload);
    if (state.errors.length > 1000) state.errors = state.errors.slice(-1000);
    renderKPIs();
    if (state.activeTab === "errors") renderErrors();
  } else if (msg.type === "resource") {
    state.resourceLatest = msg.payload;
    renderKPIs();
  }
}

// ── Throttle helpers: coalesce bursts of SSE events into a small number of
// ── actual API calls + re-renders. With 30+ trials live, naive 1:1 mapping
// ── floods the browser.
let _trialsRefreshTimer = null;
function scheduleTrialsRefresh(delayMs = 1500) {
  if (_trialsRefreshTimer) return;
  _trialsRefreshTimer = setTimeout(() => {
    _trialsRefreshTimer = null;
    refreshTrials();
  }, delayMs);
}

let _detailRefreshTimer = null;
let _detailRefreshTarget = null;
function scheduleTrialDetailRefresh(trialId, delayMs = 3000) {
  _detailRefreshTarget = trialId;
  if (_detailRefreshTimer) return;
  _detailRefreshTimer = setTimeout(() => {
    _detailRefreshTimer = null;
    const tid = _detailRefreshTarget;
    if (tid && state.activeTab === "trial" && tid === state.selectedTrialId) {
      loadTrialDetail(tid);
    }
  }, delayMs);
}

// ── helpers ──
// Resolve an API path against the document's base URL so the dashboard works
// behind a path-prefixing reverse proxy (for example a code-server that serves it
// under /proxy/<port>/). A leading "/" would escape the prefix and hit the proxy
// host's root, which is why the page used to render with every counter at 0
// and stay on "connecting…" — the HTML and JS loaded fine, but every fetch
// and the SSE stream 404'd. Callers pass "api/..." without a leading slash.
function apiURL(path) {
  return new URL(String(path).replace(/^\/+/, ""), document.baseURI).toString();
}
async function fetchJSON(url, options = {}) {
  const r = await fetch(apiURL(url), options);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}
function escape(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
// Agent slugs the monitor can classify (mirrors _classify_trial_agent in
// aos_bench/monitor/discover.py). Batch launchers name each job
// "<agent>__<stream>__<mode>__att<N>", and the agent already has its own
// column, so the prefix is redundant in the name cell. Matching against the
// known set rather than "strip up to the first __" is deliberate: per-job
// names are only "<stream>__<hash>", and blind stripping would eat the stream.
const AGENT_SLUGS = [
  "claude-agent-sdk",
  "claude-code",
  "kimi-code",
  "kimi-cli",
  "codex",
  "opencode",
];
function shortTrialName(id) {
  const s = String(id == null ? "" : id);
  for (const slug of AGENT_SLUGS) {
    if (s.startsWith(slug + "__")) return s.slice(slug.length + 2);
  }
  return s;
}
function trialDisplayName(trial) {
  return trial?.display_name || shortTrialName(trial?.trial_id || "");
}

bootstrap();
