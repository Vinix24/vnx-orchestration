#!/usr/bin/env python3
"""Tests for vnx update subcommand."""

import io
import json
import os
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
    ProtectionSetUnavailable,
    _resolve_root,
    _list_version_dirs,
    _current_target,
    _prune_old_versions,
    _collect_protected_versions,
    _load_fleet_pins,
    _resolve_pin_scan_roots,
    _scan_root_for_pins,
    _scan_disk_for_pins,
    _atomic_symlink_flip,
    _validate_version_name,
    _fetch_version,
    _write_install_marker,
    _ensure_install_marker,
    _git_toplevel,
    INSTALL_MODE_MARKER,
    INSTALL_MODE_VALUE,
    DEFAULT_KEEP_LAST,
    DEFAULT_PIN_SCAN_MAX_DEPTH,
)


@pytest.fixture(autouse=True)
def _isolate_pin_scan_roots(monkeypatch, tmp_path):
    """Every test in this module gets an empty, isolated default disk-scan
    root instead of the real ``$HOME`` (source 4, OI-1379). Without this, the
    new disk-scan protection source would walk the REAL home directory of
    whatever machine runs the suite on every test that exercises
    ``_collect_protected_versions``/``_prune_old_versions`` — slow, and liable
    to pick up unrelated real ``.vnx-version`` pins. Tests that want to
    exercise the scan explicitly override via ``monkeypatch.setenv`` or an
    explicit ``pin_scan_roots=``/``scan_roots=`` argument.
    """
    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(tmp_path / "empty-pin-scan-root"))


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


def _update_args(*, to_version=None, keep_last=DEFAULT_KEEP_LAST, dry_run=False, rollback=False, protect_pins=None):
    return Namespace(
        to_version=to_version,
        keep_last=keep_last,
        dry_run=dry_run,
        rollback=rollback,
        protect_pins=protect_pins,
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

    assert "would migrate all central per-project stores" in buf.getvalue().lower()


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


# ---------------------------------------------------------------------------
# Pin-safe GC: _prune_old_versions never prunes a pinned/protected version
# ---------------------------------------------------------------------------

def _version_dirs(root: Path, names, *, oldest_first=True):
    """Create version dirs with strictly increasing mtimes (deterministic age)."""
    dirs = []
    ordered = names if oldest_first else list(reversed(names))
    base = 1_700_000_000
    for i, name in enumerate(ordered):
        d = root / "versions" / name
        d.mkdir(parents=True, exist_ok=True)
        ts = base + i * 100
        os.utime(d, (ts, ts))
        dirs.append(d)
    return dirs


def _empty_registry(tmp_path, monkeypatch):
    """Point the fleet registry at a nonexistent path for hermetic tests."""
    missing = tmp_path / "no-such-registry.json"
    monkeypatch.setenv("VNX_PROJECT_REGISTRY", str(missing))
    return missing


def _audit_events(audit_log: Path):
    return [json.loads(line) for line in audit_log.read_text().strip().splitlines()]


def test_prune_protects_cli_protect_pins(tmp_path, monkeypatch):
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        protect_pins="v1.0.1,v1.0.2", audit_log=audit_log,
    )

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    # v1.0.1 and v1.0.2 are prune candidates (oldest of 5, keep 3) but protected.
    assert remaining == ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]

    events = _audit_events(audit_log)
    protected_events = [e for e in events if e["event_type"] == "central_install_prune_protected"]
    assert sorted(e["protected_version"] for e in protected_events) == ["v1.0.1", "v1.0.2"]
    for event in protected_events:
        assert event["keep_last_N"] == 3
        assert any("--protect-pins" in r for r in event["reasons"])
        assert "timestamp" in event and event["timestamp"]
    # Nothing was actually pruned (both candidates protected), so no prune events.
    assert not [e for e in events if e["event_type"] == "central_install_prune"]


