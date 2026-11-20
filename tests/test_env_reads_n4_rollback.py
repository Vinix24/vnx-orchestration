"""The state-simplification rollback cluster and the last script names are gone (config-een-oppervlak N4).

The names are read nowhere in the six files that carried them (the three security
files are covered by tests/test_env_reads_n4b_security.py), and no ``_env_flag``
copy is left in the four Python files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

DELETED = (
    "VNX_STATE_DUAL_WRITE_LEGACY",
    "VNX_STATE_SIMPLIFICATION_ROLLBACK",
    "VNX_CENTRAL_ROOT",
    "VNX_T0_ROTATE_BUSY_PATTERN",
    "VNX_T0_ROTATE_FOLDER_TIMEOUT",
    "VNX_T0_ROTATE_FOOTER_PATTERN",
    "VNX_T0_ROTATE_GOAL_ACTIVE_PATTERN",
    "VNX_T0_ROTATE_START_DIR",
)
FILES = (
    "scripts/gather_intelligence.py",
    "scripts/generate_t0_recommendations.py",
    "scripts/intelligence_daemon.py",
    "scripts/open_items_manager.py",
    "scripts/t0_rotate_spawn.sh",
    "scripts/vnx_contamination_check.sh",
)
PY_FILES = tuple(f for f in FILES if f.endswith(".py"))
SH_FILES = tuple(f for f in FILES if f.endswith(".sh"))


@pytest.mark.parametrize("rel", FILES)
def test_no_deleted_name_is_left_in_the_file(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    left = [name for name in DELETED if name in text]
    assert left == []


@pytest.mark.parametrize("rel", PY_FILES)
def test_no_env_flag_copy_is_left(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert "_env_flag" not in text
    assert "_rollback_mode" not in text


def test_the_rotation_defaults_are_unchanged_constants():
    text = (REPO / "scripts/t0_rotate_spawn.sh").read_text(encoding="utf-8")
    assert "\nFOLDER_TIMEOUT=5\n" in text
    assert '\nFOOTER_PATTERN="auto mode"\n' in text
    assert '\nBUSY_PATTERN="esc to interrupt|… \\([0-9]+[hms]"\n' in text
    assert '\nGOAL_ACTIVE_PATTERN="/goal active"\n' in text


@pytest.mark.parametrize("rel", SH_FILES)
def test_shell_files_parse(rel):
    assert subprocess.run(["bash", "-n", str(REPO / rel)]).returncode == 0
