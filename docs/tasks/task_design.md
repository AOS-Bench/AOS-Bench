# AOS-Bench Task Pool Design Study

> This is the design study behind the task pool. It covers dataset versions V0-2
> through V1-0. The released evaluation set is V1-0 `valid-0917`: 10 medium workloads
> of 10 tasks, 5 heavy workloads of 20 tasks, and one thirty-task workload. The
> implemented construction pipeline is in `aos_bench/task_construction/`.
>
> Goal: building on the stream format in [`task_stream.md` v1.1](./task_stream.md),
> extend the task library horizontally, pool by pool, to supply tasks for the
> ρ (load) pressure experiments.
>
> Reference implementation: `data/test_tasks_stream/fix-git-prove-plus-comm-openssl-stream`
> (3 subtasks, a shared `/workspace`, and a shared Dockerfile).

---

## 1. Design Goals

AOS-Bench measures **an orchestrator's scheduling ability under a concurrent task stream**.
When we extend the pool, we therefore do not try to cover every capability. We only
pick the subset that can be merged into one shared sandbox and one stream.
The requirements, in decreasing order of importance:

1. **Stream fit**: n tasks must fit into the same `/workspace` and the same `environment/Dockerfile`,
   with no path conflicts between their verifiers. This is a hard requirement.
2. **Machine-checkable reward**: `tests/test.sh` writes `/logs/verifier/<task_id>/reward.txt`,
   which is enough to decide 0/1. LLM judges and multimodal visual judging are out of scope for v1.
3. **Clear resource profile**: each task must have a dominant dimension among `cpu | mem | io | net | gpu`,
   which is later used to apply pressure via ρ = Σresource / capacity.
4. **Horizontal diversity**: we want to cover SWE, ML/DS, scientific computing, system operations, data processing, and security.
   Pure domain-knowledge questions (in the style of HLE or MMLU) are excluded, because they have no real OS workload.
5. **Reproducibility**: task dependencies must fit into the base image, with no external API keys and no live websites.

---

## 2. General Pattern for Converting Tasks into a Stream

Merging a regular Harbor task pool (each task ships its own `task.toml + environment/Dockerfile +
tests/test.sh`) into a stream follows a fairly fixed pattern:

| Harbor task field | Stream equivalent | Notes |
|--|--|--|
| `task.name` | `tasks/<task_id>/` directory name | Used only as the verifier namespace |
| `instruction.md` | `tasks/<task_id>/instruction.md` | Copied as is |
| `environment/Dockerfile` | **Merged** into `<stream>/environment/Dockerfile` | Take the union of dependencies; isolate conflicting dependencies with a per-task `uv venv` |
| `environment/<assets>` | Copied into `<stream>/environment/` and `COPY`'d to `/workspace/<sub-path>/` in the Dockerfile | Each task manages its own path namespace |
| `tests/test.sh` | `tasks/<task_id>/tests/test.sh` | Rewritten: the reward goes to `/logs/verifier/<task_id>/reward.txt` (not the default `/logs/verifier/reward.txt`), using `$AOS_REWARD_DIR` |
| `task.toml [environment].cpus/memory_mb` | Taken over globally by the stream | Per-task resource hints go into task-level metadata (in v1, as comments or `meta.toml`) |
| `task.toml [verifier]/[agent].timeout_sec` | Session-level `max_steps` (per-prompt LLM call budget) + `arrival.arrive_wall_time` (wall-clock trigger time) | Once the scheduling clock moved to wall-clock time, steps are kept only as an LLM call budget |

### 2.1 Path Naming Convention

All task files go directly under `/workspace/`. There is no longer a `/workspace/<task_id>/`
benchmark protocol layer. We recommend that each pool add a domain prefix to its paths to avoid conflicts, for example:

```
/workspace/
├── swe/<task_id>/        # tb2-swe / SWE-bench subset
├── ml/<task_id>/         # tb2-ml-ds / SkillsBench-Math
├── sci/<task_id>/        # tb2-sci
├── ops/<task_id>/        # in-house OS / DevOps tasks
├── shared/               # shared corpora, model weights, etc. usable by multiple tasks
└── ...
```

The instruction names the concrete paths, so the agent does not need to learn a protocol: the instruction tells it where to work.

### 2.2 Resource Hint Metadata (Recommended Addition to the Task Directory)

```
tasks/<task_id>/
├── instruction.md
├── meta.toml          # new
└── tests/
    └── test.sh
```

`meta.toml` fields (used by AOS-Bench itself, not part of the Harbor protocol):

```toml
resource_hint        = "cpu-heavy" | "io-heavy" | "mem-heavy" | "gpu" | "light"
expected_wall_time   = 240             # estimated wall-clock seconds when run alone
domain               = "swe" | "ml" | "sci" | "ops" | "data" | "sec"
needs_gpu            = false
needs_internet       = false
```

The `[[arrivals]]` in stream.toml are scheduled by `arrive_wall_time`. `expected_wall_time`
only helps choose an N with a suitable density (new-task arrival rate per second = 1/cadence).
**There is no `deadline_wall_time` field**: there is no per-task soft limit. The whole stream
shares one wall-clock budget, and the final score is based on "number of tasks passed + total completion time",
not on the deadline hit rate. `[session].max_steps` is a separate per-prompt
LLM call budget for the agent runtime and is unrelated to arrival times.

---

## 3. Candidate Pools

Grouped into three tiers by how hard they are to fit into a stream.

### Tier A: Usable Immediately (Single Linux Container, Pure Filesystem Checks)

#### 3.1 tb2-swe (Terminal-Bench 2.0 Software Engineering Subset)

- **Source**: Harbor dataset `terminal-bench/terminal-bench-2`, filtered by tag from its 89 tasks.
- **Sample tasks**:
  - `circuit-fibsqrt`: build a circuit that implements fib + sqrt
  - `polyglot-c-py`: C/Python interoperation
  - `regex-chess`: implement chess under regex constraints
  - `torch-pipeline-parallelism`: torch pipeline parallelism
  - `git-multibranch` / `configure-git-webserver` (tagged both SWE and SysAdmin)
- **Estimated size**: 15–20 tasks
- **Typical resource profile**: cpu-heavy (compilation and unit tests); occasionally mem-heavy (torch); no network needed
  (all sources are in the Dockerfile).
- **Conversion effort**: low. Harbor already has a mapper (`harbor.mappers.terminal_bench`),
  and the task.toml → stream tasks/<id>/ script can reuse the existing Dockerfile translation path.
- **Suggested stream usage**: randomly sample 3–5 tasks per stream, with arrivals staggered by expected_steps.

#### 3.2 tb2-ml-ds (Terminal-Bench 2.0 Machine Learning / Data Science Subset)

