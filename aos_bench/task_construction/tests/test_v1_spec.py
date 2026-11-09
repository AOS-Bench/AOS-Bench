from aos_bench.task_construction.scripts.generate_v1_0 import BUCKETS
from aos_bench.task_construction.scripts.v1_low_overlap_scan import (
    BUCKET_SPECS,
    FINAL_COUNTS,
)
from pathlib import Path

import pytest

from aos_bench.task_construction.v1_spec import (
    STREAM_TIER_MIX,
    validate_agent_pack_lock,
)


def test_v1_tier_v2_composition_is_shared_by_generators() -> None:
    assert STREAM_TIER_MIX == {
        "light": {"L2": 5, "L3": 0},
        "medium": {"L2": 10, "L3": 0},
        "heavy": {"L2": 10, "L3": 10},
    }
    for bucket, mix in STREAM_TIER_MIX.items():
        assert BUCKETS[bucket]["k_L2"] == mix["L2"]
        assert BUCKETS[bucket]["k_L3"] == mix["L3"]
        _, l2, l3 = BUCKET_SPECS[bucket]
        assert (l2, l3) == (mix["L2"], mix["L3"])


def test_low_overlap_selection_count_is_unchanged() -> None:
    assert FINAL_COUNTS == {"light": 10, "medium": 5, "heavy": 5}


def test_tier_v2_requires_digest_locked_agent_packs(tmp_path: Path) -> None:
    digest = "sha256:" + "1" * 64
    lock = tmp_path / "agents.lock.toml"
    lock.write_text(
        'schema_version = 1\n'
        '[agents.claude-code]\n'
        'version = "2.1.198"\nstatus = "locked"\n'
        f'image = "registry/claude@{digest}"\n'
        '[agents.codex]\n'
        'version = "0.145.0"\nstatus = "locked"\n'
        f'image = "registry/codex@{digest}"\n'
    )
    assert set(validate_agent_pack_lock(lock)) == {"claude-code", "codex"}

    lock.write_text(lock.read_text().replace('status = "locked"', 'status = "unbuilt"', 1))
    with pytest.raises(ValueError, match="not prebuilt"):
        validate_agent_pack_lock(lock)
