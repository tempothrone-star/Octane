#!/usr/bin/env python3
"""Compatibility entry point for scripts using the previous filename."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from octane.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
