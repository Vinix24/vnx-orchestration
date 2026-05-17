#!/usr/bin/env python3
"""Tests for vnx update subcommand."""

import io
import sys
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.update import (
    vnx_update,
    _resolve_root,
    _list_version_dirs,
    _current_target,
    _prune_old_versions,
    DEFAULT_KEEP_LAST,
)


def _update_args(*, to_version=None, keep_last=DEFAULT_KEEP_LAST, dry_run=False, rollback=False):
    return Namespace(
        to_version=to_version,
        keep_last=keep_last,
        dry_run=dry_run,
        rollback=rollback,
    )


def _capture_update(args) -> tuple[str, str, int]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with redirect_stdout(out_buf):
        rc = vnx_update(args)
    return out_buf.getvalue(), "", rc


# ---------------------------------------------------------------------------
# _resolve_root
# ---------------------------------------------------------------------------

def test_resolve_root_respects_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    assert _resolve_root() == tmp_path.resolve()


def test_resolve_root_returns_path_object(monkeypatch):
    monkeypatch.delenv("VNX_HOME_ROOT", raising=False)
    result = _resolve_root()
    assert isinstance(result, Path)


# ---------------------------------------------------------------------------
# _list_version_dirs / _current_target
# ---------------------------------------------------------------------------

def test_list_version_dirs_empty_root(tmp_path):
    assert _list_version_dirs(tmp_path) == []


def test_list_version_dirs_finds_dirs(tmp_path):
    (tmp_path / "versions" / "1.0.0-rc1").mkdir(parents=True)
    (tmp_path / "versions" / "1.0.0-rc2").mkdir(parents=True)
    dirs = _list_version_dirs(tmp_path)
    assert len(dirs) == 2
    assert all(d.is_dir() for d in dirs)


def test_current_target_no_symlink(tmp_path):
    assert _current_target(tmp_path) is None


def test_current_target_resolves_symlink(tmp_path):
    target = tmp_path / "versions" / "1.0.0-rc1"
    target.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(target)
    assert _current_target(tmp_path) == target.resolve()


# ---------------------------------------------------------------------------
# _prune_old_versions
# ---------------------------------------------------------------------------

def test_prune_dry_run_no_deletions(tmp_path):
    for v in ("v1", "v2", "v3", "v4", "v5"):
        (tmp_path / "versions" / v).mkdir(parents=True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(tmp_path, keep_last=3, dry_run=True)

    output = buf.getvalue()
    assert "[dry-run]" in output
    # All dirs still present
    assert len(list((tmp_path / "versions").iterdir())) == 5


def test_prune_keeps_correct_count(tmp_path):
    for v in ("v1", "v2", "v3", "v4", "v5"):
        d = tmp_path / "versions" / v
        d.mkdir(parents=True)

    _prune_old_versions(tmp_path, keep_last=3, dry_run=False)
    remaining = list((tmp_path / "versions").iterdir())
    assert len(remaining) == 3


# ---------------------------------------------------------------------------
# vnx_update --dry-run --to edge
# ---------------------------------------------------------------------------

def test_dry_run_to_edge_prints_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="edge", dry_run=True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vnx_update(args)

    output = buf.getvalue()
    assert rc == 0
    assert "[dry-run]" in output
    assert "edge" in output


def test_dry_run_no_filesystem_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="1.0.0-rc3", dry_run=True)

    vnx_update(args)

    # No versions/ directory should be created
    assert not (tmp_path / "versions").exists()
    assert not (tmp_path / "current").exists()


def test_dry_run_schema_warning_present(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="edge", dry_run=True)

    buf = io.StringIO()
    with redirect_stdout(buf):
        vnx_update(args)

    assert "schema-bootstrap" in buf.getvalue().lower() or "central-4" in buf.getvalue().lower()


# ---------------------------------------------------------------------------
# vnx_update error cases
# ---------------------------------------------------------------------------

def test_missing_to_without_rollback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version=None, rollback=False)

    rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "--to" in captured.err or "required" in captured.err.lower()


def test_rollback_no_current_symlink(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(rollback=True)

    rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "rollback" in captured.err.lower() or "symlink" in captured.err.lower()


def test_rollback_no_previous_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))

    only_ver = tmp_path / "versions" / "1.0.0-rc1"
    only_ver.mkdir(parents=True)
    (tmp_path / "current").symlink_to(only_ver)

    args = _update_args(rollback=True)
    rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "previous" in captured.err.lower()