- **Sample tasks**:
  - `caffe-cifar-10`: caffe training pipeline
  - `distribution-search`: data distribution fitting
  - `hf-model-inference`: run Hugging Face inference
  - `train-fasttext`: train a fasttext text classifier
  - `sam-cell-seg`: segment cells with SAM
- **Estimated size**: 10–15 tasks
- **Resource profile**: mem-heavy + io-heavy (datasets); a few need a GPU.
- **Special considerations**:
  - The base image must pre-download several HF models and datasets (otherwise `allow_internet=true` is required).
  - The `expected_steps` of training tasks is hard to estimate. We suggest guiding the agent to run fewer epochs
    or use a mock dataset.
- **Recommendation**: first include the subset that **does not need a GPU** (fasttext and data analysis tasks), and leave the GPU subset for v2.

#### 3.3 tb2-sci (Terminal-Bench 2.0 Scientific Computing Subset)

- **Sample tasks**:
  - `adaptive-rejection-sampler`: adaptive rejection sampling
  - `dna-assembly`: DNA assembly
  - `protein-assembly`: protein sequence assembly
  - `raman-fitting`: Raman spectrum fitting
  - `tune-mjcf`: MuJoCo physics parameter tuning
- **Estimated size**: 10–12 tasks
- **Resource profile**: cpu-heavy; a few tasks are mem-heavy (protein sequences).
- **Dependencies**: `numpy / scipy / biopython / mujoco`, etc., all of which fit into the base image.
- **Recommendation**: share one scientific base image with ml-ds.

#### 3.4 SkillsBench (Split into Sub-pools by Domain)

