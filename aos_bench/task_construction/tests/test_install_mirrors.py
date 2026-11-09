import json
import re
from pathlib import Path

import pytest

from aos_bench.task_construction.assemble.install_mirrors import (
    DOMESTIC_MIRROR_MARKER,
    TENCENT_MAVEN,
    normalize_domestic_install_sources,
)
from aos_bench.task_construction.scripts.apply_domestic_install_mirrors import apply


def test_domestic_install_source_rewrite_is_comprehensive_and_idempotent() -> None:
    original = """FROM ubuntu:24.04
RUN echo 'deb http://ports.ubuntu.com/ubuntu-ports noble main'
RUN echo 'deb http://deb.debian.org/debian stable main'
RUN pip install --index-url https://pypi.org/simple numpy
RUN npm config set registry https://registry.npmjs.org
RUN curl https://services.gradle.org/distributions/gradle.zip -o /tmp/g.zip
RUN curl https://mirrors.tuna.tsinghua.edu.cn/nodejs-release/v22/node.tar.xz -o /tmp/node.tar.xz
RUN go env -w GOPROXY=https://proxy.golang.org
RUN curl https://static.rust-lang.org/rustup.sh | sh
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu torch
"""
    rewritten = normalize_domestic_install_sources(original)

    assert DOMESTIC_MIRROR_MARKER in rewritten
    assert "mirrors.cloud.tencent.com/ubuntu-ports" in rewritten
    assert "mirrors.cloud.tencent.com/debian" in rewritten
    assert "mirrors.cloud.tencent.com/pypi/simple" in rewritten
    # No pypi EXTRA index by default: pip and uv query every configured index,
    # so one that the runtime cannot route to stalls the build and, worse,
    # fails graders that resolve a package at verify time. aliyun is exactly
    # that from some sandboxes.
    assert "mirrors.aliyun.com/pypi/simple" not in rewritten
    assert "PIP_EXTRA_INDEX_URL" not in rewritten
    assert "UV_EXTRA_INDEX_URL" not in rewritten
    assert 'PIP_TRUSTED_HOST="mirrors.cloud.tencent.com"' in rewritten
    assert "repo.huaweicloud.com" not in rewritten
    assert "retries=10" in rewritten
    assert "timeout=120" in rewritten
    assert "registry.npmmirror.com" in rewritten
    assert "mirrors.cloud.tencent.com/gradle" in rewritten
    assert "mirrors.cloud.tencent.com/nodejs-release/v22/node.tar.xz" in rewritten
    assert "repo.huaweicloud.com/nodejs-release" not in rewritten
    assert "goproxy.cn" in rewritten
    assert "rsproxy.cn" in rewritten
    assert "mirrors.aliyun.com/pytorch-wheels" in rewritten
    assert normalize_domestic_install_sources(rewritten) == rewritten


def test_extra_pypi_index_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extra index is off unless a host explicitly asks for one.

    Regression guard for the false-zero class: shipping
    UV_EXTRA_INDEX_URL=aliyun in the image made every grader that resolved a
    package at verify time fail on an unroutable host and then write a
    well-formed reward of 0.0.
    """
    original = "FROM ubuntu:24.04\n"

    monkeypatch.delenv("AOS_PYPI_EXTRA_INDEX_URL", raising=False)
    assert "EXTRA_INDEX_URL" not in normalize_domestic_install_sources(original)

    monkeypatch.setenv("AOS_PYPI_EXTRA_INDEX_URL", "https://example.test/pypi/simple")
    opted_in = normalize_domestic_install_sources(original)
    assert "PIP_EXTRA_INDEX_URL=https://example.test/pypi/simple" in opted_in
    assert "UV_EXTRA_INDEX_URL=https://example.test/pypi/simple" in opted_in
    # The opted-in host has to be trusted too, or pip rejects it.
    assert "example.test" in re.search(
        r'PIP_TRUSTED_HOST="([^"]*)"', opted_in
    ).group(1)


def test_domestic_rewrite_replaces_legacy_huawei_profile() -> None:
    original = """FROM ubuntu:24.04
# AOS_DOMESTIC_INSTALL_MIRRORS_V1
RUN echo http://repo.huaweicloud.com/ubuntu
# AOS_DOMESTIC_INSTALL_MIRRORS_END
RUN sed -i 's|archive.ubuntu.com|repo.huaweicloud.com|g' /etc/apt/sources.list
"""

    rewritten = normalize_domestic_install_sources(original)

    assert rewritten.count(DOMESTIC_MIRROR_MARKER) == 1
    assert "AOS_DOMESTIC_INSTALL_MIRRORS_V1" not in rewritten
    assert "repo.huaweicloud.com" not in rewritten
    assert "mirrors.cloud.tencent.com" in rewritten


def test_domestic_rewrite_does_not_touch_task_data_or_model_urls() -> None:
    original = """FROM ubuntu:24.04
