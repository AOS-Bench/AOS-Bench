"""Post-hoc analysis of completed AOSBench stream trials.

The eval loop writes a pile of per-trial artifacts (ATIF trajectory, harness
transcript, cgroup samples, verifier rewards, scheduling metrics). This package
turns a ``jobs/`` tree into four views that we actually reason about:

  1. ``health``      — is this trial *quotable*, or was it throttled / stunted /
                       killed by infra? Everything else is conditioned on this.
  2. ``metrics``     — per-trial basics: steps, tool histogram, tokens, CPU/mem.
  3. ``performance`` — pass rate, wall-clock phase split, throughput.
  4. ``behavior``    — shortcut/hacking detection, resource-awareness, strategy.

Design rules, learned the hard way:

* **Health gating is not optional.** A rate-limited trial still writes a
  well-formed ``result.json`` with no exception and ``n_errors=0``; its only
  fingerprint is in the harness transcript. If you average rewards without
  splitting clean-vs-throttled you measure gateway capacity, not model skill.
* **Never grep a bare HTTP status.** Sandbox hostnames and MCP session ids
  contain ``429``/``401``/``403`` as literal digits. Detectors here match full
  provider-specific phrases only (see ``health.THROTTLE_PATTERNS``).
* **Stdlib only.** This runs on operator boxes against read-only NFS copies of
  the jobs directory; adding deps would make it unrunnable where it is needed.
* **Never crash on partial data.** Jobs are analysed while sibling trials are
  still in flight, so every artifact is optional and every parse is defensive.
"""

from __future__ import annotations

__all__ = [
    "behavior",
    "health",
    "loader",
    "metrics",
    "performance",
    "report",
]
