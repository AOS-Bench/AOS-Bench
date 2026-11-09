"""End-to-end tests for the task_construction assemble pipeline."""

from __future__ import annotations

import os
import shutil
import textwrap
from pathlib import Path

import pytest

from aos_bench.task_construction.assemble.assemble import (
    StreamSpec,
    TaskRef,
    assemble,
    load_manifest,
)
from aos_bench.task_construction.assemble.dockerfile_parse import parse_dockerfile
from aos_bench.task_construction.assemble.instruction_rewrite import (
    PathRewriteRule,
    build_rules,
    rewrite_text,
)
from aos_bench.task_construction.assemble.path_namespace import SlotAllocator
from aos_bench.task_construction.assemble.reward_rewrite import (
    rewrite_test_sh,
    rewrite_tests_helper_py,
    rewrite_wildclaw_judge_budget,
)


# ---------------------------------------------------------------------------
# Synthetic source tasks
# ---------------------------------------------------------------------------


def _write_task(
    root: Path,
    name: str,
    *,
    dockerfile: str,
    instruction: str,
    test_sh: str,
    extra_env_files: dict[str, str] | None = None,
    extra_test_files: dict[str, str] | None = None,
) -> Path:
    task = root / name
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "environment" / "Dockerfile").write_text(textwrap.dedent(dockerfile))
    (task / "instruction.md").write_text(instruction)
    test_path = task / "tests" / "test.sh"
    test_path.write_text(textwrap.dedent(test_sh))
    test_path.chmod(0o755)
    for n, c in (extra_env_files or {}).items():
        target = task / "environment" / n
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(c)
    for n, c in (extra_test_files or {}).items():
        target = task / "tests" / n
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(c)
    return task


# ---------------------------------------------------------------------------
# Unit-level tests
# ---------------------------------------------------------------------------


def test_path_rewrite_does_not_match_periods_in_prose() -> None:
    """A natural-language `.` followed by space must NOT trigger a path substitution."""
    text = "Made some changes. Please help me."
    rules = [PathRewriteRule(from_prefix="/app", to_prefix="/workspace/foo")]
    assert rewrite_text(text, rules) == text


def test_path_rewrite_only_at_path_boundary() -> None:
    text = "Use /app/foo and /apple is not affected."
    rules = [PathRewriteRule(from_prefix="/app", to_prefix="/workspace/foo")]
    assert rewrite_text(text, rules) == "Use /workspace/foo/foo and /apple is not affected."


def test_path_rewrite_handles_overlapping_rules_without_double_match() -> None:
    """Confirms that `/workspace` does not re-match the output of an earlier rewrite."""
    rules = [
        PathRewriteRule(from_prefix="/app", to_prefix="/workspace/fix-git"),
        PathRewriteRule(from_prefix="/workspace", to_prefix="/workspace/fix-git"),
    ]
    text = "ABOUT = '/app/resources/patch_files/about.md'"
    out = rewrite_text(text, rules)
    assert out == "ABOUT = '/workspace/fix-git/resources/patch_files/about.md'"


def test_build_rules_skips_relative_dot_token() -> None:
    """`COPY foo ./` mustn't add a `.` rule that hijacks every period in instruction text."""
    df_text = """\
        FROM ubuntu:24.04
        WORKDIR /app
        COPY setup.sh ./
        COPY resources /app/resources
        RUN bash /app/setup.sh
        WORKDIR /app/personal-site
    """
    df = parse_dockerfile(textwrap.dedent(df_text))
    allocator = SlotAllocator()
    slot = allocator.allocate(
        source_dir=Path("/tmp/sentinel"),
        pool="swe",
        desired_task_id="fix-git",
        source_workdir=df.first_workdir or "/app",
    )
    rules = build_rules(slot=slot, dockerfile=df)
    assert all(r.from_prefix.startswith("/") for r in rules)
    assert "." not in {r.from_prefix for r in rules}


def test_collision_renames_rewrite_relative_task_paths() -> None:
    df_text = """\
        FROM ubuntu:24.04
        WORKDIR /app
    """
    df = parse_dockerfile(textwrap.dedent(df_text))
    allocator = SlotAllocator()
    slot = allocator.allocate(
        source_dir=Path("/tmp/sentinel"),
        pool="science",
        desired_task_id="virtualhome-agent-planning",
        source_workdir=df.first_workdir or "/app",
    )
    rules = build_rules(
        slot=slot,
        dockerfile=df,
        collision_renames={
            "/app/skills": "/workspace/skills__virtualhome-agent-planning"
        },
    )

    text = 'sys.path.append("skills/pddl-skills/"); skill_dir="skills"'
    out = rewrite_text(text, rules)

    assert '"/workspace/skills__virtualhome-agent-planning/pddl-skills/"' in out
    assert 'skill_dir="/workspace/skills__virtualhome-agent-planning"' in out
    assert "pddl-/workspace" not in out


def test_build_rules_leaves_foreign_system_paths_alone() -> None:
    """A COPY into /bin/, /etc/, /usr/local/bin/ etc. must NOT get redirected
    into the per-task slot — those are shared system locations and the binary
    is meant to be on $PATH."""
    df_text = """\
        FROM ubuntu:24.04
        WORKDIR /app
        COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/
        COPY tool.sh /usr/local/bin/tool.sh
        COPY app.cfg /etc/myapp/app.cfg
        COPY entry.sh /app/entry.sh
        RUN uv run /app/entry.sh
    """
    df = parse_dockerfile(textwrap.dedent(df_text))
    allocator = SlotAllocator()
    slot = allocator.allocate(
        source_dir=Path("/tmp/sentinel"),
        pool="ml-ds",
        desired_task_id="reshard-c4-data",
        source_workdir=df.first_workdir or "/app",
    )
    rules = build_rules(slot=slot, dockerfile=df)
    from_prefixes = {r.from_prefix for r in rules}
    # System paths must NOT be rewritten.
    assert "/bin" not in from_prefixes
    assert "/usr/local/bin" not in from_prefixes
    assert "/etc/myapp" not in from_prefixes
    assert "/etc" not in from_prefixes
    # Sanity: paths under source_workdir still get a rule.
    assert "/app" in from_prefixes
    # Apply the rules and check that foreign paths survive intact.
    from aos_bench.task_construction.assemble.instruction_rewrite import rewrite_text
    assert rewrite_text("install at /bin/", rules) == "install at /bin/"
    assert rewrite_text("config in /etc/myapp/", rules) == "config in /etc/myapp/"
    assert rewrite_text("source at /app/entry.sh", rules) == "source at /workspace/entry.sh"


def test_reward_rewrite_replaces_known_paths_and_injects_shim_once() -> None:
    src = textwrap.dedent("""\
        #!/bin/bash
        set -e
        echo "running"
        pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py
        echo 1 > /logs/verifier/reward.txt
    """)
    allocator = SlotAllocator()
    slot = allocator.allocate(
        source_dir=Path("/tmp/sentinel"),
        pool="swe",
        desired_task_id="t1",
    )
    once = rewrite_test_sh(src, slot)
    twice = rewrite_test_sh(once, slot)
    assert once == twice  # idempotent
    assert '"$__AOS_REWARD_DIR"/reward.txt' in once
    assert '"$__AOS_REWARD_DIR"/ctrf.json' in once
    assert "aos-bench: stream verifier shim" in once
    # The shim uses single-brace shell expansion (no `{{}}`).
    assert "${BASH_SOURCE[0]:-$0}" in once
    assert "{{" not in once


