from __future__ import annotations

import argparse
from pathlib import Path

from .agent_trace import write_agent_trace_svg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render AgentTrace JSON to SVG.")
    parser.add_argument("trace_json", help="Path to AgentTrace JSON.")
    parser.add_argument("output_svg", help="Path to output SVG.")
    parser.add_argument("--title", default=None, help="Optional SVG title.")
    parser.add_argument("--width", type=int, default=1440, help="SVG width in px.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    write_agent_trace_svg(
        Path(args.trace_json),
        Path(args.output_svg),
        title=args.title,
        width=args.width,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
