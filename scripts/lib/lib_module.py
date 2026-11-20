"""Bind a bare module name to its ``scripts/lib`` module, whatever ``sys.path`` says.

Eight names exist twice: as a CLI wrapper or module in ``scripts/`` and as the real
module in ``scripts/lib/`` (``capture_audit``, ``cost_tracker``, ``crash_recovery_sweep``,
``intelligence_backfill``, ``orphan_sweep``, ``pattern_reattribution``,
``regression_attribution``, ``scout_effectiveness``). A plain ``import <name>`` returns
whichever one comes first on ``sys.path``, or whatever an earlier import left in
``sys.modules``. A lib module that needs a sibling of one of those names calls
:func:`load_lib_module` instead.

There is one instance per name: ``sys.modules[name]`` holds the lib module afterwards, so a
test that patches ``<name>.attr`` steers the same object the caller uses.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

_LIB_DIR = Path(__file__).resolve().parent


def _is_lib_module(module: object, path: Path) -> bool:
    file = getattr(module, "__file__", None)
    if not file:
        return False
    return os.path.realpath(file) == os.path.realpath(path)


def load_lib_module(name: str) -> ModuleType:
    """Return the ``scripts/lib/<name>.py`` module, registered as ``sys.modules[name]``.

    Raises ``ImportError`` when the lib file does not exist or does not load; the previous
    ``sys.modules`` entry is restored when loading fails.
    """
    if not name.isidentifier():
        raise ImportError(f"not a module name: {name!r}")
    path = _LIB_DIR / f"{name}.py"
    if not path.is_file():
        raise ImportError(f"no lib module {name!r} at {path}")

    current = sys.modules.get(name)
    if current is not None and _is_lib_module(current, path):
        return current

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before it executes: dataclasses and circular imports resolve the module here.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if current is not None:
            sys.modules[name] = current
        else:
            sys.modules.pop(name, None)
        raise
    return module