RUN curl https://example.com/task-input.bin -o /workspace/input.bin
ENV HF_ENDPOINT=https://huggingface.co
"""
    rewritten = normalize_domestic_install_sources(original)

    assert "https://example.com/task-input.bin" in rewritten
    assert "HF_ENDPOINT=https://huggingface.co" in rewritten


def test_domestic_rewrite_guards_optional_deb822_source_file() -> None:
    original = """FROM debian:12
RUN sed -i 's/deb.debian.org/mirror.example/g' /etc/apt/sources.list.d/debian.sources \\
    && apt-get update
"""

    rewritten = normalize_domestic_install_sources(original)

    assert "/etc/apt/sources.list /etc/apt/sources.list.d/*.sources" in rewritten
    assert "2>/dev/null || true" in rewritten
    assert "&& apt-get update" in rewritten
    assert normalize_domestic_install_sources(rewritten) == rewritten


def test_bulk_mirror_rewrite_skips_passed_stream_unless_invalidated(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan"
    run_root = tmp_path / "run"
    candidate = scan_dir / "heavy" / "stream-001" / "environment" / "Dockerfile"
    workspace = (
        run_root / "results" / "heavy" / "stream-001"
        / "workspace" / "environment" / "Dockerfile"
    )
    state = run_root / "results" / "heavy" / "stream-001" / "state.json"
    original = "FROM ubuntu:24.04\nRUN pip install pytest\n"
    for dockerfile in (candidate, workspace):
        dockerfile.parent.mkdir(parents=True, exist_ok=True)
        dockerfile.write_text(original)
    state.write_text(json.dumps({"status": "passed"}))

    skipped = apply(scan_dir, run_root)

    assert skipped["changed"] == 0
    assert skipped["skipped_passed"] == 2
    assert candidate.read_text() == original
    assert workspace.read_text() == original
    assert json.loads(state.read_text())["status"] == "passed"

    invalidated = apply(scan_dir, run_root, invalidate_passed=True)

    assert invalidated["changed"] == 2
    assert invalidated["skipped_passed"] == 0
    assert DOMESTIC_MIRROR_MARKER in candidate.read_text()
    assert DOMESTIC_MIRROR_MARKER in workspace.read_text()
    assert json.loads(state.read_text())["status"] == "verifying-build"


def test_bulk_mirror_rewrite_skips_build_passed_stream(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan"
    run_root = tmp_path / "run"
    dockerfile = scan_dir / "medium" / "stream-001" / "environment" / "Dockerfile"
    state = run_root / "results" / "medium" / "stream-001" / "state.json"
    dockerfile.parent.mkdir(parents=True)
    state.parent.mkdir(parents=True)
    dockerfile.write_text("FROM ubuntu:24.04\nRUN pip install pytest\n")
    state.write_text(json.dumps({"status": "build-passed"}))

    result = apply(scan_dir, run_root)

    assert result["changed"] == 0
    assert result["skipped_passed"] == 1
    assert DOMESTIC_MIRROR_MARKER not in dockerfile.read_text()


_MAVEN_SRC = """FROM ubuntu:24.04
RUN curl -fsSL https://repo1.maven.org/maven2/x.pom -o /tmp/x
ENV COURSIER_REPOSITORIES=https://repo.maven.apache.org/maven2
RUN gradle --init-script /x  # https://plugins.gradle.org/m2
"""


def test_maven_mirror_defaults_to_aliyun(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AOS_MAVEN_MIRROR_URL", raising=False)

    rewritten = normalize_domestic_install_sources(_MAVEN_SRC)

    assert "maven.aliyun.com/repository/public" in rewritten
    assert "maven.aliyun.com/repository/gradle-plugin" in rewritten
    # The default settings.xml mirror id is part of the frozen output.
    assert "<id>aliyun-public</id>" in rewritten


def test_maven_mirror_env_override_replaces_every_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosts that cannot route to Aliyun need a reachable maven mirror.

    Coursier is a GraalVM native-image binary that honors neither
    JAVA_TOOL_OPTIONS nor http_proxy, so repointing the repository URL is the
    only way to fix a JVM task block on such a host.
    """
    monkeypatch.setenv("AOS_MAVEN_MIRROR_URL", f"{TENCENT_MAVEN}/")

    rewritten = normalize_domestic_install_sources(_MAVEN_SRC)

    assert "maven.aliyun.com" not in rewritten
    assert f"COURSIER_REPOSITORIES={TENCENT_MAVEN}\n" in rewritten
    # The gradle-plugin endpoint collapses onto the same mirror, and the
    # trailing slash is normalized away so URLs do not double up.
    assert f"{TENCENT_MAVEN}//" not in rewritten
    assert rewritten.count(TENCENT_MAVEN) == 6
    assert normalize_domestic_install_sources(rewritten) == rewritten


