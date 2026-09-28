#!/usr/bin/env python3
"""CLI entry point for pattern reattribution (D3b, learning-loop-sluiten).

Thin wrapper over ``scripts/lib/pattern_reattribution.py`` — see that module's
docstring for the decision rule and safety properties. This file only adds
the ``scripts/lib`` bootstrap so the module can be invoked directly, mirroring
``scripts/attribute_rework.py``.

Usage:
    python3 scripts/pattern_reattribution.py --dry-run [--db PATH] [--report PATH]
    python3 scripts/pattern_reattribution.py --apply --db PATH [--report PATH]
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(LIB_DIR))

from pattern_reattribution import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