def test_prune_protects_versions_from_protected_versions_file(tmp_path, monkeypatch):
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    # v-prefix normalization: the file lists the bare form, the dir is v-prefixed.
    (tmp_path / "protected-versions").write_text(
        "# operator-curated GC protection\n1.0.1\n\n", encoding="utf-8"
    )
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    # v1.0.2 pruned (unprotected candidate); v1.0.1 survives via the file.
    assert remaining == ["v1.0.1", "v1.0.3", "v1.0.4", "v1.0.5"]

    events = _audit_events(audit_log)
    protected_events = [e for e in events if e["event_type"] == "central_install_prune_protected"]
    assert len(protected_events) == 1
    assert protected_events[0]["protected_version"] == "v1.0.1"
    assert any("protected-versions" in r for r in protected_events[0]["reasons"])
    pruned = [e["pruned_version"] for e in events if e["event_type"] == "central_install_prune"]
    assert pruned == ["v1.0.2"]


def test_prune_protects_fleet_registry_pins(tmp_path, monkeypatch):
    """A pin in any registered consumer project's .vnx-version protects the dir."""
    monkeypatch.delenv("VNX_PROJECT_REGISTRY", raising=False)
    consumer = tmp_path / "consumer-project"
    consumer.mkdir()
    (consumer / ".vnx-version").write_text("v1.0.1\n", encoding="utf-8")
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps({"schema_version": 2, "projects": [{"path": str(consumer)}]}),
        encoding="utf-8",
    )

    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        registry_path=registry, audit_log=audit_log,
    )

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == ["v1.0.1", "v1.0.3", "v1.0.4", "v1.0.5"]

    events = _audit_events(audit_log)
    protected_events = [e for e in events if e["event_type"] == "central_install_prune_protected"]
    assert len(protected_events) == 1
    assert protected_events[0]["protected_version"] == "v1.0.1"
    assert any("pinned by project" in r for r in protected_events[0]["reasons"])


def test_prune_protection_sources_union(tmp_path, monkeypatch):
    """Registry + protected-versions file + --protect-pins all union together."""
    monkeypatch.delenv("VNX_PROJECT_REGISTRY", raising=False)
    consumer = tmp_path / "consumer-project"
    consumer.mkdir()
    (consumer / ".vnx-version").write_text("v1.0.1\n", encoding="utf-8")
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps({"projects": [{"path": str(consumer)}]}), encoding="utf-8"
    )
    (tmp_path / "protected-versions").write_text("v1.0.2\n", encoding="utf-8")

    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        protect_pins="v1.0.3", registry_path=registry,
        audit_log=tmp_path / "events" / "central_install.ndjson",
    )

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]


def test_prune_protection_dry_run_no_deletion_no_audit(tmp_path, monkeypatch):
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(
            tmp_path, keep_last=3, dry_run=True,
            protect_pins="v1.0.1", audit_log=audit_log,
        )

    output = buf.getvalue()
    assert "[dry-run] Would protect from prune:" in output
    assert "v1.0.1" in output
    assert len(list((tmp_path / "versions").iterdir())) == 5
    assert not audit_log.exists()


def test_prune_keep_last_still_prunes_unprotected(tmp_path, monkeypatch):
    """Protecting one candidate must not shield the other old candidates."""
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5", "v1.0.6"]
    _version_dirs(tmp_path, names)

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        protect_pins="v1.0.1",
        audit_log=tmp_path / "events" / "central_install.ndjson",
    )

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    # 3 prune candidates (v1.0.1..v1.0.3): v1.0.1 protected, other two pruned.
    assert remaining == ["v1.0.1", "v1.0.4", "v1.0.5", "v1.0.6"]


def test_prune_protection_ignores_malformed_entries(tmp_path, monkeypatch, capsys):
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        protect_pins="../escape,v1.0.1,foo;rm -rf /",
        audit_log=tmp_path / "events" / "central_install.ndjson",
    )

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert "v1.0.1" in remaining
    assert "v1.0.2" not in remaining
    captured = capsys.readouterr()
    assert "malformed protected version" in captured.err


