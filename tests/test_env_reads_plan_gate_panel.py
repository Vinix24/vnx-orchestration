"""config-een-oppervlak PR-2a: the plan-gate panel env switches are gone.

One literal-name grep over the tracked sources (outside tests/ and docs/) must find none of the
four deleted names, in any form: an env read, a lookup table, a help text. The fixed values that
replaced them are asserted next to it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import plan_gate_panel as pgp

DELETED_NAMES = (
    "VNX_PANEL_RETRY",
    "VNX_PLAN_GATE_MAX_DOC_CHARS",
    "VNX_PLAN_GATE_REEXTRACT_TIMEOUT",
    "VNX_PLAN_GATE_SEAT_TIMEOUT",
)


def test_no_tracked_source_names_a_deleted_variable():
    proc = subprocess.run(
        ["git", "grep", "-nwE", "|".join(DELETED_NAMES), "--",
         "scripts", "vnx_cli", "dashboard", "bin", "hooks"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    # exit 1 = no match (the wanted outcome); 0 = a hit; anything else = grep itself failed.
    assert proc.returncode == 1, f"rc={proc.returncode}\n{proc.stdout}{proc.stderr}"


@pytest.mark.parametrize("name", DELETED_NAMES)
def test_a_set_variable_changes_nothing(monkeypatch, name):
    monkeypatch.setenv(name, "7")
    assert pgp._panel_retry_count() == 1
    assert pgp._max_doc_chars() == 400_000
    assert pgp._reextraction_timeout() == 120
    assert pgp._seat_timeout() == 900


def test_fixed_values():
    assert pgp.PANEL_RETRY_COUNT == 1
    assert pgp.DEFAULT_MAX_DOC_CHARS == 400_000
    assert pgp.DEFAULT_REEXTRACTION_TIMEOUT_SECONDS == 120
    assert pgp.DEFAULT_SEAT_TIMEOUT_SECONDS == 900


def test_explicit_timeouts_still_win():
    assert pgp._seat_timeout(600) == 600
    assert pgp._reextraction_timeout(30) == 30
