from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


def render_agent_trace_svg(
    trace: Mapping[str, Any],
    *,
    title: str | None = None,
    width: int = 1440,
) -> str:
    """Render one AgentTrace dict into a readable static SVG.

    The layout is intentionally simple and dependency-free:
    - summary header
    - run graph (parent/child relationships)
    - per-run timeline swimlanes
    """

    runs = list(trace.get("runs") or [])
    root_run_id = str(trace.get("root_run_id") or "")
    title = title or str(trace.get("trace_id") or "Agent Trace")

    layout = _layout_runs(runs, root_run_id=root_run_id, width=width)
    timeline = _timeline_layout(runs, width=width)

    header_h = 132
    graph_h = max(180, 80 + 120 * (layout["max_depth"] + 1))
    legend_h = 88
    timeline_h = 96 + timeline["lane_height"] * max(1, len(runs))
    height = header_h + graph_h + legend_h + timeline_h + 48

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">',
        "<defs>",
        '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#52607a"/>',
        "</marker>",
        '<style><![CDATA[',
        ".title { font: 700 28px ui-sans-serif, system-ui, sans-serif; fill: #102033; }",
        ".subtitle { font: 500 13px ui-sans-serif, system-ui, sans-serif; fill: #52607a; }",
        ".section { font: 700 16px ui-sans-serif, system-ui, sans-serif; fill: #102033; }",
        ".label { font: 600 12px ui-sans-serif, system-ui, sans-serif; fill: #24364d; }",
        ".tiny { font: 500 11px ui-sans-serif, system-ui, sans-serif; fill: #5d6c82; }",
        ".mono { font: 500 11px ui-monospace, SFMono-Regular, monospace; fill: #24364d; }",
        ".lane-bg { fill: #f7f9fc; }",
        ".card { fill: #ffffff; stroke: #d7deea; stroke-width: 1; rx: 14; }",
        ".axis { stroke: #c9d3e3; stroke-width: 1; }",
        ".grid { stroke: #ebf0f7; stroke-width: 1; }",
        ".event-rail { stroke: #e4eaf4; stroke-width: 1; stroke-dasharray: 3 4; }",
        ".edge { stroke: #52607a; stroke-width: 1.6; fill: none; marker-end: url(#arrow); }",
        "]]></style>",
        "</defs>",
    ]

    parts.extend(_render_header(trace, title=title, width=width))
    parts.extend(_render_run_graph(runs, layout=layout, top=header_h, width=width))
    parts.extend(
        _render_legend(
            top=header_h + graph_h,
            left=40,
            width=width - 80,
        )
    )
    parts.extend(
        _render_timeline(
            runs,
            timeline=timeline,
            top=header_h + graph_h + legend_h,
            width=width,
        )
    )
    parts.append("</svg>")
    return "\n".join(parts)


def write_agent_trace_svg(
    trace: Mapping[str, Any] | Path | str,
    output_path: Path | str,
    *,
    title: str | None = None,
    width: int = 1440,
) -> Path:
    """Write an AgentTrace SVG to disk.

    ``trace`` may be a loaded dict or a JSON file path.
    """

    trace_obj = _load_trace(trace)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_agent_trace_svg(trace_obj, title=title, width=width),
        encoding="utf-8",
    )
    return output


def _load_trace(trace: Mapping[str, Any] | Path | str) -> Mapping[str, Any]:
    if isinstance(trace, Mapping):
        return trace
    path = Path(trace)
    return json.loads(path.read_text(encoding="utf-8"))


