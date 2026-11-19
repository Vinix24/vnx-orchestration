"""Two tests that failed only after a new test file reshuffled the Profile A shards.

Both are order dependence, reproduced here as the smallest pair in a subprocess
(like tests/test_sys_path_pollution_regression.py):

1. ``test_ndjson_hash_chain.py::test_tail_window_is_the_closer_window`` inserted
   ``scripts/`` into ``sys.path`` and never removed it, so a later
   ``import crash_recovery_sweep`` found the CLI ``scripts/crash_recovery_sweep.py``
   (which imports the library module of the same name) and failed with a circular
   import in ``test_orphan_sweep_worktree_pass.py``.

The second failure (``build/`` not empty) is covered in
``test_sessionstart_hook_beacon_health.py::TestRmdirNamingForeignEntries``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ORPHAN = "tests/test_orphan_sweep_worktree_pass.py::test_non_directory_and_foreign_name_entries_are_ignored"
POLLUTER = "tests/test_ndjson_hash_chain.py::test_tail_window_is_the_closer_window"


def _pytest(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["VNX_HOME"] = str(REPO)
    env["VNX_DATA_DIR"] = str(tmp_path / "data")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )


def test_ndjson_tail_window_test_leaves_sys_path_alone(tmp_path):
    """The polluter followed by the victim passes (failed on b06ae8de3)."""
    result = _pytest([POLLUTER, ORPHAN], tmp_path)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-1000:]
    assert "2 passed" in result.stdout


def test_orphan_sweep_test_survives_the_cli_cached_under_the_bare_name(tmp_path):
    """Whatever cached the CLI as ``crash_recovery_sweep``, the orphan test resolves the library."""
    polluter = tmp_path / "test_zz_pollute.py"
    polluter.write_text(textwrap.dedent(f"""
        import sys
        import types

        def test_leaves_the_cli_cached_and_scripts_ahead_of_lib():
            sys.path.insert(0, {str(REPO / "scripts")!r})
            cli = types.ModuleType("crash_recovery_sweep")
            cli.__file__ = {str(REPO / "scripts" / "crash_recovery_sweep.py")!r}
            sys.modules["crash_recovery_sweep"] = cli
    """))
    result = _pytest([str(polluter), ORPHAN], tmp_path)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-1000:]
    assert "2 passed" in result.stdout
