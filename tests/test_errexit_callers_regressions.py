"""OI-1904 follow-up: callers must not rely on vnx_paths.sh turning errexit off.

Runs the real function text from the production script under
``set -euo pipefail`` (the options its caller really has now).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "queue_popup_watcher.sh"


def _count_files(directory: Path) -> subprocess.CompletedProcess:
    cmd = (
        "set -euo pipefail; "
        f"eval \"$(sed -n '/^count_files()/,/^}}/p' '{_SCRIPT}')\"; "
        f"n=$(count_files '{directory}'); echo \"count=$n\""
    )
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)


def test_count_files_on_missing_dir_does_not_abort(tmp_path):
    result = _count_files(tmp_path / "does-not-exist")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "count=0"


def test_count_files_counts_markdown(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "b.md").write_text("x")
    (tmp_path / "c.txt").write_text("x")
    result = _count_files(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "count=2"
