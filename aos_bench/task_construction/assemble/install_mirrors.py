"""Deterministic, logic-preserving domestic package-source normalization.

The REFINE loop may edit a merged Dockerfile directly, so applying a mirror
profile only while assembling is insufficient.  This module is deliberately
text based and idempotent: it can normalize both freshly assembled files and
already-refined workspaces without changing task instructions, tests, or
solutions.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

DOMESTIC_MIRROR_MARKER = "# AOS_DOMESTIC_INSTALL_MIRRORS_V2"
DOMESTIC_MIRROR_END = "# AOS_DOMESTIC_INSTALL_MIRRORS_END"

TENCENT_PYPI = "https://mirrors.cloud.tencent.com/pypi/simple"
ALIYUN_PYPI = "https://mirrors.aliyun.com/pypi/simple"
# apt over http, NOT https. A bare ubuntu:24.04 / debian base ships no CA
# bundle, and `ca-certificates` is installed BY the very apt block that needs
# it -- a chicken-and-egg that makes every https apt source fail TLS with
# "The certificate issuer is unknown". Measured inside ubuntu:24.04 against
# this mirror: https -> 24 certificate errors and `E: Unable to locate package
# unzip`; http -> 0 errors, unzip installs. A proxy does NOT fix it (verified:
# with Acquire::http[s]::Proxy set, https still produced the same 24 errors)
# because the failure is certificate verification, not reachability.
#
# The visible symptom is misleading and cost real debugging time: `apt-get
# update` exits 0 with only `W: Failed to fetch`, leaving an EMPTY package
# list, so the build dies one step later claiming basic packages like unzip or
# wget do not exist. That reads like a bad package name, not a TLS problem.
#
# Integrity is unaffected: apt authenticates packages with GPG signatures
# independently of the transport, which is why Debian/Ubuntu ship http in
# their own default sources.
#
# pip/maven above stay on https: those run after ca-certificates is installed,
# and pip has no GPG equivalent, so transport security is what protects them.
TENCENT_APT = "http://mirrors.cloud.tencent.com/ubuntu"
TENCENT_APT_PORTS = "http://mirrors.cloud.tencent.com/ubuntu-ports"
# Debian repos are http for the same CA chicken-and-egg reason as ubuntu: a
# debian:*-slim base ships no CA bundle either.
#
# Do NOT "fix" an unsigned-debian failure by switching this to https. Measured
# on ubuntu:24.04 with the debian repo added, varying only the scheme:
#   http  -> rc=100, NO_PUBKEY 6ED0E7B82643E131 78DBA3BC47EF2265 762F67A0B2C39DE4
#   https -> rc=100, byte-identical NO_PUBKEY
# The failure is GPG trust, not transport: an ubuntu image carries only
# ubuntu-archive-keyring, so it cannot verify a Debian InRelease at any scheme.
# The real defect in that case is a debian source on an ubuntu base; retarget
# the source at the base distro (or install debian-archive-keyring) instead.
TENCENT_DEBIAN = "http://mirrors.cloud.tencent.com/debian"
TENCENT_DEBIAN_SECURITY = "http://mirrors.cloud.tencent.com/debian-security"
ALIYUN_MAVEN = "https://maven.aliyun.com/repository/public"
ALIYUN_MAVEN_GRADLE = "https://maven.aliyun.com/repository/gradle-plugin"
TENCENT_MAVEN = "https://mirrors.cloud.tencent.com/nexus/repository/maven-public"

# Secondary pypi index, EMPTY by default. pip and uv query *every* configured
# index, so an unreachable extra-index does not degrade gracefully: it turns a
# working build into a ~20 min stall (10 retries x 120s connect timeout) before
# it finally succeeds against the primary.
#
# The default used to be ALIYUN_PYPI, which baked
#   UV_EXTRA_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
# into 27 shipped images. aliyun was NOT routable from the sandboxes
# where graders run (measured: tencent connects in 0.12s, aliyun times out past
# 12s), so any grader that resolved a package at verify time died with
#   error: Failed to fetch: `https://mirrors.aliyun.com/pypi/simple/pytest/`
# and then wrote a well-formed reward of 0.0 — the agent's work was never
# judged, and nothing in the artifacts said so. That is the false-zero class
# documented in docs/experiments/v1_0_results_1004.md; 44 graders were patched
# individually to unset the variable before this root cause was fixed here.
#
# Build-time resolution is unaffected: the primary index
# (mirrors.cloud.tencent.com) serves the same packages and is reachable from
# both the build hosts and the sandboxes. Set AOS_PYPI_EXTRA_INDEX_URL to
# re-enable a secondary index on a host that can actually route to it.
_EXTRA_PYPI_ENV = "AOS_PYPI_EXTRA_INDEX_URL"

# Maven/central mirror, same story as the pypi extra index: hosts that cannot
# route to maven.aliyun.com fail the Scala/JVM task blocks outright. Coursier
# is a GraalVM native-image binary, so it honors neither JAVA_TOOL_OPTIONS nor
# http_proxy — pointing it at a reachable mirror is the only fix that works.
# Set AOS_MAVEN_MIRROR_URL to override (e.g. TENCENT_MAVEN).
_MAVEN_MIRROR_ENV = "AOS_MAVEN_MIRROR_URL"


# Longest/specific prefixes come first.  Rewrites are limited to dependency
# and tool distribution endpoints; task data/model URLs are intentionally not
# touched.
INSTALL_SOURCE_REWRITES: tuple[tuple[str, str], ...] = (
    # Ubuntu / Debian / Alpine / RPM repositories.
    # The https:// forms of our own ubuntu repos come first: earlier passes
    # emitted them, and they must be downgraded to http for the CA reason
    # documented on TENCENT_APT. Longest paths first so `-ports` is not eaten by
    # the shorter `/ubuntu` prefix.
    ("https://mirrors.cloud.tencent.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("https://mirrors.cloud.tencent.com/ubuntu", TENCENT_APT),
    ("https://mirrors.cloud.tencent.com/debian-security", TENCENT_DEBIAN_SECURITY),
    ("https://mirrors.cloud.tencent.com/debian", TENCENT_DEBIAN),
    ("http://mirrors.cloud.tencent.com/debian-security", TENCENT_DEBIAN_SECURITY),
    ("http://mirrors.cloud.tencent.com/debian", TENCENT_DEBIAN),
    ("http://mirrors.cloud.tencent.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("http://mirrors.cloud.tencent.com/ubuntu", TENCENT_APT),
    ("https://repo.huaweicloud.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("http://repo.huaweicloud.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("https://repo.huaweicloud.com/ubuntu", TENCENT_APT),
    ("http://repo.huaweicloud.com/ubuntu", TENCENT_APT),
    ("https://repo.huaweicloud.com/debian-security", TENCENT_DEBIAN_SECURITY),
    ("http://repo.huaweicloud.com/debian-security", TENCENT_DEBIAN_SECURITY),
    ("https://repo.huaweicloud.com/debian", TENCENT_DEBIAN),
    ("http://repo.huaweicloud.com/debian", TENCENT_DEBIAN),
    ("https://ports.ubuntu.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("http://ports.ubuntu.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("https://security.ubuntu.com/ubuntu", TENCENT_APT),
    ("http://security.ubuntu.com/ubuntu", TENCENT_APT),
    ("https://archive.ubuntu.com/ubuntu", TENCENT_APT),
    ("http://archive.ubuntu.com/ubuntu", TENCENT_APT),
    ("https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports", TENCENT_APT_PORTS),
    ("http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports", TENCENT_APT_PORTS),
    ("https://mirrors.tuna.tsinghua.edu.cn/ubuntu", TENCENT_APT),
    ("http://mirrors.tuna.tsinghua.edu.cn/ubuntu", TENCENT_APT),
    ("https://mirrors.aliyun.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("http://mirrors.aliyun.com/ubuntu-ports", TENCENT_APT_PORTS),
    ("https://mirrors.aliyun.com/ubuntu", TENCENT_APT),
    ("http://mirrors.aliyun.com/ubuntu", TENCENT_APT),
    ("https://security.debian.org/debian-security", TENCENT_DEBIAN_SECURITY),
    ("http://security.debian.org/debian-security", TENCENT_DEBIAN_SECURITY),
    ("https://deb.debian.org/debian", TENCENT_DEBIAN),
    ("http://deb.debian.org/debian", TENCENT_DEBIAN),
    (
        "https://dl-cdn.alpinelinux.org/alpine",
        "https://mirrors.cloud.tencent.com/alpine",
    ),
    ("http://dl-cdn.alpinelinux.org/alpine", "http://mirrors.cloud.tencent.com/alpine"),
    ("https://dl.fedoraproject.org/pub/epel", "https://mirrors.cloud.tencent.com/epel"),
    ("http://dl.fedoraproject.org/pub/epel", "http://mirrors.cloud.tencent.com/epel"),
    # Python / Node / JVM / Gradle.
    ("https://pypi.tuna.tsinghua.edu.cn/simple", TENCENT_PYPI),
    ("https://mirrors.aliyun.com/pypi/simple", TENCENT_PYPI),
    ("https://repo.huaweicloud.com/repository/pypi/simple", TENCENT_PYPI),
    ("https://pypi.org/simple", TENCENT_PYPI),
    ("http://pypi.org/simple", "http://mirrors.cloud.tencent.com/pypi/simple"),
    ("https://registry.npmjs.org", "https://registry.npmmirror.com"),
    ("https://registry.yarnpkg.com", "https://registry.npmmirror.com"),
    ("https://repo1.maven.org/maven2", ALIYUN_MAVEN),
    (
        "https://repo.maven.apache.org/maven2",
        ALIYUN_MAVEN,
    ),
    (
        "https://plugins.gradle.org/m2",
        ALIYUN_MAVEN_GRADLE,
    ),
    (
        "https://services.gradle.org/distributions",
        "https://mirrors.cloud.tencent.com/gradle",
    ),
    # Node binary archives.  Do not let the generic TUNA-host rewrite below
    # turn this into repo.huaweicloud.com/nodejs-release: that endpoint returns
    # HTTP 401 while Tencent's archive mirror serves the same artifacts.
    (
        "https://repo.huaweicloud.com/nodejs-release",
        "https://mirrors.cloud.tencent.com/nodejs-release",
    ),
    (
        "https://mirrors.tuna.tsinghua.edu.cn/nodejs-release",
        "https://mirrors.cloud.tencent.com/nodejs-release",
    ),
    (
        "https://nodejs.org/download/release",
        "https://mirrors.cloud.tencent.com/nodejs-release",
    ),
    ("https://nodejs.org/dist", "https://mirrors.cloud.tencent.com/nodejs-release"),
    # Go / Rust / Conda / PyTorch and common language ecosystems.
    ("https://proxy.golang.org", "https://goproxy.cn"),
    ("https://static.rust-lang.org", "https://rsproxy.cn"),
    ("https://index.crates.io", "https://rsproxy.cn/index"),
    (
        "https://repo.anaconda.com/pkgs",
        "https://mirrors.cloud.tencent.com/anaconda/pkgs",
    ),
    ("https://conda.anaconda.org", "https://mirrors.cloud.tencent.com/anaconda/cloud"),
    ("https://download.pytorch.org/whl", "https://mirrors.aliyun.com/pytorch-wheels"),
    ("https://cloud.r-project.org", "https://mirrors.tuna.tsinghua.edu.cn/CRAN"),
    ("https://cran.r-project.org", "https://mirrors.tuna.tsinghua.edu.cn/CRAN"),
    ("https://rubygems.org", "https://gems.ruby-china.com"),
    ("https://repo.packagist.org", "https://mirrors.aliyun.com/composer"),
    # Host-only forms occur in trusted-host settings, sed expressions, and
    # source-list snippets assembled across continued lines.
    ("pypi.tuna.tsinghua.edu.cn", "mirrors.cloud.tencent.com"),
    ("repo.huaweicloud.com", "mirrors.cloud.tencent.com"),
    ("mirrors.tuna.tsinghua.edu.cn", "mirrors.cloud.tencent.com"),
    ("ports.ubuntu.com", "mirrors.cloud.tencent.com"),
    ("archive.ubuntu.com", "mirrors.cloud.tencent.com"),
    ("security.ubuntu.com", "mirrors.cloud.tencent.com"),
    ("deb.debian.org", "mirrors.cloud.tencent.com"),
    ("security.debian.org", "mirrors.cloud.tencent.com"),
)


def _extra_pypi_index() -> str:
    """Secondary pypi index, or "" when the build should use only the primary.

    Defaults to "" — see _EXTRA_PYPI_ENV for why an unreachable extra index is
    worse than no extra index at all.
    """
    raw = os.environ.get(_EXTRA_PYPI_ENV)
    return "" if raw is None else raw.strip()


def _maven_mirror() -> str:
    """Maven/central mirror to route JVM builds through."""
    raw = os.environ.get(_MAVEN_MIRROR_ENV)
    if raw is None:
        return ALIYUN_MAVEN
    return raw.strip().rstrip("/") or ALIYUN_MAVEN


def _apply_maven_mirror(text: str) -> str:
    """Repoint every Aliyun maven endpoint at the configured mirror.

    Runs after INSTALL_SOURCE_REWRITES, which normalizes central/gradle URLs
    onto Aliyun first, so one pass here catches both the rewritten URLs and
    any that the task Dockerfile already hardcoded. A no-op when the mirror is
    the default, keeping output byte-identical for unconfigured builds.
    """
    mirror = _maven_mirror()
    if mirror == ALIYUN_MAVEN:
        return text
    return text.replace(ALIYUN_MAVEN_GRADLE, mirror).replace(ALIYUN_MAVEN, mirror)


def _domestic_config_block() -> str:
    extra_pypi = _extra_pypi_index()
    maven = _maven_mirror()
    # Keep the generated settings.xml byte-identical for unconfigured builds.
    maven_id = "aliyun-public" if maven == ALIYUN_MAVEN else "aos-central"
    pip_hosts = "mirrors.cloud.tencent.com"
    pip_extra_env = ""
    uv_extra_env = ""
    extra_conf = ""
    if extra_pypi:
        extra_host = urlsplit(extra_pypi).hostname
        if extra_host and extra_host != "mirrors.cloud.tencent.com":
            pip_hosts = f"{pip_hosts} {extra_host}"
        pip_extra_env = f"    PIP_EXTRA_INDEX_URL={extra_pypi} \\\n"
        uv_extra_env = f"    UV_EXTRA_INDEX_URL={extra_pypi} \\\n"
        extra_conf = f"\\nextra-index-url={extra_pypi}"

    return f'''{DOMESTIC_MIRROR_MARKER}
ENV PIP_INDEX_URL={TENCENT_PYPI} \\
{pip_extra_env}    UV_INDEX_URL={TENCENT_PYPI} \\
{uv_extra_env}    PIP_TRUSTED_HOST="{pip_hosts}" \\
    NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \\
    YARN_NPM_REGISTRY_SERVER=https://registry.npmmirror.com \\
    GOPROXY=https://goproxy.cn,direct \\
    RUSTUP_DIST_SERVER=https://rsproxy.cn \\
    RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup \\
    CARGO_REGISTRIES_CRATES_IO_INDEX=sparse+https://rsproxy.cn/index/ \\
    COURSIER_REPOSITORIES={maven}
RUN sed -i \\
      -e 's|https\\?://archive.ubuntu.com/ubuntu|{TENCENT_APT}|g' \\
      -e 's|https\\?://security.ubuntu.com/ubuntu|{TENCENT_APT}|g' \\
      -e 's|https\\?://ports.ubuntu.com/ubuntu-ports|{TENCENT_APT_PORTS}|g' \\
      -e 's|https\\?://deb.debian.org/debian|{TENCENT_DEBIAN}|g' \\
      -e 's|https\\?://security.debian.org/debian-security|{TENCENT_DEBIAN_SECURITY}|g' \\
      /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true
RUN mkdir -p /etc/pip /root/.m2 /root/.gradle /root/.config/pip /root/.cargo \\
    && printf '[global]\\nindex-url={TENCENT_PYPI}{extra_conf}\\ntrusted-host={pip_hosts}\\nretries=10\\ntimeout=120\\n' > /etc/pip.conf \\
    && printf 'registry=https://registry.npmmirror.com\\n' > /root/.npmrc \\
    && printf '[source.crates-io]\\nreplace-with="rsproxy-sparse"\\n[source.rsproxy-sparse]\\nregistry="sparse+https://rsproxy.cn/index/"\\n' > /root/.cargo/config.toml \\
    && printf '%s\\n' '<settings><mirrors><mirror><id>{maven_id}</id><mirrorOf>central</mirrorOf><url>{maven}</url></mirror></mirrors></settings>' > /root/.m2/settings.xml \\
    && printf '%s\\n' 'allprojects {{ repositories.configureEach {{ r -> if (r.hasProperty("url") && ["https://repo1.maven.org/maven2/", "https://repo.maven.apache.org/maven2/"].contains(r.url.toString())) r.url = uri("{maven}") }} }}' > /root/.gradle/init.gradle
{DOMESTIC_MIRROR_END}
'''


_ADD_ARCH_RE = re.compile(r"dpkg\s+--add-architecture\s+(?P<arch>[A-Za-z0-9_]+)")

# Base images whose contents a task may safely be rebased away from: they
# provide a stock distro or language runtime that the merged image reinstalls
# from apt/pip anyway. Anything else ships software the task consumes but never
# installs, so rebasing silently produces a Dockerfile that cannot work.
_GENERIC_BASE_RE = re.compile(
    r"^(?:docker\.io/)?(?:library/)?"
    r"(?:ubuntu|debian|python|node|golang|rust|alpine|gcc|openjdk|maven|"
    r"buildpack-deps|mcr\.microsoft\.com/devcontainers)\b",
    re.IGNORECASE,
)


def detect_unrebasable_base(original_base: str) -> str | None:
    """Return a reason if a task cannot be rebased off `original_base`.

    A task built `FROM vllm/vllm-openai-cpu:v0.21.0` consumes the vllm package
    that image ships -- its only build step is `python3 -c 'import vllm'` and a
    `mv` of the installed tree. Rebased onto ubuntu:24.04 nothing installs vllm,
    so the merged Dockerfile fails with ModuleNotFoundError at the step that
    consumes it. Measured on heavy-20-scan-203, STEP 96/174.

    This is worth failing fast on because the usual "has this task ever built?"
    heuristic says yes: six build-valid streams contain it. All of them were
    hand-patched afterwards -- two insert `pip install vllm==0.11.0`, two wrap
    the consume step in `if VLLM_PKG=$(... 2>/dev/null); then ... fi` so it
    skips on failure. The merger does not emit either shape, so a freshly
    assembled stream is guaranteed to fail while the corpus suggests it is safe.

    Other specialized bases in the pool with the same exposure:
    aos-llvmfix-base:v1 (5 tasks), julia (3), ruby (2), tarantool, php,
    pandare/panda, kalilinux, google/cloud-sdk.
    """
    base = original_base.strip()
    if not base or base.startswith("$"):
        # An ARG-substituted base cannot be judged statically.
        return None
    if _GENERIC_BASE_RE.match(base):
        return None
    return (
        f"task's original base image {base!r} is not a generic distro/runtime "
        "image; it likely provides software the task consumes but never "
        "installs, so rebasing onto a shared base yields a Dockerfile that "
        "cannot build. Install that software explicitly, or exclude the task."
    )


# Ubuntu release codenames, newest first, mapped from the version in a FROM tag.
_UBUNTU_CODENAMES = {
    "24.04": "noble",
    "22.04": "jammy",
    "20.04": "focal",
}

# Only the suite field of an ubuntu-ports line. Anchoring on `/ubuntu-ports`
# rather than on the bare codename is what makes this safe to run corpus-wide:
# a naive \bjammy\b would also rewrite unrelated strings (image tags, comments,
# vendored paths).
#
# The `-updates` / `-security` suffix is CAPTURED and re-emitted rather than
# excluded by a lookahead. An earlier version used `(?![\w-])`, which refused
# to match `jammy-updates` at all -- so the plain `jammy` line was rebased
# while `jammy-updates` and `jammy-security` silently stayed behind, which is
# the same suite mismatch this function exists to remove.
_PORTS_SUITE_RE = re.compile(
    r"(/ubuntu-ports\s+)(?P<suite>focal|jammy|noble)(?P<pocket>-[a-z]+)?(?![\w-])"
)


def _rebase_ports_suite(text: str) -> str:
    """Align arm64 ubuntu-ports suites with the merged image's base release.

    Source tasks written against an older Ubuntu hardcode both `FROM
    ubuntu:22.04` and `ubuntu-ports jammy`. The assembler rebases the merged
    image onto a single base (usually 24.04) but leaves the ports suite alone,
    so the arm64 indices resolve to jammy while the primary sources are noble.
    apt then cannot satisfy cross-architecture dependencies and fails with
    `pkgProblemResolver::Resolve generated breaks`, i.e. `held broken packages`
    -- a message that reads like a broken package list, not a suite mismatch.

    Measured on a real candidate's package list inside the merged image:
    ports at `jammy` -> apt-get install --dry-run rc=100 with resolver breaks;
    ports at `noble` (matching the base) -> rc=0.

    Conditional on an actual mismatch, so a stream whose suite already matches
    its base is returned byte-identical and its published tag does not move.

    One caveat for callers: this is NOT safe to apply blindly to the whole
    corpus. `heavy-20-scan-070` is build-valid and published, yet mixes jammy
    and noble ports lines -- and it builds because the two sets are written to
    *different* sources.list.d files and never resolve together. Rewriting it
    would replace a working configuration with two duplicate noble blocks for
    no gain. Apply this only to streams that have not yet produced a published
    image; `scripts/local/rebuild_failed_streams.sh` satisfies that by reading
    exclusively from build-failed/.
    """
    from_lines = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    if not from_lines:
        return text
    base = from_lines[0].lower()
    if "ubuntu" not in base:
        return text
    target = next(
        (name for ver, name in _UBUNTU_CODENAMES.items() if ver in base), None
    )
    if target is None:
        target = next((n for n in _UBUNTU_CODENAMES.values() if n in base), None)
    if target is None:
        return text

    def _sub(match: re.Match[str]) -> str:
        # Leave a suite that already matches the base untouched.
        if match.group("suite") == target:
            return match.group(0)
        return f"{match.group(1)}{target}{match.group('pocket') or ''}"

    return _PORTS_SUITE_RE.sub(_sub, text)


# A debian archive line (binary or source) added on top of an ubuntu base.
# Captures the whole clause including any [trusted=yes] / [signed-by=...] block
# so the replacement cannot leave a stale option behind.
_DEBIAN_ON_UBUNTU_RE = re.compile(
    r"(?P<kind>deb|deb-src)(?:\s+\[[^\]]*\])?\s+https?://[^\s]*?/debian(?:-security)?"
    r"\s+(?:stable|oldstable|testing|bookworm|bullseye|trixie)(?:-[\w]+)?"
    r"(?P<components>(?:\s+[a-z][\w-]*)+)"
)


def _retarget_debian_on_ubuntu(text: str) -> str:
    """Point debian archive lines at the ubuntu base distro instead.

    An ubuntu image ships only ubuntu-archive-keyring, so it cannot verify a
    Debian `InRelease` and apt refuses the repo outright:

        NO_PUBKEY 6ED0E7B82643E131 78DBA3BC47EF2265 762F67A0B2C39DE4
        E: The repository '.../debian stable InRelease' is not signed.

    This is GPG trust, not transport -- measured identical under http and
    https, so switching scheme does not help (and that mistake has now been
    made twice; see the note on TENCENT_DEBIAN).

    Of the available fixes -- installing debian-archive-keyring, marking the
    repo [trusted=yes], or retargeting it -- retargeting is chosen because the
    others either add a package the task never asked for or disable
    verification. The packages these tasks want (e.g. pmars sources) exist in
    Ubuntu's universe component, so `main universe` is emitted.
    """
    from_lines = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    if not from_lines:
        return text
    base = from_lines[0].lower()
    # Only relevant when a DEBIAN repo sits on an UBUNTU base; a debian base
    # has the right keyring and must be left alone.
    if "ubuntu" not in base:
        return text
    suite = next(
        (name for ver, name in _UBUNTU_CODENAMES.items() if ver in base), None
    ) or next((n for n in _UBUNTU_CODENAMES.values() if n in base), None)
    if suite is None:
        return text

    def _sub(match: re.Match[str]) -> str:
        return (
            f"{match.group('kind')} {TENCENT_APT} {suite} main universe"
        )

    return _DEBIAN_ON_UBUNTU_RE.sub(_sub, text)


# Packages Ubuntu renamed between jammy (22.04) and noble (24.04) *and* left
# without a resolvable alias. A merged stream inherits the source task's
# package list verbatim, so such a name makes apt exit 100 with
# `E: Unable to locate package <old>`, killing the build mid-`apt-get install`.
#
# Membership was decided by `apt-get install --dry-run` inside ubuntu:24.04
# against the tencent mirror, NOT by `apt-cache policy`: policy reports
# `Candidate: (none)` for names that nonetheless install fine via a
# `Provides:` alias. libglib2.0-0 and libncurses5 are exactly that case --
# their dry-run installs libglib2.0-0t64 / libncurses6 automatically, and 4
# streams that use libglib2.0-0 already build successfully. Rewriting them
# would perturb working streams (and their published tags) for no gain.
#
# Names with no noble equivalent at all (libtiff5, libssl1.1) are deliberately
# absent too: substituting a different soname would produce an image that
# builds but breaks at runtime, which is worse than a visible build failure.
_NOBLE_PACKAGE_RENAMES: tuple[tuple[str, str], ...] = (
    ("libnetcdf15", "libnetcdf19t64"),
    # libasound2 is a real rename, unlike libglib2.0-0 above: apt reports
    # `has no installation candidate` and --dry-run fails. The Provides: alias
    # does not rescue it because the provider apt finds is
    # liboss4-salsa-asound2, not libasound2t64, so apt refuses to choose.
    # Verified by --dry-run: libasound2 -> E: no installation candidate;
    # libasound2t64 -> Inst libasound2t64 1.2.11-1ubuntu0.3.
    ("libasound2", "libasound2t64"),
)


def _rename_noble_packages(text: str) -> str:
    """Map jammy-era package names to their noble equivalents.

    Only applied when the merged image is actually noble-based: on a jammy base
    the original names are correct and rewriting them would break a stream that
    currently builds. Detection is by FROM line, and the rewrite is a
    word-boundary match so `libglib2.0-0` does not also mangle an existing
    `libglib2.0-0t64` or `libglib2.0-0-dev`.
    """
    from_lines = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    if not from_lines:
        return text
    # A merged Dockerfile is single-stage (the assembler rejects multi-stage),
    # so the first FROM is the base.
    base = from_lines[0].lower()
    if not ("24.04" in base or "noble" in base):
        return text

    for old, new in _NOBLE_PACKAGE_RENAMES:
        # (?![\w.+-]) keeps `libncurses5` from matching inside `libncurses5-dev`
        # and stops a second pass from turning `...t64` into `...t64t64`.
        pattern = re.compile(rf"(?<![\w.+-]){re.escape(old)}(?![\w.+-])")
        text = pattern.sub(new, text)
    return text


# Pin the primary apt source to the host architecture before a foreign one is
# enabled. Emitted as its own RUN so it is legible in the build log and can be
# grepped for when triaging a 404.
_MULTIARCH_PIN_MARKER = "# AOS_MULTIARCH_PIN"
_MULTIARCH_PIN = f"""{_MULTIARCH_PIN_MARKER}
RUN set -e; \\
    host_arch="$(dpkg --print-architecture)"; \\
    for f in /etc/apt/sources.list.d/*.sources; do \\
      [ -f "$f" ] || continue; \\
      sed -i "/^Architectures:/d" "$f"; \\
      sed -i "/^Types:/a Architectures: $host_arch" "$f"; \\
    done
"""


def _pin_primary_apt_arch(text: str) -> str:
    """Constrain the primary apt sources to the host arch for multiarch builds.

    WHY THIS EXISTS
    ---------------
    A task that cross-compiles runs `dpkg --add-architecture arm64`, after which
    apt queries *every* configured source for *every* enabled architecture. The
    mirrors use the standard Debian layout, where `/ubuntu` carries amd64 only
    and arm64 lives in `/ubuntu-ports` -- verified by direct HEAD probe:

        ubuntu/dists/noble/main/binary-amd64/Packages       -> 200
        ubuntu/dists/noble/main/binary-arm64/Packages       -> 404
        ubuntu-ports/dists/noble/main/binary-arm64/Packages -> 200

    So the very next `apt-get update` 404s on the primary source and the build
    dies before installing anything. The suite name is NOT the problem: jammy
    under ubuntu-ports also returns 200, and 16 streams pin jammy while building
    successfully.

    The correct fix is to tell apt that the primary source serves only the host
    architecture, letting the ports source (which these Dockerfiles already add)
    answer for the foreign one. Applied here rather than in the assembler
    because REFINE edits merged Dockerfiles directly, so an assembly-time-only
    fix would be reverted by the next refine turn.

    WHY IT IS CONDITIONAL
    ---------------------
    Emitted only when the text enables a foreign architecture and does not
    already constrain the sources. That keeps output byte-identical for every
    non-multiarch stream and for those whose author already hand-rolled the fix,
    so `source_hash()` and already-published image tags do not move. It also
    converges what is currently 17 mutually inconsistent hand-written variants
    onto one, without rewriting the ones that work.

    Two portability details that the hand-rolled variants get wrong:
      * `dpkg --print-architecture` instead of a literal `amd64`, so the pin is
        still correct if the image is ever built on an arm64 host -- a literal
        would there exclude the *host* arch and break everything.
      * Only the deb822 files under sources.list.d/ are touched, never
        /etc/apt/sources.list. That asymmetry is deliberate and was measured:
        all 65 multiarch streams in tier-v2 use a noble (deb822) base, and 61
        of them already run `sed 's/^deb /deb [arch=amd64] /'
        /etc/apt/sources.list` themselves. On noble that sed is a guaranteed
        no-op -- the file is comments only, with the real entries in
        sources.list.d/ubuntu.sources -- which is exactly how the 404 slips
        through. But on a one-line-format base (jammy and older) the same sed
        *works*, and adding a second `arch=` on top of it yields
        `deb [arch=amd64] [arch=amd64] ...`, which apt rejects outright with
        "E: Malformed entry (URI parse)" -- verified by running the stacked seds
        in ubuntu:22.04. So handling sources.list here would break the one case
        that currently works while fixing nothing: zero streams have a one-line
        base with no pin of their own. The jammy strings that appear in these
        Dockerfiles are suite names inside the *added ports* source, not base
        images.
    """
    if _MULTIARCH_PIN_MARKER in text:
        return text
    match = _ADD_ARCH_RE.search(text)
    if not match:
        return text
    # Respect an existing hand-rolled pin: rewriting it would perturb streams
    # that already build, for no gain.
    if re.search(r"^[^\n]*Architectures:", text, re.MULTILINE):
        return text
    # Debian's /debian serves all architectures from one tree, so there is no
    # 404 to fix and no reason to touch the sources.
    from_lines = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
    if not any("ubuntu" in line.lower() for line in from_lines):
        return text

    lines = text.splitlines(keepends=True)
    insert_at = None
    for idx, line in enumerate(lines):
        if _ADD_ARCH_RE.search(line):
            # Walk back to the start of this RUN instruction: the add-architecture
            # call is usually mid-continuation, and splitting a `\`-continued
            # block would produce an unparsable Dockerfile.
            start = idx
            while start > 0 and lines[start - 1].rstrip("\n").endswith("\\"):
                start -= 1
            insert_at = start
            break
    if insert_at is None:
        return text
    return "".join(lines[:insert_at]) + _MULTIARCH_PIN + "".join(lines[insert_at:])


def normalize_domestic_install_sources(text: str) -> str:
    """Return an idempotently normalized merged Dockerfile."""
    normalized = re.sub(
        r"\n?# AOS_DOMESTIC_INSTALL_MIRRORS_V[12].*?"
        rf"{re.escape(DOMESTIC_MIRROR_END)}\n?",
        "",
        text,
        flags=re.DOTALL,
    )
    for source, target in INSTALL_SOURCE_REWRITES:
        normalized = normalized.replace(source, target)
    normalized = _apply_maven_mirror(normalized)

    # Runs before the mirror block is spliced in so the FROM-based noble check
    # sees the task's own base image rather than generated content. Conditional
    # on a noble base, so jammy streams are byte-identical and already-published
    # tags do not move.
    normalized = _rename_noble_packages(normalized)

    # Must run after the host rewrites above (which is what puts the ports URL
    # on our mirror in the first place) and before the config-block splice, so
    # the FROM inspected is the task's own base rather than generated content.
    normalized = _rebase_ports_suite(normalized)
    normalized = _retarget_debian_on_ubuntu(normalized)

    # Merged task Dockerfiles sometimes assume one distro-specific deb822 file
    # exists.  Base-image changes make that path optional; guard the rewrite so
    # dependency installation can continue with whichever apt source layout is
    # present instead of failing before apt-get runs.
    normalized = re.sub(
        r"RUN\s+sed\s+-i\s+(?P<expr>'[^'\n]*'|\"[^\"\n]*\")\s+"
        r"/etc/apt/sources\.list\.d/(?:debian|ubuntu)\.sources\s*\\\n\s*&&",
        lambda match: (
            "RUN (sed -i "
            + match.group("expr")
            + " /etc/apt/sources.list /etc/apt/sources.list.d/*.sources "
            + "2>/dev/null || true) \\\n    &&"
        ),
        normalized,
    )

    # Remove legacy nested GitHub proxy chains, then use one stable prefix for
    # release/archive downloads.  Plain git clone URLs are left unchanged and
    # continue to use the Docker proxy.
    normalized = normalized.replace(
        "https://ghfast.top/https://ghproxy.net/https://github.com/",
        "https://ghfast.top/https://github.com/",
    )
    normalized = normalized.replace(
        "https://ghproxy.net/https://github.com/",
        "https://ghfast.top/https://github.com/",
    )
    normalized = re.sub(
        r"(?<!ghfast\.top/)(?<!ghproxy\.net/)https://github\.com/"
        r"([^/\s\"']+/[^/\s\"']+)/(archive|releases/download)/",
        r"https://ghfast.top/https://github.com/\1/\2/",
        normalized,
    )
    normalized = re.sub(
        r"# AOS-Bench mirror profile: [^\n]+",
        "# AOS-Bench mirror profile: tencent-domestic-v2",
        normalized,
    )

    # Runs before the mirror block is spliced in, so the pin is positioned
    # relative to the task's own `dpkg --add-architecture` call rather than to
    # generated content. Must also run after the INSTALL_SOURCE_REWRITES above,
    # which are what point the sources at a mirror using the split
    # /ubuntu + /ubuntu-ports layout in the first place.
    normalized = _pin_primary_apt_arch(normalized)

    match = re.search(r"^FROM\s+[^\n]+$", normalized, re.MULTILINE)
    if not match:
        raise ValueError("Dockerfile has no FROM line")
    insertion = match.end()
    normalized = (
        normalized[:insertion]
        + "\n\n"
        + _domestic_config_block().rstrip()
        + normalized[insertion:]
    )
    return normalized
