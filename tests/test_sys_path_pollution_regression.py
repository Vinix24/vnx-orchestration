"""Regression: test_dashboard_feature's attention class must not leave scripts/ off sys.path.

On 889782a6 (PR #3045) the file pair below failed with
ModuleNotFoundError: heartbeat_ack_monitor, because
TestComputeTerminalAttention.setUpClass removed scripts/ and never restored it.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_dashboard_then_heartbeat_pair_passes():
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "tests/test_dashboard_feature.py",
            "tests/test_heartbeat_subprocess_cleanup.py",
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
