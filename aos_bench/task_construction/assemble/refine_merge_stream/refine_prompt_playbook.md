# Verifier-confirmed live repair playbook

- For Ubuntu multiarch, match the ports suites to the base release: use Noble
  ports with Ubuntu 24.04 and never Jammy arm64 with Noble amd64. After
  `dpkg --add-architecture`, constrain every active native legacy and deb822
  Ubuntu source to amd64 before `apt-get update`, leaving only verified
  `ubuntu-ports` entries eligible for arm64. This pattern advanced two Heavy
  candidates past their prior held-package or `binary-arm64` build failure.
- When an Oracle run is broadly partial and multiple failed tasks already have
  `replacement_status=avoid`, do not infer a shared image conflict without a
  common first-causal error. Replace exactly one failed avoid-rated task in a
  plan-only turn, preferring a zero-pass/high-failure task and an eligible
  same-tier replacement. Do not combine the replacement with environment edits.
- When the verifier layer is `overlap`, build and Oracle are already valid. Edit
  only `stream_plan.json`; do not touch Dockerfile, instruction, tests, or solution.
  Preserve the exact task count, tier mix, uniqueness, and blacklist constraints,
  and reduce overlap against every named accepted stream. The metric is
  intersection divided by the smaller task count and the threshold is exclusive:
  at `0.60`, a 20-task stream sharing 12 tasks fails; sharing at most 11 passes.
- Normalize Huawei APT URLs to Tencent HTTPS before diagnosing package
  availability; Huawei returned repeated 503 responses while Tencent/Alibaba
  repairs contributed to verifier-confirmed builds. Retry `apt-get update` and
  install separately with a finite `Acquire::Retries` value (3--10), optionally
  using `--fix-missing`; never add an unbounded retry loop.
- On Ubuntu 24.04, use `libasound2t64` instead of the transitioned
  `libasound2`, and prefer `default-jdk-headless` when no GUI JDK is required.
  Never add Debian `deb-src` to Ubuntu; use an Ubuntu Noble `deb-src` entry.
- Python 3.12 is externally managed. Prefer an isolated venv. If the task
  intentionally installs into the system interpreter with `uv pip install
  --system`, add the required externally-managed override rather than changing
  task semantics.
- Do not relocate or symlink vLLM until `import vllm` succeeds. Do not assume
  npm exists or install Node twice; explicitly install npm only when the runtime
  contract needs it. Make git remote/tag cleanup idempotent with `|| true`.
- A registry OAuth/token-service 503 after a successful image build is transient
  infrastructure evidence. Preserve the successful build and retry the push;
  do not spend a semantic REFINE turn changing the Dockerfile for that error.
