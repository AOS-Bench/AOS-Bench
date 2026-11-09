"""Smoke tests for Octopus M1 + M2.

These tests don't hit a real LLM; they drive the agent with a scripted
mock LLM that returns a planned sequence of tool calls. The goal is to
verify that:

  - The tool dispatch loop works end-to-end (Bash + Write + Read).
  - The MCP stdio client can talk to the actual aos_pool MCP server.
  - The subagent layer (foreground + background + nested) works.
  - The emitted AgentTrace conforms to the v0.1 shape we promise in
    docs/octopus.md.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from aos_bench.harness.octopus.agent import Agent, AgentConfig
from aos_bench.harness.octopus.coordinator import Coordinator, SubagentType
from aos_bench.harness.octopus.llm import LLMResponse, ToolCall
from aos_bench.harness.octopus.mcp_client import StdioMCPClient, parse_mcp_spec
from aos_bench.harness.octopus.tools import (
    ToolRegistry,
    builtin_tools,
)
from aos_bench.harness.octopus.trace import TraceWriter


# ── mock LLM ──────────────────────────────────────────────────────────


@dataclass
class _Script:
    """A scripted mock LLM. ``steps_by_agent[agent_name]`` is the list of
    responses returned to that agent's loop in order. For the root agent
    use the configured ``agent_name`` (default "octopus-default")."""

    steps_by_agent: dict[str, list[LLMResponse]]
    idx: dict[str, int]

    @classmethod
    def make(cls, **per_agent: list[LLMResponse]) -> "_Script":
        return cls(steps_by_agent=dict(per_agent), idx=defaultdict(int))

    async def chat(self, *, messages, tools, model):  # noqa: D401
        # Pick the agent identity from the system message if present, else
        # treat as the root. Tests set a system prompt prefixed with
        # "AGENT:<name>" to multiplex.
        agent_name = "root"
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content") or ""
                if content.startswith("AGENT:"):
                    agent_name = content.split("\n", 1)[0][len("AGENT:"):].strip()
                    break
        steps = self.steps_by_agent.get(agent_name, [])
        i = self.idx[agent_name]
        if i >= len(steps):
            return LLMResponse(
                content="done.", tool_calls=[], finish_reason="stop",
                prompt_tokens=1, completion_tokens=1, raw={},
            )
        self.idx[agent_name] = i + 1
        return steps[i]


def _tc(call_id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


def _resp(*, content=None, tool_calls=(), pt=10, ct=5):
    return LLMResponse(
        content=content,
        tool_calls=list(tool_calls),
        finish_reason="tool_calls" if tool_calls else "stop",
        prompt_tokens=pt,
        completion_tokens=ct,
        raw={},
    )


def _make_trace(tmp_path):
    return TraceWriter(
        agent_system={"name": "octopus", "version": "test", "trace_source": "native"},
        event_log_path=tmp_path / "events.jsonl",
        trace_out_path=tmp_path / "agent_trace.json",
    )


def _root_via_agent(tmp_path, script, *, max_steps=10):
    """Run a single root agent without going through the Coordinator —
    used by M1 tests that don't need subagents."""
    cwd = tmp_path
    registry = ToolRegistry()
    for t in builtin_tools(cwd=cwd):
        registry.register(t)
    trace = _make_trace(tmp_path)
    run = trace.start_root(
        agent_name="octopus-default",
        model={"provider": "openai-compat", "name": "mock"},
        resource_limits={"max_steps": max_steps},
    )
    agent = Agent(
        cfg=AgentConfig(model="mock", max_steps=max_steps),
        llm=script, tools=registry, trace=trace, run=run,
        instruction="please do something",
    )
    result = asyncio.run(agent.run())
    trace.flush_to_disk()
    trace.close()
    return result, json.loads((tmp_path / "agent_trace.json").read_text())


# ── M1: builtin tools, max_steps, error handling ─────────────────────


