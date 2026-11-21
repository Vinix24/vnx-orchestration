"""The rollback names read by the three security files are gone (config-een-oppervlak N4, part b).

The six names are read nowhere in the three files, and no ``_env_flag`` copy is left in
``scripts/pr_queue_manager.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

DELETED = (
    "VNX_STATE_DUAL_WRITE_LEGACY",
    "VNX_STATE_SIMPLIFICATION_ROLLBACK",
    "VNX_PR_QUEUE_DUAL_WRITE_LEGACY",
    "VNX_DOCTOR_PACKAGE_CHECK",
    "VNX_APPEND_RECEIPT_SCRIPT",
    "VNX_ROTATE_SCRIPT",
)
FILES = (
    "hooks/vnx_handover_detector.sh",
    "scripts/commands/doctor.sh",
    "scripts/pr_queue_manager.py",
)
SH_FILES = tuple(f for f in FILES if f.endswith(".sh"))


@pytest.mark.parametrize("rel", FILES)
def test_no_deleted_name_is_left_in_the_file(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    left = [name for name in DELETED if name in text]
    assert left == []


def test_no_env_flag_copy_is_left():
    text = (REPO / "scripts/pr_queue_manager.py").read_text(encoding="utf-8")
    assert "_env_flag" not in text
    assert "_rollback_mode" not in text


@pytest.mark.parametrize("rel", SH_FILES)
def test_shell_files_parse(rel):
    assert subprocess.run(["bash", "-n", str(REPO / rel)]).returncode == 0