def _render_header(trace: Mapping[str, Any], *, title: str, width: int) -> list[str]:
    agent = trace.get("agent_system") or {}
    benchmark = trace.get("benchmark") or {}
    summary = trace.get("summary") or {}
    subtitle = " · ".join(
        part
        for part in [
            str(agent.get("name") or "unknown-agent"),
            str(agent.get("default_model") or "").strip() or None,
            str(trace.get("schema_version") or ""),
        ]
        if part
    )
    meta_rows = [
        ("trace_id", trace.get("trace_id")),
        ("root_run_id", trace.get("root_run_id")),
        ("runs", len(trace.get("runs") or [])),
        ("subagents", summary.get("subagent_count_total")),
        ("background_tasks", summary.get("background_task_count")),
        ("task_id", benchmark.get("task_id")),
    ]
    totals = _trace_resource_totals(trace.get("runs") or [])

    parts = [
        '<rect x="24" y="20" width="{w}" height="92" class="card"/>'.format(w=width - 48),
        f'<text x="44" y="54" class="title">{_esc(title)}</text>',
        f'<text x="44" y="78" class="subtitle">{_esc(subtitle)}</text>',
    ]
    x = 44
    y = 104
    for label, value in meta_rows:
        if value in (None, "", []):
            continue
        parts.append(f'<text x="{x}" y="{y}" class="tiny">{_esc(label)}</text>')
        parts.append(f'<text x="{x + 74}" y="{y}" class="mono">{_esc(str(value))}</text>')
        x += 210
        if x > width - 260:
            x = 44
            y += 18
    totals_text = " · ".join(
        part
        for part in [
            f"tokens {_compact_int(totals['prompt_tokens'])}/{_compact_int(totals['completion_tokens'])}"
            if totals["prompt_tokens"] or totals["completion_tokens"]
            else None,
            f"wall {totals['wall_time_ms']}ms" if totals["wall_time_ms"] else None,
            f"cpu {totals['cpu_time_ms']}ms" if totals["cpu_time_ms"] else None,
            f"peak rss {totals['memory_peak_kb']}KB" if totals["memory_peak_kb"] else None,
        ]
        if part
    )
    if totals_text:
        parts.append(f'<text x="{width - 44}" y="78" text-anchor="end" class="tiny">{_esc(totals_text)}</text>')
    return parts


def _layout_runs(runs: list[dict[str, Any]], *, root_run_id: str, width: int) -> dict[str, Any]:
    by_id = {str(run.get("run_id")): run for run in runs}
    children: dict[str | None, list[str]] = {}
    for run in runs:
        parent = run.get("parent_run_id")
        run_id = str(run.get("run_id"))
        children.setdefault(parent if parent else None, []).append(run_id)

    order: list[str] = []
    queue = [root_run_id] if root_run_id in by_id else [str(runs[0].get("run_id"))] if runs else []
    seen: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        queue.extend(sorted(children.get(current, [])))
    for run in runs:
        run_id = str(run.get("run_id"))
        if run_id not in seen:
            order.append(run_id)

    levels: dict[str, int] = {}

    def depth(run_id: str) -> int:
        if run_id in levels:
            return levels[run_id]
        parent = by_id[run_id].get("parent_run_id")
        if not parent or parent not in by_id:
            levels[run_id] = 0
        else:
            levels[run_id] = depth(str(parent)) + 1
        return levels[run_id]

    for run_id in order:
        depth(run_id)

    depth_buckets: dict[int, list[str]] = {}
    for run_id in order:
        d = levels[run_id]
        depth_buckets.setdefault(d, []).append(run_id)

    positions: dict[str, tuple[float, float]] = {}
    max_depth = max(depth_buckets.keys(), default=0)
    for d, bucket in depth_buckets.items():
        count = len(bucket)
        left_margin = 170
        right_margin = 170
        usable = width - left_margin - right_margin
        for idx, run_id in enumerate(bucket):
            x = left_margin + idx * (usable / max(1, count - 1)) if count > 1 else width / 2
            y = 84 + d * 118
            positions[run_id] = (x, y)
    return {
        "positions": positions,
        "levels": levels,
        "max_depth": max_depth,
    }


