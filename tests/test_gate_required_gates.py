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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SH = REPO_ROOT / "scripts" / "commands" / "gate.sh"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
from config_registry import CONFIG_REGISTRY


@pytest.fixture
def env(tmp_path):
    """A fake VNX_HOME with an .vnx/ dir and an empty project root, enough for
    _g_required_gates to resolve the enforcement config."""
    fake_home = tmp_path / "vnx-home"
    (fake_home / ".vnx").mkdir(parents=True)
    # gate.sh reads the stack through $VNX_HOME/scripts/gate_profile_cli.py.
    (fake_home / "scripts").symlink_to(REPO_ROOT / "scripts")
    project = tmp_path / "project"
    project.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    return {"fake_home": fake_home, "project": project, "state": state}


_AMBIENT_KEYS = (
    "VNX_GOVERNANCE_PROFILE", "VNX_DEFAULT_REVIEW_STACK", "VNX_CI_GATE_REQUIRED", "VNX_PROJECT_ID",
    "VNX_OVERRIDE_GOVERNANCE_PROFILE", "VNX_OVERRIDE_DEFAULT_REVIEW_STACK", "VNX_OVERRIDE_CI_GATE_REQUIRED",
)


def _clean_environ(env, extra=None) -> dict:
    """The process environment without any ambient profile or stack setting, state dir in tmp."""
    import os

    clean = {k: v for k, v in os.environ.items() if k not in _AMBIENT_KEYS}
    clean.update({"VNX_STATE_DIR": str(env["state"]), "VNX_DATA_DIR": str(env["state"].parent)})
    clean.update(extra or {})
    return clean


def _write_enforcement_config(env, body: str) -> Path:
    cfg = env["fake_home"] / ".vnx" / "governance_enforcement.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _run_required_gates(env, extra_env=None) -> subprocess.CompletedProcess:
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
        capture_output=True, text=True, timeout=30, env=_clean_environ(env, extra_env),
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


def _default_review_stack() -> list[str]:
    raw = CONFIG_REGISTRY["VNX_DEFAULT_REVIEW_STACK"].default
    return [item.strip() for item in raw.split(",") if item.strip()]


