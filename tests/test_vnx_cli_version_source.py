#!/usr/bin/env python3
"""Tests for vnx_cli.__init__._read_version() version-source order.

The root VERSION file must win over pip package metadata. In the
editable-install + `current` symlink deployment model the dist-info
metadata is stamped once at install time and goes stale as the code
underneath moves; the VERSION file moves with the code and is the
source of truth. Metadata is only a fallback for real wheel installs
where VERSION may be absent.
"""

import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import vnx_cli


def _relocate_version_file(monkeypatch, tmp_path, content=None):
    """Point vnx_cli.__file__ into tmp_path so _read_version() looks for
    VERSION at tmp_path / "VERSION". Returns the VERSION path."""
    fake_init = tmp_path / "pkg" / "vnx_cli" / "__init__.py"
    fake_init.parent.mkdir(parents=True)
    fake_init.touch()
    monkeypatch.setattr(vnx_cli, "__file__", str(fake_init))
    version_path = tmp_path / "pkg" / "VERSION"
    if content is not None:
        version_path.write_text(content, encoding="utf-8")
    return version_path


def test_version_file_wins_over_metadata(monkeypatch, tmp_path):
    """Live case: VERSION=1.4.1 next to stale dist-info metadata=1.3.0
    must yield 1.4.1."""
    _relocate_version_file(monkeypatch, tmp_path, "1.4.1\n")
    monkeypatch.setattr(vnx_cli, "_pkg_version", lambda name: "1.3.0")
    assert vnx_cli._read_version() == "1.4.1"


def test_metadata_fallback_when_version_file_absent(monkeypatch, tmp_path):
    """No VERSION file (real wheel install) -> package metadata answers."""
    _relocate_version_file(monkeypatch, tmp_path, content=None)
    monkeypatch.setattr(vnx_cli, "_pkg_version", lambda name: "1.3.0")
    assert vnx_cli._read_version() == "1.3.0"


def test_unknown_sentinel_when_both_absent(monkeypatch, tmp_path):
    """No VERSION file and no package metadata -> 0.0.0+unknown."""
    _relocate_version_file(monkeypatch, tmp_path, content=None)

    def _not_found(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(vnx_cli, "_pkg_version", _not_found)
    assert vnx_cli._read_version() == "0.0.0+unknown"


def test_empty_version_file_falls_through_to_metadata(monkeypatch, tmp_path):
    """Empty/whitespace VERSION must not produce an empty version string;
    fall through to package metadata."""
    _relocate_version_file(monkeypatch, tmp_path, "   \n")
    monkeypatch.setattr(vnx_cli, "_pkg_version", lambda name: "1.3.0")
    assert vnx_cli._read_version() == "1.3.0"


def test_empty_version_file_and_no_metadata_yields_sentinel(monkeypatch, tmp_path):
    """Empty VERSION plus missing metadata -> 0.0.0+unknown, never ''."""
    _relocate_version_file(monkeypatch, tmp_path, "")

    def _not_found(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(vnx_cli, "_pkg_version", _not_found)
    result = vnx_cli._read_version()
    assert result == "0.0.0+unknown"
    assert result != ""
