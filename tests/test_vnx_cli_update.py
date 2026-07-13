#!/usr/bin/env python3
"""Tests for vnx update subcommand."""

import io
import json
import subprocess
import sys
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import vnx_cli.commands.update as update_module
from vnx_cli.commands.update import (
    vnx_update,
    _resolve_root,
    _list_version_dirs,
    _current_target,
    _prune_old_versions,
    _atomic_symlink_flip,
    _validate_version_name,
    _fetch_version,
    _write_install_marker,
    _ensure_install_marker,
    _git_toplevel,
    INSTALL_MODE_MARKER,
    INSTALL_MODE_VALUE,
    DEFAULT_KEEP_LAST,
)


def _git_repo(path: Path) -> Path:
    """Init a minimal, committed git repo at ``path`` (no remote)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)
    return path


def _git_origin(tmp_path: Path) -> Path:
    """A local git repo standing in for VNX_GIT_REMOTE (offline, deterministic)."""
    origin = _git_repo(tmp_path / "origin")
    (origin / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add readme"], cwd=origin, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=origin, check=True)
    return origin


def _update_args(*, to_version=None, keep_last=DEFAULT_KEEP_LAST, dry_run=False, rollback=False):
    return Namespace(
        to_version=to_version,
        keep_last=keep_last,
        dry_run=dry_run,
        rollback=rollback,
    )


def _capture_update(args) -> tuple:
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


# ---------------------------------------------------------------------------
# _validate_version_name — path traversal + injection
# ---------------------------------------------------------------------------

def test_validate_rejects_path_traversal_dotdot(capsys):
    with pytest.raises(ValueError, match="invalid version name"):
        _validate_version_name("../../outside")


def test_validate_rejects_absolute_path(capsys):
    with pytest.raises(ValueError, match="invalid version name"):
        _validate_version_name("/etc/passwd")


def test_validate_rejects_shell_injection():
    with pytest.raises(ValueError, match="invalid version name"):
        _validate_version_name("foo;rm -rf /")


def test_validate_accepts_semver_with_rc():
    assert _validate_version_name("v1.0.0-rc2") == "v1.0.0-rc2"


def test_validate_accepts_edge():
    assert _validate_version_name("edge") == "edge"


def test_validate_accepts_latest():
    assert _validate_version_name("latest") == "latest"


def test_validate_accepts_bare_semver():
    assert _validate_version_name("1.2.3") == "1.2.3"


# ---------------------------------------------------------------------------
# vnx_update path-traversal: no filesystem mutation on invalid target
# ---------------------------------------------------------------------------

def test_update_path_traversal_no_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="../../outside")

    rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "invalid version name" in captured.err
    # No filesystem mutation
    assert not (tmp_path / "versions").exists()
    assert not (tmp_path / "current").exists()


def test_update_absolute_path_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="/etc/passwd")

    rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "invalid version name" in captured.err
    assert not (tmp_path / "versions").exists()


def test_update_shell_injection_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="foo;rm -rf /")

    rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "invalid version name" in captured.err


# ---------------------------------------------------------------------------
# ADR-005: symlink flip emits NDJSON audit events
# ---------------------------------------------------------------------------

def test_symlink_flip_emits_audit_events(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    target_dir = root / "versions" / "v1.0.0"
    target_dir.mkdir(parents=True)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _atomic_symlink_flip(root, target_dir, dry_run=False, audit_log=audit_log)

    assert audit_log.exists()
    lines = audit_log.read_text().strip().splitlines()
    assert len(lines) == 2

    before = json.loads(lines[0])
    after = json.loads(lines[1])

    assert before["event_type"] == "central_install_update"
    assert before["to_version"] == "v1.0.0"
    assert before["success"] is False
    assert before["phase"] == "before_flip"

    assert after["event_type"] == "central_install_update"
    assert after["to_version"] == "v1.0.0"
    assert after["success"] is True
    assert after["phase"] == "after_flip"


def test_symlink_flip_audit_event_has_timestamp(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    target_dir = root / "versions" / "v2.0.0"
    target_dir.mkdir(parents=True)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _atomic_symlink_flip(root, target_dir, dry_run=False, audit_log=audit_log)

    lines = audit_log.read_text().strip().splitlines()
    for line in lines:
        record = json.loads(line)
        assert "timestamp" in record
        assert record["timestamp"]  # non-empty


def test_symlink_flip_dry_run_no_audit_event(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    target_dir = root / "versions" / "v1.0.0"
    target_dir.mkdir(parents=True)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _atomic_symlink_flip(root, target_dir, dry_run=True, audit_log=audit_log)

    assert not audit_log.exists()


# ---------------------------------------------------------------------------
# ADR-005: prune emits NDJSON audit events
# ---------------------------------------------------------------------------

def test_prune_emits_audit_event(tmp_path):
    for v in ("v1", "v2", "v3", "v4", "v5"):
        (tmp_path / "versions" / v).mkdir(parents=True)

    audit_log = tmp_path / "events" / "central_install.ndjson"
    _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    assert audit_log.exists()
    lines = audit_log.read_text().strip().splitlines()
    assert len(lines) == 2  # 5 - 3 = 2 pruned

    for line in lines:
        record = json.loads(line)
        assert record["event_type"] == "central_install_prune"
        assert "pruned_version" in record
        assert record["keep_last_N"] == 3
        assert "timestamp" in record


def test_prune_dry_run_no_audit_event(tmp_path):
    for v in ("v1", "v2", "v3", "v4", "v5"):
        (tmp_path / "versions" / v).mkdir(parents=True)

    audit_log = tmp_path / "events" / "central_install.ndjson"
    _prune_old_versions(tmp_path, keep_last=3, dry_run=True, audit_log=audit_log)

    assert not audit_log.exists()


# ---------------------------------------------------------------------------
# Subprocess FileNotFoundError — controlled error, no crash
# ---------------------------------------------------------------------------

def test_git_not_found_returns_controlled_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="v1.0.0")

    with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
        rc = vnx_update(args)

    assert rc == 1
    captured = capsys.readouterr()
    assert "git executable not found in PATH" in captured.err


def test_git_not_found_no_exception_raised(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    args = _update_args(to_version="v1.0.0")

    with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
        try:
            rc = vnx_update(args)
        except FileNotFoundError:
            pytest.fail("FileNotFoundError leaked out of vnx_update — must be caught internally")

    assert rc == 1


# ---------------------------------------------------------------------------
# central-install-mode-marker-missing: _git_toplevel / marker helpers
# ---------------------------------------------------------------------------

def test_git_toplevel_matches_repo_root(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    assert _git_toplevel(repo) == repo.resolve()


def test_git_toplevel_none_for_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _git_toplevel(plain) is None


def test_write_install_marker_atomic(tmp_path):
    version_dir = tmp_path / "v1"
    version_dir.mkdir()
    _write_install_marker(version_dir)
    marker = version_dir / INSTALL_MODE_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


# ---------------------------------------------------------------------------
# ADR-005: marker write emits an NDJSON audit event
# ---------------------------------------------------------------------------

def test_write_install_marker_emits_audit_event(tmp_path):
    version_dir = tmp_path / "v1"
    version_dir.mkdir()
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _write_install_marker(version_dir, audit_log=audit_log)

    assert audit_log.exists()
    lines = audit_log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "central_install_marker_written"
    assert record["version_dir"] == str(version_dir)
    assert "timestamp" in record and record["timestamp"]


def test_fetch_version_emits_marker_audit_event(tmp_path, monkeypatch):
    origin = _git_origin(tmp_path)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))
    root = tmp_path / "vnx-system"
    audit_log = tmp_path / "events" / "central_install.ndjson"

    target_dir = _fetch_version(root, "edge", dry_run=False, audit_log=audit_log)

    lines = audit_log.read_text().strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert any(
        e["event_type"] == "central_install_marker_written" and e["version_dir"] == str(target_dir)
        for e in events
    )


def test_ensure_install_marker_repair_emits_audit_event(tmp_path):
    root = tmp_path / "vnx-system"
    repo = _git_repo(root / "versions" / "edge")
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _ensure_install_marker(root, repo, audit_log=audit_log)

    lines = audit_log.read_text().strip().splitlines()
    events = [json.loads(line) for line in lines]
    assert any(
        e["event_type"] == "central_install_marker_written" and e["version_dir"] == str(repo)
        for e in events
    )


def test_ensure_install_marker_skip_emits_no_audit_event(tmp_path):
    """The ownership guard's silent skip must not emit an event — nothing was
    written, so there is nothing to audit."""
    root = tmp_path / "vnx-system"
    root.mkdir()
    dev_checkout = _git_repo(tmp_path / "some-other-repo" / "vnx-orchestration")
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _ensure_install_marker(root, dev_checkout, audit_log=audit_log)

    assert not audit_log.exists()


def test_ensure_install_marker_writes_when_under_versions_and_git_toplevel(tmp_path):
    root = tmp_path / "vnx-system"
    repo = _git_repo(root / "versions" / "edge")

    _ensure_install_marker(root, repo)

    marker = repo / INSTALL_MODE_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


def test_ensure_install_marker_noop_for_non_git_dir(tmp_path):
    """Guard: never stamp a marker into a tree that isn't its own git toplevel
    (e.g. a consumer project's own dev checkout) — matches
    vnx_paths._is_central_install()'s git-toplevel==vnx_home condition.
    Placed under <root>/versions/ so this isolates the git-toplevel guard
    specifically, independent of the ownership guard below."""
    root = tmp_path / "vnx-system"
    plain = root / "versions" / "plain"
    plain.mkdir(parents=True)

    _ensure_install_marker(root, plain)

    assert not (plain / INSTALL_MODE_MARKER).exists()


def test_ensure_install_marker_noop_for_dev_checkout_not_under_versions(tmp_path):
    """Fix (central-install-mode-marker-missing follow-up): a standalone dev
    checkout is ALSO its own git toplevel — true of essentially any git repo —
    so the git-toplevel check alone is not sufficient. Without the ownership
    guard, a dev checkout that happens to resolve as the active `current` or a
    flip/rollback target would get falsely stamped `.vnx-install-mode=central`,
    the inverse of the mis-resolution class this marker exists to prevent."""
    root = tmp_path / "vnx-system"
    root.mkdir()
    dev_checkout = _git_repo(tmp_path / "some-other-repo" / "vnx-orchestration")

    _ensure_install_marker(root, dev_checkout)

    assert not (dev_checkout / INSTALL_MODE_MARKER).exists()


def test_ensure_install_marker_noop_for_none():
    _ensure_install_marker(Path("/tmp/unused-root"), None)  # must not raise


def test_ensure_install_marker_idempotent_when_already_valid(tmp_path):
    root = tmp_path / "vnx-system"
    repo = _git_repo(root / "versions" / "edge")
    marker = repo / INSTALL_MODE_MARKER
    marker.write_text("central\n", encoding="utf-8")
    mtime_before = marker.stat().st_mtime_ns

    _ensure_install_marker(root, repo)

    assert marker.stat().st_mtime_ns == mtime_before


def test_ensure_install_marker_overwrites_invalid_content(tmp_path):
    root = tmp_path / "vnx-system"
    repo = _git_repo(root / "versions" / "edge")
    marker = repo / INSTALL_MODE_MARKER
    marker.write_text("embedded\n", encoding="utf-8")

    _ensure_install_marker(root, repo)

    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


# ---------------------------------------------------------------------------
# central-install-mode-marker-missing: _fetch_version writes the marker
# ---------------------------------------------------------------------------

def test_fetch_version_clone_path_writes_marker(tmp_path, monkeypatch):
    origin = _git_origin(tmp_path)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))
    root = tmp_path / "vnx-system"

    target_dir = _fetch_version(root, "edge", dry_run=False)

    marker = target_dir / INSTALL_MODE_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


def test_fetch_version_pull_path_backfills_missing_marker(tmp_path, monkeypatch):
    """Reproduces the reported bug directly: a version dir fetched before this
    fix (marker stripped/never written) must get it back on the next fetch —
    the ``vnx update`` pull branch, not just the fresh-clone branch."""
    origin = _git_origin(tmp_path)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))
    root = tmp_path / "vnx-system"

    target_dir = _fetch_version(root, "edge", dry_run=False)
    marker = target_dir / INSTALL_MODE_MARKER
    assert marker.is_file()
    marker.unlink()
    assert not marker.is_file()

    target_dir_again = _fetch_version(root, "edge", dry_run=False)

    assert target_dir_again == target_dir
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


def test_fetch_version_dry_run_writes_no_marker(tmp_path, monkeypatch):
    origin = _git_origin(tmp_path)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))
    root = tmp_path / "vnx-system"

    target_dir = _fetch_version(root, "edge", dry_run=True)

    assert not (target_dir / INSTALL_MODE_MARKER).exists()


# ---------------------------------------------------------------------------
# central-install-mode-marker-missing: _atomic_symlink_flip self-heals target
# ---------------------------------------------------------------------------

def test_symlink_flip_backfills_marker_on_git_toplevel_target(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    target_dir = _git_repo(root / "versions" / "v1.0.0")
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _atomic_symlink_flip(root, target_dir, dry_run=False, audit_log=audit_log)

    marker = target_dir / INSTALL_MODE_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


def test_symlink_flip_skips_marker_on_non_git_target(tmp_path):
    root = tmp_path / "install"
    root.mkdir()
    target_dir = root / "versions" / "v1.0.0"
    target_dir.mkdir(parents=True)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _atomic_symlink_flip(root, target_dir, dry_run=False, audit_log=audit_log)

    assert not (target_dir / INSTALL_MODE_MARKER).exists()


# ---------------------------------------------------------------------------
# central-install-mode-marker-missing: vnx_update repairs the active install
# ---------------------------------------------------------------------------

def test_vnx_update_repairs_active_marker_less_install(tmp_path, monkeypatch):
    """The exact reported bug: `current` already points at a git-toplevel
    version dir with no marker (fetched by a pre-fix `vnx update`). Any
    subsequent `vnx update` invocation must repair it, even though the
    requested target itself (here: re-fetching `edge`, which fails offline
    with no configured remote) does not succeed."""
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    root = tmp_path
    active = _git_repo(root / "versions" / "edge")
    (root / "current").symlink_to(active)
    marker = active / INSTALL_MODE_MARKER
    assert not marker.is_file()

    rc = vnx_update(_update_args(to_version="edge"))

    assert rc == 1  # `git pull` fails: no remote configured on the local repo
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


def test_vnx_update_dry_run_does_not_repair_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    root = tmp_path
    active = _git_repo(root / "versions" / "edge")
    (root / "current").symlink_to(active)

    vnx_update(_update_args(to_version="edge", dry_run=True))

    assert not (active / INSTALL_MODE_MARKER).exists()
