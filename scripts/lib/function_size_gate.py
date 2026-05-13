"""Compatibility shim — canonical home is vnx_core.function_size_gate (Wave 2 Phase 1a).

Tries pip-installed vnx_core first; falls back to dist/vnx-orchestration on sys.path.
"""
from __future__ import annotations

try:
    from vnx_core.function_size_gate import *  # noqa: F401, F403
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    _pkg = Path(__file__).resolve().parents[2] / 'dist' / 'vnx-orchestration'
    if _pkg.is_dir() and str(_pkg) not in sys.path:
        sys.path.insert(0, str(_pkg))
    from vnx_core.function_size_gate import *  # noqa: F401, F403
