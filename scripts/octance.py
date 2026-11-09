#!/usr/bin/env python3
"""Repository-local entry point for the self-contained Octance skill CLI."""
from pathlib import Path
import sys

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "octance" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
from octance.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