def _render_run_graph(
    runs: list[dict[str, Any]],
    *,
    layout: dict[str, Any],
    top: int,
    width: int,
) -> list[str]:
    parts = [
        f'<text x="40" y="{top + 24}" class="section">Run Graph</text>',
        f'<rect x="24" y="{top + 36}" width="{width - 48}" height="{max(132, 120 * (layout["max_depth"] + 1))}" class="card"/>',
    ]
    positions = layout["positions"]
    for run in runs:
        run_id = str(run.get("run_id"))
        parent = run.get("parent_run_id")
        if not parent or run_id not in positions or parent not in positions:
            continue
        x1, y1 = positions[str(parent)]
        x2, y2 = positions[run_id]
        parts.append(
            f'<path class="edge" d="M {x1} {top + 36 + y1 + 16} C {x1} {top + 36 + y1 + 58}, {x2} {top + 36 + y2 - 40}, {x2} {top + 36 + y2 - 8}"/>'
        )

    for run in runs:
        run_id = str(run.get("run_id"))
        if run_id not in positions:
            continue
        x, y = positions[run_id]
        role = str(run.get("role") or "unknown")
        launch_mode = str(run.get("launch_mode") or "unknown")
        session_mode = str(run.get("session_mode") or "unknown")
        status = str(run.get("status") or "unknown")
        agent_name = str(run.get("agent_name") or run_id)
        fill = _role_fill(role)
        stroke = _role_stroke(role)
        parts.extend(
            [
                f'<rect x="{x - 108}" y="{top + 36 + y - 22}" width="216" height="86" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>',
                f'<text x="{x - 92}" y="{top + 36 + y}" class="label">{_esc(agent_name)}</text>',
                f'<text x="{x - 92}" y="{top + 36 + y + 18}" class="tiny">{_esc(role)} · {_esc(launch_mode)} · {_esc(status)}</text>',
                f'<text x="{x}" y="{top + 36 + y + 34}" text-anchor="middle" class="tiny">{_esc(session_mode)} · {_esc(_truncate(run_id, 28))}</text>',
            ]
        )
        badges = _resource_badges(run)
        bx = x - 96
        by = top + 36 + y + 44
        for badge in badges[:3]:
            parts.append(f'<rect x="{bx}" y="{by}" width="56" height="16" rx="8" fill="#ffffff" fill-opacity="0.65" stroke="{stroke}" stroke-width="0.8"/>')
            parts.append(f'<text x="{bx + 28}" y="{by + 11}" text-anchor="middle" class="tiny">{_esc(badge)}</text>')
            bx += 62
    return parts


def _timeline_layout(runs: list[dict[str, Any]], *, width: int) -> dict[str, Any]:
    margin_left = 286
    margin_right = 220
    inner_w = max(520, width - margin_left - margin_right)
    lane_height = 96

    times: list[float] = []
    for run in runs:
        timing = run.get("timing") or {}
        for key in ("created_at", "started_at", "ended_at"):
            value = _as_float(timing.get(key))
            if value is not None:
                times.append(value)
        for event in run.get("events") or []:
            ts = _as_float(event.get("timestamp"))
            if ts is not None:
                times.append(ts)

    if times:
        min_t = min(times)
        max_t = max(times)
        if max_t <= min_t:
            max_t = min_t + 1.0
    else:
        min_t = 0.0
        max_t = max(1.0, float(max(len((run.get("events") or [])) for run in runs or [{}])))

    return {
        "min_t": min_t,
        "max_t": max_t,
        "margin_left": margin_left,
        "margin_right": margin_right,
        "inner_w": inner_w,
        "lane_height": lane_height,
        "resource_left": width - margin_right + 12,
    }


