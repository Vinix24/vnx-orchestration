"""The hook-style start of build_t0_state must still import pr_queue_state (OI-2997)."""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PROBE = """
import runpy, sys
mod = runpy.run_path(sys.argv[1], run_name="probe")
print("PQS_NONE=%s" % (mod["_build_pqs"] is None))
print("DM=%s" % __import__("daily_metrics").__file__)
print("LIBFIRST=%s" % (sys.path[0] == sys.argv[2]))
"""


def test_hook_style_start_imports_pr_queue_state(tmp_path):
    lib = str(REPO / "scripts" / "lib")
    data = tmp_path / ".vnx-data"
    (data / "state").mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VNX_", "PROJECT_"))}
    env.update(
        PYTHONPATH=lib,
        VNX_DATA_DIR=str(data),
        VNX_STATE_DIR=str(data / "state"),
        VNX_DISPATCH_DIR=str(data / "dispatches"),
        PROJECT_ROOT=str(tmp_path),
        VNX_DATA_DIR_EXPLICIT="1",
    )
    # Script start: sys.path[0] is scripts/, scripts/lib comes in via PYTHONPATH behind it.
    code = "import sys; sys.path.insert(0, %r)\n%s" % (str(REPO / "scripts"), PROBE)
    r = subprocess.run(
        [sys.executable, "-c", code, str(REPO / "scripts" / "build_t0_state.py"), lib],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "PQS_NONE=False" in r.stdout, r.stdout + r.stderr
    assert "scripts/lib/daily_metrics/__init__.py" in r.stdout
    assert "LIBFIRST=True" in r.stdout
