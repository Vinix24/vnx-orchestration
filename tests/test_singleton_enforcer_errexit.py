#!/usr/bin/env python3
"""`enforce_singleton` must survive errexit on the normal contention path.

When vnx_paths.sh stopped clobbering callers' `set -e`, the daemons/watchers that
source `singleton_enforcer.sh` began running `enforce_singleton` under errexit.
`flock -n -x -E 75` exits 75 on lock contention — the *normal* "another instance
is already running" outcome. An unguarded flock call then aborted the caller at
rc 75 before the `case` that turns that into a clean `exit 0`.

The contender here re-asserts `set -euo pipefail` right after the source so the
test isolates `enforce_singleton`'s own handling (the resolver's preservation is
covered separately in test_vnx_paths_shellopts.py). The test fails on the old
unguarded flock and passes once the status is captured with `|| rc=$?`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
ENFORCER = REPO_ROOT / "scripts" / "singleton_enforcer.sh"
LOCK_NAME = "vnx_bench_singleton_errexit"

pytestmark = pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) not installed")


def _env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    data = tmp_path / "data"
    (data / "state").mkdir(parents=True, exist_ok=True)
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "VNX_DATA_DIR": str(data),
        "VNX_DATA_DIR_EXPLICIT": "1",
    }
    return env


def test_contention_exits_zero_without_aborting_the_caller(tmp_path: Path):
    env = _env(tmp_path)
    home = Path(env["HOME"])
    ready = home / "holder_ready"

    holder_script = f'''
set -euo pipefail
source "{ENFORCER}"
enforce_singleton "{LOCK_NAME}"
: > "{ready}"
sleep 10
'''
    contender_script = f'''
set -euo pipefail
source "{ENFORCER}"
# Force errexit back on so this test isolates enforce_singleton: on the base
# code the resolver had just turned it off, which would mask the flock abort.
set -euo pipefail
enforce_singleton "{LOCK_NAME}"
echo CONTENDER_ACQUIRED
'''

    holder = subprocess.Popen(
        ["/bin/bash", "-c", holder_script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 15
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "holder never acquired the singleton lock"

        result = subprocess.run(
            ["/bin/bash", "-c", contender_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()

    assert result.returncode == 0, (
        f"contender should exit cleanly on contention, got {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert "Another instance" in result.stdout
    assert "CONTENDER_ACQUIRED" not in result.stdout
