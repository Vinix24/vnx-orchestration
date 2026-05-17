import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.mark.integration
def test_wheel_builds():
    """Verify wheel can be built with python -m build."""
    repo_root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmpdir:
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", tmpdir, str(repo_root)],
            capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, f"Build failed: {result.stderr}"
        wheels = list(Path(tmpdir).glob("vnx_orchestration-*.whl"))
        assert len(wheels) == 1, f"Expected 1 wheel, got {len(wheels)}"
