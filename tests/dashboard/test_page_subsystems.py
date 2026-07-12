#!/usr/bin/env python3
"""Runs the React Testing Library coverage for the subsystem cockpit tile
(framework-status-audit-and-cockpit PR-4).

The actual assertions live in the Jest/RTL suite at
``dashboard/token-dashboard/__tests__/observability-page.test.tsx`` (this repo's existing
convention for `.tsx` component tests — see ``dashboard/token-dashboard/jest.config.js``'s
``testMatch``). This module is a thin pytest wrapper so the cockpit tile's frontend coverage is
collectible via plain ``pytest``, matching the other Python test entrypoints in this repo.

Requires the token-dashboard Node toolchain (``npm ci`` run in ``dashboard/token-dashboard``) to
execute the real assertions. When ``node_modules`` is not installed (e.g. a fresh worktree that
has not run ``npm ci``), the test skips with an explicit reason instead of silently reporting a
false pass -- run ``cd dashboard/token-dashboard && npm ci && npm test`` to exercise it directly.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
TOKEN_DASHBOARD = REPO / "dashboard" / "token-dashboard"
TEST_FILE = "__tests__/observability-page.test.tsx"


def _npm_toolchain_ready() -> bool:
    if shutil.which("npx") is None:
        return False
    return (TOKEN_DASHBOARD / "node_modules" / ".bin" / "jest").exists()


@pytest.mark.skipif(
    not _npm_toolchain_ready(),
    reason=(
        "token-dashboard Node toolchain not installed (no node_modules/.bin/jest) -- "
        "run `cd dashboard/token-dashboard && npm ci` first"
    ),
)
def test_subsystem_cockpit_tile_rtl_suite_passes():
    """The RTL suite in observability-page.test.tsx covers the subsystem cockpit tile: it renders
    every subsystem row (incl. `governance-enforcement-stack`), status/health badges, and its own
    loading/error states independent of the observability data hook."""
    result = subprocess.run(
        ["npx", "jest", TEST_FILE, "--silent"],
        cwd=str(TOKEN_DASHBOARD),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"jest {TEST_FILE} failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
