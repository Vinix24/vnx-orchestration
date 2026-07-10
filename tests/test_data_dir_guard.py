"""Tests for the VNX data-dir / project_id guard (ADR-028 Phase-0).

The guard is ``warn`` by default in production but ``off`` by default in the
shared ``conftest.py`` so the existing suite does not drown in mismatch warnings
for its temp-data isolation.  These tests override the flag explicitly.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest

import data_dir_guard
import vnx_paths
from data_dir_guard import VNXDataDirMismatchWarning, check_data_dir_project_id_guard


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temp directory for the test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _clean_data_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that would interfere with explicit data-dir resolution."""
    for key in (
        "VNX_DATA_DIR",
        "VNX_DATA_DIR_EXPLICIT",
        "VNX_DATA_HOME",
        "XDG_DATA_HOME",
        "VNX_STATE_DIR",
        "VNX_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)


class TestGuardDirect:
    def test_off_mode_is_silent_on_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
        home = _fake_home(tmp_path, monkeypatch)
        wrong = tmp_path / "other-project" / ".vnx-data"
        wrong.mkdir(parents=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            check_data_dir_project_id_guard(wrong, "myproj")

        assert len(caught) == 0

    def test_warn_mode_emits_warning_on_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        home = _fake_home(tmp_path, monkeypatch)
        wrong = tmp_path / "other-project" / ".vnx-data"
        wrong.mkdir(parents=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            check_data_dir_project_id_guard(wrong, "myproj")

        assert len(caught) == 1
        assert issubclass(caught[0].category, VNXDataDirMismatchWarning)
        msg = str(caught[0].message)
        assert "myproj" in msg
        assert str(wrong.resolve()) in msg
        assert "expected central dir" in msg

    def test_warn_mode_does_not_abort(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        wrong = tmp_path / "wrong"
        wrong.mkdir()
        # Must not raise.
        check_data_dir_project_id_guard(wrong, "myproj")

    def test_enforce_mode_raises_on_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "enforce")
        home = _fake_home(tmp_path, monkeypatch)
        wrong = tmp_path / "other-project" / ".vnx-data"
        wrong.mkdir(parents=True)

        with pytest.raises(RuntimeError, match="VNX data-dir mismatch"):
            check_data_dir_project_id_guard(wrong, "myproj")

    @pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
    def test_matching_central_dir_is_silent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", mode)
        home = _fake_home(tmp_path, monkeypatch)
        central = home / ".vnx-data" / "myproj"
        central.mkdir(parents=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            check_data_dir_project_id_guard(central, "myproj")
            check_data_dir_project_id_guard(central / "state", "myproj")

        assert len(caught) == 0

    def test_enforce_mode_missing_project_id_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No marker / env project_id must not be treated as a mismatch."""
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "enforce")
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        monkeypatch.chdir(tmp_path)  # no .vnx-project-id here
        wrong = tmp_path / "wrong"
        wrong.mkdir()

        # Should skip, not raise.
        check_data_dir_project_id_guard(wrong, None)

    def test_invalid_project_id_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "enforce")
        wrong = tmp_path / "wrong"
        wrong.mkdir()

        check_data_dir_project_id_guard(wrong, "Bad_ID_123")

    def test_default_mode_is_warn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # conftest sets this to off; deleting it reverts to production default.
        monkeypatch.delenv("VNX_DATA_DIR_GUARD", raising=False)
        home = _fake_home(tmp_path, monkeypatch)
        wrong = tmp_path / "other" / ".vnx-data"
        wrong.mkdir(parents=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            check_data_dir_project_id_guard(wrong, "myproj")

        assert len(caught) == 1
        assert issubclass(caught[0].category, VNXDataDirMismatchWarning)


class TestGuardViaVnxPaths:
    def test_resolve_data_root_matching_central_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        _clean_data_env(monkeypatch)
        home = _fake_home(tmp_path, monkeypatch)
        project_root = tmp_path / "myproj"
        project_root.mkdir()
        (project_root / ".vnx-project-id").write_text("myproj\n")
        central = home / ".vnx-data" / "myproj"
        central.mkdir(parents=True)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = vnx_paths.resolve_data_root(project_root)

        assert result == central.resolve()
        assert len(caught) == 0

    def test_resolve_paths_mismatch_warns_but_does_not_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        _clean_data_env(monkeypatch)
        home = _fake_home(tmp_path, monkeypatch)
        central = home / ".vnx-data" / "myproj"
        central.mkdir(parents=True)
        wrong = tmp_path / "wrong-data"
        wrong.mkdir()

        monkeypatch.setenv("VNX_PROJECT_ID", "myproj")
        monkeypatch.setenv("VNX_DATA_DIR", str(wrong))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            paths = vnx_paths.resolve_paths()

        assert paths["VNX_DATA_DIR"] == str(wrong.resolve())
        assert len(caught) == 1
        assert issubclass(caught[0].category, VNXDataDirMismatchWarning)

    def test_resolve_paths_enforce_aborts_on_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "enforce")
        _clean_data_env(monkeypatch)
        home = _fake_home(tmp_path, monkeypatch)
        central = home / ".vnx-data" / "myproj"
        central.mkdir(parents=True)
        wrong = tmp_path / "wrong-data"
        wrong.mkdir()

        monkeypatch.setenv("VNX_PROJECT_ID", "myproj")
        monkeypatch.setenv("VNX_DATA_DIR", str(wrong))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        with pytest.raises(RuntimeError, match="VNX data-dir mismatch"):
            vnx_paths.resolve_paths()

    def test_resolve_paths_off_silent_on_mismatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
        _clean_data_env(monkeypatch)
        home = _fake_home(tmp_path, monkeypatch)
        central = home / ".vnx-data" / "myproj"
        central.mkdir(parents=True)
        wrong = tmp_path / "wrong-data"
        wrong.mkdir()

        monkeypatch.setenv("VNX_PROJECT_ID", "myproj")
        monkeypatch.setenv("VNX_DATA_DIR", str(wrong))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            paths = vnx_paths.resolve_paths()

        assert paths["VNX_DATA_DIR"] == str(wrong.resolve())
        assert len(caught) == 0