def test_reward_rewrite_replaces_tests_cwd() -> None:
    allocator = SlotAllocator()
    slot = allocator.allocate(source_dir=Path("/tmp/sentinel"), pool="swe", desired_task_id="t1")
    rewritten = rewrite_test_sh("#!/bin/bash\ncd /tests\npytest test_outputs.py\n", slot)
    assert 'cd "$__AOS_TESTS_DIR"' in rewritten
    assert "cd /tests" not in rewritten


def test_reward_rewrite_puts_tests_dir_on_pythonpath() -> None:
    """`--import-mode=importlib` drops the test dir from sys.path.

    A grader importing a helper shipped beside it (e.g. seismic-phase-picking's
    ``from evaluate_picks import ...``) would fail collection and score 0, even
    though the helper is present, unless the shim exports PYTHONPATH.
    """
    allocator = SlotAllocator()
    slot = allocator.allocate(source_dir=Path("/tmp/sentinel"), pool="swe", desired_task_id="t1")
    rewritten = rewrite_test_sh("#!/bin/bash\npytest -rA /tests/test_outputs.py\n", slot)

    assert 'PYTHONPATH="$__AOS_TESTS_DIR${PYTHONPATH:+:$PYTHONPATH}"' in rewritten
    assert "export PYTHONPATH" in rewritten
    # The export must precede the pytest invocation it is meant to affect
    # (match the command itself, not the word "pytest" inside shim comments).
    assert rewritten.index("export PYTHONPATH") < rewritten.index(
        "pytest --import-mode=importlib"
    )
    # importlib mode is still required for dashed task_id directories.
    assert "--import-mode=importlib" in rewritten
    assert rewritten == rewrite_test_sh(rewritten, slot)


def test_reward_rewrite_upgrades_existing_shim_tests_cwd() -> None:
    """Old generated overlays must receive later body-only path rewrites."""
    allocator = SlotAllocator()
    slot = allocator.allocate(source_dir=Path("/tmp/sentinel"), pool="swe", desired_task_id="t1")
    old_overlay = rewrite_test_sh("#!/bin/bash\ncd /tests\npytest test_outputs.py\n", slot)
    old_overlay = old_overlay.replace('cd "$__AOS_TESTS_DIR"', "cd /tests")

    upgraded = rewrite_test_sh(old_overlay, slot)
    assert upgraded.count("aos-bench: stream verifier shim") == 1
    assert 'cd "$__AOS_TESTS_DIR"' in upgraded
    assert "\ncd /tests\n" not in upgraded
    assert upgraded == rewrite_test_sh(upgraded, slot)


def test_python_tests_helper_rewrites_tests_root_and_file_literals() -> None:
    source = """\
from pathlib import Path
import os
test_dir = Path("/tests")
main = Path("/tests/main.tex")
joined = os.path.join("/tests", "input.tex")
"""
    rewritten = rewrite_tests_helper_py(source)
    assert 'test_dir = Path(__aos_tests_dir__())' in rewritten
    assert 'main = Path(__aos_tests__("main.tex"))' in rewritten
    assert 'joined = os.path.join(__aos_tests_dir__(), "input.tex")' in rewritten
    assert rewritten == rewrite_tests_helper_py(rewritten)

    namespace: dict[str, object] = {}
    exec(rewritten, namespace)
    assert namespace["__aos_tests_dir__"]() == "/tests"
    old = os.environ.get("AOS_TESTS_DIR")
    try:
        os.environ["AOS_TESTS_DIR"] = "/tests/tb2-overfull-hbox"
        assert namespace["__aos_tests_dir__"]() == "/tests/tb2-overfull-hbox"
        assert namespace["__aos_tests__"]("main.tex") == "/tests/tb2-overfull-hbox/main.tex"
    finally:
        if old is None:
            os.environ.pop("AOS_TESTS_DIR", None)
        else:
            os.environ["AOS_TESTS_DIR"] = old


def test_python_tests_helper_upgrades_old_shim_for_bare_tests_root() -> None:
    old_overlay = """\
# === aos-bench: stream tests-dir shim ===
import os as _aos_os
def __aos_tests__(name):
    return _aos_os.environ.get('AOS_TESTS_DIR', '/tests') + '/' + name
# === end shim ===
from pathlib import Path
test_dir = Path("/tests")
specific = Path("/tests/main.tex")
"""
    rewritten = rewrite_tests_helper_py(old_overlay)
    assert rewritten.count("aos-bench: stream tests-dir shim") == 1
    assert "def __aos_tests_dir__():" in rewritten
    assert 'test_dir = Path(__aos_tests_dir__())' in rewritten
    assert 'specific = Path(__aos_tests__("main.tex"))' in rewritten
    assert rewritten == rewrite_tests_helper_py(rewritten)


def test_wildclaw_judge_budget_raises_only_truncation_prone_limits() -> None:
    allocator = SlotAllocator()
    slot = allocator.allocate(
        source_dir=Path("/tmp/wildclaw"),
        pool="wcb",
        desired_task_id="wildclawbench-visual-canary",
    )
    source = """\
def call(client, messages, threshold=0.75):
    default_budget = 128
    note = "leave max_tokens=256 in prompt text"
    # Leave max_tokens=512 in comments too.
    first = client.chat.completions.create(
        messages=messages,
        max_tokens = 256,
    )
    second = client.chat.completions.create(messages=messages, max_tokens=512)
    third = client.chat.completions.create(messages=messages, max_tokens=2048)
    return first, second, third, threshold
"""
    rewritten = rewrite_wildclaw_judge_budget(source, slot)

    assert "default_budget = 128" in rewritten
    assert '"leave max_tokens=256 in prompt text"' in rewritten
    assert "# Leave max_tokens=512 in comments too." in rewritten
    assert "max_tokens = 4096" in rewritten
    assert "max_tokens=4096" in rewritten
    assert "max_tokens=2048" in rewritten
    assert "threshold=0.75" in rewritten
    assert rewritten == rewrite_wildclaw_judge_budget(rewritten, slot)


def test_wildclaw_judge_budget_does_not_touch_non_wildclaw_tasks() -> None:
    allocator = SlotAllocator()
    slot = allocator.allocate(
        source_dir=Path("/tmp/not-wildclaw"),
        pool="swe",
        desired_task_id="ordinary-task",
    )
    source = "client.chat.completions.create(max_tokens=256)\n"
    assert rewrite_wildclaw_judge_budget(source, slot) == source


def test_wildclaw_judge_transport_accepts_task_id_and_routes_visual_model() -> None:
    source = """\
response = client.chat.completions.create(
    model=os.environ.get("JUDGE_MODEL", "openai/gpt-5.4"),
    max_tokens=1024,
)
"""
    rewritten = rewrite_wildclaw_judge_budget(
        source, "wildclawbench-visual-canary"
    )

    assert "max_tokens=4096" in rewritten
    assert (
        'model=os.environ.get("AOS_JUDGE_VISION_MODEL", '
        'os.environ.get("JUDGE_MODEL", "openai/gpt-5.4"))'
    ) in rewritten
    assert rewritten == rewrite_wildclaw_judge_budget(
        rewritten, "wildclawbench-visual-canary"
    )


def test_slot_allocator_dedupes_collisions() -> None:
    a = SlotAllocator()
    s1 = a.allocate(source_dir=Path("/x"), pool="p", desired_task_id="foo")
    s2 = a.allocate(source_dir=Path("/y"), pool="p", desired_task_id="foo")
    assert s1.task_id == "foo"
    assert s2.task_id == "foo-2"
    assert s1.workspace_dir == "/workspace"
    assert s2.workspace_dir == "/workspace"


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------


