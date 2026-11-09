#!/usr/bin/env python3
"""Shrink a captured kimi-code ``wire.jsonl`` without changing its structure.

Two record fields dominate the on-disk size of a real journal and carry no
information the AOS-Bench readers need twice:

* ``llm.tools_snapshot.tools`` — the full JSON-Schema of all 25 built-in tools
  (~70 KB per agent journal);
* ``profile.bind.systemPrompt`` — the built-in system prompt (~24 KB).

Both are preserved verbatim in ``live_probe/solo/`` so the exact shape stays on
record; every other capture runs through this script so the fixture tree stays
a few tens of KB instead of ~800 KB. Nothing else is touched: record order,
record types, every other key and value are byte-identical to what the CLI
wrote. See ``../../README.md`` for provenance.

Usage: elide_wire.py <wire.jsonl> [<wire.jsonl> ...]   (rewrites in place)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def elide(record: dict) -> dict:
    if record.get("type") == "llm.tools_snapshot" and isinstance(
        record.get("tools"), list
    ):
        n = len(record["tools"])
        record["tools"] = [
            f"<elided: {n} tool schemas — see live_probe/solo for the real shape>"
        ]
    elif record.get("type") == "profile.bind" and isinstance(
        record.get("systemPrompt"), str
    ):
        n = len(record["systemPrompt"])
        record["systemPrompt"] = (
            f"<elided: {n} chars — see live_probe/solo for the real shape>"
        )
    return record


def main(paths: list[str]) -> int:
    for raw_path in paths:
        path = Path(raw_path)
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            out.append(
                json.dumps(
                    elide(json.loads(line)), ensure_ascii=False, separators=(",", ":")
                )
            )
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"elided {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