def test_builtin_tools_roundtrip(tmp_path):
    cwd = tmp_path
    script = _Script.make(root=[
        _resp(tool_calls=[_tc("c1", "Write",
                              {"path": "hello.txt", "content": "hi octopus\n"})]),
        _resp(tool_calls=[_tc("c2", "Bash", {"command": "wc -c hello.txt"})]),
        _resp(tool_calls=[_tc("c3", "Read", {"path": "hello.txt"})]),
        _resp(content="all three tools worked.", tool_calls=[]),
    ])
    result, doc = _root_via_agent(tmp_path, script)

    assert result.stop_reason == "natural_end"
    assert result.total_steps == 4
    assert (cwd / "hello.txt").read_text() == "hi octopus\n"
    assert doc["schema_version"] == "AgentTrace-v0.1"
    assert len(doc["runs"]) == 1
    run = doc["runs"][0]
    assert run["role"] == "root"
    assert run["status"] == "completed"
    kinds = [e["kind"] for e in run["events"]]
    assert kinds.count("step_begin") == 4
    assert kinds.count("step_end") == 4
    assert kinds.count("tool_call") == 3
    assert kinds.count("tool_result") == 3
    assert run["resource_usage"]["prompt_tokens"] == 40
    assert run["resource_usage"]["completion_tokens"] == 20


def test_max_steps_truncation(tmp_path):
    script = _Script.make(root=[
        _resp(tool_calls=[_tc(f"c{i}", "Bash", {"command": "echo hi"})])
        for i in range(20)
    ])
    result, _ = _root_via_agent(tmp_path, script, max_steps=3)
    assert result.stop_reason == "max_steps_reached"
    assert result.total_steps == 3


def test_tool_failure_is_reported_not_raised(tmp_path):
    script = _Script.make(root=[
        _resp(tool_calls=[_tc("c1", "Read", {"path": "/no/such/file/12345"})]),
        _resp(content="i couldn't read it.", tool_calls=[]),
    ])
    result, doc = _root_via_agent(tmp_path, script)
    assert result.stop_reason == "natural_end"
    err_results = [
        e for e in doc["runs"][0]["events"]
        if e["kind"] == "tool_result" and e.get("phase") == "error"
    ]
    assert len(err_results) == 1
    assert "tool 'Read' raised" in err_results[0]["payload"]["output"]


def test_parse_mcp_spec():
    name, argv = parse_mcp_spec("aos_pool=python3,-m,aos_bench.pool.server")
    assert name == "aos_pool"
    assert argv == ["python3", "-m", "aos_bench.pool.server"]

    name, argv = parse_mcp_spec(r"x=cmd,arg with\,comma,arg2")
    assert name == "x"
    assert argv == ["cmd", "arg with,comma", "arg2"]

    name, argv = parse_mcp_spec("y=cmd 'arg with space' arg2")
    assert name == "y"
    assert argv == ["cmd", "arg with space", "arg2"]

    with pytest.raises(ValueError):
        parse_mcp_spec("missing-equals")
    with pytest.raises(ValueError):
        parse_mcp_spec("=empty-name")


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("mcp") is None,
    reason="mcp package not installed (used by aos_pool MCP server)",
)
def test_mcp_client_against_aos_pool(tmp_path):
    sched = {
        "max_wall_sec": 60,
        "arrivals": [
            {"at_sec": 0, "task_id": "t1", "instruction": "say hi"},
        ],
    }
    sched_path = tmp_path / "schedule.json"
    sched_path.write_text(json.dumps(sched))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    async def go():
        client = StdioMCPClient(
            "aos_pool",
            ["python3", "-m", "aos_bench.pool.server",
             "--schedule", str(sched_path),
             "--state-dir", str(state_dir)],
            cwd=str(Path(__file__).resolve().parents[4]),
        )
        try:
            await client.start(init_timeout_s=15.0)
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert "todo_next" in tool_names
            assert "todo_update" in tool_names
            result = await client.call_tool("todo_next", {"timeout_sec": 5})
            payload = json.loads(result["text"])
            assert payload["task_id"] == "t1"
            done = await client.call_tool(
                "todo_update", {"task_id": "t1", "status": "completed"}
            )
            assert json.loads(done["text"])["ok"] is True
        finally:
            await client.close()

    asyncio.run(go())


# ── M2: subagent ──────────────────────────────────────────────────────


def _coordinator(tmp_path, script, **subagent_types) -> tuple[Coordinator, TraceWriter]:
    """Build a Coordinator with builtin tools and the given subagent types."""
    trace = _make_trace(tmp_path)
    coord = Coordinator(
        trace=trace,
        llm=script,
        default_cfg=AgentConfig(
            model="mock",
            agent_name="root",
            max_steps=12,
        ),
        base_tools=list(builtin_tools(cwd=tmp_path)),
        subagent_types=subagent_types,
    )
    return coord, trace


