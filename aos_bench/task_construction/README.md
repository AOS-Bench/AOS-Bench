# aos_bench.task_construction

Merges several **single Harbor tasks** (`task.toml + environment/Dockerfile + tests/test.sh + instruction.md`)
into one AOS-Bench **Stream** (`stream.toml + environment/Dockerfile + tasks/<id>/`),
and guarantees that:

- the resources of all source tasks are collected into the stream's **single `environment/Dockerfile`**
- each source task gets its own namespace under `/workspace/`, with automatic numbering on name collisions
- paths inside `instruction.md` are rewritten to the new namespace accordingly
- reward paths inside `tests/test.sh` are rewritten to `$AOS_REWARD_DIR/reward.txt`, the location the stream verifier expects

The output format follows [`docs/tasks/task_stream.md` v1.1](../../docs/tasks/task_stream.md).

## Directory layout

```
aos_bench/task_construction/
├── __init__.py
├── README.md                  # this document
├── manifests/                 # bundled manifests (which source tasks go into which stream)
├── assemble/
│   ├── __init__.py
│   ├── assemble.py            # main entry point
│   ├── dockerfile_merge.py    # merges FROM/ENV/RUN/COPY/WORKDIR/USER
│   ├── instruction_rewrite.py # path prefix substitution
│   └── reward_rewrite.py      # rewrites reward paths inside test.sh
├── scripts/
│   ├── __init__.py
│   ├── download_sources.py    # fetches the TB-2 / SkillsBench / WildClawBench source repositories
│   └── assemble_stream.py     # CLI: reads a manifest and produces a stream directory
└── tests/
```

## Shared /workspace namespace

Convention: **a stream contains only one domain (pool)** (for example, one stream for tb2-ml and
one stream for tb2-swe). All source tasks share the same container workspace:

```
/workspace/
```

Task-local paths in the source Dockerfiles are mapped directly onto this shared root:

- `/app/foo` (or the home root given by the source task's first `WORKDIR`) → `/workspace/foo`
- relative paths `foo` / `./foo` → the current rewritten `WORKDIR`, usually `/workspace/foo`
- paths already written as `/workspace/foo` stay at `/workspace/foo`

In other words, the assembler no longer adds a per-task `/workspace/<task_id>/` isolation layer by default.
If the `COPY`/`ADD` targets of several tasks map to the same final path, the first one in stream order is kept,
later conflicting targets automatically get a `__<task_id>` suffix, and the corresponding paths in the Dockerfile,
`instruction.md`, `tests/` and `solution/solve.sh` are rewritten to the new location.

### `shared_prefixes`: marking explicitly shared directories

`shared_prefixes` can still be declared at the top level of a manifest to mark which source paths are intentionally shared directories.
In unified `/workspace` mode, it mainly serves as an explicit hint for collision detection and path rules:

```toml
[stream]
shared_prefixes = ["/app/data", "/app/shared"]
```

Effect:

| Source path | Rewritten to |
|--|--|
| `/app/data/imagenet` of `task-1` | `/workspace/data/imagenet` |
| `/app/data/mnist` of `task-2`    | `/workspace/data/mnist`   |
| `/app/setup.sh` of `task-1`      | `/workspace/task-1/setup.sh` |
| `/app/setup.sh` of `task-2`      | `/workspace/task-2/setup.sh` |

Only subtrees matching `shared_prefixes` go into the shared directory; everything else is still isolated per task.

**Collision detection**: the COPY/ADD targets of all tasks are scanned. If two tasks both COPY to the same
final path (for example, both place `labels.csv` at `/workspace/data/labels.csv`), the later one automatically
gets a `__<task_id>` suffix, and all references in instruction.md / test.sh are rewritten accordingly.
During assembly, a `[collision]` log line lists each rewrite.

### Duplicate task_id

In principle a stream should not contain duplicate task_ids; if a clash does occur, `SlotAllocator` automatically appends
a `-2`, `-3` suffix and writes the effective task_id into stream.toml and the verifier output dir.

## Core principles

1. **Sources are immutable**: source task directories are read-only; all rewriting happens in the output.
2. **Instruction and Dockerfile stay in sync**: when a path changes, the path written in instruction.md must change too,
   and so must the path written in the verifier test.sh. A single "path mapping table" drives all three rewrites.
3. **Dependencies are unioned**: `apt-get install` / `pip install` lines from the source Dockerfiles are merged
   semantically and deduplicated, so that git is not installed six times; conflicting dependencies (Python version / NumPy version)
   currently fail fast.
4. **Reproducible**: the scripts are pure functions; the same manifest should produce a byte-identical stream
   (except for timestamps).

## Usage

```bash
# 1) download the source repositories once into a local cache directory (see --cache-dir)
python -m aos_bench.task_construction.scripts.download_sources

# 2) assemble a stream from a manifest (one manifest per domain)
python -m aos_bench.task_construction.scripts.assemble_stream \
    --manifest aos_bench/task_construction/manifests/tb2-swe.toml \
    --out streams/tb2-swe

python -m aos_bench.task_construction.scripts.assemble_stream \
    --manifest aos_bench/task_construction/manifests/tb2-ml-ds.toml \
    --out streams/tb2-ml-ds
```

Bundled manifests (under `aos_bench/task_construction/manifests/`):

| manifest | Source | # tasks |
|--|--|--|
| `tb2-swe.toml` | TB-2 software-engineering subset | 22 |
| `tb2-ml-ds.toml` | TB-2 machine-learning + data-science + model-training + data-processing | 17 |
| `tb2-sci.toml` | TB-2 scientific-computing + mathematics | 9 |
| `mini-tb2-3.toml` | minimal example, 3 tasks (fix-git / prove-plus-comm / openssl) | 3 |
