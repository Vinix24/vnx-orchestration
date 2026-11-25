"""The runtime-core rollback script and runbook are deleted (config-een-oppervlak PR-Q1a)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NAMES = ("rollback_runtime_core", "RUNTIME_CORE_ROLLBACK")


def test_rollback_script_does_not_exist() -> None:
    assert not (REPO / "scripts" / "rollback_runtime_core.py").exists()


def test_rollback_runbook_does_not_exist() -> None:
    assert not (REPO / "docs" / "operations" / "RUNTIME_CORE_ROLLBACK.md").exists()


def test_no_tracked_file_names_the_rollback() -> None:
    args = ["git", "-C", str(REPO), "grep", "-l", "-I"]
    for name in NAMES:
        args += ["-e", name]
    args += [
        "--",
        ".",
        ":(exclude)docs/_archive",
        ":(exclude)changelog.d",
        # the release cut folds the fragments into CHANGELOG.md, a historical record
        ":(exclude)CHANGELOG.md",
        ":(exclude)claudedocs",
        # The fixture is the file list of historical sha a052c0e38.
        ":(exclude)tests/fixtures/docs_layers",
        ":(exclude)tests/test_runtime_core_rollback_removed.py",
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        pytest.fail(f"git grep failed ({proc.returncode}): {proc.stderr}")
    assert proc.stdout.split() == [], f"files still name the rollback: {proc.stdout}"
