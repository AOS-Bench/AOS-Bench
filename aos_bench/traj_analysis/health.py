"""Decide whether a trial's number is quotable at all.

This module exists because of a specific, expensive failure mode: **a
rate-limited trial looks healthy in every structured artifact**. It writes a
well-formed ``result.json``, ``exception_info`` is ``null``, the verifier runs,
and the reward file is complete — the agent simply got fewer turns and scored
lower. Within ``main-cc-stream-medium`` the measured gap is 0.850 (clean) vs
0.530 (throttled). Averaging across both therefore measures gateway capacity,
not model skill, so every downstream metric in this package is reported split
by :func:`classify` and the headline uses clean trials only.

Four independent detectors, because none of them is sufficient alone:

1. :data:`THROTTLE_PATTERNS` — provider-specific phrases in the raw transcript.
   Each harness/gateway words throttling differently and the wording is the
   *only* signal.
2. Stunted trajectory (``<= STUNTED_MAX_STEPS`` steps) — harness-agnostic
   backstop for "the agent never got going", covering throttles whose wording
   we have not catalogued and launch failures that leave no exception. Healthy
   observed runs are 34-218 steps for light/medium.
3. Infra errors — ``exception.txt`` classified into image-pull / sandbox-API /
   agent-exit / LLM-transport buckets. These are *not* model quality and must
   never be folded into a low score.
4. :data:`ACCOUNT_PATTERNS` — budget exhaustion. Added
   after a mid-sweep budget outage dropped kimi-stream-medium from 0.800 to
   0.204 **with every throttle detector silent**. This is the same trap as (1)
   one level up: the account died, not the gateway, and the structured
   artifacts again look fine. Ranked above ``throttled`` in
   :func:`classify` because it is the upstream cause. Note what is deliberately
   *excluded* there: a phrase the agent could quote from its own environment is
   not a health signal (see the ``UserModelNotFound`` note).

**Do not add a bare status-code pattern.** Matching ``429``/``401``/``403`` as
digits hits sandbox hostnames (``sandbox-...123...example.com``) and MCP session ids;
that mistake previously burned ~780 sandboxes on a false diagnosis. Every
pattern here is a full provider phrase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Iterable

from aos_bench.traj_analysis.loader import TrialRecord

# Harness-agnostic backstop. Observed healthy light/medium runs span 34-218
# steps; <=6 means the agent produced at most a couple of turns before dying.
STUNTED_MAX_STEPS = 6

# Throttle phrases, keyed by the label we report. Values are alternative
# spellings for the same provider condition.
#
# Provenance of each pattern:
#   kimi      -> moonshot harness raises APIProviderRateLimitError by name.
#   gateway_* -> an OpenAI/Anthropic-compatible model gateway that returns a
#                Chinese message, usually with a bucket marker:
#                bucket=project_rpm,limit=100 or bucket=project_tpm,limit=1000000.
#   codex_*   -> two distinct wordings. The documented one is the retry-limit
#                line; the one that actually dominates this run is the upstream
#                Azure text surfaced through `stream disconnected before
#                completion`, which carries NO bucket= and NO literal "429".
#                Both are matched; missing the second understates codex
#                throttling by an order of magnitude (0/15 vs 14/15 on heavy).
THROTTLE_PATTERNS: dict[str, tuple[str, ...]] = {
    "kimi_api_provider_rate_limit": ("APIProviderRateLimitError",),
    "gateway_platform_throttle": ("触发平台限流",),
    "codex_retry_limit_429": ("exceeded retry limit, last status: 429 Too Many Requests",),
    "codex_upstream_rate_limit": ("have exceeded rate limit",),
}

# Auth exhaustion looks like throttling in its effect but is a credential
# problem; kept separate so it is never reported as "the gateway was busy".
AUTH_PATTERNS: dict[str, tuple[str, ...]] = {
    "codex_retry_limit_401": ("exceeded retry limit, last status: 401 Unauthorized",),
    "token_refresh_failed": ("Failed to refresh token",),
}

# Account-level failures that are neither throttling nor model behaviour. These
# are the most dangerous class in this corpus because **the first visible
# symptom is a score collapse with the throttle detector silent**: the project
# budget emptied mid-sweep and kimi-stream-medium fell 0.800 -> 0.204 with zero
# rate-limit lines anywhere. A cell whose reward craters while
# THROTTLE_PATTERNS stay quiet must be checked against these before any
# capability claim is made.
#
# ``UserBudgetExhausted`` is the platform's own wording; ``code 2005`` is its
# numeric form. Both are account-level and unambiguous.
#
# ``UserModelNotFound``/``2004`` is deliberately **NOT** here, despite being the
# sibling failure from the mid-run model rename. Verified false positive: the
# phrase appears in 28 trials, mean reward 0.602, because *the agent itself*
# probes the gateway for other models while solving tasks (a wildclaw OCR task
# has the agent discover that `gpt-5-mini-2025-08-07` is not served, and narrate
# it). Matching it would reclassify 28 healthy trials -- including 5 of
# kimi-stream-heavy -- as account failures and delete a working cell from the
# grid. The harness losing *its own* configured model manifests instead as a
# `PersistentLLMError` in ``exception.txt``, which INFRA_ERROR_CLASSES already
# catches, so nothing is lost by leaving it out.
#
# The general rule this instance illustrates: a transcript contains the agent's
# own observations about the world, not only the harness's failures. Any phrase
# an agent could plausibly *quote* is unsafe as a health detector.
#
# NOTE matched as full phrases, never bare numbers: "2005" occurs in timestamps
# and hashes.
ACCOUNT_PATTERNS: dict[str, tuple[str, ...]] = {
    "budget_exhausted": ("UserBudgetExhausted", '"type":"UserBudgetExhausted"', "code 2005"),
}

# Bucket markers, extracted when present purely as corroborating evidence.
BUCKET_RE = re.compile(r"bucket=(?P<bucket>[a-z_]+),limit=(?P<limit>\d+)")

# exception.txt classification. Ordered: first match wins, so the specific
# image-pull signature is checked before the generic sandbox-API one. The
# numeric codes are a remote sandbox provider's error codes.
INFRA_ERROR_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("image_pull_backoff", ("ImagePullBackOff",)),
    ("registry_500", ("500 Internal Server Error",)),
    ("sandbox_260503", ("code=260503",)),
    ("sandbox_260403", ("code=260403",)),
    ("anthropic_refusal", ("anthropic_refusal", "trajectory is poisoned")),
    ("apt_broken_packages", ("Unable to correct problems, you have held broken packages",)),
    ("nonzero_agent_exit", ("NonZeroAgentExitCodeError",)),
    ("persistent_llm_error", ("PersistentLLMError",)),
    ("sandbox_api_error", ("Sandbox API error", "sandbox API error")),
)

# Artifacts a *finished* trial is expected to have. Absence is only a finding
# when the trial is not in flight — see TrialHealth.missing_artifacts.
EXPECTED_ARTIFACTS: tuple[str, ...] = (
    "result.json",
    "agent/trajectory.json",
    "verifier/reward.json",
    "verifier/scheduling_metrics.json",
    "agent/.harbor-cgroup-resources.tsv",
)

# Read the transcript in chunks; these files reach hundreds of MB and the
# throttle phrases are short, so we only need a small overlap to avoid missing
# a phrase split across a chunk boundary.
_CHUNK_BYTES = 4 * 1024 * 1024
_OVERLAP = 256


@dataclass
class TrialHealth:
    """Verdict for one trial, with the evidence that produced it."""

    trial: str
    job: str
    arm: str
    tier: str
    # clean | throttled | account_error | stunted | infra_error | in_flight
    # | unparseable
    status: str
    throttled: bool = False
    throttle_labels: list[str] = field(default_factory=list)
    throttle_hits: int = 0
    throttle_buckets: dict[str, int] = field(default_factory=dict)
    throttle_evidence: list[str] = field(default_factory=list)
    auth_labels: list[str] = field(default_factory=list)
    account_labels: list[str] = field(default_factory=list)
    account_hits: int = 0
    stunted: bool = False
    n_steps: int | None = None
    infra_error_classes: list[str] = field(default_factory=list)
    infra_error_snippet: str | None = None
    missing_artifacts: list[str] = field(default_factory=list)
    load_errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        """Quotable: finished, not throttled, not stunted, no infra error.

        Note this is deliberately *stricter* than "no exception": a throttled
        trial has no exception at all.
        """
        return self.status == "clean"

    @property
    def is_analysable(self) -> bool:
        """Has a usable trajectory, whether or not it is quotable.

        Throttled/stunted trials are still worth measuring for behaviour and
        for quantifying the throttle's cost; only in-flight and infra-dead
        trials have nothing to read.
        """
        return self.n_steps is not None and self.n_steps > 0


def _iter_chunks(path: Path) -> Iterable[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            tail = ""
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                yield tail + chunk
                tail = chunk[-_OVERLAP:]
    except OSError:
        return


def scan_transcript(
    path: Path,
) -> tuple[dict[str, int], dict[str, int], dict[str, int], list[str], dict[str, int]]:
    """Count throttle/auth/account phrase hits in one transcript.

    Returns ``(throttle, auth, buckets, evidence, account)`` where ``evidence``
    holds up to three trimmed matching excerpts so a reader can confirm the
    classification without re-grepping a 300 MB file.
    """
    throttle: dict[str, int] = {}
    auth: dict[str, int] = {}
    buckets: dict[str, int] = {}
    evidence: list[str] = []
    account: dict[str, int] = {}

    for chunk in _iter_chunks(path):
        for label, phrases in THROTTLE_PATTERNS.items():
            for phrase in phrases:
                count = chunk.count(phrase)
                if count:
                    throttle[label] = throttle.get(label, 0) + count
                    if len(evidence) < 3:
                        at = chunk.find(phrase)
                        excerpt = chunk[max(0, at - 60) : at + len(phrase) + 60]
                        evidence.append(f"{label}: ...{excerpt.strip()}...")
        for label, phrases in AUTH_PATTERNS.items():
            for phrase in phrases:
                count = chunk.count(phrase)
                if count:
                    auth[label] = auth.get(label, 0) + count
        for label, phrases in ACCOUNT_PATTERNS.items():
            for phrase in phrases:
                count = chunk.count(phrase)
                if count:
                    account[label] = account.get(label, 0) + count
        for match in BUCKET_RE.finditer(chunk):
            key = f"{match.group('bucket')}:{match.group('limit')}"
            buckets[key] = buckets.get(key, 0) + 1

    return throttle, auth, buckets, evidence, account


def classify_infra_error(text: str) -> list[str]:
    """Bucket an ``exception.txt`` body into known infra failure classes.

    Returns every class that matches (an ImagePullBackOff traceback also
    mentions the sandbox API), ordered as in :data:`INFRA_ERROR_CLASSES` so the
    most specific label comes first.
    """
    return [label for label, phrases in INFRA_ERROR_CLASSES if any(p in text for p in phrases)]


def missing_artifacts(record: TrialRecord) -> list[str]:
    return [name for name in EXPECTED_ARTIFACTS if not (record.path / name).exists()]


def classify(record: TrialRecord) -> TrialHealth:
    """Produce the health verdict for one trial.

    Status precedence is deliberate:

    ``in_flight`` > ``infra_error`` > ``account_error`` > ``throttled``
    > ``stunted`` > ``clean``

    * in_flight first, so unfinished trials never enter any denominator;
    * infra_error next, because an image that never pulled says nothing about
      the model and its trajectory is absent anyway;
    * account_error before throttled, because an emptied budget produces
      throttle-shaped symptoms (short runs, low scores) without a single
      rate-limit line, and calling it "throttled" would misattribute an
      operations failure to gateway capacity;
    * throttled before stunted, because throttling is the *cause* of most
      stunted trajectories and reporting the cause is more useful than
      reporting the symptom (both flags stay set, so either view is available).
    """
    n_steps = len(record.steps) if record.trajectory is not None else None

    throttle: dict[str, int] = {}
    auth: dict[str, int] = {}
    buckets: dict[str, int] = {}
    evidence: list[str] = []
    account: dict[str, int] = {}
    for path in record.transcripts:
        t, a, b, e, acct = scan_transcript(path)
        for dst, src in ((throttle, t), (auth, a), (buckets, b), (account, acct)):
            for key, value in src.items():
                dst[key] = dst.get(key, 0) + value
        evidence.extend(e[: max(0, 3 - len(evidence))])

    # kimi writes one throttle line to stderr only, so stderr is a *detection*
    # fallback: consulted just to flip the boolean when the main transcript is
    # silent, never summed into the hit count (it mirrors the main file).
    if not throttle:
        for path in record.stderr_transcripts:
            t, a, b, e, acct = scan_transcript(path)
            for key, value in acct.items():
                account[key] = account.get(key, 0) + value
            if t:
                for key, value in t.items():
                    throttle[key] = throttle.get(key, 0) + value
                evidence.extend(e[: max(0, 3 - len(evidence))])
                for key, value in b.items():
                    buckets[key] = buckets.get(key, 0) + value

    infra_classes: list[str] = []
    snippet: str | None = None
    if record.exception_text:
        infra_classes = classify_infra_error(record.exception_text)
        if not infra_classes:
            infra_classes = ["unclassified_exception"]
        snippet = " ".join(record.exception_text.split())[-400:]

    stunted = n_steps is not None and n_steps <= STUNTED_MAX_STEPS
    throttled = bool(throttle)

    if record.is_in_flight:
        status = "in_flight"
    elif infra_classes:
        status = "infra_error"
    elif account:
        # Account-level outage (budget emptied, model unrouted). Ranked above
        # throttled/stunted because it is the *cause*, and above clean because
        # such a trial's score is an artifact even when it looks well formed.
        status = "account_error"
    elif record.trajectory is None:
        # Finished (or raised) but no readable trajectory: an export problem,
        # not a model result. Kept distinct so it cannot masquerade as a zero.
        status = "unparseable"
    elif throttled:
        status = "throttled"
    elif stunted:
        status = "stunted"
    else:
        status = "clean"

    return TrialHealth(
        trial=record.trial_name,
        job=record.job,
        arm=record.arm,
        tier=record.tier,
        status=status,
        throttled=throttled,
        throttle_labels=sorted(throttle),
        throttle_hits=sum(throttle.values()),
        throttle_buckets=buckets,
        throttle_evidence=evidence[:3],
        auth_labels=sorted(auth),
        account_labels=sorted(account),
        account_hits=sum(account.values()),
        stunted=stunted,
        n_steps=n_steps,
        infra_error_classes=infra_classes,
        infra_error_snippet=snippet,
        missing_artifacts=[] if record.is_in_flight else missing_artifacts(record),
        load_errors=dict(record.load_errors),
    )