def test_maven_mirror_blank_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AOS_MAVEN_MIRROR_URL", raising=False)
    default = normalize_domestic_install_sources(_MAVEN_SRC)

    monkeypatch.setenv("AOS_MAVEN_MIRROR_URL", "  ")

    assert normalize_domestic_install_sources(_MAVEN_SRC) == default


# --- multiarch apt pin -------------------------------------------------------
#
# Regression cover for the arm64 404: on a noble base, `dpkg
# --add-architecture arm64` makes apt query the primary source for arm64, but
# the mirrors follow the standard Debian split (/ubuntu = amd64,
# /ubuntu-ports = arm64), so the next `apt-get update` 404s. Measured in a real
# ubuntu:24.04 container: 8 x "404  Not Found" and `zlib1g-dev:arm64` failing
# rc=100 without the pin, versus 0 x 404 and rc=0 with it.

_MULTIARCH_SRC = """FROM ubuntu:24.04
RUN apt-get update && apt-get install -y ca-certificates
RUN dpkg --add-architecture arm64 && \\
    echo "deb [arch=arm64] http://ports.ubuntu.com/ubuntu-ports noble main" \\
      > /etc/apt/sources.list.d/arm64.list && \\
    sed -i 's/^deb /deb [arch=amd64] /' /etc/apt/sources.list && \\
    apt-get update && apt-get install -y zlib1g-dev:arm64
"""


def test_multiarch_pin_added_for_foreign_architecture() -> None:
    rewritten = normalize_domestic_install_sources(_MULTIARCH_SRC)

    assert "# AOS_MULTIARCH_PIN" in rewritten
    # Host arch is resolved at build time, never hardcoded: a literal amd64
    # would exclude the host's own arch when built on arm64.
    assert 'host_arch="$(dpkg --print-architecture)"' in rewritten
    assert "Architectures: $host_arch" in rewritten
    # The pin must precede the RUN that enables the foreign architecture,
    # otherwise apt has already failed by the time it applies.
    assert rewritten.index("# AOS_MULTIARCH_PIN") < rewritten.index(
        "dpkg --add-architecture"
    )
    # It must not split the `\`-continued RUN block it is inserted before.
    assert "RUN dpkg --add-architecture arm64 && \\" in rewritten
    assert normalize_domestic_install_sources(rewritten) == rewritten


def test_multiarch_pin_leaves_sources_list_alone() -> None:
    """Never touch /etc/apt/sources.list -- only the deb822 files.

    The task's own `sed 's/^deb /deb [arch=amd64] /'` is a no-op on noble
    (comments only) but *works* on jammy. Stacking a second `arch=` on top of
    it there yields `deb [arch=amd64] [arch=amd64] ...`, which apt rejects with
    "E: Malformed entry (URI parse)" -- verified in ubuntu:22.04. So the pin
    fixes the deb822 layout and leaves the one-line layout to the task.
    """
    rewritten = normalize_domestic_install_sources(_MULTIARCH_SRC)
    pin = rewritten.split("# AOS_MULTIARCH_PIN", 1)[1].split("RUN dpkg", 1)[0]

    assert "sources.list.d/*.sources" in pin
    assert "/etc/apt/sources.list " not in pin
    assert "arch=$host_arch" not in pin


def test_no_pin_without_add_architecture() -> None:
    """Byte-identical output for non-multiarch Dockerfiles.

    This is what keeps `source_hash()` and already-published image tags stable
    for the streams that already build.
    """
    plain = """FROM ubuntu:24.04
RUN apt-get update && apt-get install -y build-essential
"""
    assert "# AOS_MULTIARCH_PIN" not in normalize_domestic_install_sources(plain)


