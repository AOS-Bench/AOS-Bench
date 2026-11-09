"""Throttle / stunted / infra-error detection tests.

The false-negative tests (each harness's real wording) and the false-positive
tests (bare status codes in hostnames and session ids) are equally load-bearing:
missing codex's wording understated its throttling 0/15 vs 14/15, and matching a
bare ``429`` previously cost ~780 sandboxes on a wrong diagnosis.
"""

from __future__ import annotations

from pathlib import Path

from aos_bench.traj_analysis import health, loader
from aos_bench.traj_analysis.tests import fixtures

HEALTHY_STEPS = [
    fixtures.step(i, f"2026-09-19T03:{i // 60:02d}:{i % 60:02d}.000Z", tool="Bash", arguments={"command": "ls"})
    for i in range(1, 41)
]


def _classify(tmp_path: Path, **kwargs) -> health.TrialHealth:
    fixtures.write_trial(tmp_path, kwargs.pop("job", "main-codex-stream-light"), kwargs.pop("trial", "a__X"), **kwargs)
    record = loader.discover_trials(tmp_path)[0]
    return health.classify(record)


def test_clean_trial(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        steps=HEALTHY_STEPS,
        transcript='{"type":"item.completed","item":{"status":"completed"}}',
        rewards={"t": 1.0},
    )
    assert result.status == "clean"
    assert result.is_clean is True
    assert result.throttled is False
    assert result.n_steps == 40


def test_kimi_rate_limit_detected(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        job="main-kimi-stream-light",
        harness="kimi-code",
        steps=HEALTHY_STEPS,
        transcript='{"error":"APIProviderRateLimitError: slow down"}',
        rewards={"t": 0.0},
    )
    assert result.status == "throttled"
    assert result.throttle_labels == ["kimi_api_provider_rate_limit"]
    assert result.throttle_hits == 1
    assert result.throttle_evidence


def test_kimi_rate_limit_in_stderr_only(tmp_path: Path) -> None:
    """One real kimi trial carries the marker only in stderr; still throttled."""
    result = _classify(
        tmp_path,
        job="main-kimi-stream-light",
        harness="kimi-code",
        steps=HEALTHY_STEPS,
        transcript="nothing interesting here",
        stderr_transcript="APIProviderRateLimitError",
        rewards={"t": 0.0},
    )
    assert result.throttled is True
    assert result.status == "throttled"


def test_gateway_chinese_throttle_and_bucket(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        job="main-cc-stream-medium",
        harness="claude-code",
        steps=HEALTHY_STEPS,
        transcript='{"content":"触发平台限流(bucket=project_rpm,limit=100)"}',
        rewards={"t": 0.0},
    )
    assert result.status == "throttled"
    assert result.throttle_labels == ["gateway_platform_throttle"]
    assert result.throttle_buckets == {"project_rpm:100": 1}


def test_gateway_tpm_bucket(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        job="main-glm-stream-medium",
        harness="claude-code",
        steps=HEALTHY_STEPS,
        transcript="触发平台限流(bucket=project_tpm,limit=1000000)",
        rewards={"t": 0.0},
    )
    assert result.throttle_buckets == {"project_tpm:1000000": 1}


def test_codex_documented_retry_limit_wording(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        steps=HEALTHY_STEPS,
        transcript="stream error: exceeded retry limit, last status: 429 Too Many Requests",
        rewards={"t": 0.0},
    )
    assert result.status == "throttled"
    assert result.throttle_labels == ["codex_retry_limit_429"]


def test_codex_upstream_wording_without_bucket_or_429(tmp_path: Path) -> None:
    """The wording that actually dominates the real run.

    It contains neither ``bucket=`` nor the literal ``429``; relying on the
    documented phrase alone reports codex as never throttled.
    """
    line = (
        '{"type":"turn.failed","error":{"message":"stream disconnected before completion: '
        'Your requests to gpt-6-astra for gpt-6-astra in eastus2 have exceeded rate limit."}}'
    )
    assert "429" not in line and "bucket=" not in line
    result = _classify(tmp_path, steps=HEALTHY_STEPS, transcript=line, rewards={"t": 0.0})
    assert result.status == "throttled"
    assert result.throttle_labels == ["codex_upstream_rate_limit"]


def test_bare_status_digits_in_hostnames_do_not_fire(tmp_path: Path) -> None:
    """Regression guard for the ~780-sandbox false-positive incident."""
    noise = (
        '{"url":"https://sandbox-123401403.example.com/v1/start",'
        '"session_id":"429f1a-401b-403c-9d28-4297e175ec87",'
        '"output":"HTTP/1.1 200 OK; retry_after=429; pid 401; exit 403"}'
    )
    result = _classify(tmp_path, steps=HEALTHY_STEPS, transcript=noise, rewards={"t": 1.0})
    assert result.throttled is False
    assert result.status == "clean"


def test_auth_failure_is_not_throttling(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        steps=HEALTHY_STEPS,
        transcript="exceeded retry limit, last status: 401 Unauthorized\nFailed to refresh token",
        rewards={"t": 0.0},
    )
    assert result.throttled is False
    assert result.auth_labels == ["codex_retry_limit_401", "token_refresh_failed"]


def test_stunted_trajectory_backstop(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        steps=HEALTHY_STEPS[:3],
        transcript="no known throttle wording at all",
        rewards={"t": 0.0},
    )
    assert result.stunted is True
    assert result.status == "stunted"
    assert result.n_steps == 3


