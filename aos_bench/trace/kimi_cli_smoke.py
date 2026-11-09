from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .kimi_cli import collect_kimi_cli_agent_trace, find_kimi_cli_latest_session_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run kimi-cli once and export AgentTrace.")
    parser.add_argument("--prompt", default=None, help="Prompt to send to kimi-cli")
    parser.add_argument(
        "--prompt-file",
        default=None,
        help="Optional file whose contents are used as the prompt",
    )
    parser.add_argument("--work-dir", default=".", help="Workspace passed to kimi-cli")
    parser.add_argument(
        "--kimi-project",
        default="aos_bench/harness/kimi-cli",
        help="Path to kimi-cli submodule for `uv run --project`",
    )
    parser.add_argument(
        "--share-dir",
        default=None,
        help="KIMI_SHARE_DIR. Defaults to a temporary directory.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
        help="OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY", ""),
        help="API key for the provider",
    )
    parser.add_argument(
        "--model",
        default="kimi-k2.5",
        help="Model name to configure in kimi-cli",
    )
    parser.add_argument(
        "--provider-type",
        default="openai_legacy",
        choices=["openai_legacy", "openai_responses"],
        help="kimi-cli provider type",
    )
    parser.add_argument(
        "--trace-json",
        default=None,
        help="Optional output path for collected AgentTrace JSON",
    )
    parser.add_argument(
        "--stdout-file",
        default=None,
        help="Optional file to store kimi-cli stdout",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> str:
    config = {
        "default_model": "default",
        "models": {
            "default": {
                "provider": "bench",
                "model": args.model,
                "max_context_size": 128000,
            }
        },
        "providers": {
            "bench": {
                "type": args.provider_type,
                "base_url": args.base_url,
                "api_key": args.api_key,
                "reasoning_key": "",
            }
        },
        "telemetry": False,
    }
    return json.dumps(config)


def main() -> int:
    args = parse_args()
    if not args.api_key:
        print("missing --api-key or OPENAI_API_KEY", file=sys.stderr)
        return 2
    if bool(args.prompt) == bool(args.prompt_file):
        print("pass exactly one of --prompt or --prompt-file", file=sys.stderr)
        return 2

    kimi_project = Path(args.kimi_project).resolve()
    work_dir = Path(args.work_dir).resolve()
    trace_json = Path(args.trace_json).resolve() if args.trace_json else None
    stdout_file = Path(args.stdout_file).resolve() if args.stdout_file else None
    prompt = (
        Path(args.prompt_file).resolve().read_text(encoding="utf-8")
        if args.prompt_file
        else str(args.prompt)
    )

    with tempfile.TemporaryDirectory(prefix="kimi-share-") as tmp_share:
        share_dir = Path(args.share_dir).resolve() if args.share_dir else Path(tmp_share)
        env = os.environ.copy()
        env["KIMI_SHARE_DIR"] = str(share_dir)

        cmd = [
            "uv",
            "run",
            "--project",
            str(kimi_project),
            "kimi",
            "--print",
            "--yolo",
            "--output-format",
            "text",
            "--final-message-only",
            "--work-dir",
            str(work_dir),
            "--prompt",
            prompt,
            "--config",
            build_config(args),
        ]
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            env=env,
            cwd=Path.cwd(),
        )
        if stdout_file is not None:
            stdout_file.write_text(result.stdout, encoding="utf-8")

        if result.returncode != 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            return result.returncode

        session_dir = find_kimi_cli_latest_session_dir(share_dir, work_dir=work_dir)
        trace = collect_kimi_cli_agent_trace(
            session_dir,
            model_name=args.model,
        )
        if trace_json is not None:
            trace_json.write_text(json.dumps(trace, indent=2), encoding="utf-8")
        else:
            print(json.dumps(trace, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
