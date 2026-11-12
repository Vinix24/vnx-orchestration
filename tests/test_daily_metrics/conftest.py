"""Make ``daily_metrics`` resolve to the package, never the same-named CLI (OI-2696).

``scripts/daily_metrics.py`` (the CLI) and ``scripts/lib/daily_metrics/`` (the
package) share a name. ``scripts/lib/forge_gate_publisher.py`` -- imported by an
autouse fixture in ``tests/conftest.py`` -- prepends ``scripts/``, ``scripts/lib``
and ``scripts/forge`` to ``sys.path`` in that order while a test is being set up.
That leaves ``scripts/`` ahead of ``scripts/lib``, so a bare ``import daily_metrics``
loads the CLI script instead of the package. Running the whole directory still
went green only because an earlier test file fixed the path; run one file on its
own (or split it across xdist workers) and every daily-metrics test that imports
the package fails with no code defect behind it.

This conftest runs at collection time, before any fixture can reorder the path:
it moves ``<repo>/scripts/lib`` to ``sys.path[0]``, drops a ``daily_metrics``
module that was loaded from the CLI script (plus its submodules), and imports
the package while the package directory is first. That import cache entry keeps
the identity fixed for the rest of the session, because the autouse fixture
above prepends ``scripts/`` again during setup and would otherwise shadow the
package before the first test body runs. The repo root comes from this file's
own location, never from the cwd.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB_DIR = _REPO_ROOT / "scripts" / "lib"
_CLI_SCRIPT = _REPO_ROOT / "scripts" / "daily_metrics.py"


def _move_lib_to_front() -> None:
    """Put ``scripts/lib`` first, whether or not it is already on the path."""
    lib = str(_LIB_DIR)
    while lib in sys.path:
        sys.path.remove(lib)
    sys.path.insert(0, lib)


def _drop_cli_shadow() -> None:
    """Drop a ``daily_metrics`` loaded from ``scripts/daily_metrics.py`` (the CLI)."""
    module = sys.modules.get("daily_metrics")
    origin = getattr(module, "__file__", None)
    if not origin:
        return
    try:
        loaded_from_cli = Path(origin).resolve() == _CLI_SCRIPT.resolve()
    except OSError:
        return
    if not loaded_from_cli:
        return
    for name in [
        name
        for name in sys.modules
        if name == "daily_metrics" or name.startswith("daily_metrics.")
    ]:
        del sys.modules[name]


def _pin_package_identity() -> None:
    """Import the package now, so a later ``scripts/`` entry cannot shadow it."""
    importlib.import_module("daily_metrics")


_move_lib_to_front()
_drop_cli_shadow()
_pin_package_identity()