def test_existing_hand_rolled_pin_is_respected() -> None:
    """Do not rewrite a stream that already pins the architecture itself.

    tier-v2 contains 17 mutually inconsistent hand-written variants; 27 of them
    build fine. Converging them would perturb working streams for no gain, so
    the pin only fills in the ones with no `Architectures:` line at all.
    """
    already = """FROM ubuntu:24.04
RUN sed -i '/^Types:/a Architectures: amd64' /etc/apt/sources.list.d/ubuntu.sources && \\
    dpkg --add-architecture arm64 && apt-get update
"""
    assert "# AOS_MULTIARCH_PIN" not in normalize_domestic_install_sources(already)


def test_no_pin_for_debian_base() -> None:
    """Debian's /debian serves every architecture from one tree -- no 404."""
    deb = """FROM debian:12
RUN dpkg --add-architecture arm64 && apt-get update
"""
    assert "# AOS_MULTIARCH_PIN" not in normalize_domestic_install_sources(deb)


_NOBLE_PKG_SRC = """FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \\
    libnetcdf15 \\
    libnetcdf-dev \\
    libglib2.0-0 \\
    libncurses5 \\
    curl
"""

_JAMMY_PKG_SRC = _NOBLE_PKG_SRC.replace("ubuntu:24.04", "ubuntu:22.04")


def test_noble_package_renames_applied():
    """jammy-era names make apt exit 100 on a noble base."""
    out = normalize_domestic_install_sources(_NOBLE_PKG_SRC)
    assert "libnetcdf19t64" in out
    # The stale name must be gone, or apt still fails.
    assert not re.search(r"(?<![\w.+-])libnetcdf15(?![\w.+-])", out)
    # These two install fine on noble via a Provides: alias, and streams using
    # them already build -- renaming them would perturb working images.
    assert "libglib2.0-0" in out
    assert "libncurses5" in out
    # An unrelated package sharing a prefix must survive untouched.
    assert "libnetcdf-dev" in out


def test_noble_renames_are_idempotent():
    once = normalize_domestic_install_sources(_NOBLE_PKG_SRC)
    twice = normalize_domestic_install_sources(once)
    assert once == twice
    assert "t64t64" not in twice


def test_jammy_base_is_left_alone():
    """On jammy the original names are correct; rewriting breaks a working build."""
    out = normalize_domestic_install_sources(_JAMMY_PKG_SRC)
    assert "libnetcdf15" in out
    assert "libnetcdf19t64" not in out


def test_rename_does_not_touch_dev_suffixed_names():
    src = "FROM ubuntu:24.04\nRUN apt-get install -y libnetcdf15-dev libnetcdf-dev\n"
    out = normalize_domestic_install_sources(src)
    assert "libnetcdf15-dev" in out, "a -dev suffix must not be partially rewritten"
    assert "libnetcdf-dev" in out
    assert "libnetcdf19t64-dev" not in out


def test_apt_sources_use_http_not_https():
    """A bare ubuntu/debian base has no CA bundle, so https apt fails TLS.

    Measured in ubuntu:24.04: https -> 24 'certificate issuer is unknown'
    errors and `E: Unable to locate package unzip`; http -> clean. A proxy does
    not help, because the failure is certificate verification.
    """
    src = (
        "FROM ubuntu:24.04\n"
        "RUN sed -i 's|http://archive.ubuntu.com/ubuntu|"
        "http://archive.ubuntu.com/ubuntu|g' /etc/apt/sources.list\n"
        "RUN apt-get update && apt-get install -y curl\n"
    )
    out = normalize_domestic_install_sources(src)
    assert "https://mirrors.cloud.tencent.com/ubuntu" not in out
    # pip must stay on https: it runs after ca-certificates and has no GPG.
    assert "https://mirrors.cloud.tencent.com/pypi/simple" in out


def test_existing_https_apt_is_downgraded():
    """Earlier passes emitted https apt URLs; they must be rewritten."""
    src = (
        "FROM ubuntu:24.04\n"
        "RUN sed -i 's|x|https://mirrors.cloud.tencent.com/ubuntu|g' /etc/apt/sources.list\n"
        "RUN echo https://mirrors.cloud.tencent.com/ubuntu-ports\n"
        "RUN echo http://mirrors.cloud.tencent.com/debian-security\n"
    )
    out = normalize_domestic_install_sources(src)
    assert "http://mirrors.cloud.tencent.com/ubuntu" in out
    assert "http://mirrors.cloud.tencent.com/ubuntu-ports" in out
    assert "http://mirrors.cloud.tencent.com/debian-security" in out
    assert "https://mirrors.cloud.tencent.com/ubuntu" not in out
    assert "https://mirrors.cloud.tencent.com/debian" not in out
    # `-ports` must not have been mangled by the shorter `/ubuntu` prefix.
    assert "ubuntu-portss" not in out and "ubuntu-ports-ports" not in out


