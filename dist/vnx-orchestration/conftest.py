"""pytest conftest for vnx-orchestration package tests.

Adds the package root to sys.path so vnx_core is importable without pip install.
"""
from __future__ import annotations

import sys
from pathlib import Path

_pkg_root = Path(__file__).resolve().parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))
