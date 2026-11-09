"""Claude-engine env builder: config-dir isolation + OAuth credential seeding."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def base_env(extra_config_dir: Path) -> dict[str, str]:
    env = {
        "IS_SANDBOX": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    # Isolate each session's config dir by default (clean attribution + avoids
    # session-store collisions under API-key auth). Set CLAUDE_TEAM_SHARED_CONFIG=1
    # to instead reuse the default ~/.claude (e.g. local OAuth-authed testing).
    if os.environ.get("CLAUDE_TEAM_SHARED_CONFIG") != "1":
        env["CLAUDE_CONFIG_DIR"] = extra_config_dir.as_posix()
        # OAuth credential forwarding: when no ANTHROPIC_API_KEY is supplied,
        # the adapter uploads the user's Claude credentials into the container
        # and points CLAUDE_TEAM_OAUTH_CREDS at them. Seed a private copy into
        # each session's (writable) config dir so the SDK authenticates via the
        # existing Claude / Agent-SDK subscription quota.
        seed = os.environ.get("CLAUDE_TEAM_OAUTH_CREDS")
        if seed and not os.environ.get("ANTHROPIC_API_KEY") and Path(seed).exists():
            extra_config_dir.mkdir(parents=True, exist_ok=True)
            dst = extra_config_dir / ".credentials.json"
            try:
                shutil.copyfile(seed, dst)
                dst.chmod(0o600)
            except OSError:
                pass
    for k in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "no_proxy",
    ):
        v = os.environ.get(k)
        if v:
            env[k] = v
    return {k: v for k, v in env.items() if v}