def _render_timeline(
    runs: list[dict[str, Any]],
    *,
    timeline: dict[str, Any],
    top: int,
    width: int,
) -> list[str]:
    left = timeline["margin_left"]
    inner_w = timeline["inner_w"]
    lane_height = timeline["lane_height"]
    min_t = timeline["min_t"]
    max_t = timeline["max_t"]
    resource_max = _resource_maxima(runs)
    resource_left = timeline["resource_left"]

    parts = [
        f'<text x="40" y="{top + 24}" class="section">Timeline</text>',
        f'<rect x="24" y="{top + 36}" width="{width - 48}" height="{60 + lane_height * max(1, len(runs))}" class="card"/>',
    ]

    for tick in range(6):
        ratio = tick / 5
        x = left + inner_w * ratio
        value = min_t + (max_t - min_t) * ratio
        parts.append(f'<line x1="{x}" y1="{top + 54}" x2="{x}" y2="{top + 48 + lane_height * max(1, len(runs))}" class="grid"/>')
        parts.append(f'<text x="{x}" y="{top + 52}" text-anchor="middle" class="tiny">{_esc(_format_clock_time(value))}</text>')

    parts.append(f'<line x1="{left - 16}" y1="{top + 54}" x2="{left - 16}" y2="{top + 48 + lane_height * max(1, len(runs))}" class="axis"/>')
    parts.append(f'<line x1="{resource_left - 18}" y1="{top + 54}" x2="{resource_left - 18}" y2="{top + 48 + lane_height * max(1, len(runs))}" class="axis"/>')
    parts.append(f'<text x="48" y="{top + 52}" class="tiny">Run</text>')
    parts.append(f'<text x="{left}" y="{top + 52}" class="tiny">Events / Time</text>')
    parts.append(f'<text x="{resource_left}" y="{top + 52}" class="tiny">Resources</text>')

    for idx, run in enumerate(runs):
        lane_top = top + 62 + idx * lane_height
        mid_y = lane_top + 28
        started = _run_start_time(run, fallback=min_t)
        ended = _run_end_time(run, fallback=started)
        x1 = _scale_time(started, min_t=min_t, max_t=max_t, left=left, width=inner_w)
        x2 = _scale_time(ended, min_t=min_t, max_t=max_t, left=left, width=inner_w)
        if x2 < x1 + 10:
            x2 = x1 + 10
        parts.append(f'<rect x="36" y="{lane_top - 10}" width="{width - 72}" height="72" class="lane-bg" rx="14"/>')
        parts.append(f'<text x="48" y="{lane_top + 10}" class="label">{_esc(str(run.get("agent_name") or run.get("run_id")))}</text>')
        meta = " · ".join(
            part
            for part in [
                str(run.get("role") or ""),
                str(run.get("launch_mode") or ""),
                _truncate(str(run.get("run_id") or ""), 26),
            ]
            if part
        )
        parts.append(f'<text x="48" y="{lane_top + 27}" class="tiny">{_esc(meta)}</text>')
        parts.append(
            f'<text x="48" y="{lane_top + 43}" class="tiny">'
            f'{_esc(_format_clock_time(started))} → {_esc(_format_clock_time(ended))}'
            f'</text>'
        )
        role_color = _role_stroke(str(run.get("role") or "unknown"))
        for rail_key, rail_label in _event_rail_specs():
            rail_y = mid_y + _event_y_offset(rail_key)
            parts.append(f'<line x1="{left}" y1="{rail_y}" x2="{left + inner_w}" y2="{rail_y}" class="event-rail"/>')
            parts.append(f'<text x="{left - 24}" y="{rail_y + 4}" text-anchor="end" class="tiny">{_esc(rail_label)}</text>')
        parts.append(f'<rect x="{x1}" y="{mid_y - 7}" width="{max(10, x2 - x1)}" height="14" rx="7" fill="{role_color}" opacity="0.24"/>')
        parts.append(f'<circle cx="{x1}" cy="{mid_y}" r="4.5" fill="{role_color}"/>')
        parts.append(f'<circle cx="{x2}" cy="{mid_y}" r="4.5" fill="{role_color}"/>')

        labeled_by_kind: dict[str, int] = {}
        for event in run.get("events") or []:
            ts = _as_float(event.get("timestamp"))
            if ts is None:
                continue
            x = _scale_time(ts, min_t=min_t, max_t=max_t, left=left, width=inner_w)
            kind = str(event.get("kind") or "event")
            color = _event_color(kind)
            shape = _event_shape(kind)
            marker_y = mid_y + _event_y_offset(kind)
            parts.extend(_draw_marker(x=x, y=marker_y, color=color, shape=shape))
            label_count = labeled_by_kind.get(kind, 0)
            if label_count < 2 and kind in {"tool_call", "subagent_spawn", "background_task"}:
                label = _event_label(event)
                if label:
                    label_y = _event_label_y(marker_y, kind=kind, ordinal=label_count)
                    label_text = f'{label} · {_format_clock_time(ts)}'
                    label_w = max(48, min(208, 8 + len(label_text) * 6))
                    label_x = x + 6
                    label_top = label_y - 10
                    parts.append(
                        f'<rect x="{label_x - 4}" y="{label_top - 2}" width="{label_w}" height="16" '
                        'rx="8" fill="#ffffff" fill-opacity="0.92" stroke="#d8e0ec" stroke-width="0.8"/>'
                    )
                    parts.append(
                        f'<text x="{label_x}" y="{label_y}" class="tiny">'
                        f'{_esc(label_text)}'
                        f'</text>'
                    )
                    labeled_by_kind[kind] = label_count + 1

        usage = run.get("resource_usage") or {}
        bars = _resource_bar_specs(usage, resource_max)
        bar_x = resource_left + 22
        bar_y = lane_top + 6
        for idx_bar, (label, ratio, color, text) in enumerate(bars):
            yy = bar_y + idx_bar * 12
            parts.append(f'<text x="{resource_left}" y="{yy + 5}" text-anchor="start" class="tiny">{_esc(label)}</text>')
            parts.append(f'<rect x="{bar_x}" y="{yy}" width="96" height="6" rx="3" fill="#dfe6f0"/>')
            parts.append(f'<rect x="{bar_x}" y="{yy}" width="{96 * ratio:.2f}" height="6" rx="3" fill="{color}"/>')
            parts.append(f'<text x="{bar_x + 104}" y="{yy + 5}" text-anchor="start" class="tiny">{_esc(text)}</text>')

    return parts