def test_subagent_foreground(tmp_path):
    """Root spawns one foreground subagent that writes a file and returns."""
    explorer = SubagentType(
        name="explorer",
        system_prompt="AGENT:explorer\nbe an explorer.",
        max_steps=5,
    )
    script = _Script.make(
        root=[
            _resp(tool_calls=[_tc("c1", "agent", {
                "subagent_type": "explorer",
                "description": "list files",
                "prompt": "write a file then say hi",
            })]),
            _resp(content="parent done.", tool_calls=[]),
        ],
        explorer=[
            _resp(tool_calls=[_tc("e1", "Write",
                                  {"path": "from_sub.txt", "content": "x\n"})]),
            _resp(content="i wrote x.", tool_calls=[]),
        ],
    )
    coord, trace = _coordinator(tmp_path, script, explorer=explorer)

    async def go():
        return await coord.run_root(instruction="please delegate")

    result = asyncio.run(go())
    asyncio.run(coord.shutdown())
    trace.close()

    assert result.stop_reason == "natural_end"
    assert (tmp_path / "from_sub.txt").exists()

    doc = json.loads((tmp_path / "agent_trace.json").read_text())
    assert len(doc["runs"]) == 2
    roots = [r for r in doc["runs"] if r["role"] == "root"]
    subs = [r for r in doc["runs"] if r["role"] == "subagent"]
    assert len(roots) == 1
    assert len(subs) == 1
    sub = subs[0]
    assert sub["parent_run_id"] == roots[0]["run_id"]
    assert sub["agent_name"] == "explorer"
    assert sub["launch_mode"] == "foreground"
    assert sub["status"] == "completed"
    assert sub["resource_limits"]["max_steps"] == 5

    # subagent_spawn event lives in the parent
    spawns = [e for e in roots[0]["events"] if e["kind"] == "subagent_spawn"]
    assert len(spawns) == 1
    assert spawns[0]["payload"]["child_run_id"] == sub["run_id"]
    assert spawns[0]["payload"]["launch_mode"] == "foreground"
    assert doc["summary"]["subagent_count"] == 1


def test_subagent_background_status_then_wait(tmp_path):
    """Root spawns background subagent, polls status, then waits to drain."""
    builder = SubagentType(
        name="builder",
        system_prompt="AGENT:builder\nbuild stuff.",
        max_steps=4,
    )
    script = _Script.make(
        root=[
            _resp(tool_calls=[_tc("c1", "agent", {
                "subagent_type": "builder",
                "description": "background work",
                "prompt": "do work in background",
                "run_in_background": True,
            })]),
            _resp(tool_calls=[_tc("c2", "agent_wait", {
                "task_id": "__PLACEHOLDER__",  # fixed up below
                "timeout_s": 5,
            })]),
            _resp(content="parent observed completion.", tool_calls=[]),
        ],
        builder=[
            _resp(tool_calls=[_tc("b1", "Bash", {"command": "echo built"})]),
            _resp(content="built.", tool_calls=[]),
        ],
    )

    # We need the actual task_id from the first tool result. Patch the
    # script's second response on the fly by intercepting in the chat
    # function — easiest path: pre-spawn, then run.
    coord, trace = _coordinator(tmp_path, script, builder=builder)

    # We'll splice the task_id at runtime by replacing the script step
    # right before the second LLM call. To do that simply, we monkey-patch
    # _Script.chat.
    original_chat = script.chat
    captured_task_id = {}

    async def patched(*, messages, tools, model):
        # If the most recent message is a tool result for our background
        # spawn, capture the task_id and rewrite the next agent_wait call.
        for m in reversed(messages):
            if m.get("role") == "tool":
                try:
                    payload = json.loads(m["content"])
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                if payload.get("status") == "running_background" and "task_id" in payload:
                    captured_task_id["id"] = payload["task_id"]
                break
        if captured_task_id and script.idx.get("root", 0) == 1:
            # Patch the second root step's agent_wait task_id.
            step = script.steps_by_agent["root"][1]
            for tc in step.tool_calls:
                if tc.name == "agent_wait":
                    tc.arguments["task_id"] = captured_task_id["id"]
        return await original_chat(messages=messages, tools=tools, model=model)

    script.chat = patched  # type: ignore[method-assign]

    async def go():
        return await coord.run_root(instruction="please delegate in background")

    result = asyncio.run(go())
    asyncio.run(coord.shutdown())
    trace.close()

    assert result.stop_reason == "natural_end"

    doc = json.loads((tmp_path / "agent_trace.json").read_text())
    subs = [r for r in doc["runs"] if r["role"] == "subagent"]
    assert len(subs) == 1
    assert subs[0]["launch_mode"] == "background"
    assert subs[0]["status"] == "completed"
    spawns = [e for e in doc["runs"][0]["events"] if e["kind"] == "subagent_spawn"]
    assert spawns and spawns[0]["payload"]["launch_mode"] == "background"


