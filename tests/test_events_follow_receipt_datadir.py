"""OI-1276: events resolve their data dir with the same rule as receipts.

A bare ``VNX_DATA_DIR`` (without ``VNX_DATA_DIR_EXPLICIT=1``) used to send
events to ``$VNX_DATA_DIR/events`` while receipts, through
``vnx_paths.resolve_paths``, ignored it (issue #225). Both now follow
``resolve_paths``; the ignored variable is named on stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import data_dir_resolution
import event_store
import vnx_paths
from data_dir_resolution import DataDirResolutionError


@pytest.fixture(autouse=True)
def _fresh_warning_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_dir_resolution, "_WARNED_BARE_VALUES", set(), raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)


def _receipt_data_root() -> Path:
    return Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"]).resolve().parent


def test_bare_vnx_data_dir_events_and_receipts_share_one_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(bare))
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

    events_root = event_store._events_dir().resolve().parent

    assert events_root == _receipt_data_root()
    assert events_root != bare.resolve()


def test_bare_vnx_data_dir_warns_once_on_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    bare = tmp_path / "bare"
    monkeypatch.setenv("VNX_DATA_DIR", str(bare))
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

    data_dir_resolution.resolve_data_dir_fail_loud()
    data_dir_resolution.resolve_data_dir_fail_loud()

    lines = [ln for ln in capsys.readouterr().err.splitlines() if "VNX_DATA_DIR" in ln]
    assert len(lines) == 1
    assert "VNX_DATA_DIR_EXPLICIT=1" in lines[0]
    assert str(bare) in lines[0]


def test_explicit_vnx_data_dir_puts_events_and_receipts_under_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(pinned))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    assert event_store._events_dir() == pinned.resolve() / "events"
    assert _receipt_data_root() == pinned.resolve()
    assert "VNX_DATA_DIR_EXPLICIT" not in capsys.readouterr().err


@pytest.mark.parametrize("explicit", [None, "0", "true", ""])
def test_flag_other_than_one_is_not_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("VNX_DATA_DIR", str(bare))
    if explicit is None:
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    else:
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", explicit)

    assert data_dir_resolution.resolve_data_dir_fail_loud() != bare.resolve()


def test_explicit_version_store_path_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    version_data = home / ".vnx-system" / "versions" / "v1.4.7" / ".vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(version_data))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with pytest.raises(DataDirResolutionError) as excinfo:
        event_store._events_dir()
    assert "version store" in str(excinfo.value)


@pytest.mark.parametrize("explicit", [None, "1"])
def test_daemon_default_matches_receipts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit
) -> None:
    from headless_dispatch_daemon import _default_data_dir

    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    if explicit is None:
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    else:
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", explicit)

    assert _default_data_dir() == _receipt_data_root()


def test_bare_value_equal_to_resolved_root_is_not_warned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # bin/vnx and vnx_paths.sh export the resolved root as a bare VNX_DATA_DIR.
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    root = data_dir_resolution.resolve_data_dir_fail_loud()
    monkeypatch.setenv("VNX_DATA_DIR", str(root))

    assert data_dir_resolution.resolve_data_dir_fail_loud() == root
    assert "VNX_DATA_DIR" not in capsys.readouterr().err


def test_explicit_vnx_data_dir_spawns_no_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit data dir resolves without any git/subprocess call."""
    import subprocess

    def _boom(*_a, **_k):
        raise AssertionError("a process was spawned while resolving an explicit data dir")

    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    for name in ("Popen", "run", "check_output"):
        monkeypatch.setattr(subprocess, name, _boom)
    store = event_store.EventStore()
    assert store._events_dir == (tmp_path.resolve() / "events")