def test_ports_suite_rebased_to_match_base():
    """jammy ports on a noble base makes apt fail with resolver breaks."""
    src = (
        "FROM ubuntu:24.04\n"
        "RUN echo 'deb [arch=arm64] http://mirrors.cloud.tencent.com/ubuntu-ports jammy main' >> /etc/apt/sources.list\n"
        "RUN echo 'deb [arch=arm64] http://mirrors.cloud.tencent.com/ubuntu-ports jammy-updates main' >> /etc/apt/sources.list\n"
        "RUN echo 'deb [arch=arm64] http://mirrors.cloud.tencent.com/ubuntu-ports jammy-security main' >> /etc/apt/sources.list\n"
    )
    out = normalize_domestic_install_sources(src)
    # The -updates / -security suffixes must survive the rewrite.
    assert "ubuntu-ports noble main" in out
    assert "ubuntu-ports noble-updates main" in out
    assert "ubuntu-ports noble-security main" in out
    assert not re.search(r"/ubuntu-ports\s+jammy", out)


def test_ports_suite_untouched_when_it_already_matches():
    src = (
        "FROM ubuntu:22.04\n"
        "RUN echo 'deb http://mirrors.cloud.tencent.com/ubuntu-ports jammy main'\n"
    )
    out = normalize_domestic_install_sources(src)
    assert "ubuntu-ports jammy main" in out, "a matching suite must not be rewritten"
    assert "noble" not in out


def test_codename_rewrite_is_anchored_on_ports():
    """A bare codename elsewhere must not be rewritten."""
    src = (
        "FROM ubuntu:24.04\n"
        "RUN echo 'building against jammy for reference' > /note\n"
        "RUN git clone https://example.invalid/jammy-tool.git\n"
    )
    out = normalize_domestic_install_sources(src)
    assert "against jammy for reference" in out
    assert "jammy-tool.git" in out


def test_debian_repo_on_ubuntu_base_is_retargeted():
    """An ubuntu image has no Debian keyring, so the repo is unusable."""
    src = (
        "FROM ubuntu:24.04\n"
        "RUN echo 'deb-src [trusted=yes] http://mirrors.cloud.tencent.com/debian stable main' > /etc/apt/sources.list.d/d.list\n"
    )
    out = normalize_domestic_install_sources(src)
    assert "deb-src http://mirrors.cloud.tencent.com/ubuntu noble main universe" in out
    assert "/debian stable" not in out
    # The stale option block must not survive.
    assert "trusted=yes" not in out


def test_debian_repo_on_debian_base_is_left_alone():
    """A debian base HAS the keyring; retargeting it would be wrong."""
    src = (
        "FROM debian:bookworm-slim\n"
        "RUN echo 'deb http://mirrors.cloud.tencent.com/debian bookworm main' > /etc/apt/sources.list\n"
    )
    out = normalize_domestic_install_sources(src)
    assert "/debian bookworm main" in out
    # the retarget must not have fired at all on a debian base
    assert not re.search(r"deb\s+\S*/ubuntu\s+\w+\s+main universe", out)


@pytest.mark.parametrize(
    "base",
    [
        "ubuntu:24.04",
        "debian:bookworm-slim",
        "python:3.12-slim",
        "node:20",
        "golang:1.22",
        "docker.io/library/ubuntu:22.04",
        "",
        "${FIRST_PARTY_CLI_BASE_IMAGE}",
    ],
)
def test_generic_bases_are_rebasable(base):
    from aos_bench.task_construction.assemble.install_mirrors import (
        detect_unrebasable_base,
    )

    assert detect_unrebasable_base(base) is None


@pytest.mark.parametrize(
    "base",
    [
        "vllm/vllm-openai-cpu:v0.21.0",
        "aos-llvmfix-base:v1",
        "julia:1.9",
        "tarantool/tarantool:3.0",
        "php:8.1-apache",
        "pandare/panda:latest",
    ],
)
def test_specialized_bases_are_rejected(base):
    """These ship software the task consumes but never installs."""
    from aos_bench.task_construction.assemble.install_mirrors import (
        detect_unrebasable_base,
    )

    reason = detect_unrebasable_base(base)
    assert reason is not None
    assert base in reason
