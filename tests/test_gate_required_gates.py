#!/usr/bin/env python3
"""OI-1265 — `_g_required_gates` must map enforcement check names to gate names
that review_gate_manager actually knows.

The bug: gate.sh mapped 'ci_green_required' -> 'ci', but review_gate_manager
builds its stack with 'ci_gate' (scripts/review_gate_manager.py:38). A
ci_green_required requirement therefore resolved to a gate nobody knows. The
unknown gate was then pre-booked with status 'blocked' (unknown_review_gate)
and — because the required-failure count only checked not_executable /
not_configured — slipped past enforcement entirely.

These tests source gate.sh and call `_g_required_gates` against a minimal
governance_enforcement.yaml, proving the emitted gate name is the one the
manager knows (ci_gate), never the orphan name (ci).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SH = REPO_ROOT / "scripts" / "commands" / "gate.sh"


@pytest.fixture
def env(tmp_path):
    """A fake VNX_HOME with an .vnx/ dir and an empty project root, enough for
    _g_required_gates to resolve the enforcement config."""
    fake_home = tmp_path / "vnx-home"
    (fake_home / ".vnx").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir(parents=True)
    return {"fake_home": fake_home, "project": project}


def _write_enforcement_config(env, body: str) -> Path:
    cfg = env["fake_home"] / ".vnx" / "governance_enforcement.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _run_required_gates(env) -> subprocess.CompletedProcess:
    """Source gate.sh and run `_g_required_gates`, printing the comma-separated
    gate stack to stdout."""
    driver = env["fake_home"].parent / "run_required_gates.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
log() {{ printf '%s\\n' "$*"; }}
err() {{ printf 'ERROR: %s\\n' "$*" >&2; }}
export VNX_HOME={env['fake_home']}
export PROJECT_ROOT={env['project']}
source {GATE_SH}
_g_required_gates
""",
        encoding="utf-8",
    )
    driver.chmod(0o755)
    return subprocess.run(
        ["bash", str(driver)],
        capture_output=True, text=True, timeout=30,
    )


CONFIG_WITH_CI = """\
version: 1
mode: standard
checks:
  codex_gate_required:
    level: 2
  ci_green_required:
    level: 3
"""


def test_ci_green_required_maps_to_gate_review_gate_manager_knows(env):
    """`_g_required_gates` must emit `ci_gate`, never the orphan `ci`."""
    _write_enforcement_config(env, CONFIG_WITH_CI)
    result = _run_required_gates(env)
    assert result.returncode == 0, result.stderr
    gates = [g for g in result.stdout.strip().split(",") if g]
    assert "ci_gate" in gates, result.stdout
    assert "ci" not in gates, f"gate name 'ci' is not known to review_gate_manager: {result.stdout}"


def test_codex_required_still_maps_to_codex_gate(env):
    """codex_gate_required keeps mapping to codex_gate (no regression)."""
    _write_enforcement_config(env, CONFIG_WITH_CI)
    result = _run_required_gates(env)
    assert result.returncode == 0, result.stderr
    gates = [g for g in result.stdout.strip().split(",") if g]
    assert "codex_gate" in gates, result.stdout
