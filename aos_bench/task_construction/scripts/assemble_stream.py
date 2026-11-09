"""CLI: aos_bench.task_construction.scripts.assemble_stream

Convenience wrapper that calls into aos_bench.task_construction.assemble.assemble.
"""

from __future__ import annotations

import sys

from aos_bench.task_construction.assemble.assemble import main


if __name__ == "__main__":
    sys.exit(main())
