# Merged-stream verifier loop

This package runs a persistent refinement state machine:

```text
buildx build
  -> Harbor oracle + per-task reward.json
  -> sanitized verifier feedback
  -> codex exec / codex exec resume
  -> validate Dockerfile or task replacement
  -> repeat until every reward is 1
```

Codex sees a pruned copy of `environment/`, `stream_plan.json`, and the current
tasks' controlled instruction/test/solution overlay. The driver rejects edits
outside `environment/Dockerfile`, `stream_plan.json`, and the permitted task
overlay, audits Codex shell commands for workspace escapes, and rejects
Dockerfiles that COPY test or solution material into the runtime image.

Before every Codex turn, the driver refreshes a read-only `logs/` directory in
the controlled workspace. It contains redacted copies of the complete build and
Harbor command logs. Because task verifier stdout and the oracle-agent
transcript normally live below `jobs/`, the driver also materializes the latest
`reward.json`, `agent/oracle.txt`, and per-task `verifier/*/stdout.txt` below
`logs/attempt-XX/oracle-artifacts/`. These diagnostics are context only: they
are excluded from the edit hash and are not copied back from the container.

Codex runs in a dedicated long-lived Docker container because this host cannot
start Codex's `bwrap` sandbox. Bind mounts are not used: the driver copies the
independently staged and pruned workspace into the container with `docker cp`,
then copies back only allowlisted editable files. Diagnostic logs are reset to
root-owned read-only permissions after staging. Codex's session home is copied
back separately so `codex exec resume` survives driver/container restarts.
Command events and copied edits are validated before they are applied to the
stream. A turn that changes no allowed file is rejected as a no-op.

Task replacement is allowed. It must preserve the original number of tasks and
the exact L2/L3 distribution, use configured candidate-pool IDs, and contain no
duplicates. Replacement regenerates the merged stream via `assemble()` before
the next build.

## Start

```bash
source <SECRETS_DIR>/gateway_api.sh
export GLM_API_KEY="${GLM_API_KEY:-$OPENAI_API_KEY}"

python -m aos_bench.task_construction.assemble.refine_merge_stream \
  --stream-dir data/stream/V1-0/built/light/light-10-03 \
  --run-dir <RESULT_ROOT>/refine-loop/light-10-03 \
  --whitelist /path/to/allowed_task_ids.txt \
  --max-rounds 8
```

The default refine image is `tb3-agent:codex-0.144.1`; override it with
`--codex-image`. Containers are submitted with `--job-queue` (defaulting to
`HARBOR_JOB_QUEUE` or this host's standard queue label).

For a long run, invoke the same command under the host's normal `setsid/nohup`
supervisor. All mutable state lives in `--run-dir`.

## Resume

```bash
python -m aos_bench.task_construction.assemble.refine_merge_stream \
  --stream-dir data/stream/V1-0/built/light/light-10-03 \
  --run-dir <RESULT_ROOT>/refine-loop/light-10-03 \
  --whitelist /path/to/allowed_task_ids.txt \
  --resume
```

Build or oracle interruption is retried. A completed Codex turn pending apply
is applied transactionally; an incomplete turn reuses its saved verifier
prompt and recovered session.

Use `--dry-run` to create and inspect the sanitized workspace without invoking
Docker, Harbor, or Codex. Use `--verify-only` for one build/oracle pass without
refinement.
