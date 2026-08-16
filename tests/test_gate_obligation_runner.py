"""tests/test_gate_obligation_runner.py — OI-1253 runner store-resolution guard.

The runner must never silently serve a store it cannot attribute to a project.
A central install whose only identity signal was a release-time git origin
resolves no project_id (the origin is refused by ``_project_id_from_git_remote``),
and ``_resolve_state_root`` would otherwise fall back to a project-local dir
under the immutable install. The runner must fail LOUD with an actionable
message instead of writing to a fabricated or unattributable store.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "scripts" / "lib", ROOT / "scripts", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import vnx_paths  # noqa: E402
import gate_obligation_runner as runner  # noqa: E402


class TestDefaultStateDirGuard:
    """A runner without ``--state-dir`` must fail LOUD when project_id is None."""

    def test_loud_when_project_id_unresolvable(self, monkeypatch):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: None,
        )
        with pytest.raises(runner.UnresolvableProjectError) as excinfo:
            runner._default_state_dir()
        message = str(excinfo.value)
        assert "--state-dir" in message
        assert "VNX_PROJECT_ID" in message

    def test_resolves_when_project_id_present(self, monkeypatch):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: "vnx-dev",
        )
        state_dir = runner._default_state_dir()
        assert state_dir.name == "state"

    def test_main_returns_20_with_loud_error(self, monkeypatch, capsys):
        monkeypatch.setattr(
            vnx_paths, "_resolve_state_project_id", lambda project_root: None,
        )
        rc = runner.main([])
        assert rc == 20
        err = capsys.readouterr().err
        assert "project_id" in err
        assert "--state-dir" in err

    def test_main_state_dir_missing_still_returns_20(self, tmp_path, capsys):
        missing = tmp_path / "does-not-exist" / "state"
        rc = runner.main(["--state-dir", str(missing)])
        assert rc == 20
        assert "state dir not found" in capsys.readouterr().err
