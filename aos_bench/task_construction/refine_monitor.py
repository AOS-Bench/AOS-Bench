"""Small HTTP monitor for Codex REFINE data-synthesis loops."""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ERROR_WORDS = (
    "error",
    "failed",
    "failure",
    "timeout",
    "traceback",
    "exception",
    "503",
    "429",
)
TERMINAL_FAILURE_STATUSES = {"failed", "exhausted", "blocked", "rejected"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tail(path: Path, *, max_bytes: int = 24_000, max_lines: int = 80) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = text.split("\n", 1)[-1]
    return "\n".join(text.splitlines()[-max_lines:])


def _error_excerpt(text: str, *, limit: int = 8) -> list[str]:
    matches = [line.strip() for line in text.splitlines() if any(word in line.lower() for word in ERROR_WORDS)]
    return matches[-limit:]


def active_run_dirs() -> dict[str, list[int]]:
    """Return resolved ``--run-dir`` values found in live process command lines."""
    active: dict[str, list[int]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
            args = [part.decode(errors="replace") for part in parts if part]
        except OSError:
            continue
        for index, arg in enumerate(args):
            value = ""
            if arg == "--run-dir" and index + 1 < len(args):
                value = args[index + 1]
            elif arg.startswith("--run-dir="):
                value = arg.partition("=")[2]
            if value:
                resolved = str(Path(value).resolve(strict=False))
                active.setdefault(resolved, []).append(int(entry.name))
                break
    return active


def _latest_path(candidate: Path, pattern: str) -> Path | None:
    paths = list(candidate.glob(pattern))
    return max(paths, key=lambda path: path.stat().st_mtime, default=None)


def _round_summary(round_data: dict[str, Any]) -> tuple[str, list[str]]:
    summaries: list[str] = []
    errors: list[str] = []
    for layer in ("build", "oracle", "overlap"):
        result = round_data.get(layer)
        if not isinstance(result, dict):
            continue
        summary = result.get("summary")
        if summary:
            summaries.append(f"{layer}: {summary}")
        details = result.get("details")
        if isinstance(details, dict):
            signals = details.get("error_signals")
            if isinstance(signals, list):
                errors.extend(str(signal) for signal in signals[-8:])
            failure_class = details.get("failure_class")
            if failure_class:
                errors.append(f"failure_class: {failure_class}")
    return "; ".join(summaries), errors[-8:]


def collect_state(run_root: Path, *, max_rounds: int = 48) -> dict[str, Any]:
    """Aggregate persisted loop state and live process information."""
    run_root = run_root.resolve(strict=False)
    registry = _read_json(run_root / "build-valid-streams.json")
    accepted = {
        (str(item.get("bucket", "")), str(item.get("name", ""))): item
        for item in registry.get("streams", [])
        if isinstance(item, dict)
    }
    active = active_run_dirs()
    loops: list[dict[str, Any]] = []
    results_root = run_root / "results"
    for state_path in sorted(results_root.glob("*/*/state.json")):
        candidate = state_path.parent
        state = _read_json(state_path)
        bucket = candidate.parent.name
        name = candidate.name
        rounds = state.get("rounds") if isinstance(state.get("rounds"), list) else []
        latest_round = rounds[-1] if rounds and isinstance(rounds[-1], dict) else {}
        summary, errors = _round_summary(latest_round)
        latest_build = _latest_path(candidate, "logs/attempt-*/build.log")
        latest_codex = _latest_path(candidate, "logs/turn-*-codex.jsonl")
        driver = candidate / "driver.log"
        if not errors:
            for log_path in (latest_build, driver):
                if log_path is not None and log_path.exists():
                    errors.extend(_error_excerpt(_tail(log_path), limit=8))
                    if errors:
                        break
        mtimes = [path.stat().st_mtime for path in (state_path, driver, latest_build, latest_codex) if path and path.exists()]
        last_update = max(mtimes, default=0.0)
        resolved = str(candidate.resolve(strict=False))
        codex_turn = int(state.get("codex_turn") or 0)
        status = str(state.get("status") or "unknown")
        loops.append(
            {
                "id": f"{bucket}/{name}",
                "bucket": bucket,
                "name": name,
                "status": status,
                "active": resolved in active,
                "pids": active.get(resolved, []),
                "attempt": int(state.get("attempt") or 0),
                "round_count": len(rounds),
                "codex_turn": codex_turn,
                "max_rounds": max_rounds,
                "progress_percent": min(100.0, 100.0 * codex_turn / max(1, max_rounds)),
                "build_valid": (bucket, name) in accepted,
                "summary": summary or str(state.get("final_reason") or ""),
                "final_reason": str(state.get("final_reason") or ""),
                "errors": errors[-8:],
                "last_update": last_update,
                "age_seconds": max(0, int(time.time() - last_update)) if last_update else None,
                "session_id": str(state.get("session_id") or ""),
                "logs": {
                    "driver": str(driver) if driver.exists() else "",
                    "build": str(latest_build) if latest_build else "",
                    "codex": str(latest_codex) if latest_codex else "",
                },
            }
        )
    loops.sort(key=lambda item: (not item["active"], item["bucket"], item["name"]))
    statuses = Counter(loop["status"] for loop in loops)
    buckets: dict[str, dict[str, int]] = {}
    for bucket in sorted({loop["bucket"] for loop in loops}):
        selected = [loop for loop in loops if loop["bucket"] == bucket]
        buckets[bucket] = {
            "total": len(selected),
            "active": sum(bool(loop["active"]) for loop in selected),
            "build_valid": sum(bool(loop["build_valid"]) for loop in selected),
        }
    return {
        "run_root": str(run_root),
        "generated_at": time.time(),
        "summary": {
            "total": len(loops),
            "active": sum(bool(loop["active"]) for loop in loops),
            "build_valid": len(accepted),
            "failed": sum(loop["status"] in TERMINAL_FAILURE_STATUSES for loop in loops),
            "statuses": dict(statuses),
            "buckets": buckets,
        },
        "loops": loops,
    }


def _render_page(run_root: Path, max_rounds: int, refresh_seconds: int) -> bytes:
    title = html.escape(run_root.name)
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>REFINE Monitor · {title}</title>
<style>
:root{{--bg:#0b1020;--panel:#151c31;--line:#2a3556;--text:#e8ecf7;--muted:#9aa7c2;--ok:#55d187;--warn:#f3b75b;--bad:#ff7171;--blue:#66a6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}}
main{{max-width:1800px;margin:auto;padding:22px}}h1{{font-size:20px;margin:0 0 4px}}#meta{{color:var(--muted);margin-bottom:16px}}
.cards{{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin-bottom:14px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px}}.num{{font-size:25px;font-weight:700}}
.filters{{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}}button{{background:#1e2946;border:1px solid var(--line);color:var(--text);padding:6px 10px;border-radius:6px;cursor:pointer}}
table{{width:100%;border-collapse:collapse;background:var(--panel)}}th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:#1b2440;z-index:1}}tr.active{{box-shadow:inset 3px 0 var(--blue)}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}.muted{{color:var(--muted)}}.bar{{width:120px;height:7px;background:#303b5c;border-radius:9px;overflow:hidden;margin-top:4px}}.fill{{height:100%;background:var(--blue)}}
details pre{{white-space:pre-wrap;word-break:break-word;max-width:680px;max-height:260px;overflow:auto;color:#ffc5c5}}a{{color:#8bb9ff}}@media(max-width:900px){{.cards{{grid-template-columns:repeat(2,1fr)}}table{{font-size:12px}}}}
</style></head><body><main><h1>Codex REFINE Monitor</h1><div id="meta">{title}</div><div class="cards" id="cards"></div>
<div class="filters"><button onclick="setFilter('all')">全部</button><button onclick="setFilter('active')">运行中</button><button onclick="setFilter('medium')">Medium</button><button onclick="setFilter('heavy')">Heavy</button><span id="updated" class="muted"></span></div>
<table><thead><tr><th>Loop</th><th>Live / 状态</th><th>REFINE 进度</th><th>最近阶段</th><th>更新时间</th><th>Error log</th></tr></thead><tbody id="rows"></tbody></table></main>
<script>
let filter='all'; let state=null; const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function setFilter(value){{filter=value;render()}} function age(v){{if(v===null)return '-';if(v<60)return v+'s';if(v<3600)return Math.floor(v/60)+'m';return (v/3600).toFixed(1)+'h'}}
function render(){{if(!state)return;const s=state.summary;document.getElementById('cards').innerHTML=[['总候选',s.total],['活跃 loops',s.active],['Build-valid',s.build_valid],['终态失败',s.failed]].map(x=>`<div class="card"><div class="muted">${{x[0]}}</div><div class="num">${{x[1]}}</div></div>`).join('');
const loops=state.loops.filter(x=>filter==='all'||(filter==='active'&&x.active)||x.bucket===filter);document.getElementById('rows').innerHTML=loops.map(x=>`<tr class="${{x.active?'active':''}}"><td><b>${{esc(x.id)}}</b><br><span class="muted">${{esc(x.session_id)}}</span></td><td><span class="${{x.active?'ok':(x.status==='passed'?'ok':'')}}">${{x.active?'● active':'○ idle'}}</span><br>${{esc(x.status)}} ${{x.build_valid?'<span class="ok">✓ image</span>':''}}<br><span class="muted">PID ${{esc(x.pids.join(','))}}</span></td><td>turn ${{x.codex_turn}} / ${{x.max_rounds}}<br>attempt ${{x.attempt}} · rounds ${{x.round_count}}<div class="bar"><div class="fill" style="width:${{x.progress_percent}}%"></div></div></td><td>${{esc(x.summary)||'-'}}</td><td>${{age(x.age_seconds)}}</td><td><details><summary>${{x.errors.length?x.errors.length+' 条':'查看日志'}}</summary><pre>${{esc(x.errors.join('\\n'))}}</pre>${{['driver','build','codex'].map(k=>x.logs[k]?`<a target="_blank" href="/api/log?loop=${{encodeURIComponent(x.id)}}&kind=${{k}}">${{k}}</a> `:'').join('')}}</details></td></tr>`).join('');document.getElementById('updated').textContent='updated '+new Date(state.generated_at*1000).toLocaleString()+' · '+loops.length+' rows'}}
async function load(){{try{{state=await (await fetch('/api/state',{{cache:'no-store'}})).json();render()}}catch(e){{document.getElementById('updated').textContent='刷新失败: '+e}}}}load();setInterval(load,{max(1, refresh_seconds) * 1000});
</script></body></html>"""
    return page.encode()


def make_handler(run_root: Path, max_rounds: int, refresh_seconds: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(
            self,
            body: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
            *,
            include_body: bool = True,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/healthz", "/api/state"}:
                content_type = "text/html; charset=utf-8" if parsed.path == "/" else "application/json"
                self._send(b"", content_type, include_body=False)
                return
            self._send(b"", "text/plain", HTTPStatus.NOT_FOUND, include_body=False)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(_render_page(run_root, max_rounds, refresh_seconds), "text/html; charset=utf-8")
                return
            if parsed.path == "/healthz":
                self._send(b'{"ok":true}\n', "application/json")
                return
            state = collect_state(run_root, max_rounds=max_rounds)
            if parsed.path == "/api/state":
                body = json.dumps(state, ensure_ascii=False).encode()
                self._send(body, "application/json; charset=utf-8")
                return
            if parsed.path == "/api/log":
                query = parse_qs(parsed.query)
                loop_id = query.get("loop", [""])[0]
                kind = query.get("kind", ["driver"])[0]
                match = next((loop for loop in state["loops"] if loop["id"] == loop_id), None)
                path_value = match and match["logs"].get(kind) if kind in {"driver", "build", "codex"} else ""
                if not path_value:
                    self._send(b"unknown loop or log kind\n", "text/plain", HTTPStatus.NOT_FOUND)
                    return
                path = Path(path_value).resolve(strict=False)
                candidate = (run_root / "results" / loop_id).resolve(strict=False)
                if candidate not in path.parents:
                    self._send(b"unsafe log path\n", "text/plain", HTTPStatus.BAD_REQUEST)
                    return
                self._send(_tail(path, max_bytes=200_000, max_lines=1000).encode(), "text/plain; charset=utf-8")
                return
            self._send(b"not found\n", "text/plain", HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} [{self.log_date_time_string()}] {format % args}", flush=True)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=3462)
    parser.add_argument("--max-rounds", type=int, default=48)
    parser.add_argument("--refresh-seconds", type=int, default=5)
    args = parser.parse_args()
    run_root = args.run_root.resolve(strict=False)
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(run_root, args.max_rounds, args.refresh_seconds))
    print(f"REFINE monitor listening on http://{args.bind}:{args.port} for {run_root}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
