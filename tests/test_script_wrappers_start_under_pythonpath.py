"""The sweep and scout wrappers share a name with their scripts/lib module (OI-2593)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WRAPPERS = ["orphan_sweep.py", "crash_recovery_sweep.py", "scout_effectiveness.py"]


def _run(wrapper: str, tmp_path: Path, with_pythonpath: bool):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if with_pythonpath:
        env["PYTHONPATH"] = str(REPO / "scripts" / "lib")
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / wrapper), "--help"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("with_pythonpath", [True, False], ids=["pythonpath", "no-pythonpath"])
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_wrapper_help_starts(wrapper, with_pythonpath, tmp_path):
    result = _run(wrapper, tmp_path, with_pythonpath)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