def _render_legend(*, top: int, left: int, width: int) -> list[str]:
    items = [
        ("root run", _role_fill("root"), "rect"),
        ("subagent run", _role_fill("subagent"), "rect"),
        ("message", _event_color("message"), "circle"),
        ("tool_call", _event_color("tool_call"), "diamond"),
        ("subagent_spawn", _event_color("subagent_spawn"), "diamond"),
        ("background_task", _event_color("background_task"), "square"),
        ("status", _event_color("status"), "square"),
    ]
    parts = [
        f'<text x="{left}" y="{top + 24}" class="section">Legend</text>',
        f'<rect x="24" y="{top + 36}" width="{width + 32}" height="52" class="card"/>',
    ]
    x = left
    y = top + 66
    for label, color, shape in items:
        if shape == "rect":
            parts.append(f'<rect x="{x}" y="{y - 10}" width="18" height="12" rx="5" fill="{color}" stroke="#92a1b8" stroke-width="1"/>')
        else:
            parts.extend(_draw_marker(x=x + 8, y=y - 4, color=color, shape=shape))
        parts.append(f'<text x="{x + 26}" y="{y}" class="tiny">{_esc(label)}</text>')
        x += 160
    return parts


def _draw_marker(*, x: float, y: float, color: str, shape: str) -> list[str]:
    if shape == "diamond":
        return [f'<path d="M {x} {y - 5} L {x + 5} {y} L {x} {y + 5} L {x - 5} {y} Z" fill="{color}" stroke="white" stroke-width="0.7"/>']
    if shape == "square":
        return [f'<rect x="{x - 4.5}" y="{y - 4.5}" width="9" height="9" rx="2" fill="{color}" stroke="white" stroke-width="0.7"/>']
    return [f'<circle cx="{x}" cy="{y}" r="4.5" fill="{color}" stroke="white" stroke-width="0.7"/>']