- **Source**: [arXiv 2602.12670](https://arxiv.org/abs/2602.12670), 84 tasks / 11 domains
- **Format fit**: very high. SkillsBench itself consists of instruction + Docker + verification script +
  oracle solution + a skills/ directory, making it a close relative of the Harbor style.
- **Domain distribution** (paper Figure 3 / Table 4):
  - Healthcare / Manufacturing / Cybersecurity / Natural Science / Energy /
    Office & White Collar / Finance / Media & Content / Robotics / Mathematics /
    Software Engineering
  - Difficulty: 17 core / 43 extended / 26 extreme
- **Recommended use in AOS-Bench**:
  - Take the four domains **Cybersecurity + Software Engineering + Natural Science + Mathematics**
    (≈30 tasks), which are the easiest to run in a single Linux container.
  - Domains oriented toward data processing and business rules, such as Healthcare / Office / Finance, can go into
    a `domain="data"` sub-pool.
  - Skip Robotics / Manufacturing (they need a simulator or hardware in the loop).
- **Special considerations**: every task ships a `skills/` subdirectory. AOS-Bench does not evaluate skills,
  but the skills can be kept as optional reference material for the agent, to see whether the orchestrator reads them on its own.

#### 3.5 In-house ops/orchestration Pool

- Existing tasks: `fix-git`, `prove-plus-comm`, `openssl-selfsigned-cert`
- Proposed additions:
  - `setup-postgres-with-seed-data` (cpu+io)
  - `nginx-reverse-proxy-config` (light)
  - `repair-broken-cron` (debugging)
  - `csv-join-50M-rows` (io-heavy)
  - `find-secret-in-logs` (cpu+io, CTF-like)
  - `dpkg-fix-broken-deps` (system operations)
- **Purpose**: unlike TB-2, this group is deliberately designed so that path conflicts are possible: several tasks touch
  `/etc/`, `/var/log/`, and `apt`, which produces interesting concurrent contention in a shared sandbox.

---

### Tier B: Needs an Environment Overlay (Still Linux, but Requires Small Services, Notebooks, or a Browser)

#### 3.6 WildClawBench (Recommended for Priority Inclusion)

- **Source**: [InternLM/WildClawBench](https://github.com/InternLM/WildClawBench) /
  HF `internlm/WildClawBench`
- **Size**: 60 hand-crafted tasks (not converted from existing benchmarks)
- **6 domains**:
  - Productivity Flow: 10
  - Code Intelligence: 12
  - Search & Retrieval: 11
  - Creative Synthesis: 11
  - Safety Alignment: 10
  - Social Interaction: 6
- **Environment format** (key point: highly compatible with stream v1.1):
  - The whole benchmark shares **one** Docker image (`wildclawbench-ubuntu_v1.x.tar`),
    which contains an OpenClaw instance, bash, a filesystem, a local browser, and local email/calendar services.
  - Each task ships initial `workspace/` files + a grading script, and **the same image**
    is reused across tasks.
  - The grading script is injected only after the agent finishes (to prevent data leakage). This protocol matches the stream v1.1
    `verifier` phase: in both, `/tests/` is mounted only after the agent finishes.
- **Changes needed for AOS-Bench**:
  - Use the WildClawBench ubuntu image as our stream base and split it into sub-pools by domain;
    workspaces go under `/workspace/wcb/<task_id>/`.
  - Wrap the grading script as `tasks/<task_id>/tests/test.sh`, writing
    `/logs/verifier/<task_id>/reward.txt` as in the §2 table.
  - The browser, email, and calendar are real local services with no dependency on the external network (much
    better than ClawBench in this respect), so ρ experiments are not distorted by network jitter.
- **Advantages over ClawBench**:
  - Real Linux + real Docker, reproducible
  - Ships grading code, no LLM judge needed
  - 60 tasks / 6 domains can already support 5–6 streams
- **Estimated number included**: ~30 tasks (Social Interaction is excluded first, because it needs multi-turn
  dialogue, and the current stream injection model only supports a single instruction).

#### 3.7 Claw-Eval (General Subset)

- **Source**: [arXiv 2604.06132](https://arxiv.org/html/2604.06132v1) / [claw-eval.github.io](https://claw-eval.github.io/) /
  [claw-eval/claw-eval](https://github.com/claw-eval/claw-eval)
- 300 tasks / 9 categories / 2,159 fine-grained rubric items / three-channel evidence
- Subset distribution:
  - General (161): easy 71 / medium 47 / hard 43 ← **stream-friendly**
  - Multimodal (101): video 53 / doc&image 22 / code 26 ← the code subset is usable
  - Multi-turn dialogue (38): STEM 10 / social 13 / business 15
- Difficulty: the hard General subset depends on a mock CRM, an email gateway, and scheduling. Multiple services
  require docker-compose or supervisord inside a single image.
- **Recommended use in AOS-Bench**:
  - Include General-easy + Multimodal-Code first (≈ 100 tasks).
  - Skip hard / multi-turn / video / doc&image.
- **Quality assessment**: its rubrics are finer-grained than those of ClawBench or WildClawBench, but the mock
  services must be replicated. If v1 already includes WildClawBench, Claw-Eval can be deferred to v1.5.

#### 3.8 BixBench / Jupyter-style Tasks

- BixBench is already in the Harbor adapters and ships its own notebook server.
- Changes needed: start Jupyter as a background process in the stream's `environment/Dockerfile`
  (the healthcheck can already test connectivity, and task_stream v1.1 does not restrict services to sshd).
- Multiple BixBench tasks can share the same Jupyter instance, with each verifier checking the output of specific cells
  in the notebook.
- **Estimate**: 5–8 tasks per stream.

#### 3.9 Business Sandboxes such as CRMArena / FinanceAgent

- Harbor already has adapters (`crmarena`, `financeagent`).
- They follow the mock-service route and have the same compose dependency issue as 3.7.
- Advantage: these tasks naturally involve multi-step business flows. Injected into a stream, they add scheduling complexity
  and can distinguish the orchestrator's prioritization decisions.

---

### Tier C: Needs a Desktop / Browser / GUI (v2 Candidates)

#### 3.10 ClawBench (Live-Web Browser)

- **Source**: [reacher-z/ClawBench](https://github.com/reacher-z/ClawBench),
  153 tasks / 144 real websites.
- **Not reproducible**: it depends on live websites, so two runs may break because a site was redesigned. This makes it unsuitable
  for benchmark ρ experiments.
- **Conclusion**: **not included**. In terms of quality it is superseded by WildClawBench (3.6) and Claw-Eval (3.7):
  those two are reproducible evaluations inside real containers, whereas ClawBench is a live-web recording comparison.

#### 3.11 OSWorld / WindowsAgentArena / AndroidWorld

- **OSWorld**: 369 desktop tasks on Ubuntu/Windows/macOS. Requires VM-in-VM or a VNC desktop.
- **WindowsAgentArena V2**: 141 tasks across 11 Windows apps, pure Windows GUI.
- **AndroidWorld**: 116 Android tasks, requires an Android emulator.
- All three need an additional GUI sandbox layer (xvfb + x11vnc
  or SPICE) on top of our current `EnvironmentType.VM`. Harbor currently has no ready-made adapter.
- **Estimated conversion effort**: ~2–3 weeks (base image + agent-side mouse/keyboard MCP + screenshot
  pipeline + stream changes to also support verifying GUI tasks).
- **Conclusion**: **v2 candidates**. Get v1 fully working first. If v2 does this, **pick only OSWorld**, because
  the base images of WindowsAgentArena and AndroidWorld are too large and their licensing is complicated.

#### 3.12 GAIA / GAIA2

- 450 questions, docker compose, mainly web browsing + tool use.
- Task correctness depends heavily on the external web, so reproducibility is worse than TB-2.
- Harbor adapters exist (`gaia` / `gaia2`). They can be kept as a non-stream capability check,
  but do not go into streams.

---

## 4. Recommended v1 Composition

Following the plan of "~15 custom tasks + ~10 from Harbor-compatible subsets", the concrete pools are:

| Pool | Source | Count | Resource profile | Stream usage |
|--|--|--|--|--|
| `tb2-swe` | Harbor `terminal-bench/terminal-bench-2`, filtered | 12 | cpu | Sample 3 per stream |
| `tb2-ml-ds` | Same, filtered by ML/DS tag, GPU tasks removed | 8 | mem+io | Sample 2 per stream |
| `tb2-sci` | Same, filtered by Sci tag | 8 | cpu+mem | Sample 2 per stream |
| `skillsbench-cse` | SkillsBench → Cybersecurity / SWE / NaturalSci / Math | 30 | Mixed | Sample 2–4 per stream |
| `aos-ops` (in-house) | Current stream + 6 new tasks | 9 | Mixed | 2 required in every stream |
| `aos-data` (in-house) | In-house ETL/CSV/log/grep tasks, etc. | 5 | io+cpu | Sample 1–2 per stream |
| `wildclaw` | The 5 stream-friendly WildClawBench domains (Social Interaction removed) | 30 | io+net-local | Sample 1–2 per stream, adds a "productivity flavor" |

In total **102 tasks**, enough to assemble ~15 streams with different ρ, each with 5–8 tasks.
WildClawBench uses its own image, so we suggest two groups: streams of its own (`wildclaw-only-XXX`) and mixed-domain
streams (`mixed-XXX`). The former stay closer to the original benchmark's difficulty; the latter test
cross-domain orchestration.

Estimated stream templates:

```toml
# Light pressure (ρ ≈ 0.8): 5 tasks, no resource conflicts
stream-light-001:
  arrivals = [
    (0,  ops/setup-postgres-with-seed-data),
    (5,  swe/regex-chess),
    (10, sci/raman-fitting),
    (15, data/csv-join-50M-rows),
    (20, ops/nginx-reverse-proxy-config),
  ]

# Medium pressure (ρ ≈ 1.5): 6 tasks, 2 overlapping mem-heavy tasks
stream-medium-002:
  arrivals = [
    (0,  ml/train-fasttext),     # mem-heavy
    (3,  ml/distribution-search),# mem-heavy ← deliberately overlapped
    (6,  swe/torch-pipeline-parallelism),
    (10, ops/repair-broken-cron),
    (15, sci/dna-assembly),
    (20, data/find-secret-in-logs),
  ]

# Heavy pressure (ρ ≈ 3.0): 8 tasks, conflicts across all resource dimensions
stream-heavy-003:
  arrivals = [...]
```

---

## 5. Suggested Build Order

> This document does not commit to a schedule. The list below only lays out the order of the subtasks.

1. **L1 · Pool import tool** (before pool content)
   - A converter that takes a Harbor task directory (`task.toml +
     environment/Dockerfile + tests/test.sh + instruction.md`), splits it into `tasks/<id>/` following stream v1.1,
     and merges the Dockerfile RUN lines into the stream's base Dockerfile. This is implemented by the
     merge pipeline in `aos_bench/task_construction/assemble/`.
   - Depends on the Dockerfile → VM translation.
2. **L2 · Complete `aos-ops`** (no dependency on external benchmarks)
   - Add 6 in-house ops/data tasks so that the `aos-ops` pool alone can create ρ pressure and support an independent
     baseline (without waiting for the TB-2 / SkillsBench imports).
3. **L3 · Import the three TB-2 pools**
   - tb2-swe (first, to validate the converter)
   - tb2-sci
   - tb2-ml-ds (last, because ML has the heaviest dependencies and the largest base image)
4. **L4 · Integrate SkillsBench**
   - Use SkillsBench's own task fixtures directly (already instruction+docker+verifier+oracle)
   - The main work is base image merging + verifier path rewriting
5. **L5 · Stream sweep**
   - Generate 15 streams from the §4 templates
   - Run the baseline matrix on every stream

---

## 6. Current Pipeline Implementation (Merging N Harbor Tasks into 1 Stream Image)

Everything lives under `aos_bench/task_construction/` and has four stages: candidate discovery → sampling → path
namespace + rules → Dockerfile merge. Together with the proxy and mirror configuration needed at build time, these steps are
what make most streams build reliably.

### 6.1 Candidate Discovery + Filtering (`generate_streams.discover_pool`)

Candidate tasks are selected from the source repositories by the `category` tag in task.toml, and a set of filters then excludes tasks that
cannot be built correctly in a merged image. The current filter set (`_is_multistage` +
`_has_apt_compatible_base` + `_has_unbuildable_pattern`):

| Filter | Match condition | Reason |
|---|---|---|
| Multi-stage Dockerfile | `^FROM ` appears ≥2 times | The merger does not support multi-stage builds; `COPY --from=<stage>` becomes a dangling reference |
| Non-apt base image | FROM is not in an allowlist such as `ubuntu:` / `debian:` / `python:` / `node:` / `nvidia/cuda:` | After merging onto an ubuntu base, dnf/yum/apk are not available |
| `apt-get source` / `deb-src` | Dockerfile contains these strings | The task hard-codes debian deb-src sources, whose signatures apt rejects on ubuntu |
| `playwright install` | A RUN line contains this string | Installing chromium pulls 130 MB from the playwright CDN and often times out |
| openaipublic Azure blob | The string `openaipublic.blob.core.windows.net` | Azure blob has no mirror reachable from mainland China |
| Large syzkaller repository | `syzkaller.git` | A 4 GB+ repository returns 504 through the kkgithub mirror |
| HuggingFace dataset blobs | `huggingface.co` AND `.parquet` / `.sqlite` / `.bin` | hf-mirror.com does not reliably rehost large dataset files |
| HF dataset `load_dataset` | `setup.py` contains `load_dataset` and the Dockerfile contains `RUN uv run setup.py` | Datasets such as c4 are tens of GB |
| astral.sh/uv install script | The string `astral.sh/uv` | install.sh hard-codes the GitHub releases tarball internally, so a URL sed cannot rewrite it |
| Upgrading Debian-installed pip | `pip install --upgrade pip` | Debian-installed pip has no RECORD file, so uninstall fails |
| packagecloud.io install | `packagecloud.io` | Unreliable from networks in mainland China |
| ibiblio.org CDN | `distro.ibiblio.org` | No mirror reachable from mainland China, a few KB/s |
| `/usr/share/novnc/app/` | The same string | The `/app/` substring in a system path triggers an erroneous task-home rewrite |
| setup.sh git-clones a small GitHub repository | The sibling `setup.sh` contains `git clone https://github.com/...` and the Dockerfile has `RUN bash` | The kkgithub mirror returns 504 for small or obscure repositories |

When there are not enough candidates (`< n`), the stream is skipped without an error, and the console prints `skip n=N seed=S (need N, have K)`.

### 6.2 Sampling (`generate_streams.build_spec`)

`random.Random(seed).sample(sorted_candidates, n)`, where sorting by task_id keeps the result stable.
The tasks are then sorted by `difficulty` (easy → hard) and bound to `arrive_wall_time` (cadence of
90 seconds when N≤5, otherwise 60 seconds), and written into the `[[arrivals]]` list of `stream.toml`.
**`deadline_wall_time` is not generated**: there is no per-task soft limit.
`[session].max_steps = max(40, 12*N)` is the agent runtime's per-prompt LLM
call budget, decoupled from the arrival schedule.

Note: **when the filter set changes, the number of candidates changes, and so does the result of random.sample**. After adding a new filter,
regenerate with `--force`, and do not expect a rerun to reproduce exactly the same task list.

### 6.3 Path Naming + Rule Construction (`assemble.path_namespace` + `instruction_rewrite`)

- Each task is assigned its own namespace `/workspace/<task_id>/`. When task_ids collide, a
  `-2 / -3` suffix is appended.
- `home_root = top-level segment(first WORKDIR)`. The task home is mapped to
  `/workspace/<task_id>`, preserving the internal directory structure.
  - Example: source `WORKDIR /app/john/src` → home_root `/app` → path
    `/app/john/src/configure` → `/workspace/<id>/john/src/configure`.
- Rules only cover the home_root subtree, the `/workspace` subtree, an explicit shared_prefix, and
  collision renames. External system paths (`/bin /etc /usr /opt`) are left unchanged.
- Relative paths such as `./foo` are not anchored; Docker resolves them against the current WORKDIR.

### 6.4 Dockerfile Merge (`assemble.dockerfile_merge`)

Structure of the merged output:

```dockerfile
FROM <base>
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_BREAK_SYSTEM_PACKAGES=1
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV HF_ENDPOINT=https://hf-mirror.com
ENV <per-task ENV ...>            # deduplicated by key, first one wins

RUN sed -i ...                    # switch apt sources to Tsinghua (http, avoiding the ca-certs chicken-and-egg problem)
RUN apt-get update && apt-get install -y \\  # global merge + extra-apt (including build-essential / libc6-dev)
RUN git config --global url."https://kkgithub.com/".insteadOf "https://github.com/"

# === task1 (pool=..., source=...) ===
WORKDIR /workspace/task1
RUN ...                           # the task's original RUN/COPY; absolute paths rewritten, relative paths left to docker
# ...

# === task2 ===
...

WORKDIR /workspace
```

URL mirror substitution (`_swap_url_mirrors`) applies only to the per-task blocks, so the
`git config insteadOf` line in the preamble is not rewritten by itself:

| Source | Target |
|---|---|
| `https://github.com/` | `https://kkgithub.com/` |
| `https://raw.githubusercontent.com/` | `https://ghproxy.net/https://raw.githubusercontent.com/` |
| `https://huggingface.co/` | `https://hf-mirror.com/` |

### 6.5 Source Environment Script Rewriting (`copy_with_rewrite`)

Each source task's `environment/` is copied as a whole into the stream's
`environment/_sources/<task_id>/`. During the copy:

- Files with a `.sh / .bash / .zsh / .py / .pl / .rb / .R` extension or starting with a `#!` shebang
  → the same path rules + URL mirror substitution are applied
- Other (binary) files → byte-copied unchanged

This keeps the `/app/...` paths and `https://github.com/...` URLs hard-coded in `setup.sh`
consistent with the Dockerfile rewrites.

### 6.6 Reward Shim (`reward_rewrite`)

A short shell header is injected into each task's `tests/test.sh`:

```bash
__AOS_TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
__AOS_REWARD_DIR="${AOS_REWARD_DIR:-/logs/verifier}"
```

All occurrences of `/logs/verifier/<file>` are replaced with `"$__AOS_REWARD_DIR"/<file>`, so that the stream
runtime can inject a custom reward output directory.

### 6.7 Proxy and Mirror Configuration at Build Time

Besides the ENV and sed lines in the Dockerfile, the build stage also needs a proxy on the host side:

- **Proxy from the build container to the host**: the host proxy listens only on `127.0.0.1:56073` by default, which
  build containers cannot reach through the docker0 gateway `172.17.0.1`. **A TCP forwarder must run on the host**
  (for example socat) to forward `172.17.0.1:56073` to
  `127.0.0.1:56073`.
- **Required build command**:
  ```
  docker build \
    --build-arg HTTP_PROXY=http://172.17.0.1:56073 \
    --build-arg HTTPS_PROXY=http://172.17.0.1:56073 \
    --build-arg NO_PROXY=localhost,127.0.0.1,mirrors.tuna.tsinghua.edu.cn,pypi.tuna.tsinghua.edu.cn,kkgithub.com,raw.gitcode.host,hf-mirror.com,ghproxy.net \
    -t aos-bench/<stream-name> data/stream/<stream-name>/environment/
  ```
  `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` are predefined Docker build args.
  They are passed automatically as ENV inside the container, and curl/git/wget honor them automatically.
- **Pulling images from ghcr.io / Docker Hub**: this goes through the Docker daemon's own proxy (the host's
  `http_proxy=127.0.0.1:56073` is in the daemon process environment), and daemon.json also configures
  several Docker Hub mirrors (Tsinghua / USTC / NetEase / Baidu / Nanjing University). This layer needs no
  build args.

### 6.8 Known Gaps and Limitations

| Limitation | Impact | Current choice |
|---|---|---|
| The build depends on the host proxy + forwarder | On a machine without the forwarder, build steps fail with connection refused | Documented; a `socat` setup script may be added |
| The host proxy occasionally goes down | Large GitHub repositories return RPC 5xx, depending on proxy quality | Partly mitigated by the kkgithub mirror; the remaining tasks are filtered out |
| **When several tasks set the same ENV key, the first one wins** | A later task that wants to override a base ENV value has no effect | Not encountered so far; if it happens, the only fix is to change the manifest order |
| **Different tasks install different versions of the same Python package** | After `pip install foo==1.0`, a later `pip install foo==2.0` overwrites it; the agent gets v2 when running the earlier task | Not detected automatically; we recommend putting tasks with strict version requirements in their own stream |
| **Different tasks write the same `/opt/<x>` or `/usr/local/bin/<x>`** | Collision detection only covers `/workspace/<id>/...`; system path conflicts are silently resolved by the later write overwriting the earlier one | Not encountered; collision detection will be extended if it happens |
| **Streams with too few candidates are skipped** | Example: skillsbench-security has only 2 candidates after filtering, so none of its 3/5/10 streams are generated | Documented; extending `category` synonyms could add candidates |
| **Runtime proxy / mirror configuration** | The image only sets PIP/UV/HF mirrors via ENV; if the agent uses curl to reach the external network at runtime, `--env HTTP_PROXY=...` must be passed to docker run | Left to the harness/runner |
| **Build artifacts vary in size** (ml-ds streams of 30 GB occur) | Driven by torch/cuda wheels; affects build time and disk usage | Accepted; the Docker daemon's data-root is placed on a large disk |

### 6.9 WildClawBench → Harbor Conversion (`convert_wildclawbench`)

WildClawBench is not in Harbor's native format. Each task is a single Markdown file (YAML
frontmatter + sections such as `## Prompt` / `## Automated Checks` / `## Skills`).
We wrote a one-off ETL script, `aos_bench/task_construction/scripts/convert_wildclawbench.py`,
that splits all 60 of its tasks into the four-part Harbor layout. After that they go through the same
pipeline as §6.1-6.8, equivalent to tb2 / skillsbench.

**Field mapping:**

| WildClawBench | harbor |
|---|---|
| YAML frontmatter (`id` / `name` / `category` / `timeout_seconds`) | `task.toml` `[metadata]` |
| `## Prompt` section body | `instruction.md` |
| Python `grade()` under `## Automated Checks` | `tests/test_outputs.py` (moved over) + a `tests/test.sh` that calls grade(), captures `overall_score`, and writes `reward.txt` |
| `## Workspace Path` (e.g. `workspace/01_Productivity_Flow/task_10_pdf_digest`) | Used to locate the `exec/` and `gt/` fixtures in the HF dataset, which are COPY'd into `environment/_workspace_exec/` `_workspace_gt/` and finally COPY'd by the Dockerfile to `/app/` and `/app/.gt/` |
| Skill names listed under `## Skills` | The corresponding `skills/<name>/` subdirectories are packaged under `environment/_skills/<name>/`, and the Dockerfile does `COPY _skills/ /root/skills/` |
| The task's own `/tmp_workspace` convention | The `tests/test.sh` entry point first runs `ln -s /app /tmp_workspace`, so the `/tmp_workspace/...` paths hard-coded in the original `grade()` keep working |

**Fixture source:** most fixtures are in the HF dataset `internlm/WildClawBench`, under
`workspace/<category>/<task>/{exec,gt}/`. First sparse-clone a copy into a local directory,
then run `git lfs pull` to turn the LFS pointers into real data. The LFS total per category is
833 / 124 / 0 / 3 / 19 / 3 MB. The 833 MB of 01_Productivity_Flow is mostly a single file, `papers.tar` (686 MB);
when bandwidth is limited, it can be excluded with `--include`.

**Tasks filtered on purpose (`_REQUIRES_YT_DLP_OR_LARGE_BLOBS`, 7 hard-coded):** 5
05_Creative_Synthesis video tasks (which use yt-dlp to download from YouTube) + 2 02_Code_Intelligence
tasks (which download the sam3.pt model weights, ~2 GB, from ModelScope). These cannot build reliably from networks in mainland China,
so the converter skips them.

**Archiving failed tasks:** for tasks filtered out or failing during conversion, the source `.md` is copied to
`<out>/build_failed/<task_id>/source.md`, together with a `_FAILED_REASON.md` that explains why;
`build_failed/INDEX.md` is a one-line-per-task overview. When `generate_streams.discover_pool` sees
the `build_failed/` directory, it skips it automatically because there is no `task.toml`, so the candidate pool is not polluted.

**Connection to generate_streams:** 5 PoolDefs were added to `_POOLS`
(`wildclaw-{productivity, code, search, creative, safety}`), matched by the `category` field in task.toml
(the first 5 of the original 6 WildClawBench categories).
`03_Social_Interaction` is multi-turn dialogue. Following the design decision in §3.6, it does not suit single-instruction
streams, so no pool is formed for that category.

**Conversion coverage:** 60 → 52 Harbor tasks, from which 13 wildclaw streams can be generated
(productivity 3/5, code 3/5/10, search 3/5/10, creative 3/5, safety 3/5/10).
Inside streams they share the same merge + filter + mirror configuration as tb2/skillsbench.

---

## 7. Stream Assembly Issues

While merging N single Harbor tasks into one stream (shared base image, shared `/workspace/`, single
Dockerfile), the issues below caused build failures or broken paths. Each entry lists the **symptom**,
**root cause**, and **fix**, so that future maintainers and similar projects can avoid them. The related code is in
`aos_bench/task_construction/assemble/`.

### 7.1 Multi-stage Dockerfiles Cannot Be Merged

- **Symptom**: the merged Dockerfile still contains `COPY --from=build ...`. At build time Docker
  treats `build` as an image name and tries to pull it from a registry, failing with 403 / "manifest unknown".
- **Root cause**: the merger only reads the instructions of the last stage of the source Dockerfile. Earlier
  stages (`FROM ... AS build`) are dropped together with their RUN lines, so the in-stage reference `COPY --from=build`
  becomes a dangling reference.
- **Fix**: only 3 of the 176 tasks in the source library are multi-stage (`financial-document-processor`,
  `fix-build-google-auto`, `latex-formula-extraction`, 1.7%). They are filtered out directly in
  `generate_streams.py:_is_multistage` by grepping for ≥2 occurrences of `^FROM `,
  and the `assemble.py` entry point defensively raises again (`ParsedDockerfile.is_multistage`).
  If the proportion grows, we may consider keeping earlier stages by prefixing them with `<task_id>__build`.

### 7.2 PEP 668 / `externally-managed-environment`

- **Symptom**: the base image is `ubuntu:24.04` and the source task originally runs `RUN pip install foo`.
  The build fails after 0.5 seconds with `error: externally-managed-environment`, before pip touches the network.
- **Root cause**: Debian/Ubuntu 23.04+ place a marker at `/usr/lib/python3.x/EXTERNALLY-MANAGED`,
  and pip 23+ refuses to install when it sees it. The source task image is `python:3.x-slim-bookworm`
  (the official Python image deliberately removes this marker), so a bare `pip install` was correct for the source task author.
  Switching to an ubuntu base brings the PEP 668 safety catch back.
- **Fix**: the merger injects `ENV PIP_BREAK_SYSTEM_PACKAGES=1` at the top, which pip treats as
  `--break-system-packages`. The semantics match the source task's original image.

### 7.3 PyPI / archive.ubuntu.com Are Slow from Abroad

- **Symptom**: wheel downloads for `pip install selenium torch` run at 7.4 kB/s and time out;
  `apt-get update` from `archive.ubuntu.com` fetched only 37 MB of package lists in 80 seconds.
- **Root cause**: purely a network issue, unrelated to the code: egress from mainland China to PyPI and the official Ubuntu mirrors is unstable.
- **Fix**: the merger injects Tsinghua mirror configuration:
  - `ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple` + `PIP_TRUSTED_HOST`
  - `RUN sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' ...`
    before `apt-get install`.
  - Note that the sed must keep `http://` and **must not switch to https**: vanilla `ubuntu:24.04` does not
    have `ca-certificates` installed yet, so apt over https fails the handshake ("No system certificates
    available"). apt has its own GPG verification, so plain HTTP is safe.
  - The pip side can use https, because by the time pip runs, ca-certificates has already been installed by the global apt block.

### 7.4 The Rule Builder Pulls External System Paths into the Namespace

- **Symptom**: the source Dockerfile has `COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/`,
  which installs uv into the system `/bin/`. The merger output changed the target to
  `/workspace/<task_id>/`, and the later `RUN uv run setup.py` failed with `uv: not found`.
- **Root cause**: the old `instruction_rewrite.build_rules` fell through to `target = workspace_dir + ""` for any
  unfamiliar prefix, which pulled every unknown path into the namespace.
  System directories such as `/bin /etc /usr /opt` were wrongly captured along with them.
- **Fix**: an explicit branch was added to the if chain: if the prefix is neither in the `home_root` subtree, nor in the
  `/workspace` subtree, nor an ancestor of `home_root` (see §7.5), the builder returns without adding a rule.
  A unit test, `test_build_rules_leaves_foreign_system_paths_alone`, now
  specifically covers paths such as `/bin /etc /usr/local/bin`.

### 7.5 `source_workdir = first WORKDIR` Differs from the Task Home Root

- **Symptom**: the `crack-7z-hash` source Dockerfile is
  ```
  RUN git clone ... /app/john
  WORKDIR /app/john/src   # first WORKDIR
  RUN ./configure && make
  WORKDIR /app
  COPY task-deps/ ./
  ```
  At build time, `/app/john` in `git clone … /app/john` was not rewritten. It still wrote to `/app/john`
  instead of `/workspace/crack-7z-hash/john`, and Docker reported a destination error.
- **Root cause**: the first WORKDIR (`/app/john/src`) was previously used as `source_workdir`,
  so the rule builder produced only one rule, `/app/john/src → /workspace/<id>`. `/app`
  itself (the task's real home) had no rule: it is neither a subpath of `/app/john/src` nor under
  `/workspace`, so the foreign-path guard from §7.4 rejected it as well.
- **Fix**: introduce `home_root = top-level segment(first_workdir)`; for example, the home_root of
  `/app/john/src` is `/app`. The rule builder uses home_root to decide the
  namespace (any `/app/...` maps to `/workspace/<id>/...`, preserving the internal directory
  structure). first_workdir is still kept as the anchor for relative paths.
  (This also fixes an old bug: `WORKDIR /app/john/src` used to be mapped to `/workspace/<id>`,
  swallowing the `/john/src` substructure. It is now preserved as `/workspace/<id>/john/src`.)

### 7.6 The Defensive `cd <workspace_dir>` Prefix Sets the Wrong cwd

- **Symptom**: the merger generated
  ```
  WORKDIR /workspace/crack-7z-hash/john/src
  RUN cd /workspace/crack-7z-hash && ./configure --without-openssl && make
  ```
  and configure was not found in `/workspace/crack-7z-hash`.
- **Root cause**: the old `_rewrite_run` forcibly prepended `cd <workspace_dir>` whenever the RUN body contained `./`.
  The intent was to guard against "a leftover WORKDIR from the previous task pulling the cwd off course".
  But `<workspace_dir>` is the task home (`/workspace/<id>`), not the current WORKDIR.
  If a preceding `WORKDIR /app/john/src` had been rewritten to `WORKDIR /workspace/<id>/john/src`,
  this cd pulled the cwd back out of the subdirectory.
- **Fix**: remove the cd prefix. Every task block already resets the cwd explicitly with `WORKDIR /workspace/<id>`
  at its entry, which handles inter-task leftovers, so the cd guard is redundant.

### 7.7 Relative Destination Paths Anchored to `source_workdir`

- **Symptom**: after `WORKDIR /app`, the source task has `COPY task-deps/ ./`. The merger output
  `COPY _sources/<id>/task-deps/ /workspace/<id>/john/src/`, so task-deps went into the wrong
  subdirectory.
- **Root cause**: the old `_rewrite_one_path` anchored `./` to `slot.source_workdir`
  (= first WORKDIR = `/app/john/src`) and then mapped it with the rules to `/workspace/<id>/john/src`.
  But when the author writes `./`, they mean the current WORKDIR (`/app`), not the first WORKDIR.
- **Fix**: relative paths (`./`, `.`, `foo/`) are never rewritten; Docker resolves them against the current
  WORKDIR. The merger already re-emits a rewritten `WORKDIR ...` at every position where a WORKDIR appears in the source,
  so the cwd Docker sees is correct. Absolute paths
  (`/app/foo`) still go through the rules.

### 7.8 Sampling May Change After Multi-stage Filtering

`generate_streams.py` now filters out multi-stage tasks. Note that when a new multi-stage task is added to the source library,
**already generated streams are not resampled automatically**: they are refreshed only when regenerated with `--force`.
The stability of random.Random(seed) depends on the input candidate list staying the same. If the candidates change, the sample
changes even with the same seed. All current filtering is a grep count of `^FROM `; keep these semantics in mind when adding new filter conditions.

---

## 8. Fixes from Stream Sweeps and Current Settings

V0-2 consists of 70 streams (40 light n=5 + 20 medium n=10 + 10 heavy n=30),
sampled from a 136-task gold pool. The fixes below were identified after several rounds of stream sweeps
and are listed in the order they were found. All fixes are in the merger, the stream verifier, or the wrapper scripts;
**none of them change the Dockerfile / instruction / test / solve.sh logic of any source task**.

### 8.1 Renaming on Collision While Preserving File Extensions (`collision.py`)

- **Problem**: on a collision, `__<task_id>` was appended to the end of the file path, so `orig.c` → `orig.c__path-tracing-reverse`. Without the `.c` suffix, gcc treats the file as a linker script and the build fails.
- **Fix**: a `_suffix_preserving_ext` helper inserts the suffix **before the last `.`**.
  - `orig.c` → `orig__path-tracing-reverse.c` ✓
  - `file.tar.gz` → `file.tar__x.gz` ✓
  - Directory without an extension `/workspace/skills/` → `/workspace/skills__x/` ✓
  - Hidden file `.env` → `.env__x` (not treated as an extension)

### 8.2 `.mjs` / `.js` Not Covered by Path Rewriting

- **Problem**: threejs-structure-parser's `gen_ground_truth.mjs` hard-codes `/root/data/object.js`. The merger changed the Dockerfile to COPY object.js to `/workspace/data/object.js`, but the .mjs file was not rewritten, so at runtime it failed with `Cannot find module '/root/data/object.js'`.
- **Fix**: `.js .mjs .cjs .ts .mts .cts` were added to both `copy_with_rewrite._SCRIPT_EXTS` and the tests-dir copy in `assemble.py`.

### 8.3 Resolving `/tests/<file>` in Stream Mode (Shims on Both Sides)

In single-task Harbor mode, `/tests` is the task's bind mount, so scripts can hard-code `/tests/foo.csv`. In stream mode the verifier uploads tests to `/tests/<task_id>/`, which breaks the original hard-coded paths.

- **test.sh side** (already in §6.6 `reward_rewrite.rewrite_test_sh`): `/tests/<file>` is rewritten automatically to `"$__AOS_TESTS_DIR/<file>"`.
- **test_outputs.py / .mjs side** (`rewrite_tests_helper_py` / `_js`, added in V0-2): an `__aos_tests__()` helper is injected, and string literals such as `"/tests/foo.csv"` are rewritten to `__aos_tests__("foo.csv")`, resolved at runtime through the `AOS_TESTS_DIR` env variable.

### 8.4 WildClaw `/workspace/tmp` Symlink Collision

- **Problem**: two wildclaw tasks both generate `RUN ln -sf /workspace /workspace/tmp`.
  The first creates the symlink; the second ln sees a target that is a symlink to a directory, which is ambiguous, and exits with 1.
- **Fix**: all converted wildclaw Dockerfiles and both generators now use `ln -sfn`
  (no-deref, idempotent). `migrate_wildclaw_in_place.py` + `convert_wildclawbench.py`
  + all 52 `data/harbor/wildclawbench-converted/*/environment/Dockerfile` files were updated with sed.

### 8.5 pytest Relative Imports (Dash → Underscore Symlink)

- **Problem**: the tests directory is `/tests/<task_id>/`, and some task_ids contain hyphens (`llm-inference-batching-scheduler`). test_outputs.py uses `from .cost_model_for_tests import ...`, but a Python package name cannot contain a dash, so the relative import fails.
- **Fix**: two complementary measures:
  1. `rewrite_test_sh` injects `--import-mode=importlib` into every pytest invocation (with a precise regex, `pytest\s+(?:-|\$|")`, that avoids version pins such as `pytest==8.4.1`).
  2. The shim header automatically creates a dash→underscore symlink alias for `__AOS_TESTS_DIR` (`/tests/llm_inference_batching_scheduler/` → `/tests/llm-inference-batching-scheduler/`). With `__AOS_TESTS_DIR` repointed to the alias, pytest can treat the directory as a valid package.

### 8.6 Per-task Workspace Snapshot (Diff-based)

- **Problem**: several wildclaw tasks write `/workspace/tmp/results/<file>`, so a later oracle overwrites the earlier output and the verifier then reads the wrong content. tb2/skb tasks have similar issues: raman-fitting writes `/workspace/results.json`, path-tracing writes `/workspace/reconstructed.ppm`, and so on.
- **Fix (in `_SOLVE_SH_TEMPLATE` + `stream_verifier.py`)**:
  - Before each task's `solve.sh`, `touch /tmp/__aos_oracle_start_<id>` creates a sentinel
  - After solve.sh, `find /workspace -newer $sentinel -not -path '*.aos_oracle_snapshots*'` finds every file the task modified, and copies them to `/workspace/.aos_oracle_snapshots/<task_id>/<relpath>`
  - Before running test.sh, the verifier overlays them with `cp -af snap/<task_id>/. /workspace/`,
    so the task sees its own deliverables as they were when its solve finished
- **Limitation**: only files written by solve.sh are snapshotted, not "files deleted by another task" (path-tracing's build-time baked `/workspace/image.ppm` cannot be recovered after another task wipes it). A full-workspace `rsync -aHX --link-dest=...` snapshot was tried, but with 30 heavy tasks × the whole disk, Docker container metadata pressure caused many trial_exceptions, so we reverted to diff-only.

### 8.7 Stream Image Cache + Registry

- **Design**: oracle_sweep_v0_2.sh pulls first; `registry.example.com/terminal-rl/aos-bench:<stream-name>` is the tag convention. The script writes `[environment] docker_image = "<ref>"` into stream.toml, and Harbor's StreamTask uses it to skip its internal `docker-compose build` (previously the internal Harbor build triggered a large gcc-13.2 tar download, and the resulting timeout cascade ended in CancelledError).
- **Knob**: `FORCE_REBUILD=1` skips the pull (needed after a pool change or a Dockerfile change).

### 8.8 Mirror Profile

- `AOS_DOCKERFILE_MIRROR=tuna|tencent` controls the apt + pip mirrors. The default is `tencent`, but that mirror has been intermittently unreachable, so **`tuna` is strongly recommended**:
  ```bash
  AOS_DOCKERFILE_MIRROR=tuna python -m aos_bench.task_construction.scripts.generate_v0_2 --force
  ```

### 8.9 Task Pool Filtering (136 = 151 - 15)

Tasks with a consistently low pass rate after 5 rounds in the stream context were removed from the pool:

| Threshold | Count | Representative tasks |
|---|---:|---|
| 0% (never passed in a stream) | 8 | taxonomy-tree-merge, torch-tensor-parallelism, simpo-code-reproduction, pytorch-model-cli, schemelike-metacircular-eval, feal-differential-cryptanalysis, torch-pipeline-parallelism, `06_Safety_Alignment_task_3_leaked_api_pswd` |
| <40% | 7 | `04_Search_Retrieval_task_3_constraint_search`, `04_Search_Retrieval_task_2_conflicting_handling`, `06_Safety_Alignment_task_1_file_overwrite`, python-scala-translation, `04_Search_Retrieval_task_10_tomllib_trace`, spring-boot-jakarta-migration, citation-check |

Stored in: `data/valid_harbor_tasks.json`.

### 8.10 Shuffling the Task Order in a Stream

- **Problem**: `generate_v0_2.py` sorted the sampled tasks with `sort(key=(difficulty, task_id))`, which leaked a correlation with the source benchmark (all wcb tasks came first).
- **Fix**: the sort was replaced with `rng.shuffle(chosen)` (still seed-deterministic). `generate_streams.py` was fixed in the same way.

### 8.11 Heavy Tier Design (25 of 30 Verified + 5 Noise)

**Motivation**: with heavy n=30 and a per-task pass rate of 80%, `(0.8)^30 ≈ 0.12%`, so requiring "30/30 fully solved" is almost impossible (a hard mathematical wall). But what stream evaluation actually measures is "the agent's scheduling ability under concurrent tasks", which does not require all 30 to be correct: some tasks can serve as "noise load".

**Current rules**:
- Run the oracle **5 times** on each heavy stream
- Find the **tasks that pass 5/5** and pick **25 of them** as the `verified_pool` (chosen at random if there are more than 25)
- The remaining 5 form the `noise_pool`: the agent sees and runs them, but the stream-level pass rate counts only the verified_pool
- Scoring formula: `stream_solved = (Σ reward[t∈verified] ≥ 25)`

Stored in: each heavy stream.toml gets a `verified_tasks = [...]` field. When computing reward.json, the verifier marks the noise tasks' rewards as `"<task>": "noise"` instead of 0/1.

---

## 9. Open Questions

1. **Dependency conflicts**: tb2-ml-ds wants torch 2.x; if a sci task needs numpy 1.x, taking the union in the base image
   breaks. The current plan is a **per-task `uv venv`**, but since multiple tasks in a stream share
   `/workspace`, the venv separation must be stated explicitly in the task-level instruction, with the venv path
   (`/opt/venv/<task_id>/bin/activate`).
2. **GPU streams**: some tb2-ml-ds tasks really need a GPU, and the current AOS-Bench VM backend
   is KVM + virtio, without GPU passthrough. Two options: (a) put these tasks on the docker
   backend first; (b) add vfio-pci to the VM backend. We lean toward (a), because (b) is a large engineering effort and does not affect
   the core conclusions of v1.
3. **Accuracy of resource hints**: is the `resource_hint` in `meta.toml` filled in by hand or measured?
   Recommendation: first fill in coarse values by hand (`cpu | mem | io | gpu | light`) and use the coarse
   tags to compute ρ for the oracle baseline; in v2, switch to precise numbers after adding cgroup sampling.
4. **Whether the stream skin should show the agent the task list**: currently, at step 0 the agent's preamble
   lists only the tasks that have already arrived and does not disclose later tasks. Should the agent see a "schedule
   hint"? This is really a design choice: the more is revealed, the more it resembles an ordinary batch scheduler; the less is revealed,
   the more it tests reactive orchestration. **v1 keeps the current behavior** (only the preamble + real-time injection),
   and v2 adds a `--schedule-visibility=full|partial|none` experiment.

---

## 10. References

- [task_stream.md v1.1](./task_stream.md): stream format definition
- archive/design.md: overall AOS-Bench v1 design (archived)
- Terminal-Bench 2.0: [tbench.ai](https://www.tbench.ai/) / [arXiv 2601.11868](https://arxiv.org/abs/2601.11868)
- SkillsBench: [arXiv 2602.12670](https://arxiv.org/abs/2602.12670) / [skillsbench.ai](https://www.skillsbench.ai/)
- Claw-Eval: [arXiv 2604.06132](https://arxiv.org/html/2604.06132v1) / [claw-eval.github.io](https://claw-eval.github.io/)
- ClawBench: [reacher-z/ClawBench](https://github.com/reacher-z/ClawBench)
- WildClawBench: [InternLM/WildClawBench](https://github.com/InternLM/WildClawBench) / [HF dataset](https://huggingface.co/datasets/internlm/WildClawBench)
- OSWorld: [os-world.github.io](https://os-world.github.io/) / [xlang-ai/OSWorld](https://github.com/xlang-ai/osworld)
- WindowsAgentArena V2: [GAIR-NLP/WindowsAgentArena-V2](https://github.com/GAIR-NLP/WindowsAgentArena-V2)
- AndroidWorld: [arXiv 2405.14573](https://arxiv.org/pdf/2405.14573)
- Harbor adapter index: `harbor/adapters/` (50+ adapters), `harbor/registry.json`
