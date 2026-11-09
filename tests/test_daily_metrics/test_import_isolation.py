"""OI-2696: every daily-metrics test file must pass when it runs on its own.

``scripts/daily_metrics.py`` (the CLI) and ``scripts/lib/daily_metrics/`` (the
package) share a name, and a fixture in ``tests/conftest.py`` prepends
``scripts/`` ahead of ``scripts/lib`` during test setup. Before this guard the
directory was green only because an earlier file happened to fix the path: an
xdist split or a targeted single-file run went red with no code defect behind
it. Each test here runs one file in its own interpreter and requires exit 0.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_DIR = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).resolve()
CONFTEST = TEST_DIR / "conftest.py"
PACKAGE_INIT = REPO_ROOT / "scripts" / "lib" / "daily_metrics" / "__init__.py"
CLI_SCRIPT = REPO_ROOT / "scripts" / "daily_metrics.py"

# Same isolation knobs the CI/worker shell may carry; a file must pass without
# them, exactly as the red measurement was taken.
_UNSET_ENV = ("VNX_PROJECT_ID", "VNX_DISPATCH_PATHS", "VNX_ENFORCE_WORKER_PERMISSIONS")
_PER_FILE_TIMEOUT_SECONDS = 120


def _sibling_test_files() -> list[Path]:
    return sorted(
        path for path in TEST_DIR.glob("test_*.py")
        if path.resolve() != THIS_FILE
    )


def _subprocess_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in _UNSET_ENV}


@pytest.mark.parametrize(
    "test_file", _sibling_test_files(), ids=lambda path: path.name,
)
def test_each_file_passes_on_its_own(test_file: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file),
            "-q", "-p", "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=_PER_FILE_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, (
        f"{test_file.name} fails when run on its own "
        f"(exit {completed.returncode}, expected 0):\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )


def test_conftest_replaces_a_cli_module_loaded_as_daily_metrics() -> None:
    """The drop branch: a cached CLI shadow must be removed, not merely preempted.

    In a clean session nothing imports ``scripts/daily_metrics.py`` as
    ``daily_metrics`` before this conftest runs, so the branch that drops such a
    module is not reached by the per-file runs above. This spawns an interpreter
    that plants the CLI (and one submodule) under the package name, loads the
    conftest, and requires the package to be in its place afterwards.
    """
    script = f"""
import importlib.util
import sys
import types
from pathlib import Path

conftest_path = Path({str(CONFTEST)!r})
package_init = Path({str(PACKAGE_INIT)!r})
cli_path = Path({str(CLI_SCRIPT)!r})

shadow = types.ModuleType("daily_metrics")
shadow.__file__ = str(cli_path)
submodule = types.ModuleType("daily_metrics.collect")
submodule.__file__ = str(cli_path)
sys.modules["daily_metrics"] = shadow
sys.modules["daily_metrics.collect"] = submodule

spec = importlib.util.spec_from_file_location("daily_metrics_conftest_under_test", conftest_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

resolved = Path(sys.modules["daily_metrics"].__file__).resolve()
assert resolved == package_init.resolve(), f"daily_metrics resolved to {{resolved}}"
assert "daily_metrics.collect" not in sys.modules, "CLI submodule survived the drop"
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=_PER_FILE_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0, (
        f"conftest did not replace the CLI shadow (exit {completed.returncode}):\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}"
    )