def test_stunted_boundary_is_inclusive(tmp_path: Path) -> None:
    at_limit = _classify(tmp_path, steps=HEALTHY_STEPS[: health.STUNTED_MAX_STEPS], transcript="x")
    assert at_limit.stunted is True


def test_stunted_boundary_above_limit_is_clean(tmp_path: Path) -> None:
    above = _classify(
        tmp_path, steps=HEALTHY_STEPS[: health.STUNTED_MAX_STEPS + 1], transcript="x", rewards={"t": 1.0}
    )
    assert above.stunted is False
    assert above.status == "clean"


def test_throttled_takes_precedence_over_stunted(tmp_path: Path) -> None:
    """Report the cause, not the symptom - but keep both flags."""
    result = _classify(
        tmp_path, steps=HEALTHY_STEPS[:2], transcript="have exceeded rate limit", rewards={"t": 0.0}
    )
    assert result.status == "throttled"
    assert result.stunted is True
    assert result.throttled is True


def test_image_pull_infra_error_classified(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        job="main-cc-stream-light",
        harness="claude-code",
        steps=None,
        exception=(
            "RuntimeError: Sandbox API error (code=260503): container sandbox: "
            'ImagePullBackOff - Back-off pulling image "registry.example.com/x:y"'
        ),
        write_result=False,
    )
    assert result.status == "infra_error"
    assert result.infra_error_classes[:2] == ["image_pull_backoff", "sandbox_260503"]
    assert result.is_clean is False


def test_infra_error_outranks_throttle(tmp_path: Path) -> None:
    result = _classify(
        tmp_path,
        steps=HEALTHY_STEPS,
        transcript="have exceeded rate limit",
        exception="NonZeroAgentExitCodeError: agent exited 1",
        write_result=False,
    )
    assert result.status == "infra_error"
    assert result.infra_error_classes == ["nonzero_agent_exit"]


def test_unknown_exception_still_flagged(tmp_path: Path) -> None:
    result = _classify(tmp_path, steps=None, exception="ValueError: who knows", write_result=False)
    assert result.infra_error_classes == ["unclassified_exception"]
    assert result.status == "infra_error"


def test_in_flight_outranks_everything(tmp_path: Path) -> None:
    result = _classify(tmp_path, steps=HEALTHY_STEPS[:1], write_result=False)
    assert result.status == "in_flight"
    # In-flight trials must not accrue missing-artifact findings.
    assert result.missing_artifacts == []


def test_missing_artifacts_reported_for_finished_trial(tmp_path: Path) -> None:
    result = _classify(tmp_path, steps=HEALTHY_STEPS, transcript="x", rewards={"t": 1.0})
    assert "verifier/scheduling_metrics.json" in result.missing_artifacts
    assert "agent/.harbor-cgroup-resources.tsv" in result.missing_artifacts


def test_finished_without_trajectory_is_unparseable_not_zero(tmp_path: Path) -> None:
    result = _classify(tmp_path, steps=None, rewards={"t": 0.0}, write_result=True)
    assert result.status == "unparseable"
    assert result.is_clean is False


# --- account-level outages (budget), and what must NOT match ---------------


def test_budget_exhaustion_is_account_error_not_throttled(tmp_path: Path) -> None:
    """A dead project budget is an operations failure, not gateway capacity.

    This is the trap the coordinator hit: kimi-stream-medium fell 0.800 -> 0.204
    with EVERY throttle detector silent, because the project ran out of money
    mid-sweep. Such a trial writes a well-formed result.json, so it must be
    caught by wording and ranked above `throttled`.
    """
    health_ = _classify(
        tmp_path,
        job="main-kimi-stream-medium",
        harness="kimi-code",
        steps=[fixtures.step(i, f"2026-09-19T03:00:{i:02d}.000Z", tool="Bash") for i in range(40)],
        transcript='{"message":"quota","type":"UserBudgetExhausted","code":"2005"}',
        rewards={"a": 0.0},
    )
    assert health_.status == "account_error"
    assert health_.account_labels == ["budget_exhausted"]
    assert health_.throttled is False


def test_agent_quoting_a_missing_model_is_not_an_account_error(tmp_path: Path) -> None:
    """Verified false positive: the agent narrates gateway probes of its own.

    A wildclaw task has the agent discover that `gpt-5-mini-2025-08-07` is not
    served and say so in its transcript. Matching `UserModelNotFound` would
    reclassify 28 healthy trials (mean reward 0.602) as account failures. Any
    phrase an agent could plausibly quote is unsafe as a health detector.
    """
    health_ = _classify(
        tmp_path,
        job="main-kimi-stream-heavy",
        harness="kimi-code",
        steps=[fixtures.step(i, f"2026-09-19T03:00:{i:02d}.000Z", tool="Bash") for i in range(40)],
        transcript=(
            'The KIMI endpoint does NOT serve gpt-5-mini-2025-08-07 '
            '(returns UserModelNotFound, provider=moonshot only). Proceeding without it.'
        ),
        rewards={"a": 1.0},
    )
    assert health_.status == "clean"
    assert health_.account_labels == []


def test_account_error_outranks_throttling(tmp_path: Path) -> None:
    """Budget death is upstream of any rate limit that also appears."""
    health_ = _classify(
        tmp_path,
        job="main-glm-stream-heavy",
        harness="claude-code",
        steps=[fixtures.step(i, f"2026-09-19T03:00:{i:02d}.000Z", tool="Bash") for i in range(40)],
        transcript="触发平台限流 ... later ... UserBudgetExhausted",
        rewards={"a": 0.0},
    )
    assert health_.status == "account_error"
    # The throttle evidence is retained, just not the headline classification.
    assert health_.throttled is True