def test_no_config_falls_back_to_the_default_review_stack(env):
    """Without an enforcement config both standing seats run, not codex alone."""
    result = _run_required_gates(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split(",") == _default_review_stack()


def test_config_naming_no_required_gate_falls_back_to_the_default_review_stack(env):
    _write_enforcement_config(
        env,
        "version: 1\nmode: standard\nchecks:\n  max_pr_lines:\n    level: 1\n",
    )
    result = _run_required_gates(env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split(",") == _default_review_stack()


def test_fallback_stack_holds_no_retired_gate(env):
    result = _run_required_gates(env)
    assert "gemini_review" not in result.stdout


CONFIG_CODEX_AND_KIMI = """\
version: 1
mode: standard
checks:
  codex_gate_required:
    level: 2
  kimi_gate_required:
    level: 2
"""


def test_kimi_gate_required_level_2_requests_both_seats(env):
    """kimi_gate_required is a peer of codex_gate_required: at level >= 2 both
    seats are requested (operator decision 2026-09-27)."""
    _write_enforcement_config(env, CONFIG_CODEX_AND_KIMI)
    result = _run_required_gates(env)
    assert result.returncode == 0, result.stderr
    gates = [g for g in result.stdout.strip().split(",") if g]
    assert set(gates) == {"codex_gate", "kimi_gate"}


CONFIG_KIMI_OFF = """\
version: 1
mode: standard
checks:
  codex_gate_required:
    level: 2
  kimi_gate_required:
    level: 0
"""


def test_kimi_gate_required_at_level_0_requests_codex_only(env):
    """A project that dials kimi_gate_required off (level 0) keeps codex_gate
    as the sole required seat, not the default stack (which would re-add
    kimi)."""
    _write_enforcement_config(env, CONFIG_KIMI_OFF)
    result = _run_required_gates(env)
    assert result.returncode == 0, result.stderr
    gates = [g for g in result.stdout.strip().split(",") if g]
    assert gates == ["codex_gate"]


# The dials measured in vnx-dev's .vnx/governance_enforcement.yaml (codex 2, kimi 2, ci 3),
# copied here so the lock does not follow the live file.
CONFIG_VNX_DEV_DIALS = """\
version: 1
mode: standard
checks:
  gate_before_next_feature:
    level: 3
  codex_gate_required:
    level: 2
    scope: [scripts/, dashboard/, .github/]
  kimi_gate_required:
    level: 2
  ci_green_required:
    level: 3
  max_pr_lines:
    level: 1
    threshold: 300
"""


def _gates(result) -> list[str]:
    assert result.returncode == 0, result.stderr
    return [g for g in result.stdout.strip().split(",") if g]


def test_heavy_profile_without_enforcement_config_requires_three_seats(env):
    result = _run_required_gates(env, {"VNX_GOVERNANCE_PROFILE": "heavy"})
    assert _gates(result) == ["codex_gate", "deepseek_gate", "claude_gate"]


def test_c6_lock_vnx_dev_dials_without_a_profile_keep_todays_stack(env):
    _write_enforcement_config(env, CONFIG_VNX_DEV_DIALS)
    assert _gates(_run_required_gates(env)) == ["codex_gate", "kimi_gate", "ci_gate"]


def test_level_1_dial_makes_a_seat_advisory(env):
    _write_enforcement_config(
        env, "version: 1\nchecks:\n  codex_gate_required:\n    level: 2\n  kimi_gate_required:\n    level: 1\n",
    )
    assert _gates(_run_required_gates(env)) == ["codex_gate"]


def test_level_0_dial_removes_a_seat_of_the_heavy_stack(env):
    _write_enforcement_config(
        env, "version: 1\nchecks:\n  deepseek_gate_required:\n    level: 0  # off\n",
    )
    result = _run_required_gates(env, {"VNX_GOVERNANCE_PROFILE": "heavy"})
    assert _gates(result) == ["codex_gate", "claude_gate"]


def test_ci_green_required_at_level_2_alone_requires_ci_gate_alone(env):
    """The config defines the stack (the answer of the former gate.sh): a lone ci dial is ci_gate."""
    _write_enforcement_config(env, "version: 1\nchecks:\n  ci_green_required:\n    level: 2\n")
    assert _gates(_run_required_gates(env)) == ["ci_gate"]


def test_ci_gate_stays_out_at_level_1_and_joins_on_the_env_flag(env):
    _write_enforcement_config(env, "version: 1\nchecks:\n  ci_green_required:\n    level: 1\n")
    assert _gates(_run_required_gates(env)) == ["codex_gate", "deepseek_gate"]
    assert _gates(_run_required_gates(env, {"VNX_CI_GATE_REQUIRED": "true"}))[-1] == "ci_gate"


def test_a_failing_cli_stops_gate_sh_with_the_reason_and_prints_no_stack(env):
    result = _run_required_gates(env, {"VNX_GOVERNANCE_PROFILE": "zwaar"})
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "'zwaar'" in result.stderr and "refused" in result.stderr
    assert "codex_gate" not in result.stdout


def test_a_malformed_dial_level_stops_gate_sh_instead_of_guessing(env):
    _write_enforcement_config(env, "version: 1\nchecks:\n  codex_gate_required:\n    level: hoog\n")
    result = _run_required_gates(env)
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "level='hoog'" in result.stderr


def test_a_dial_without_a_level_stops_gate_sh(env):
    _write_enforcement_config(env, "version: 1\nchecks:\n  kimi_gate_required:\n    description: x\n")
    result = _run_required_gates(env)
    assert result.returncode != 0 and result.stdout.strip() == ""
    assert "no level" in result.stderr


def test_gate_sh_holds_no_literal_stack_and_no_inline_yaml_parser():
    text = GATE_SH.read_text(encoding="utf-8")
    assert "default_stack" not in text
    assert "gate_map" not in text


# --- the four edge cases: the answer of the former bash function, run next to the new CLI ----
#
# 4b5c7dfb6 is origin/main before P7 (the parent of the commit that replaced the bash
# function). The old `_g_required_gates` is sourced from that commit, so the comparison does
# not depend on what main holds later.
PRE_P7_COMMIT = "4b5c7dfb6"


def _run_old_required_gates(env) -> subprocess.CompletedProcess:
    shown = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{PRE_P7_COMMIT}:scripts/commands/gate.sh"],
        capture_output=True, text=True, timeout=30,
    )
    assert shown.returncode == 0, shown.stderr
    old_sh = env["fake_home"].parent / "gate_pre_p7.sh"
    old_sh.write_text(shown.stdout, encoding="utf-8")
    driver = env["fake_home"].parent / "run_old_required_gates.sh"
    driver.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
log() {{ printf '%s\\n' "$*"; }}
err() {{ printf 'ERROR: %s\\n' "$*" >&2; }}
export VNX_HOME={env['fake_home']}
export PROJECT_ROOT={env['project']}
source {old_sh}
_g_required_gates
""",
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(driver)], capture_output=True, text=True, timeout=30, env=_clean_environ(env),
    )


EDGE_CASES = {
    "a_only_codex_2": ("version: 1\nchecks:\n  codex_gate_required:\n    level: 2\n", ["codex_gate"]),
    "b_every_dial_level_1": (
        "version: 1\nchecks:\n  codex_gate_required:\n    level: 1\n  kimi_gate_required:\n    level: 1\n"
        "  ci_green_required:\n    level: 1\n",
        ["codex_gate", "kimi_gate"],
    ),
    "c_no_checks_block": ("version: 1\nmode: standard\n", ["codex_gate", "kimi_gate"]),
    "d_no_config_file": (None, ["codex_gate", "kimi_gate"]),
}


@pytest.mark.parametrize("case", sorted(EDGE_CASES))
def test_required_stack_equals_the_former_bash_function(env, case):
    body, expected = EDGE_CASES[case]
    if body is not None:
        _write_enforcement_config(env, body)
    old = _run_old_required_gates(env)
    new = _run_required_gates(env, {"VNX_DEFAULT_REVIEW_STACK": "codex_gate,kimi_gate"})  # the old literal, not the default since 2026-11-11
    assert old.returncode == 0, old.stderr
    assert _gates(new) == [g for g in old.stdout.strip().split(",") if g] == expected


def test_vnx_dev_dials_equal_the_former_bash_function_byte_for_byte(env):
    _write_enforcement_config(env, CONFIG_VNX_DEV_DIALS)
    old = _run_old_required_gates(env)
    new = _run_required_gates(env)
    # The old function ended with print(): a newline the $(...) capture in gate.sh dropped.
    assert old.stdout.strip() == new.stdout == "codex_gate,kimi_gate,ci_gate"


def test_enforcement_dials_are_derived_from_the_registry_rows():
    import gate_lane_rows
    import gate_profile

    for row in gate_lane_rows.GATE_LANE_ROWS:
        assert gate_profile.ENFORCEMENT_DIALS[f"{row.gate}_required"] == row.gate
    assert gate_profile.ENFORCEMENT_DIALS["codex_gate_required"] == "codex_gate"
    assert gate_profile.ENFORCEMENT_DIALS["ci_green_required"] == "ci_gate"
