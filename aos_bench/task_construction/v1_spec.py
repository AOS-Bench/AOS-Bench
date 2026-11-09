"""Authoritative V1 stream composition and artifact-layout constants.

Tier-v2 reduces the number of tasks merged into each image. Keep these values
in one module so the canonical generator and low-overlap scan cannot drift.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


V1_TIER_SCHEMA_VERSION = 2

STREAM_TIER_MIX: dict[str, dict[str, int]] = {
    "light": {"L2": 5, "L3": 0},
    "medium": {"L2": 10, "L3": 0},
    "heavy": {"L2": 10, "L3": 10},
}

# Canonical corpus keeps the existing stream-count target and 3x
# overproduction policy. Only the task composition changes in tier-v2.
CANONICAL_TARGET_COUNTS = {"light": 20, "medium": 10, "heavy": 10}
CANONICAL_GENERATION_COUNTS = {"light": 60, "medium": 30, "heavy": 30}

# Low-overlap scan and final selection counts are independent of the larger
# canonical corpus target.
LOW_OVERLAP_SCAN_COUNTS = {"light": 40, "medium": 20, "heavy": 20}
LOW_OVERLAP_SELECTION_COUNTS = {"light": 10, "medium": 5, "heavy": 5}
EXPECTED_AGENT_VERSIONS = {"claude-code": "2.1.198", "codex": "0.145.0"}
_DIGEST_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def tier_v2_root(repo_root: Path) -> Path:
    return repo_root / "data" / "stream" / "V1-0" / "tier-v2"


def validate_agent_pack_lock(path: Path) -> dict[str, dict[str, str]]:
    """Require exact, immutable Agent packs before tier-v2 synthesis."""
    data = tomllib.loads(path.read_text())
    agents = data.get("agents") or {}
    for name, version in EXPECTED_AGENT_VERSIONS.items():
        row = agents.get(name) or {}
        if row.get("version") != version:
            raise ValueError(
                f"{name} version must be {version}, found {row.get('version')!r}"
            )
        image = str(row.get("image") or "")
        if row.get("status") != "locked" or not _DIGEST_IMAGE_RE.fullmatch(image):
            raise ValueError(
                f"{name} is not prebuilt and digest-locked in {path}"
            )
    return agents