def _run_start_time(run: Mapping[str, Any], *, fallback: float) -> float:
    timing = run.get("timing") or {}
    return (
        _as_float(timing.get("started_at"))
        or _as_float(timing.get("created_at"))
        or _first_event_ts(run)
        or fallback
    )


def _run_end_time(run: Mapping[str, Any], *, fallback: float) -> float:
    timing = run.get("timing") or {}
    return _as_float(timing.get("ended_at")) or _last_event_ts(run) or fallback


def _first_event_ts(run: Mapping[str, Any]) -> float | None:
    times = [_as_float(event.get("timestamp")) for event in run.get("events") or []]
    values = [value for value in times if value is not None]
    return min(values) if values else None


def _last_event_ts(run: Mapping[str, Any]) -> float | None:
    times = [_as_float(event.get("timestamp")) for event in run.get("events") or []]
    values = [value for value in times if value is not None]
    return max(values) if values else None


def _scale_time(value: float, *, min_t: float, max_t: float, left: float, width: float) -> float:
    if max_t <= min_t:
        return left
    return left + ((value - min_t) / (max_t - min_t)) * width


def _event_label(event: Mapping[str, Any]) -> str | None:
    kind = str(event.get("kind") or "")
    payload = event.get("payload") or {}
    if kind == "subagent_spawn":
        return f"spawn {payload.get('subagent_type') or payload.get('child_run_id')}"
    if kind == "background_task":
        return f"task {payload.get('task_id') or payload.get('status')}"
    if kind == "tool_call":
        function = payload.get("function") or {}
        return str(
            payload.get("tool_name")
            or payload.get("name")
            or function.get("name")
            or payload.get("id")
            or "tool"
        )
    if kind == "message":
        text = str(payload.get("text") or payload.get("content") or "").strip()
        return _truncate(text, 28) if text else None
    return None


def _role_fill(role: str) -> str:
    return {
        "root": "#dff2ea",
        "subagent": "#e8eefc",
        "system": "#f4e7d5",
    }.get(role, "#eef2f8")


def _role_stroke(role: str) -> str:
    return {
        "root": "#2a8f63",
        "subagent": "#3f67c6",
        "system": "#b97824",
    }.get(role, "#667892")


def _event_color(kind: str) -> str:
    return {
        "message": "#2d7ff9",
        "tool_call": "#8f52ff",
        "tool_result": "#6b7a90",
        "subagent_spawn": "#0f9f72",
        "background_task": "#d97706",
        "status": "#7c3aed",
        "step_begin": "#94a3b8",
        "step_end": "#64748b",
    }.get(kind, "#4b5d75")


def _event_shape(kind: str) -> str:
    if kind in {"tool_call", "subagent_spawn"}:
        return "diamond"
    if kind in {"background_task", "status", "tool_result"}:
        return "square"
    return "circle"


def _event_y_offset(kind: str) -> int:
    return {
        "subagent_spawn": -18,
        "tool_call": -8,
        "background_task": 10,
        "status": 18,
        "tool_result": 18,
        "message": 0,
    }.get(kind, 0)


def _event_rail_specs() -> list[tuple[str, str]]:
    return [
        ("subagent_spawn", "SP"),
        ("tool_call", "TC"),
        ("message", "MSG"),
        ("background_task", "BG"),
        ("status", "ST"),
    ]


def _event_label_y(marker_y: float, *, kind: str, ordinal: int) -> float:
    if kind == "background_task":
        return marker_y + 16 + ordinal * 12
    return marker_y - 10 - ordinal * 12


def _compact_int(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return str(number)


def _format_time(value: float, *, relative: bool = False) -> str:
    prefix = "+" if relative else ""
    return f"{prefix}{value:.2f}s"


def _format_clock_time(value: float) -> str:
    dt = datetime.fromtimestamp(value)
    return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 10000:02d}"