@pytest.fixture
def two_synthetic_tasks(tmp_path: Path) -> Path:
    sources = tmp_path / "sources"
    _write_task(
        sources,
        "alpha",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            RUN apt-get update && apt-get install -y git
            COPY data /app/data
            RUN ls /app/data > /app/listing.txt
            WORKDIR /app/inner
        """,
        instruction="Inspect /app/data and write the result to /app/listing.txt.",
        test_sh="""\
            #!/bin/bash
            set -e
            test -f /app/listing.txt
            echo 1 > /logs/verifier/reward.txt
        """,
        extra_env_files={"data/file1.txt": "hello\n"},
    )
    _write_task(
        sources,
        "beta",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            RUN apt-get update && apt-get install -y openssl
            RUN echo seed > /app/seed.txt
        """,
        instruction="Read /app/seed.txt then sign something.",
        test_sh="""\
            #!/bin/bash
            grep seed /app/seed.txt && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
        """,
    )
    return sources


def test_assemble_two_synthetic_tasks(tmp_path: Path, two_synthetic_tasks: Path) -> None:
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="synthetic-2",
        description="Two synthetic tasks",
        author="test",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=["ca-certificates", "curl"],
        tasks=[
            TaskRef(
                source=two_synthetic_tasks / "alpha",
                pool="data",
                task_id="alpha",
                arrive_wall_time=0,
                deadline_wall_time=8,
            ),
            TaskRef(
                source=two_synthetic_tasks / "beta",
                pool="sec",
                task_id="beta",
                arrive_wall_time=4,
                deadline_wall_time=14,
            ),
        ],
    )
    assemble(spec, out_dir)

    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    # Global apt block should include git + openssl + extras (deduplicated).
    assert "apt-get update && apt-get install -y" in df_text
    for pkg in ("ca-certificates", "curl", "git", "openssl"):
        assert pkg in df_text
    # Task-local paths are merged into the shared /workspace root, with no
    # /workspace/<task_id>/ or /workspace/<pool>/ prefix.
    assert "/workspace/data" in df_text
    assert "/workspace/seed.txt" in df_text
    assert "/workspace/alpha/" not in df_text
    assert "/workspace/beta/" not in df_text
    assert "/workspace/data/" not in df_text
    assert "/workspace/sec/" not in df_text
    # COPY targets are rewritten via _sources/<id>/.
    assert "COPY _sources/alpha/data /workspace/data" in df_text
    # Absolute paths inside RUN bodies got rewritten too.
    assert "ls /workspace/data > /workspace/listing.txt" in df_text

    # Per-task instructions point at the shared workspace.
    alpha_inst = (out_dir / "tasks" / "alpha" / "instruction.md").read_text()
    assert "/workspace/data" in alpha_inst
    assert "/workspace/listing.txt" in alpha_inst
    assert "/app/" not in alpha_inst

    beta_inst = (out_dir / "tasks" / "beta" / "instruction.md").read_text()
    assert "/workspace/seed.txt" in beta_inst
    assert "/app/" not in beta_inst

    # tests/test.sh got the shim + reward path rewrite.
    alpha_test = (out_dir / "tasks" / "alpha" / "tests" / "test.sh").read_text()
    assert "aos-bench: stream verifier shim" in alpha_test
    assert '"$__AOS_REWARD_DIR"/reward.txt' in alpha_test
    assert "/workspace/listing.txt" in alpha_test

    # Sources are staged.
    assert (out_dir / "environment" / "_sources" / "alpha" / "data" / "file1.txt").exists()

    # stream.toml has both arrivals.
    stream_toml = (out_dir / "stream.toml").read_text()
    assert 'task_id            = "alpha"' in stream_toml
    assert 'task_id            = "beta"' in stream_toml
    assert "arrive_wall_time   = 0" in stream_toml
    assert "arrive_wall_time   = 4" in stream_toml


def test_assemble_idempotent_with_force(tmp_path: Path, two_synthetic_tasks: Path) -> None:
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="syn",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        tasks=[
            TaskRef(
                source=two_synthetic_tasks / "alpha",
                pool="data",
                task_id="alpha",
                arrive_wall_time=0,
            ),
        ],
    )
    assemble(spec, out_dir)
    first = (out_dir / "environment" / "Dockerfile").read_text()
    with pytest.raises(FileExistsError):
        assemble(spec, out_dir, force=False)
    assemble(spec, out_dir, force=True)
    second = (out_dir / "environment" / "Dockerfile").read_text()
    assert first == second


# ---------------------------------------------------------------------------
# Shared prefixes — two tasks merge a shared dir under /workspace/data/.
# ---------------------------------------------------------------------------


@pytest.fixture
def two_data_sharing_tasks(tmp_path: Path) -> Path:
    sources = tmp_path / "sources"
    _write_task(
        sources,
        "imagenet-trainer",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY data/imagenet /app/data/imagenet
            COPY setup.sh /app/setup.sh
            RUN bash /app/setup.sh
        """,
        instruction="Train on /app/data/imagenet and use /app/setup.sh as the entry point.",
        test_sh="""\
            #!/bin/bash
            ls /app/data/imagenet
            echo 1 > /logs/verifier/reward.txt
        """,
        extra_env_files={
            "data/imagenet/labels.txt": "imagenet-labels\n",
            "setup.sh": "echo imagenet-setup\n",
        },
    )
    _write_task(
        sources,
        "mnist-trainer",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY data/mnist /app/data/mnist
            COPY setup.sh /app/setup.sh
            RUN bash /app/setup.sh
        """,
        instruction="Train on /app/data/mnist and use /app/setup.sh as the entry point.",
        test_sh="""\
            #!/bin/bash
            ls /app/data/mnist
            echo 1 > /logs/verifier/reward.txt
        """,
        extra_env_files={
            "data/mnist/labels.txt": "mnist-labels\n",
            "setup.sh": "echo mnist-setup\n",
        },
    )
    return sources


