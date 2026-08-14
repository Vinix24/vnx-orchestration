"""Tests for data_dir_resolution.py — the shared fail-loud data-dir resolver.

Covers OI-1179 (event_store two-flag trap) and OI-1172 (headless daemon
keystone fallback) — two defects of one class: a resolver landing on the
read-only pinned version store ``~/.vnx-system/versions/<v>/.vnx-data``.

The shared resolver:
  1. honors a set ``VNX_DATA_DIR`` directly (no ``VNX_DATA_DIR_EXPLICIT`` required);
  2. refuses — with a ``DataDirResolutionError`` naming the chain — any result
     under ``~/.vnx-system/versions/`` (or resolving through
     ``~/.vnx-system/current`` into it);
  3. refuses a non-version-store sibling dir (prefix boundary via os.sep).

Tests redirect ``Path.home()`` to a temp dir so the version-store refusal is
exercised against a synthetic ``~/.vnx-system`` tree, never the real one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from data_dir_resolution import (
    DataDirResolutionError,
    _is_under_version_store,
    refuse_version_store,
    resolve_data_dir_fail_loud,
)


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temp directory for the test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _clean_data_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that would short-circuit the resolver before the branch under test."""
    for key in (
        "VNX_DATA_DIR",
        "VNX_DATA_DIR_EXPLICIT",
        "VNX_PROJECT_ID",
    ):
        monkeypatch.delenv(key, raising=False)


# ── env-first: a set VNX_DATA_DIR is honored directly ────────────────────────


def test_vnx_data_dir_honored_without_explicit_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A set VNX_DATA_DIR is honored WITHOUT VNX_DATA_DIR_EXPLICIT=1 (OI-1179)."""
    data_dir = tmp_path / "operator" / ".vnx-data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    result = resolve_data_dir_fail_loud()

    assert result == data_dir.resolve()


# ── fail-loud: a read-only version store is never a valid data dir ───────────


def test_vnx_data_dir_under_version_store_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """VNX_DATA_DIR landing under ~/.vnx-system/versions/ raises, naming the chain."""
    home = _fake_home(tmp_path, monkeypatch)
    version_data = home / ".vnx-system" / "versions" / "v1.4.7" / ".vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(version_data))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    with pytest.raises(DataDirResolutionError) as excinfo:
        resolve_data_dir_fail_loud()

    assert "version store" in str(excinfo.value)
    assert "VNX_DATA_DIR" in str(excinfo.value)


def test_vnx_project_id_branch_never_lands_under_version_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VNX_PROJECT_ID resolves ~/.vnx-data/<id>, never the version store."""
    home = _fake_home(tmp_path, monkeypatch)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
    monkeypatch.setenv("VNX_PROJECT_ID", "my-project")

    result = resolve_data_dir_fail_loud()

    assert result == home / ".vnx-data" / "my-project"


def test_fallback_under_version_store_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The canonical-resolver fallback is refused too when it lands under versions/."""
    home = _fake_home(tmp_path, monkeypatch)
    _clean_data_env(monkeypatch)

    import vnx_paths

    fake = home / ".vnx-system" / "versions" / "v1.4.7" / ".vnx-data"
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {"VNX_DATA_DIR": str(fake)})

    with pytest.raises(DataDirResolutionError) as excinfo:
        resolve_data_dir_fail_loud()

    assert "resolve_paths" in str(excinfo.value)


# ── prefix boundary: a sibling dir is not a version store ────────────────────


def test_sibling_dir_is_not_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """~/.vnx-system/versions-other is a sibling, not under versions/ — not refused."""
    home = _fake_home(tmp_path, monkeypatch)
    sibling = home / ".vnx-system" / "versions-other" / "data"

    assert not _is_under_version_store(sibling.resolve())
    assert refuse_version_store(sibling, "test chain") == sibling.resolve()


def test_version_store_root_itself_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The versions/ root itself (no trailing component) is refused."""
    home = _fake_home(tmp_path, monkeypatch)
    root = home / ".vnx-system" / "versions"

    assert _is_under_version_store(root.resolve())


# ── OI-1172: the headless daemon shares the fail-loud resolver ────────────────


def test_headless_default_data_dir_refuses_version_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """headless_dispatch_daemon._default_data_dir raises on the version store (OI-1172)."""
    home = _fake_home(tmp_path, monkeypatch)
    version_data = home / ".vnx-system" / "versions" / "v1.4.7" / ".vnx-data"
    monkeypatch.setenv("VNX_DATA_DIR", str(version_data))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    from headless_dispatch_daemon import _default_data_dir

    with pytest.raises(DataDirResolutionError):
        _default_data_dir()


def test_headless_default_data_dir_honors_vnx_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """headless_dispatch_daemon._default_data_dir honors a set VNX_DATA_DIR."""
    data_dir = tmp_path / "daemon" / ".vnx-data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    from headless_dispatch_daemon import _default_data_dir

    assert _default_data_dir() == data_dir.resolve()