def test_subagent_unknown_type_returns_error(tmp_path):
    script = _Script.make(root=[
        _resp(tool_calls=[_tc("c1", "agent", {
            "subagent_type": "nope",
            "description": "x",
            "prompt": "x",
        })]),
        _resp(content="acknowledged.", tool_calls=[]),
    ])
    coord, trace = _coordinator(
        tmp_path, script,
        general=SubagentType(name="general", max_steps=5),
    )

    async def go():
        return await coord.run_root(instruction="try unknown")

    result = asyncio.run(go())
    asyncio.run(coord.shutdown())
    trace.close()

    assert result.stop_reason == "natural_end"
    doc = json.loads((tmp_path / "agent_trace.json").read_text())
    # Unknown type → no child run created.
    assert len(doc["runs"]) == 1
    # Tool result must contain the error message.
    tool_results = [
        e for e in doc["runs"][0]["events"] if e["kind"] == "tool_result"
    ]
    assert any(
        "unknown subagent_type" in tr["payload"]["output"] for tr in tool_results
    )


def test_subagent_nested(tmp_path):
    """A subagent can itself spawn a subagent (3 runs, 2 levels deep)."""
    outer = SubagentType(name="outer", system_prompt="AGENT:outer", max_steps=4)
    inner = SubagentType(name="inner", system_prompt="AGENT:inner", max_steps=3)

    script = _Script.make(
        root=[
            _resp(tool_calls=[_tc("c1", "agent", {
                "subagent_type": "outer",
                "description": "outer",
                "prompt": "spawn inner",
            })]),
            _resp(content="root done", tool_calls=[]),
        ],
        outer=[
            _resp(tool_calls=[_tc("o1", "agent", {
                "subagent_type": "inner",
                "description": "inner",
                "prompt": "do tiny work",
            })]),
            _resp(content="outer done", tool_calls=[]),
        ],
        inner=[
            _resp(content="inner done", tool_calls=[]),
        ],
    )
    coord, trace = _coordinator(tmp_path, script, outer=outer, inner=inner)

    async def go():
        return await coord.run_root(instruction="trigger nested")

    asyncio.run(go())
    asyncio.run(coord.shutdown())
    trace.close()

    doc = json.loads((tmp_path / "agent_trace.json").read_text())
    assert len(doc["runs"]) == 3
    by_name = {r["agent_name"]: r for r in doc["runs"]}
    assert "outer" in by_name and "inner" in by_name
    assert by_name["inner"]["parent_run_id"] == by_name["outer"]["run_id"]
    assert by_name["outer"]["parent_run_id"] is not None
    # Root has 1 spawn, outer has 1 spawn, inner has 0.
    spawn_counts = {
        r["agent_name"]: sum(1 for e in r["events"] if e["kind"] == "subagent_spawn")
        for r in doc["runs"]
    }
    assert spawn_counts["outer"] == 1
    assert spawn_counts["inner"] == 0
    assert doc["summary"]["subagent_count"] == 2


