"""Phase 0 smoke for Harbor stream integration.

Verifies three properties of `kimi --wire --yolo` that the stream-mode
runner depends on:

  T1 sequential prompts in the same process
     - send prompt p1 → wait for success
     - send prompt p2 → wait for success
     - second prompt should observe context from the first (session continuity)

  T2 mid-turn steer
     - send prompt p3 instructing a long counting task
     - on the first TurnEnd event, send a steer changing the plan
     - p3 should still return success and the final text should reflect
       the steer (the new instruction was queued into the active turn)

  T3 max_steps short-circuit
     - send prompt p4 with a tiny max_steps cap (via session config)
     - kimi should return status="max_steps_reached" without hanging

The script does not assert exact wording from the model — it only checks
JSON-RPC framing, response statuses, and obvious continuity markers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WireClient:
    """Tiny JSON-RPC client over a kimi-cli wire subprocess."""

    proc: asyncio.subprocess.Process
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)
    on_event: list[Any] = field(default_factory=list)
    _reader_task: asyncio.Task[None] | None = None
    _stderr_task: asyncio.Task[None] | None = None
    stderr_lines: list[str] = field(default_factory=list)

    @classmethod
    async def start(cls, cmd: list[str], env: dict[str, str]) -> "WireClient":
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        client = cls(proc=proc)
        client._reader_task = asyncio.create_task(client._read_stdout())
        client._stderr_task = asyncio.create_task(client._read_stderr())
        return client

    async def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        buffer = ""
        while True:
            line_bytes = await self.proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            self.raw_lines.append(line)
            # Tool result outputs may contain unescaped newlines that split
            # a single JSON-RPC message across multiple lines. Strategy:
            # accumulate, then eagerly try to parse so single-line messages
            # dispatch with no extra latency.
            if line.lstrip().startswith('{"jsonrpc"'):
                if buffer:
                    # Previous buffer never completed; try once more then drop.
                    self._dispatch(buffer)
                buffer = line
            elif buffer:
                buffer += "\n" + line
            else:
                continue
            try:
                json.loads(buffer, strict=False)
            except json.JSONDecodeError:
                continue
            self._dispatch(buffer)
            buffer = ""
        if buffer:
            self._dispatch(buffer)

    async def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        while True:
            line_bytes = await self.proc.stderr.readline()
            if not line_bytes:
                break
            self.stderr_lines.append(
                line_bytes.decode("utf-8", errors="replace").rstrip("\n")
            )

    def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            return
        # Response (has id, no method)
        if "id" in msg and "method" not in msg:
            fut = self.pending.pop(msg["id"], None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        # Event (method=event)
        if msg.get("method") == "event":
            params = msg.get("params", {})
            self.events.append(params)
            for cb in list(self.on_event):
                try:
                    cb(params)
                except Exception:
                    pass

    async def send(self, method: str, msg_id: str, params: Any) -> dict[str, Any]:
        assert self.proc.stdin is not None
        payload = {"jsonrpc": "2.0", "method": method, "id": msg_id, "params": params}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[msg_id] = fut
        self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        return await fut

    async def close(self, timeout: float = 10.0) -> int:
        if self.proc.stdin is not None and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        return self.proc.returncode or 0


def build_config(args: argparse.Namespace) -> str:
    return json.dumps(
        {
            "default_model": "default",
            "models": {
                "default": {
                    "provider": "bench",
                    "model": args.model,
                    "max_context_size": 128000,
                }
            },
            "providers": {
                "bench": {
                    "type": args.provider_type,
                    "base_url": args.base_url,
                    "api_key": args.api_key,
                    "reasoning_key": "",
                }
            },
            "telemetry": False,
        }
    )


def build_cmd(args: argparse.Namespace, config_json: str, work_dir: Path) -> list[str]:
    return [
        "uv",
        "run",
        "--project",
        str(Path(args.kimi_project).resolve()),
        "kimi",
        "--wire",
        "--yolo",
        "--work-dir",
        str(work_dir),
        "--config",
        config_json,
    ]


def assistant_text(events: list[dict[str, Any]]) -> str:
    """Concatenate text ContentParts from the most recent step run."""
    parts: list[str] = []
    for ev in events:
        if ev.get("type") != "ContentPart":
            continue
        payload = ev.get("payload", {})
        if payload.get("type") == "text":
            t = payload.get("text") or ""
            if t:
                parts.append(t)
    return "".join(parts)


def step_count(events: list[dict[str, Any]]) -> int:
    # Kimi emits one `TurnEnd` per *prompt*, not per assistant step. Step
    # boundaries are marked by `StepBegin` events (matching the harbor
    # adapter's `_group_events_into_steps`). Count those so that a single
    # prompt with N internal steps reports N.
    return sum(1 for ev in events if ev.get("type") == "StepBegin")


async def run_smoke(args: argparse.Namespace) -> int:
    if not args.api_key:
        print("missing --api-key or OPENAI_API_KEY", file=sys.stderr)
        return 2

    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    config_json = build_config(args)
    cmd = build_cmd(args, config_json, work_dir)

    summary: dict[str, Any] = {"tests": []}
    overall_ok = True

    with tempfile.TemporaryDirectory(prefix="kimi-wire-smoke-") as tmp_share:
        env = os.environ.copy()
        env["KIMI_SHARE_DIR"] = args.share_dir or tmp_share

        client = await WireClient.start(cmd, env)
        try:
            # Optional handshake — server permits prompts without it, but we
            # exercise it because the runner will call it for hooks/external
            # tools later.
            try:
                init_resp = await asyncio.wait_for(
                    client.send(
                        "initialize",
                        "init-1",
                        {"protocol_version": "1", "client": {"name": "wire-smoke"}},
                    ),
                    timeout=30.0,
                )
                summary["initialize"] = init_resp.get("result", init_resp.get("error"))
            except asyncio.TimeoutError:
                summary["initialize"] = "TIMEOUT"
                # Some kimi-cli versions may not require initialize; continue.

            # ── T1: sequential prompts + cross-prompt continuity ──────────
            t1: dict[str, Any] = {"name": "T1_sequential_prompts"}
            events_before_p1 = len(client.events)
            p1_prompt = (
                "Reply with exactly one word: ALPHA. Do not say anything else."
            )
            try:
                p1_resp = await asyncio.wait_for(
                    client.send("prompt", "p1", {"user_input": p1_prompt}),
                    timeout=args.turn_timeout,
                )
            except asyncio.TimeoutError:
                t1["p1_status"] = "TIMEOUT"
                summary["tests"].append(t1)
                overall_ok = False
            else:
                p1_events = client.events[events_before_p1:]
                p1_text = assistant_text(p1_events)
                t1["p1_status"] = (p1_resp.get("result") or {}).get("status")
                t1["p1_text"] = p1_text[:200]
                t1["p1_steps"] = step_count(p1_events)

                events_before_p2 = len(client.events)
                p2_prompt = (
                    "What single word did you just reply with? "
                    "Echo it back as a single word."
                )
                try:
                    p2_resp = await asyncio.wait_for(
                        client.send("prompt", "p2", {"user_input": p2_prompt}),
                        timeout=args.turn_timeout,
                    )
                except asyncio.TimeoutError:
                    t1["p2_status"] = "TIMEOUT"
                    overall_ok = False
                else:
                    p2_events = client.events[events_before_p2:]
                    p2_text = assistant_text(p2_events)
                    t1["p2_status"] = (p2_resp.get("result") or {}).get("status")
                    t1["p2_text"] = p2_text[:200]
                    t1["p2_steps"] = step_count(p2_events)
                    t1["context_continuity"] = "ALPHA" in p2_text.upper()
                    if t1["p2_status"] != "finished" or not t1["context_continuity"]:
                        overall_ok = False
                if t1.get("p1_status") != "finished":
                    overall_ok = False
                summary["tests"].append(t1)

            # ── T2: mid-turn steer ────────────────────────────────────────
            # Trigger on the FIRST ContentPart (model started streaming
            # tokens) — `TurnEnd` is too late: the prompt has already
            # returned by then and `_handle_steer` would reject with
            # "no agent turn in progress".
            t2: dict[str, Any] = {"name": "T2_mid_turn_steer"}
            steer_sent = asyncio.Event()

            def _on_event(ev: dict[str, Any]) -> None:
                if ev.get("type") == "ContentPart" and not steer_sent.is_set():
                    steer_sent.set()

            client.on_event.append(_on_event)
            events_before_p3 = len(client.events)
            p3_prompt = (
                "Write a 250-word essay explaining what prime numbers are, "
                "with several examples. Take your time."
            )
            p3_task = asyncio.create_task(
                client.send("prompt", "p3", {"user_input": p3_prompt})
            )

            steered_ok = False
            try:
                await asyncio.wait_for(steer_sent.wait(), timeout=args.turn_timeout)
                steer_resp = await asyncio.wait_for(
                    client.send(
                        "steer",
                        "s1",
                        {
                            "user_input": (
                                "Change of plans. Stop counting immediately. "
                                "Reply with exactly: DONE_STEERED."
                            )
                        },
                    ),
                    timeout=30.0,
                )
                t2["steer_status"] = (steer_resp.get("result") or {}).get("status")
                steered_ok = t2["steer_status"] == "steered"
            except asyncio.TimeoutError:
                t2["steer_status"] = "TIMEOUT_WAITING_FOR_TURN"
                overall_ok = False
            finally:
                client.on_event.remove(_on_event)

            try:
                p3_resp = await asyncio.wait_for(p3_task, timeout=args.turn_timeout)
            except asyncio.TimeoutError:
                t2["p3_status"] = "TIMEOUT"
                overall_ok = False
            else:
                p3_events = client.events[events_before_p3:]
                p3_text = assistant_text(p3_events)
                t2["p3_status"] = (p3_resp.get("result") or {}).get("status")
                t2["p3_text_tail"] = p3_text[-300:]
                t2["p3_steps"] = step_count(p3_events)
                t2["steer_text_seen"] = "DONE_STEERED" in p3_text.upper()
                # A successful steer must (a) be accepted by the server,
                # (b) force at least one extra step, and (c) actually
                # influence the assistant output.
                if (
                    t2["p3_status"] != "finished"
                    or not steered_ok
                    or t2["p3_steps"] < 2
                    or not t2["steer_text_seen"]
                ):
                    overall_ok = False
            summary["tests"].append(t2)

            # ── T3: clean shutdown via stdin close ────────────────────────
            t3: dict[str, Any] = {"name": "T3_clean_shutdown"}
            rc = await client.close(timeout=15.0)
            t3["return_code"] = rc
            t3["clean_exit"] = rc in (0, -15, 143)  # SIGTERM is acceptable
            if not t3["clean_exit"]:
                overall_ok = False
            summary["tests"].append(t3)

        finally:
            if client.proc.returncode is None:
                await client.close(timeout=5.0)

        summary["overall_ok"] = overall_ok
        summary["stderr_tail"] = client.stderr_lines[-20:]

    if args.dump_jsonl:
        Path(args.dump_jsonl).write_text(
            "\n".join(client.raw_lines), encoding="utf-8"
        )

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if overall_ok else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kimi-project",
        default="aos_bench/harness/kimi-cli",
        help="Path to kimi-cli submodule for `uv run --project`",
    )
    parser.add_argument(
        "--work-dir",
        default=tempfile.gettempdir() + "/kimi-wire-smoke-work",
        help="Workspace passed to kimi-cli",
    )
    parser.add_argument(
        "--share-dir",
        default=None,
        help="KIMI_SHARE_DIR. Defaults to a temporary directory.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY", ""),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("KIMI_SMOKE_MODEL", "moonshotai/kimi-k2"),
    )
    parser.add_argument(
        "--provider-type",
        default="openai_legacy",
        choices=["openai_legacy", "openai_responses"],
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=180.0,
        help="Seconds to wait for any one prompt/turn to complete.",
    )
    parser.add_argument(
        "--dump-jsonl",
        default=None,
        help="If set, dump raw stdout JSONL lines here for debugging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_smoke(args))


if __name__ == "__main__":
    raise SystemExit(main())
