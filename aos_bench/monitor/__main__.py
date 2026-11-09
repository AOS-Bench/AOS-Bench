"""CLI entrypoint: ``python -m aos_bench.monitor``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from aos_bench.monitor.server import create_app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="aos_bench.monitor",
        description="AOSBench live Eval Monitor (kimi-cli, V0).",
    )
    p.add_argument(
        "--jobs-dir",
        required=True,
        type=Path,
        help="Path to the harbor jobs directory to watch (contains per-trial subdirs).",
    )
    p.add_argument("--port", type=int, default=4000, help="Dashboard port (default 4000).")
    p.add_argument("--bind", default="0.0.0.0", help="Address to bind (default 0.0.0.0).")
    p.add_argument(
        "--scan-interval", type=float, default=2.0,
        help="Seconds between jobs_dir scans for new trials (default 2).",
    )
    p.add_argument(
        "--tail-interval", type=float, default=1.5,
        help="Seconds between wire.jsonl polls per trial (default 1.5).",
    )
    p.add_argument(
        "--resource-interval", type=float, default=2.0,
        help="Seconds between host CPU/mem samples (default 2).",
    )
    p.add_argument("--log-level", default="info", help="uvicorn log level (default info).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.jobs_dir.exists():
        # Allow the dir to be created later; just warn.
        logging.warning("jobs_dir %s does not yet exist; will retry as it is created", args.jobs_dir)

    app = create_app(
        jobs_dir=args.jobs_dir,
        scan_interval_s=args.scan_interval,
        tail_interval_s=args.tail_interval,
        resource_interval_s=args.resource_interval,
    )
    print(f"AOSBench monitor: http://{args.bind}:{args.port} (jobs_dir={args.jobs_dir})", flush=True)
    # SSE clients hold their connection open forever (heartbeat loop), so
    # uvicorn's default graceful shutdown ("Waiting for connections to
    # close") hangs until every browser tab disconnects. Cap the drain
    # phase so Ctrl-C exits within a few seconds.
    uvicorn.run(
        app,
        host=args.bind,
        port=args.port,
        log_level=args.log_level,
        timeout_graceful_shutdown=3,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