def test_edit_tool_unique_match(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 1\ny = 2\nz = 3\n")
    script = _Script.make(root=[
        _resp(tool_calls=[_tc("c1", "Edit", {
            "path": "src.py", "old_string": "y = 2", "new_string": "y = 22",
        })]),
        _resp(content="edited.", tool_calls=[]),
    ])
    result, doc = _root_via_agent(tmp_path, script)
    assert result.stop_reason == "natural_end"
    assert target.read_text() == "x = 1\ny = 22\nz = 3\n"
    tr = [
        e for e in doc["runs"][0]["events"]
        if e["kind"] == "tool_result" and e["payload"]["tool_name"] == "Edit"
    ]
    assert tr and tr[0]["phase"] == "completed"
    assert json.loads(tr[0]["payload"]["output"]) == {"ok": True, "replacements": 1}


def test_edit_tool_ambiguous_without_replace_all(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("a\na\na\n")
    script = _Script.make(root=[
        _resp(tool_calls=[_tc("c1", "Edit", {
            "path": "f.txt", "old_string": "a", "new_string": "b",
        })]),
        _resp(tool_calls=[_tc("c2", "Edit", {
            "path": "f.txt", "old_string": "a", "new_string": "b",
            "replace_all": True,
        })]),
        _resp(content="done.", tool_calls=[]),
    ])
    result, doc = _root_via_agent(tmp_path, script)
    assert result.stop_reason == "natural_end"
    assert target.read_text() == "b\nb\nb\n"
    edit_results = [
        e for e in doc["runs"][0]["events"]
        if e["kind"] == "tool_result" and e["payload"]["tool_name"] == "Edit"
    ]
    assert len(edit_results) == 2
    assert edit_results[0]["phase"] == "error"
    assert "occurs 3 times" in edit_results[0]["payload"]["output"]
    assert edit_results[1]["phase"] == "completed"
    assert json.loads(edit_results[1]["payload"]["output"]) == {
        "ok": True, "replacements": 3,
    }


def test_grep_tool_with_include_filter(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def hello():\n    return 1\n")
    (tmp_path / "src" / "b.py").write_text("class Hello:\n    pass\n")
    (tmp_path / "src" / "c.txt").write_text("hello world\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skip.py").write_text("hello\n")

    script = _Script.make(root=[
        _resp(tool_calls=[_tc("c1", "Grep", {
            "pattern": "[Hh]ello", "include": "*.py", "path": "src",
        })]),
        _resp(content="grepped.", tool_calls=[]),
    ])
    result, doc = _root_via_agent(tmp_path, script)
    assert result.stop_reason == "natural_end"
    tr = [
        e for e in doc["runs"][0]["events"]
        if e["kind"] == "tool_result" and e["payload"]["tool_name"] == "Grep"
    ]
    payload = json.loads(tr[0]["payload"]["output"])
    files = sorted({m["file"].rsplit("/", 1)[-1] for m in payload["matches"]})
    assert files == ["a.py", "b.py"]
    assert payload["truncated"] is False
    assert payload["count"] == 2


def test_resource_sampler_fills_root_usage(tmp_path):
    """ProcessSampler attaches cpu_time_ms / memory_peak_mb to the root run."""
    script = _Script.make(root=[
        # Burn a tiny amount of CPU in a child so the sampler sees it.
        _resp(tool_calls=[_tc("c1", "Bash", {
            "command": "python3 -c 'sum(i*i for i in range(20000))'",
        })]),
        _resp(content="ok", tool_calls=[]),
    ])
    coord, trace = _coordinator(tmp_path, script)

    async def go():
        return await coord.run_root(instruction="hi")

    asyncio.run(go())
    asyncio.run(coord.shutdown())
    trace.close()

    doc = json.loads((tmp_path / "agent_trace.json").read_text())
    root = doc["runs"][0]
    usage = root["resource_usage"]
    # Always set
    assert "cpu_time_ms" in usage
    assert "memory_peak_mb" in usage
    assert "resource_samples" in usage
    assert usage["resource_scope"] == "process_group"
    assert usage["resource_samples"] >= 1
    # CPU should be > 0 because the bash child used some CPU.
    assert usage["cpu_time_ms"] >= 0
    # On Linux we expect a non-zero RSS reading; non-Linux can be 0.
    if sys.platform.startswith("linux"):
        assert usage["memory_peak_mb"] > 0


def test_subagent_tool_allowlist(tmp_path):
    """Subagent with `tools=["Read"]` must not see Bash/Write."""
    reader = SubagentType(
        name="reader",
        system_prompt="AGENT:reader\nread only.",
        max_steps=3,
        tools=["Read"],  # only Read (and the agent family is also gated)
    )
    # Subagent attempts Bash; should come back as "unknown tool" because
    # Bash was filtered out of its registry.
    script = _Script.make(
        root=[
            _resp(tool_calls=[_tc("c1", "agent", {
                "subagent_type": "reader",
                "description": "try Bash",
                "prompt": "try to call Bash",
            })]),
            _resp(content="ack", tool_calls=[]),
        ],
        reader=[
            _resp(tool_calls=[_tc("r1", "Bash", {"command": "echo nope"})]),
            _resp(content="couldn't run bash", tool_calls=[]),
        ],
    )
    coord, trace = _coordinator(tmp_path, script, reader=reader)

    async def go():
        return await coord.run_root(instruction="trigger reader")

    asyncio.run(go())
    asyncio.run(coord.shutdown())
    trace.close()

    doc = json.loads((tmp_path / "agent_trace.json").read_text())
    sub = [r for r in doc["runs"] if r["role"] == "subagent"][0]
    bash_results = [
        e for e in sub["events"]
        if e["kind"] == "tool_result"
        and e["payload"].get("tool_name") == "Bash"
    ]
    assert len(bash_results) == 1
    assert "unknown tool" in bash_results[0]["payload"]["output"]
