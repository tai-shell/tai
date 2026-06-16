"""CLI entry point for ``python -m tai_runtime.holo_on``."""

from __future__ import annotations

import sys

from tai_runtime.holo_on.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