# ---------------------------------------------------------------------------
# OI-912: prune must not silently delete a version-dir something still points at
# (pip-install / symlink references), even when no .vnx-version pin exists.
# ---------------------------------------------------------------------------

def test_prune_protects_pip_editable_direct_url(tmp_path, monkeypatch):
    """A pip editable install whose direct_url.json names the version dir must
    protect it from prune — the OI-912 incident (global vnx_cli console script
    was `pip install -e versions/edge`; pruning edge broke `vnx --version`)."""
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    fake_site = tmp_path / "fake-site-packages"
    dist_info = fake_site / "vnx_orchestration-1.3.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "direct_url.json").write_text(
        json.dumps(
            {
                "dir_info": {"editable": True},
                "url": f"file://{tmp_path}/versions/v1.0.1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(update_module, "_iter_site_packages_dirs", lambda: [fake_site])

    audit_log = tmp_path / "events" / "central_install.ndjson"
    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    output = buf.getvalue()
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    # v1.0.1 referenced by the pip editable install -> protected, NOT pruned.
    assert remaining == ["v1.0.1", "v1.0.3", "v1.0.4", "v1.0.5"]
    assert "Protected from prune" in output
    assert "pip install vnx_orchestration-1.3.0.dist-info" in output

    events = _audit_events(audit_log)
    protected = [
        e for e in events if e["event_type"] == "central_install_prune_protected"
    ]
    assert len(protected) == 1
    assert protected[0]["protected_version"] == "v1.0.1"
    assert any("pip install" in r for r in protected[0]["reasons"])


def test_prune_protects_pip_editable_pth(tmp_path, monkeypatch):
    """A *.pth / __editable__ finder whose content names the version dir must
    protect it — the second shape a pip -e install leaves in site-packages."""
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    fake_site = tmp_path / "fake-site-packages"
    fake_site.mkdir(parents=True)
    (fake_site / "__editable__.vnx_orchestration-1.3.0.pth").write_text(
        "import __editable___vnx_orchestration_1_3_0_finder; "
        "__editable___vnx_orchestration_1_3_0_finder.install()",
        encoding="utf-8",
    )
    (fake_site / "__editable___vnx_orchestration_1_3_0_finder.py").write_text(
        f"MAPPING = {{'vnx_cli': '{tmp_path}/versions/v1.0.1'}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(update_module, "_iter_site_packages_dirs", lambda: [fake_site])

    _prune_old_versions(tmp_path, keep_last=3, dry_run=False)

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == ["v1.0.1", "v1.0.3", "v1.0.4", "v1.0.5"]


def test_prune_protects_symlink_reference(tmp_path, monkeypatch):
    """A symlink under the central install root that points into a version dir
    must protect it — a bin/ shim or legacy alias must not dangle after prune."""
    _empty_registry(tmp_path, monkeypatch)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "vnx").symlink_to(tmp_path / "versions" / "v1.0.1")

    audit_log = tmp_path / "events" / "central_install.ndjson"
    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    output = buf.getvalue()
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == ["v1.0.1", "v1.0.3", "v1.0.4", "v1.0.5"]
    assert "symlink" in output

    events = _audit_events(audit_log)
    protected = [
        e for e in events if e["event_type"] == "central_install_prune_protected"
    ]
    assert len(protected) == 1
    assert protected[0]["protected_version"] == "v1.0.1"
    assert any("symlink" in r for r in protected[0]["reasons"])


def test_collect_protected_versions_normalizes_v_prefix(tmp_path, monkeypatch):
    _empty_registry(tmp_path, monkeypatch)
    protected = _collect_protected_versions(tmp_path, protect_pins="v1.3.0,1.2.3")
    assert "1.3.0" in protected
    assert "1.2.3" in protected


def test_load_fleet_pins_skips_missing_and_malformed(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_REGISTRY", raising=False)
    pinned = tmp_path / "pinned-project"
    pinned.mkdir()
    (pinned / ".vnx-version").write_text("v2.0.0\n", encoding="utf-8")
    unpinned = tmp_path / "unpinned-project"
    unpinned.mkdir()
    malformed = tmp_path / "malformed-project"
    malformed.mkdir()
    (malformed / ".vnx-version").write_text("../escape\n", encoding="utf-8")
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps({"projects": [
            {"path": str(pinned)},
            {"path": str(unpinned)},
            {"path": str(malformed)},
            {"path": str(tmp_path / "does-not-exist")},
            {},  # no path key
        ]}),
        encoding="utf-8",
    )

    pins = _load_fleet_pins(registry)

    assert list(pins.keys()) == ["2.0.0"]
    assert any("pinned by project" in s for s in pins["2.0.0"])


# ---------------------------------------------------------------------------
# Fail-closed GC: an undeterminable protected set aborts the prune entirely
# ---------------------------------------------------------------------------

def test_prune_aborts_on_malformed_fleet_registry(tmp_path, monkeypatch, capsys):
    """Registry EXISTS but JSON parse fails => fail CLOSED: nothing pruned."""
    monkeypatch.delenv("VNX_PROJECT_REGISTRY", raising=False)
    registry = tmp_path / "projects.json"
    registry.write_text("{ not valid json", encoding="utf-8")
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        registry_path=registry, audit_log=audit_log,
    )

    # Fail-closed: NO version dir was deleted.
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == names

    captured = capsys.readouterr()
    assert "GC prune ABORTED (fail-closed)" in captured.err
    assert str(registry) in captured.err

    events = _audit_events(audit_log)
    aborted = [e for e in events if e["event_type"] == "central_install_prune_aborted"]
    assert len(aborted) == 1
    assert aborted[0]["source"] == str(registry)
    assert aborted[0]["keep_last_N"] == 3
    assert "reason" in aborted[0] and aborted[0]["reason"]
    assert "timestamp" in aborted[0] and aborted[0]["timestamp"]
    # No prune/protected events — the prune never ran.
    assert not [e for e in events if e["event_type"] == "central_install_prune"]
    assert not [e for e in events if e["event_type"] == "central_install_prune_protected"]


def test_prune_aborts_on_unreadable_fleet_registry_oserror(tmp_path, monkeypatch, capsys):
    """Registry EXISTS but the read itself fails (chmod 000) => fail CLOSED."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod-000 files — OSError path unreachable")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"projects": []}), encoding="utf-8")
    registry.chmod(0o000)
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    try:
        _prune_old_versions(
            tmp_path, keep_last=3, dry_run=False,
            registry_path=registry, audit_log=audit_log,
        )
    finally:
        registry.chmod(0o644)

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == names
    captured = capsys.readouterr()
    assert "GC prune ABORTED (fail-closed)" in captured.err
    aborted = [
        e for e in _audit_events(audit_log)
        if e["event_type"] == "central_install_prune_aborted"
    ]
    assert len(aborted) == 1
    assert aborted[0]["source"] == str(registry)


def test_prune_aborts_on_unreadable_protected_versions_file(tmp_path, monkeypatch, capsys):
    """protected-versions EXISTS but is unreadable (invalid UTF-8) => fail CLOSED."""
    _empty_registry(tmp_path, monkeypatch)
    (tmp_path / "protected-versions").write_bytes(b"v1.0.1\n\xff\xfe not utf-8\n")
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    # Fail-closed: NO version dir was deleted — not even the unprotected v1.0.2.
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == names

    captured = capsys.readouterr()
    assert "GC prune ABORTED (fail-closed)" in captured.err
    assert "protected-versions" in captured.err

    aborted = [
        e for e in _audit_events(audit_log)
        if e["event_type"] == "central_install_prune_aborted"
    ]
    assert len(aborted) == 1
    assert "protected-versions" in aborted[0]["source"]


def test_prune_abort_dry_run_warns_without_audit(tmp_path, monkeypatch, capsys):
    """Dry-run also aborts pruning, but (like all dry-run paths) writes no audit."""
    monkeypatch.delenv("VNX_PROJECT_REGISTRY", raising=False)
    registry = tmp_path / "projects.json"
    registry.write_text("{ not valid json", encoding="utf-8")
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=True,
        registry_path=registry, audit_log=audit_log,
    )

    assert len(list((tmp_path / "versions").iterdir())) == 5
    captured = capsys.readouterr()
    assert "GC prune ABORTED (fail-closed)" in captured.err
    assert not audit_log.exists()


def test_prune_absent_sources_still_prunes_unprotected(tmp_path, monkeypatch):
    """ABSENT registry + ABSENT protected-versions file = no pins, NOT a failure:
    normal pruning of unprotected candidates still happens."""
    _empty_registry(tmp_path, monkeypatch)
    assert not (tmp_path / "protected-versions").exists()
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)
    audit_log = tmp_path / "events" / "central_install.ndjson"

    _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == ["v1.0.3", "v1.0.4", "v1.0.5"]
    events = _audit_events(audit_log)
    assert sorted(e["pruned_version"] for e in events if e["event_type"] == "central_install_prune") == [
        "v1.0.1", "v1.0.2",
    ]
    assert not [e for e in events if e["event_type"] == "central_install_prune_aborted"]


def test_load_fleet_pins_raises_on_malformed_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("VNX_PROJECT_REGISTRY", raising=False)
    registry = tmp_path / "projects.json"
    registry.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(ProtectionSetUnavailable) as excinfo:
        _load_fleet_pins(registry)

    assert excinfo.value.source == str(registry)
    assert isinstance(excinfo.value.cause, json.JSONDecodeError)


def test_collect_protected_versions_raises_on_unreadable_file(tmp_path, monkeypatch):
    _empty_registry(tmp_path, monkeypatch)
    pfile = tmp_path / "protected-versions"
    pfile.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(ProtectionSetUnavailable) as excinfo:
        _collect_protected_versions(tmp_path)

    assert excinfo.value.source == str(pfile)


def test_vnx_update_dry_run_threads_protect_pins(tmp_path, monkeypatch):
    """End-to-end: vnx_update --dry-run surfaces pin protection in prune plan."""
    _empty_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("VNX_HOME_ROOT", str(tmp_path))
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vnx_update(_update_args(to_version="edge", dry_run=True, protect_pins="v1.0.1"))

    output = buf.getvalue()
    assert rc == 0
    assert "Would protect from prune" in output
    assert len(list((tmp_path / "versions").iterdir())) == 5


# ---------------------------------------------------------------------------
# OI-1379: disk-scan protection source (4). The fleet registry (source 1)
# only protects REGISTERED consumer projects; a project that pins a version
# but was never registered is invisible to it. VNX_PIN_SCAN_ROOTS bounds a
# disk scan for stray .vnx-version files outside the registry.
# ---------------------------------------------------------------------------


def test_prune_protects_pin_found_via_disk_scan(tmp_path, monkeypatch):
    """A .vnx-version pin inside a scanned root protects its version, even
    though the owning project was never registered in the fleet registry —
    the exact OI-1379 gap (pacompany's pin sat outside ~/.vnx/projects.json)."""
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    scan_root = tmp_path / "unregistered-consumers"
    consumer = scan_root / "pacompany" / "build" / "pa-engine"
    consumer.mkdir(parents=True)
    pin_file = consumer / ".vnx-version"
    pin_file.write_text("v1.0.1\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(scan_root))

    audit_log = tmp_path / "events" / "central_install.ndjson"
    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)

    output = buf.getvalue()
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == ["v1.0.1", "v1.0.3", "v1.0.4", "v1.0.5"]
    assert "Protected from prune" in output
    assert str(pin_file) in output

    events = _audit_events(audit_log)
    protected = [e for e in events if e["event_type"] == "central_install_prune_protected"]
    assert len(protected) == 1
    assert protected[0]["protected_version"] == "v1.0.1"
    assert any(str(pin_file) in r for r in protected[0]["reasons"])


def test_prune_disk_scan_ignores_pins_outside_configured_roots(tmp_path, monkeypatch):
    """A pin outside every configured VNX_PIN_SCAN_ROOTS entry does NOT
    protect its version — the scan boundary is explicit, not a silent
    catch-all over the whole filesystem."""
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    scanned_root = tmp_path / "scanned"
    scanned_root.mkdir()
    unscanned = tmp_path / "outside-scan" / "some-project"
    unscanned.mkdir(parents=True)
    (unscanned / ".vnx-version").write_text("v1.0.1\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(scanned_root))

    _prune_old_versions(
        tmp_path, keep_last=3, dry_run=False,
        audit_log=tmp_path / "events" / "central_install.ndjson",
    )

    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    # v1.0.1's pin sits outside the configured scan root: not protected, so
    # it prunes along with the other unprotected oldest candidate.
    assert remaining == ["v1.0.3", "v1.0.4", "v1.0.5"]


def test_prune_aborts_on_unreadable_pin_scan_root(tmp_path, monkeypatch, capsys):
    """A scan root that EXISTS but cannot be listed (chmod 000) => fail
    CLOSED, exactly like an unreadable registry or protected-versions file."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod-000 dirs — OSError path unreachable")
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    scan_root = tmp_path / "locked-scan-root"
    scan_root.mkdir()
    scan_root.chmod(0o000)
    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(scan_root))
    audit_log = tmp_path / "events" / "central_install.ndjson"

    try:
        _prune_old_versions(tmp_path, keep_last=3, dry_run=False, audit_log=audit_log)
    finally:
        scan_root.chmod(0o755)

    # Fail-closed: NO version dir was deleted, not even unprotected v1.0.2.
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    assert remaining == names

    captured = capsys.readouterr()
    assert "GC prune ABORTED (fail-closed)" in captured.err
    assert str(scan_root) in captured.err

    aborted = [
        e for e in _audit_events(audit_log)
        if e["event_type"] == "central_install_prune_aborted"
    ]
    assert len(aborted) == 1
    assert aborted[0]["source"] == str(scan_root)
    assert aborted[0]["keep_last_N"] == 3


def test_prune_dry_run_prints_disk_scan_protection_reason(tmp_path, monkeypatch):
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    scan_root = tmp_path / "unregistered-consumers"
    consumer = scan_root / "some-project"
    consumer.mkdir(parents=True)
    pin_file = consumer / ".vnx-version"
    pin_file.write_text("v1.0.1\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(scan_root))

    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(
            tmp_path, keep_last=3, dry_run=True,
            audit_log=tmp_path / "events" / "central_install.ndjson",
        )

    output = buf.getvalue()
    assert "[dry-run] Would protect from prune:" in output
    assert "v1.0.1" in output
    assert str(pin_file) in output
    assert len(list((tmp_path / "versions").iterdir())) == 5


def test_prune_ignores_pin_under_vnx_data_but_protects_sibling_project_pin(tmp_path, monkeypatch):
    """A .vnx-version copied into a dispatch worktree under .vnx-data/worktrees/
    is runtime state, not a real consumer pin: it must NOT protect its version.
    A .vnx-version on an ordinary project path in the SAME scan root, for a
    DIFFERENT version, still protects normally — the skip is scoped to
    .vnx-data, not the whole scan root."""
    names = ["v1.0.1", "v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    _version_dirs(tmp_path, names)

    scan_root = tmp_path / "consumers"
    dead_worktree = (
        scan_root / "mission-control" / ".vnx-data" / "worktrees"
        / "dispatch-D-1fa3850c"
    )
    dead_worktree.mkdir(parents=True)
    (dead_worktree / ".vnx-version").write_text("v1.0.1\n", encoding="utf-8")

    real_project = scan_root / "pa-engine"
    real_project.mkdir(parents=True)
    real_pin = real_project / ".vnx-version"
    real_pin.write_text("v1.0.2\n", encoding="utf-8")

    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(scan_root))

    buf = io.StringIO()
    with redirect_stdout(buf):
        _prune_old_versions(
            tmp_path, keep_last=3, dry_run=False,
            audit_log=tmp_path / "events" / "central_install.ndjson",
        )

    output = buf.getvalue()
    remaining = sorted(d.name for d in (tmp_path / "versions").iterdir())
    # v1.0.1 (only reachable via the .vnx-data worktree copy) is NOT
    # protected and prunes along with the other unprotected oldest
    # candidate; v1.0.2 (protected via the ordinary project-path pin) survives.
    assert remaining == ["v1.0.2", "v1.0.3", "v1.0.4", "v1.0.5"]
    protected_line = next(
        line for line in output.splitlines() if line.startswith("Protected from prune:")
    )
    assert str(real_pin) in protected_line
    assert "v1.0.2" in protected_line
    assert str(dead_worktree / ".vnx-version") not in output


def test_resolve_pin_scan_roots_defaults_to_home(monkeypatch):
    monkeypatch.delenv("VNX_PIN_SCAN_ROOTS", raising=False)
    assert _resolve_pin_scan_roots() == [Path.home()]


def test_resolve_pin_scan_roots_parses_colon_separated_list(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    roots = _resolve_pin_scan_roots(f"{a}::{b}:")
    assert roots == [a, b]


def test_scan_root_for_pins_respects_max_depth(tmp_path):
    """A pin deeper than max_depth is invisible; the same pin one level
    shallower (still within budget) is found — the depth bound is exact, not
    approximate."""
    deep = tmp_path
    for i in range(3):
        deep = deep / f"level{i}"
    deep.mkdir(parents=True)
    (deep / ".vnx-version").write_text("v9.9.9\n", encoding="utf-8")

    # 3 directory levels below tmp_path -> needs max_depth >= 3 to be found.
    assert _scan_root_for_pins(tmp_path, max_depth=2) == {}
    found = _scan_root_for_pins(tmp_path, max_depth=3)
    assert "9.9.9" in found
    assert any(str(deep / ".vnx-version") in r for r in found["9.9.9"])


def test_scan_root_for_pins_skips_git_node_modules_and_symlinks(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / ".vnx-version").write_text("v1.1.1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / ".vnx-version").write_text("v2.2.2\n", encoding="utf-8")
    dead_worktree = tmp_path / ".vnx-data" / "worktrees" / "dispatch-D-1fa3850c"
    dead_worktree.mkdir(parents=True)
    (dead_worktree / ".vnx-version").write_text("v4.4.4\n", encoding="utf-8")

    real_target = tmp_path / "real-project"
    real_target.mkdir()
    (real_target / ".vnx-version").write_text("v3.3.3\n", encoding="utf-8")
    symlinked = tmp_path / "symlinked-project"
    symlinked.symlink_to(real_target)

    found = _scan_root_for_pins(tmp_path, max_depth=DEFAULT_PIN_SCAN_MAX_DEPTH)
    assert "1.1.1" not in found
    assert "4.4.4" not in found
    assert "2.2.2" not in found
    # The real (non-symlinked) project's own pin is still found directly.
    assert "3.3.3" in found


def test_scan_root_for_pins_absent_root_is_not_a_failure(tmp_path):
    assert _scan_root_for_pins(tmp_path / "does-not-exist", max_depth=4) == {}


def test_scan_disk_for_pins_raises_on_unreadable_root(tmp_path):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read chmod-000 dirs — OSError path unreachable")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(ProtectionSetUnavailable) as excinfo:
            _scan_disk_for_pins(str(locked))
    finally:
        locked.chmod(0o755)
    assert excinfo.value.source == str(locked)