def test_shared_prefix_merges_data_dir_no_collision(
    tmp_path: Path, two_data_sharing_tasks: Path
) -> None:
    """User scenario: /app/data/imagenet + /app/data/mnist → /workspace/data/{imagenet,mnist}."""
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="data-share",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=["/app/data"],
        tasks=[
            TaskRef(
                source=two_data_sharing_tasks / "imagenet-trainer",
                pool="ml",
                task_id="imagenet-trainer",
                arrive_wall_time=0,
            ),
            TaskRef(
                source=two_data_sharing_tasks / "mnist-trainer",
                pool="ml",
                task_id="mnist-trainer",
                arrive_wall_time=4,
            ),
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()

    # Data dirs land under the shared /workspace/data/<name>, NOT under each
    # task's namespace.
    assert "COPY _sources/imagenet-trainer/data/imagenet /workspace/data/imagenet" in df_text
    assert "COPY _sources/mnist-trainer/data/mnist /workspace/data/mnist" in df_text
    # setup.sh is OUTSIDE shared_prefixes, but all task-local paths now share
    # /workspace. The first task keeps setup.sh; the later collision is renamed.
    assert "COPY _sources/imagenet-trainer/setup.sh /workspace/setup.sh" in df_text
    assert "COPY _sources/mnist-trainer/setup.sh /workspace/setup.sh__mnist-trainer" in df_text

    # Each task's instruction reflects its own data path under the shared root,
    # with no /workspace/<task_id>/data/ prefix.
    inst_imagenet = (
        out_dir / "tasks" / "imagenet-trainer" / "instruction.md"
    ).read_text()
    assert "/workspace/data/imagenet" in inst_imagenet
    assert "/workspace/imagenet-trainer/data" not in inst_imagenet
    assert "/workspace/setup.sh" in inst_imagenet

    inst_mnist = (
        out_dir / "tasks" / "mnist-trainer" / "instruction.md"
    ).read_text()
    assert "/workspace/data/mnist" in inst_mnist
    assert "/workspace/setup.sh__mnist-trainer" in inst_mnist

    # Test scripts also point at the shared dir.
    test_imagenet = (
        out_dir / "tasks" / "imagenet-trainer" / "tests" / "test.sh"
    ).read_text()
    assert "ls /workspace/data/imagenet" in test_imagenet
    test_mnist = (
        out_dir / "tasks" / "mnist-trainer" / "tests" / "test.sh"
    ).read_text()
    assert "ls /workspace/data/mnist" in test_mnist


def test_collision_inside_shared_prefix_auto_renames(tmp_path: Path) -> None:
    """Two tasks both COPY a file to /app/data/labels.csv → second gets __<task_id> suffix."""
    sources = tmp_path / "sources"
    for name in ("alpha", "beta"):
        _write_task(
            sources,
            name,
            dockerfile=f"""\
                FROM ubuntu:24.04
                WORKDIR /app
                COPY data/labels.csv /app/data/labels.csv
            """,
            instruction=f"Read /app/data/labels.csv ({name}).",
            test_sh="cat /app/data/labels.csv && echo 1 > /logs/verifier/reward.txt\n",
            extra_env_files={"data/labels.csv": f"{name}-labels\n"},
        )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="collide",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=["/app/data"],
        tasks=[
            TaskRef(source=sources / "alpha", pool="x", task_id="alpha", arrive_wall_time=0),
            TaskRef(source=sources / "beta", pool="x", task_id="beta", arrive_wall_time=4),
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    # First-arriving alpha keeps the canonical path; beta gets renamed.
    assert "COPY _sources/alpha/data/labels.csv /workspace/data/labels.csv" in df_text
    assert "COPY _sources/beta/data/labels.csv /workspace/data/labels.csv__beta" in df_text

    # Per-task instruction + test.sh are rewritten consistently.
    inst_alpha = (out_dir / "tasks" / "alpha" / "instruction.md").read_text()
    inst_beta = (out_dir / "tasks" / "beta" / "instruction.md").read_text()
    assert "/workspace/data/labels.csv" in inst_alpha and "__beta" not in inst_alpha
    assert "/workspace/data/labels.csv__beta" in inst_beta

    test_alpha = (out_dir / "tasks" / "alpha" / "tests" / "test.sh").read_text()
    test_beta = (out_dir / "tasks" / "beta" / "tests" / "test.sh").read_text()
    assert "cat /workspace/data/labels.csv" in test_alpha
    assert "cat /workspace/data/labels.csv__beta" in test_beta


def test_shared_prefix_with_no_collision_keeps_canonical_paths(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    _write_task(
        sources,
        "alpha",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY data/alpha /app/data/alpha
        """,
        instruction="See /app/data/alpha.",
        test_sh="echo 1 > /logs/verifier/reward.txt\n",
        extra_env_files={"data/alpha/x.txt": "x\n"},
    )
    _write_task(
        sources,
        "beta",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY data/beta /app/data/beta
        """,
        instruction="See /app/data/beta.",
        test_sh="echo 1 > /logs/verifier/reward.txt\n",
        extra_env_files={"data/beta/y.txt": "y\n"},
    )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="data-share-no-collision",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=["/app/data"],
        tasks=[
            TaskRef(source=sources / "alpha", pool="ml", task_id="alpha", arrive_wall_time=0),
            TaskRef(source=sources / "beta", pool="ml", task_id="beta", arrive_wall_time=4),
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    # Both shared dirs coexist under /workspace/data/ without rename.
    assert "/workspace/data/alpha" in df_text
    assert "/workspace/data/beta" in df_text
    assert "__alpha" not in df_text
    assert "__beta" not in df_text
    # Instructions point at the merged paths.
    assert "/workspace/data/alpha" in (out_dir / "tasks" / "alpha" / "instruction.md").read_text()
    assert "/workspace/data/beta" in (out_dir / "tasks" / "beta" / "instruction.md").read_text()


def test_collision_outside_shared_prefix_uses_namespace_isolation(
    tmp_path: Path,
) -> None:
    """Without shared_prefixes, same /app/setup.sh target collides in shared /workspace."""
    sources = tmp_path / "sources"
    for name in ("foo", "bar"):
        _write_task(
            sources,
            name,
            dockerfile=f"""\
                FROM ubuntu:24.04
                WORKDIR /app
                COPY setup.sh /app/setup.sh
            """,
            instruction=f"Run /app/setup.sh ({name}).",
            test_sh="echo 1 > /logs/verifier/reward.txt\n",
            extra_env_files={"setup.sh": f"echo {name}-setup\n"},
        )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="ns-iso",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=[],
        tasks=[
            TaskRef(source=sources / "foo", pool="x", task_id="foo", arrive_wall_time=0),
            TaskRef(source=sources / "bar", pool="x", task_id="bar", arrive_wall_time=4),
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    # First setup.sh keeps the canonical path; the second is renamed.
    assert "/workspace/setup.sh" in df_text
    assert "/workspace/setup.sh__bar" in df_text
    assert "__foo" not in df_text
    assert "__bar" in df_text


def test_skills_copy_participates_in_shared_workspace_collisions(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    for name in ("alpha", "beta"):
        task = _write_task(
            sources,
            name,
            dockerfile="""\
                FROM ubuntu:24.04
                WORKDIR /app
            """,
            instruction="Use helper scripts under /app/skills/ and import skills/demo.",
            test_sh="echo 1 > /logs/verifier/reward.txt\n",
        )
        skill_dir = task / "environment" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "helper.py").write_text(f"# {name}\n")

    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="skills-collision",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        tasks=[
            TaskRef(source=sources / "alpha", pool="x", task_id="alpha", arrive_wall_time=0),
            TaskRef(source=sources / "beta", pool="x", task_id="beta", arrive_wall_time=4),
        ],
    )
    assemble(spec, out_dir)

    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    assert "COPY _sources/alpha/skills/ /workspace/skills/" in df_text
    assert "COPY _sources/beta/skills/ /workspace/skills__beta/" in df_text

    alpha_inst = (out_dir / "tasks" / "alpha" / "instruction.md").read_text()
    beta_inst = (out_dir / "tasks" / "beta" / "instruction.md").read_text()
    assert "/workspace/skills" in alpha_inst
    assert "/workspace/skills__beta" in beta_inst
    assert "skills/demo" in alpha_inst
    assert "/workspace/skills__beta/demo" in beta_inst


# ---------------------------------------------------------------------------
# Larger streams (>3 tasks) — exercise scaling, multi-way collisions, and
# mixed shared/isolated namespacing.
# ---------------------------------------------------------------------------


def _build_dataset_task(
    root: Path, name: str, dataset_subdir: str, *, with_setup: bool = True
) -> Path:
    """One ML-style task that contributes data/<dataset_subdir>/ and has its own setup.sh."""
    extra_env: dict[str, str] = {f"data/{dataset_subdir}/labels.txt": f"{name}-labels\n"}
    if with_setup:
        extra_env["setup.sh"] = f"echo {name}-setup\n"
    dockerfile = textwrap.dedent(f"""\
        FROM ubuntu:24.04
        WORKDIR /app
        COPY data/{dataset_subdir} /app/data/{dataset_subdir}
    """)
    if with_setup:
        dockerfile += textwrap.dedent("""\
            COPY setup.sh /app/setup.sh
            RUN bash /app/setup.sh
        """)
    test_sh = (
        f"#!/bin/bash\nls /app/data/{dataset_subdir}\n"
        "echo 1 > /logs/verifier/reward.txt\n"
    )
    return _write_task(
        root,
        name,
        dockerfile=dockerfile,
        instruction=(
            f"Train on /app/data/{dataset_subdir}. "
            + ("Run /app/setup.sh first." if with_setup else "")
        ),
        test_sh=test_sh,
        extra_env_files=extra_env,
    )


def test_four_tasks_share_dataset_dir(tmp_path: Path) -> None:
    """Four ML tasks each contribute one subdir under /app/data — all coexist under /workspace/data/."""
    sources = tmp_path / "sources"
    for name, subdir in [
        ("imagenet", "imagenet"),
        ("mnist", "mnist"),
        ("cifar10", "cifar10"),
        ("squad", "squad"),
    ]:
        _build_dataset_task(sources, name, subdir, with_setup=False)

    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="ml-4way-share",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=40,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=["/app/data"],
        tasks=[
            TaskRef(source=sources / n, pool="ml", task_id=n, arrive_wall_time=i * 5)
            for i, n in enumerate(["imagenet", "mnist", "cifar10", "squad"])
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()

    # All four datasets coexist under the SAME /workspace/data/ root.
    for sub in ("imagenet", "mnist", "cifar10", "squad"):
        assert f"/workspace/data/{sub}" in df_text
        # No task-namespaced data path should appear.
        assert f"/workspace/{sub}/data/{sub}" not in df_text
    # No collision rename suffixes — all four contribute distinct subdirs.
    assert "__imagenet" not in df_text
    assert "__mnist" not in df_text
    assert "__cifar10" not in df_text
    assert "__squad" not in df_text

    # Each labels.txt physically present in its dataset subdir under _sources.
    for sub in ("imagenet", "mnist", "cifar10", "squad"):
        assert (
            out_dir / "environment" / "_sources" / sub / "data" / sub / "labels.txt"
        ).exists()

    # All four arrivals listed in stream.toml.
    stream_toml = (out_dir / "stream.toml").read_text()
    for sub in ("imagenet", "mnist", "cifar10", "squad"):
        assert f'task_id            = "{sub}"' in stream_toml


def test_three_way_collision_appends_distinct_suffixes(tmp_path: Path) -> None:
    """Three tasks all COPY labels.csv to /app/data/labels.csv: first wins, others get __<id>."""
    sources = tmp_path / "sources"
    for name in ("alpha", "beta", "gamma"):
        _write_task(
            sources,
            name,
            dockerfile="""\
                FROM ubuntu:24.04
                WORKDIR /app
                COPY data/labels.csv /app/data/labels.csv
            """,
            instruction=f"Read /app/data/labels.csv ({name}).",
            test_sh="cat /app/data/labels.csv && echo 1 > /logs/verifier/reward.txt\n",
            extra_env_files={"data/labels.csv": f"{name}-labels\n"},
        )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="3way-collide",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=["/app/data"],
        tasks=[
            TaskRef(source=sources / n, pool="x", task_id=n, arrive_wall_time=i * 4)
            for i, n in enumerate(["alpha", "beta", "gamma"])
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()

    assert "COPY _sources/alpha/data/labels.csv /workspace/data/labels.csv" in df_text
    assert (
        "COPY _sources/beta/data/labels.csv /workspace/data/labels.csv__beta" in df_text
    )
    assert (
        "COPY _sources/gamma/data/labels.csv /workspace/data/labels.csv__gamma" in df_text
    )

    # Per-task instruction + test.sh pick up the SAME suffix consistently.
    inst_alpha = (out_dir / "tasks" / "alpha" / "instruction.md").read_text()
    inst_beta = (out_dir / "tasks" / "beta" / "instruction.md").read_text()
    inst_gamma = (out_dir / "tasks" / "gamma" / "instruction.md").read_text()
    assert "/workspace/data/labels.csv" in inst_alpha and "__" not in inst_alpha
    assert "/workspace/data/labels.csv__beta" in inst_beta
    assert "/workspace/data/labels.csv__gamma" in inst_gamma

    # And test scripts.
    assert (
        "cat /workspace/data/labels.csv__beta"
        in (out_dir / "tasks" / "beta" / "tests" / "test.sh").read_text()
    )
    assert (
        "cat /workspace/data/labels.csv__gamma"
        in (out_dir / "tasks" / "gamma" / "tests" / "test.sh").read_text()
    )


def test_five_tasks_mixed_shared_and_isolated(tmp_path: Path) -> None:
    """5-task stream: 3 share /app/data, 2 work in their private namespace; one collision in private namespace must NOT appear."""
    sources = tmp_path / "sources"
    # Three tasks share /app/data
    for n, sub in (("ds-a", "alpha"), ("ds-b", "beta"), ("ds-c", "gamma")):
        _build_dataset_task(sources, n, sub, with_setup=True)
    # Two tasks operate in their own namespaces only
    for n in ("solo-1", "solo-2"):
        _write_task(
            sources,
            n,
            dockerfile="""\
                FROM ubuntu:24.04
                WORKDIR /app
                COPY tool.py /app/tool.py
                RUN python3 /app/tool.py
            """,
            instruction=f"Run /app/tool.py for {n}.",
            test_sh="test -f /app/tool.py && echo 1 > /logs/verifier/reward.txt\n",
            extra_env_files={"tool.py": f"# tool for {n}\n"},
        )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="five-mixed",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=40,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=["/app/data"],
        tasks=[
            TaskRef(source=sources / n, pool="ml", task_id=n, arrive_wall_time=i * 4)
            for i, n in enumerate(["ds-a", "ds-b", "ds-c", "solo-1", "solo-2"])
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()

    # 3-way shared dataset: each subdir lives under /workspace/data/ exactly once.
    for sub in ("alpha", "beta", "gamma"):
        assert f"/workspace/data/{sub}" in df_text
    assert "__ds-a" not in df_text
    assert "__ds-b" in df_text
    assert "__ds-c" in df_text

    # setup.sh is OUTSIDE shared_prefixes but lands in the shared workspace;
    # later tasks are renamed in stream order.
    assert "/workspace/setup.sh" in df_text
    assert "/workspace/setup.sh__ds-b" in df_text
    assert "/workspace/setup.sh__ds-c" in df_text

    # Solo tasks now share /workspace/tool.py, so the second one is renamed.
    assert "/workspace/tool.py" in df_text
    assert "/workspace/tool.py__solo-2" in df_text
    assert "RUN python3 /workspace/tool.py" in df_text
    assert "RUN python3 /workspace/tool.py__solo-2" in df_text

    # Stream toml lists all 5 arrivals.
    stream_toml = (out_dir / "stream.toml").read_text()
    for n in ("ds-a", "ds-b", "ds-c", "solo-1", "solo-2"):
        assert f'task_id            = "{n}"' in stream_toml


def test_six_tasks_dockerfile_apt_dedup_and_per_task_blocks(tmp_path: Path) -> None:
    """6 tasks each declare overlapping apt packages → merged into one block, with one per-task block each."""
    sources = tmp_path / "sources"
    pkg_sets = {
        "t1": ["git"],
        "t2": ["git", "curl"],
        "t3": ["openssl"],
        "t4": ["coq", "git"],
        "t5": ["python3", "python3-pip"],
        "t6": ["openssl", "coq"],
    }
    for name, pkgs in pkg_sets.items():
        pkg_line = " ".join(pkgs)
        _write_task(
            sources,
            name,
            dockerfile=f"""\
                FROM ubuntu:24.04
                WORKDIR /app
                RUN apt-get update && apt-get install -y {pkg_line}
                COPY work.txt /app/work.txt
            """,
            instruction=f"Open /app/work.txt for {name}.",
            test_sh="test -f /app/work.txt && echo 1 > /logs/verifier/reward.txt\n",
            extra_env_files={"work.txt": f"{name}-work\n"},
        )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="six-apt",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=50,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=[],
        tasks=[
            TaskRef(source=sources / n, pool="x", task_id=n, arrive_wall_time=i * 3)
            for i, n in enumerate(pkg_sets.keys())
        ],
    )
    assemble(spec, out_dir)
    df_text = (out_dir / "environment" / "Dockerfile").read_text()

    # All packages from all 6 tasks merged into ONE apt-get install line, deduped.
    for pkg in ("git", "curl", "openssl", "coq", "python3", "python3-pip"):
        assert f"    {pkg}" in df_text  # 4-space indent in our merged install block
    apt_lines = [
        l for l in df_text.splitlines()
        if l.strip().startswith("RUN apt-get update")
    ]
    # Exactly one global apt-get block (per-task ones are stripped to comments).
    assert len(apt_lines) == 1, f"expected 1 global apt-get RUN, got {len(apt_lines)}"
    # Each task gets its own COPY block; all target the shared workspace, so
    # later work.txt copies are renamed.
    for idx, n in enumerate(pkg_sets):
        assert f"=== {n} (pool=x" in df_text
        suffix = "" if idx == 0 else f"__{n}"
        assert f"COPY _sources/{n}/work.txt /workspace/work.txt{suffix}" in df_text


# ---------------------------------------------------------------------------
# Source-script rewriting: setup.sh + helper.py inside environment/ get the
# same path-rewrite treatment as the merged Dockerfile.
# ---------------------------------------------------------------------------


def test_environment_setup_sh_paths_get_rewritten(tmp_path: Path) -> None:
    """A source task with setup.sh that hardcodes /app/foo paths must end up
    with those paths rewritten under /workspace/foo in the staged
    _sources copy AND in any RUN that bash-execs it."""
    sources = tmp_path / "sources"
    _write_task(
        sources,
        "pwrec",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY setup.sh /app/setup.sh
            RUN bash /app/setup.sh && rm /app/setup.sh
        """,
        instruction="Recover the password from /app/varsea/disks/.",
        test_sh="""\
            #!/bin/bash
            test -f /app/varsea/done && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
        """,
        extra_env_files={
            "setup.sh": (
                "#!/bin/bash\n"
                "BASE_DIR=/app/varsea\n"
                "mkdir -p \"$BASE_DIR\"\n"
                "echo PASSWORD=secret > /app/launchcode.txt\n"
                "zip /app/secret.zip /app/launchcode.txt\n"
                "rm /app/launchcode.txt\n"
            ),
        },
    )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="pwrec-stream",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=[],
        tasks=[
            TaskRef(
                source=sources / "pwrec", pool="security", task_id="pwrec", arrive_wall_time=0
            ),
        ],
    )
    assemble(spec, out_dir)

    # 1. setup.sh inside _sources/ has been rewritten — no /app/ left, every
    # /app/foo reference is now /workspace/foo.
    staged_setup = (
        out_dir / "environment" / "_sources" / "pwrec" / "setup.sh"
    ).read_text()
    assert "/app/varsea" not in staged_setup
    assert "/app/launchcode.txt" not in staged_setup
    assert "/app/secret.zip" not in staged_setup
    assert "BASE_DIR=/workspace/varsea" in staged_setup
    assert "/workspace/launchcode.txt" in staged_setup
    assert "/workspace/secret.zip" in staged_setup

    # 2. The merged Dockerfile RUN that bash-execs setup.sh points at the
    # shared-workspace path.
    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    assert "RUN bash /workspace/setup.sh" in df_text
    assert "rm /workspace/setup.sh" in df_text
    assert "/app/setup.sh" not in df_text

    # 3. test.sh and instruction also follow.
    test_sh = (out_dir / "tasks" / "pwrec" / "tests" / "test.sh").read_text()
    assert "/workspace/varsea/done" in test_sh
    assert "/app/varsea" not in test_sh
    instruction = (out_dir / "tasks" / "pwrec" / "instruction.md").read_text()
    assert "/workspace/varsea/disks/" in instruction
    assert "/app/varsea" not in instruction


def test_environment_python_helper_paths_get_rewritten(tmp_path: Path) -> None:
    """A helper .py in environment/ that opens /app/data/file.csv must have its
    paths rewritten too."""
    sources = tmp_path / "sources"
    _write_task(
        sources,
        "datagen",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY randomize.py /app/randomize.py
            COPY data.csv /app/data.csv
            RUN python3 /app/randomize.py
        """,
        instruction="Inspect /app/data.csv.",
        test_sh="test -s /app/data.csv && echo 1 > /logs/verifier/reward.txt\n",
        extra_env_files={
            "randomize.py": (
                "#!/usr/bin/env python3\n"
                "import csv\n"
                "INPUT = '/app/data.csv'\n"
                "OUTPUT = '/app/data_random.csv'\n"
                "with open(INPUT) as f, open(OUTPUT, 'w') as g:\n"
                "    g.write(f.read())\n"
            ),
            "data.csv": "id,value\n1,2\n",
        },
    )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="datagen",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=[],
        tasks=[
            TaskRef(
                source=sources / "datagen", pool="ml", task_id="datagen", arrive_wall_time=0
            ),
        ],
    )
    assemble(spec, out_dir)

    staged_py = (
        out_dir / "environment" / "_sources" / "datagen" / "randomize.py"
    ).read_text()
    assert "INPUT = '/workspace/data.csv'" in staged_py
    assert "OUTPUT = '/workspace/data_random.csv'" in staged_py
    assert "/app/data.csv" not in staged_py

    # Source data.csv (not a script) is copied byte-identically.
    staged_csv = (
        out_dir / "environment" / "_sources" / "datagen" / "data.csv"
    ).read_text()
    assert staged_csv == "id,value\n1,2\n"


def test_environment_binary_files_are_not_rewritten(tmp_path: Path) -> None:
    """Binary or unknown-format files are copied byte-for-byte even if they
    happen to contain the substring `/app/`."""
    sources = tmp_path / "sources"
    binary_payload = b"\x00\x01\x02/app/legit-bytes\x00\x99\xff"
    task = _write_task(
        sources,
        "binblob",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY blob.bin /app/blob.bin
        """,
        instruction="Look at /app/blob.bin.",
        test_sh="test -f /app/blob.bin && echo 1 > /logs/verifier/reward.txt\n",
    )
    (task / "environment" / "blob.bin").write_bytes(binary_payload)

    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="bin",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=[],
        tasks=[
            TaskRef(
                source=sources / "binblob", pool="x", task_id="binblob", arrive_wall_time=0
            ),
        ],
    )
    assemble(spec, out_dir)

    staged = (out_dir / "environment" / "_sources" / "binblob" / "blob.bin").read_bytes()
    assert staged == binary_payload  # byte-identical, no rewriting attempted


def test_shebang_only_script_without_extension_is_rewritten(tmp_path: Path) -> None:
    """A script without an extension still gets rewritten if it has a shebang."""
    sources = tmp_path / "sources"
    task = _write_task(
        sources,
        "shebanger",
        dockerfile="""\
            FROM ubuntu:24.04
            WORKDIR /app
            COPY runner /app/runner
            RUN chmod +x /app/runner && /app/runner
        """,
        instruction="See /app/output.txt.",
        test_sh="test -f /app/output.txt && echo 1 > /logs/verifier/reward.txt\n",
    )
    (task / "environment" / "runner").write_text(
        "#!/bin/bash\necho hello > /app/output.txt\n"
    )
    out_dir = tmp_path / "stream"
    spec = StreamSpec(
        name="sb",
        description="",
        author="",
        cpus=1,
        memory_mb=1024,
        allow_internet=False,
        max_steps=20,
        base_image="ubuntu:24.04",
        accept_mixed_from=False,
        extra_apt_packages=[],
        shared_prefixes=[],
        tasks=[
            TaskRef(
                source=sources / "shebanger",
                pool="x",
                task_id="shebanger",
                arrive_wall_time=0,
            ),
        ],
    )
    assemble(spec, out_dir)
    staged = (
        out_dir / "environment" / "_sources" / "shebanger" / "runner"
    ).read_text()
    assert "echo hello > /workspace/output.txt" in staged
    assert "/app/" not in staged


# ---------------------------------------------------------------------------
# Optional: end-to-end test against the cached TB-2 mini-3 manifest.
# Skipped if the cache isn't populated.
# ---------------------------------------------------------------------------

_MINI_MANIFEST = (
    Path(__file__).resolve().parents[1] / "manifests" / "mini-tb2-3.toml"
)
_TB2_FIX_GIT = Path(str(Path.home() / "aos-bench-task-cache/terminal-bench-2/fix-git"))


@pytest.mark.skipif(
    not _TB2_FIX_GIT.exists(),
    reason="TB-2 source cache not populated; run download_sources.py first.",
)
def test_mini_tb2_3_assembly_smoke(tmp_path: Path) -> None:
    spec = load_manifest(_MINI_MANIFEST)
    out_dir = tmp_path / "mini"
    assemble(spec, out_dir)

    df_text = (out_dir / "environment" / "Dockerfile").read_text()
    for tid in ("fix-git", "prove-plus-comm", "openssl-selfsigned-cert"):
        assert f"WORKDIR /workspace/{tid}" in df_text
    assert "/workspace/swe/" not in df_text  # no pool prefix in paths

    fix_test_outputs = (
        out_dir / "tasks" / "fix-git" / "tests" / "test_outputs.py"
    ).read_text()
    assert "/workspace/fix-git/resources/patch_files/about.md" in fix_test_outputs
    assert "/app/" not in fix_test_outputs

    openssl_inst = (
        out_dir / "tasks" / "openssl-selfsigned-cert" / "instruction.md"
    ).read_text()
    assert "/workspace/openssl-selfsigned-cert/ssl/server.crt" in openssl_inst
    assert "/app/ssl/" not in openssl_inst


_TB2_SWE_MANIFEST = (
    Path(__file__).resolve().parents[1] / "manifests" / "tb2-swe.toml"
)


@pytest.mark.skipif(
    not _TB2_SWE_MANIFEST.exists() or not _TB2_FIX_GIT.exists(),
    reason="TB-2 source cache or generated manifest missing; run download_sources.py + generate_tb2_manifests.py first.",
)
def test_tb2_swe_full_manifest_assembly(tmp_path: Path) -> None:
    """Assemble the auto-generated 8-task tb2-swe stream end-to-end against real sources."""
    spec = load_manifest(_TB2_SWE_MANIFEST)
    assert len(spec.tasks) >= 6, "tb2-swe manifest unexpectedly small"

    out_dir = tmp_path / "tb2-swe"
    assemble(spec, out_dir)

    df_text = (out_dir / "environment" / "Dockerfile").read_text()

    # 1) Single global apt-get block, gathering union of dependencies.
    apt_runs = [
        line
        for line in df_text.splitlines()
        if line.strip().startswith("RUN apt-get update")
    ]
    # Per-task RUN apt-get ... blocks are not always promotable (e.g. multi-line
    # with DEBIAN_FRONTEND prefix), so we just require that the *first* one is
    # the merged global block.
    assert apt_runs, "expected at least one apt-get run"
    assert "git" in df_text and "curl" in df_text

    # 2) Each task gets its own block header and namespace.
    for ref in spec.tasks:
        tid = ref.task_id
        assert f"=== {tid} " in df_text, f"missing block header for {tid}"
        assert f"WORKDIR /workspace/{tid}" in df_text, f"missing namespace WORKDIR for {tid}"
        # Per-task instruction & tests files exist.
        assert (out_dir / "tasks" / tid / "instruction.md").exists()
        assert (out_dir / "tasks" / tid / "tests" / "test.sh").exists()
        # Source environment got staged under _sources/<id>/.
        assert (out_dir / "environment" / "_sources" / tid).is_dir()

    # 3) No /app/ leakage in any per-task instruction or test.sh — everything
    # has been rewritten to /workspace/<task_id>/...
    for ref in spec.tasks:
        tid = ref.task_id
        inst = (out_dir / "tasks" / tid / "instruction.md").read_text()
        # Allow `/app/foo` only when it's a foreign URL or unrelated word; the
        # normal case is no /app/ at all.
        assert "/app/" not in inst, f"task {tid} instruction still references /app/"
        test_sh = (out_dir / "tasks" / tid / "tests" / "test.sh").read_text()
        assert "/app/" not in test_sh, f"task {tid} test.sh still references /app/"
        # Reward path was rewritten to the per-task var.
        assert '"$__AOS_REWARD_DIR"/reward.txt' in test_sh

    # 4) stream.toml has 8 (or however many tasks the manifest had) arrivals
    # in the same order.
    stream_toml = (out_dir / "stream.toml").read_text()
    for ref in spec.tasks:
        assert f'task_id            = "{ref.task_id}"' in stream_toml
        assert f"arrive_wall_time   = {ref.arrive_wall_time}" in stream_toml

    # 5) No collision suffixes expected because TB-2 tasks don't share dirs.
    assert "__" not in [
        l for l in df_text.splitlines() if "COPY _sources" in l and "__" in l
    ] or all("__" not in l for l in df_text.splitlines() if "COPY _sources" in l)


class TestPoolV02CollisionStaging:
    """Under --pool-v02 nothing may be COPY'd into the agent's /workspace.

    Collision renames are computed against the flat workspace (that is what
    the agent sees post-reveal, and what instructions/tests are rewritten
    to), but they are applied as *absolute* destinations. If they are not
    rebased under the staging root they emit
    `COPY ... /workspace/skills__<task_id>`, putting the losing side of every
    collision in the agent's workspace before that task has arrived.
    """

    def test_stage_collision_target(self):
        from aos_bench.task_construction.assemble.dockerfile_merge import (
            _stage_collision_target,
        )

        f = _stage_collision_target
        assert f("/workspace/skills__t1", "t1") == "/aos_internal/sources/t1/skills__t1"
        assert (
            f("/workspace/.claude/skills__t1", "t1")
            == "/aos_internal/sources/t1/.claude/skills__t1"
        )
        assert f("/workspace", "t1") == "/aos_internal/sources/t1"
        # Idempotent: an already-staged target is left alone.
        assert (
            f("/aos_internal/sources/t1/skills__t1", "t1")
            == "/aos_internal/sources/t1/skills__t1"
        )
        # Paths outside the flat workspace are not ours to move, and
        # /workspaces must not be treated as a /workspace prefix match.
        assert f("/opt/thing", "t1") == "/opt/thing"
        assert f("/workspaces/other", "t1") == "/workspaces/other"

    def _task(self, root: Path, name: str) -> Path:
        d = root / name
        (d / "environment" / "skills" / "helper").mkdir(parents=True)
        (d / "environment" / "skills" / "helper" / "s.py").write_text("x\n")
        (d / "environment" / "Dockerfile").write_text(
            "FROM ubuntu:24.04\nWORKDIR /app\nCOPY skills/ ./\n"
        )
        (d / "tests").mkdir()
        (d / "tests" / "test.sh").write_text("#!/bin/bash\ntrue\n")
        (d / "instruction.md").write_text("Work in /app.\n")
        return d

    def test_no_copy_into_workspace_under_pool_v02(self, tmp_path):
        from aos_bench.task_construction.assemble.collision import (
            detect as detect_collisions,
        )
        from aos_bench.task_construction.assemble.dockerfile_merge import (
            merge_dockerfiles,
        )
        from aos_bench.task_construction.assemble.dockerfile_parse import (
            parse_dockerfile,
        )
        from aos_bench.task_construction.assemble.path_namespace import (
            SlotAllocator,
        )

        alloc = SlotAllocator()
        pairs = []
        for name in ("t1", "t2"):
            src = self._task(tmp_path, name)
            slot = alloc.allocate(source_dir=src, pool="p", source_workdir="/app")
            df = parse_dockerfile((src / "environment" / "Dockerfile").read_text())
            pairs.append((slot, df))

        renames, collisions = detect_collisions(pairs, [])
        # Both tasks COPY environment/skills -> the same flat destination.
        assert collisions, "expected the two tasks to collide on /workspace/skills"

        merged = merge_dockerfiles(
            slots_with_dockerfiles=pairs,
            collision_renames_per_slot=renames,
            pool_v02=True,
            mirror_profile="upstream",
        )
        copies = [
            ln
            for ln in merged.text.splitlines()
            if ln.startswith(("COPY ", "ADD "))
        ]
        assert copies
        leaked = [ln for ln in copies if " /workspace" in ln]
        assert not leaked, f"payload COPY'd into the agent workspace: {leaked}"
        assert any("/aos_internal/sources/t2/skills__t2" in ln for ln in copies)

    def test_pool_v02_agent_user_lands_on_uid_1000(self, tmp_path):
        """The agent user must be uid 1000, and the step must not fail silently.

        harbor hardcodes POOL_V02_AGENT_UID = 1000 and passes it to the pool
        sidecar as --agent-uid, which chowns revealed payloads to that numeric
        uid; any other uid leaves the agent unable to read its own workspace.

        ubuntu:24.04 ships a default `ubuntu` account on uid 1000, so a plain
        `useradd -u 1000` fails with "UID 1000 is not unique". With `|| true`
        that failure was swallowed and the build died several steps later on
        `install -d -o aos_agent`, pointing at the wrong line.
        """
        from aos_bench.task_construction.assemble.dockerfile_merge import (
            merge_dockerfiles,
        )
        from aos_bench.task_construction.assemble.dockerfile_parse import (
            parse_dockerfile,
        )
        from aos_bench.task_construction.assemble.path_namespace import (
            SlotAllocator,
        )

        alloc = SlotAllocator()
        src = self._task(tmp_path, "t1")
        slot = alloc.allocate(source_dir=src, pool="p", source_workdir="/app")
        df = parse_dockerfile((src / "environment" / "Dockerfile").read_text())

        merged = merge_dockerfiles(
            slots_with_dockerfiles=[(slot, df)],
            collision_renames_per_slot={},
            pool_v02=True,
            mirror_profile="upstream",
        )
        text = merged.text

        # The conflicting distro default is cleared before useradd runs.
        assert "userdel" in text
        assert text.index("userdel") < text.index("useradd")
        # Guarded so a purposefully-created uid-1000 account is never deleted.
        assert 'id -u ubuntu 2>/dev/null)" = 1000' in text
        # The uid is asserted, so a wrong uid fails here rather than later.
        assert 'id -u aos_agent)" = 1000' in text
        # No blanket `|| true` on the user-creation step.
        useradd_stanza = text[text.index("userdel") : text.index("apt-get")]
        assert "|| true" not in useradd_stanza, useradd_stanza
        # Ownership still established before the agent-visible dirs are made.
        assert text.index("useradd") < text.index("-o aos_agent -g aos_agent")

    def test_pool_v02_grants_root_traversal_without_read(self, tmp_path):
        """The agent must reach a toolchain under /root without reading /root.

        Task images routinely install tools into /root and put them on the
        global PATH (coursier drops scalac in /root/.local/share/coursier/bin).
        /root is 0700, so under pool v0.2 the non-root agent gets "Permission
        denied" on a binary its own PATH advertises.

        The verifier runs as root and is therefore unaffected, which is exactly
        what makes this dangerous: the oracle passes while the agent cannot
        reach the compiler, so a broken environment scores as a weak solution.

        o+x alone fixes traversal while `ls /root` and root-only files stay
        denied. o+r would expose the directory listing and is the thing this
        test exists to prevent; /aos_internal is sealed separately at 0700.
        """
        from aos_bench.task_construction.assemble.dockerfile_merge import (
            merge_dockerfiles,
        )
        from aos_bench.task_construction.assemble.dockerfile_parse import (
            parse_dockerfile,
        )
        from aos_bench.task_construction.assemble.path_namespace import (
            SlotAllocator,
        )

        alloc = SlotAllocator()
        src = self._task(tmp_path, "t1")
        slot = alloc.allocate(source_dir=src, pool="p", source_workdir="/app")
        df = parse_dockerfile((src / "environment" / "Dockerfile").read_text())

        merged = merge_dockerfiles(
            slots_with_dockerfiles=[(slot, df)],
            collision_renames_per_slot={},
            pool_v02=True,
            mirror_profile="upstream",
        )
        text = merged.text

        assert "RUN chmod o+x /root\n" in text
        # Traversal only — never readable, and never recursive.
        assert "o+r /root" not in text
        assert "chmod -R o+x /root" not in text
        assert "0755 /root" not in text
        # The isolation boundary is untouched by the traversal grant.
        assert "RUN chmod -R 0700 /aos_internal\n" in text
        assert text.index("0700 /aos_internal") < text.index("chmod o+x /root")

    def test_pool_v02_boilerplate_absent_without_the_flag(self, tmp_path):
        """None of the v0.2 isolation changes leak into a default build.

        The traversal grant and the agent user only make sense alongside the
        rest of v0.2; emitting either unconditionally would weaken /root and
        add an unused account to every ordinary stream image.
        """
        from aos_bench.task_construction.assemble.dockerfile_merge import (
            merge_dockerfiles,
        )
        from aos_bench.task_construction.assemble.dockerfile_parse import (
            parse_dockerfile,
        )
        from aos_bench.task_construction.assemble.path_namespace import (
            SlotAllocator,
        )

        alloc = SlotAllocator()
        src = self._task(tmp_path, "t1")
        slot = alloc.allocate(source_dir=src, pool="p", source_workdir="/app")
        df = parse_dockerfile((src / "environment" / "Dockerfile").read_text())

        merged = merge_dockerfiles(
            slots_with_dockerfiles=[(slot, df)],
            collision_renames_per_slot={},
            pool_v02=False,
            mirror_profile="upstream",
        )
        text = merged.text

        assert "chmod o+x /root" not in text
        assert "aos_agent" not in text
        assert "userdel" not in text