def _resource_badges(run: Mapping[str, Any]) -> list[str]:
    usage = run.get("resource_usage") or {}
    badges: list[str] = []
    if usage.get("wall_time_ms") is not None:
        badges.append(f"W {_compact_int(usage.get('wall_time_ms'))}ms")
    if usage.get("cpu_time_ms") is not None:
        badges.append(f"C {_compact_int(usage.get('cpu_time_ms'))}ms")
    if usage.get("memory_peak_kb") is not None:
        badges.append(f"M {_compact_int(usage.get('memory_peak_kb'))}KB")
    elif usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
        badges.append(
            "T "
            + f"{_compact_int(usage.get('prompt_tokens'))}/{_compact_int(usage.get('completion_tokens'))}"
        )
    return badges


def _resource_maxima(runs: list[dict[str, Any]]) -> dict[str, float]:
    maxima = {
        "wall_time_ms": 0.0,
        "cpu_time_ms": 0.0,
        "memory_peak_kb": 0.0,
        "token_total": 0.0,
    }
    for run in runs:
        usage = run.get("resource_usage") or {}
        maxima["wall_time_ms"] = max(maxima["wall_time_ms"], float(usage.get("wall_time_ms") or 0))
        maxima["cpu_time_ms"] = max(maxima["cpu_time_ms"], float(usage.get("cpu_time_ms") or 0))
        maxima["memory_peak_kb"] = max(maxima["memory_peak_kb"], float(usage.get("memory_peak_kb") or 0))
        token_total = float(usage.get("prompt_tokens") or 0) + float(usage.get("completion_tokens") or 0)
        maxima["token_total"] = max(maxima["token_total"], token_total)
    return maxima


def _resource_bar_specs(
    usage: Mapping[str, Any],
    maxima: Mapping[str, float],
) -> list[tuple[str, float, str, str]]:
    token_total = float(usage.get("prompt_tokens") or 0) + float(usage.get("completion_tokens") or 0)
    specs = [
        (
            "W",
            _ratio(float(usage.get("wall_time_ms") or 0), maxima.get("wall_time_ms", 0.0)),
            "#2563eb",
            _format_metric(float(usage.get("wall_time_ms") or 0), "ms"),
        ),
        (
            "C",
            _ratio(float(usage.get("cpu_time_ms") or 0), maxima.get("cpu_time_ms", 0.0)),
            "#7c3aed",
            _format_metric(float(usage.get("cpu_time_ms") or 0), "ms"),
        ),
        (
            "M",
            _ratio(float(usage.get("memory_peak_kb") or 0), maxima.get("memory_peak_kb", 0.0)),
            "#d97706",
            _format_metric(float(usage.get("memory_peak_kb") or 0), "KB"),
        ),
        (
            "T",
            _ratio(token_total, maxima.get("token_total", 0.0)),
            "#0f9f72",
            f"{float(usage.get('prompt_tokens') or 0):.2f}/{float(usage.get('completion_tokens') or 0):.2f}",
        ),
    ]
    return [spec for spec in specs if spec[1] > 0]


def _trace_resource_totals(runs: list[dict[str, Any]]) -> dict[str, int]:
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "wall_time_ms": 0,
        "cpu_time_ms": 0,
        "memory_peak_kb": 0,
    }
    for run in runs:
        usage = run.get("resource_usage") or {}
        totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        totals["wall_time_ms"] += int(usage.get("wall_time_ms") or 0)
        totals["cpu_time_ms"] += int(usage.get("cpu_time_ms") or 0)
        totals["memory_peak_kb"] = max(totals["memory_peak_kb"], int(usage.get("memory_peak_kb") or 0))
    return totals


def _ratio(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0.0
    return max(0.0, min(1.0, value / maximum))


def _format_metric(value: float, unit: str) -> str:
    return f"{value:.2f}{unit}"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _esc(value: str) -> str:
    return html.escape(value, quote=True)
