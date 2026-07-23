#!/usr/bin/env python3
"""Tests for vnx release publish (git tag -> immutable central version)."""

import io
import json
import stat
import subprocess
import sys
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.release import (
    vnx_release,
    vnx_release_publish,
    _tag_exists,
)
from vnx_cli.commands.update import INSTALL_MODE_MARKER, INSTALL_MODE_VALUE


STUB_INSTALL_CENTRAL = """#!/usr/bin/env bash
# Hermetic stand-in for install-central.sh --materialize-only: parse the flags
# and materialize an (empty) immutable version dir, nothing else.
set -euo pipefail
VERSION=""
TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --target)  TARGET="$2";  shift 2 ;;
    --source)  shift 2 ;;
    --materialize-only) shift ;;
    *) echo "stub: unknown arg $1" >&2; exit 1 ;;
  esac
done
[ -n "$VERSION" ] && [ -n "$TARGET" ]
mkdir -p "${TARGET}/versions/${VERSION}"
echo "stub-materialized ${VERSION}"
"""


def _git_repo_with_tag(path: Path, tag: str, *, extra_tags=()) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    for t in (tag, *extra_tags):
        subprocess.run(["git", "tag", t], cwd=path, check=True)
    return path


@pytest.fixture
def stub_install_central(tmp_path, monkeypatch):
    stub = tmp_path / "install-central.sh"
    stub.write_text(STUB_INSTALL_CENTRAL, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("VNX_INSTALL_CENTRAL_SCRIPT", str(stub))
    return stub


@pytest.fixture
def central_root(tmp_path, monkeypatch):
    """Isolated central store + isolated audit log location."""
    root = tmp_path / "vnx-system"
    monkeypatch.setenv("VNX_HOME_ROOT", str(root))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "vnx-data"))
    return root


def _publish_args(*, tag="v1.3.1", repo=None, dry_run=False, set_current=False):
    return Namespace(
        tag=tag,
        repo=repo,
        dry_run=dry_run,
        set_current=set_current,
    )


def _capture(args):
    out_buf = io.StringIO()
    with redirect_stdout(out_buf):
        rc = vnx_release_publish(args)
    return out_buf.getvalue(), rc


def _audit_events(central_root: Path):
    log = central_root.parent / "vnx-data" / "events" / "central_install.ndjson"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().strip().splitlines()]


# ---------------------------------------------------------------------------
# _tag_exists
# ---------------------------------------------------------------------------

def test_tag_exists_true_for_local_repo(tmp_path):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    assert _tag_exists(str(repo), "v1.3.1") is True


def test_tag_exists_false_for_missing_tag(tmp_path):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    assert _tag_exists(str(repo), "v9.9.9") is False


# ---------------------------------------------------------------------------
# publish --dry-run
# ---------------------------------------------------------------------------

def test_dry_run_prints_planned_materialize(tmp_path, central_root):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo), dry_run=True))

    assert rc == 0
    assert "[dry-run]" in out
    assert "materialize" in out
    assert "v1.3.1" in out
    assert str(central_root / "versions" / "v1.3.1") in out
    # Dry-run mutates nothing.
    assert not (central_root / "versions").exists()
    assert not (central_root / "current").exists()


def test_dry_run_reports_no_flip_by_default(tmp_path, central_root):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo), dry_run=True, set_current=False))

    assert rc == 0
    assert "current NOT flipped" in out


def test_dry_run_reports_planned_flip_with_set_current(tmp_path, central_root):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo), dry_run=True, set_current=True))

    assert rc == 0
    assert "Would flip current" in out
    assert not (central_root / "current").exists()


# ---------------------------------------------------------------------------
# Immutability + validation refusals (apply to dry-run AND real runs)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dry_run", [True, False])
def test_publish_refuses_already_existing_version(tmp_path, central_root, capsys, dry_run):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    existing = central_root / "versions" / "v1.3.1"
    existing.mkdir(parents=True)
    sentinel = existing / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")

    rc = vnx_release_publish(_publish_args(repo=str(repo), dry_run=dry_run))

    assert rc == 1
    captured = capsys.readouterr()
    assert "immutable" in captured.err
    assert "already exists" in captured.err
    # The existing dir is never touched.
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"


def test_publish_refuses_missing_tag(tmp_path, central_root, capsys):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")

    rc = vnx_release_publish(_publish_args(tag="v9.9.9", repo=str(repo)))

    assert rc == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err
    assert not (central_root / "versions").exists()


def test_publish_rejects_invalid_tag_name(tmp_path, central_root, capsys):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")

    rc = vnx_release_publish(_publish_args(tag="../../outside", repo=str(repo)))

    assert rc == 1
    captured = capsys.readouterr()
    assert "invalid version name" in captured.err
    assert not (central_root / "versions").exists()


def test_publish_requires_tag(central_root, capsys):
    rc = vnx_release_publish(_publish_args(tag=None))

    assert rc == 1
    captured = capsys.readouterr()
    assert "--tag" in captured.err


# ---------------------------------------------------------------------------
# Real publish (hermetic: stub install-central.sh, local git repo, temp root)
# ---------------------------------------------------------------------------

def test_publish_materializes_version_dir_and_marker(
    tmp_path, central_root, stub_install_central
):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo)))

    assert rc == 0
    target_dir = central_root / "versions" / "v1.3.1"
    assert target_dir.is_dir()
    marker = target_dir / INSTALL_MODE_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE
    assert "Published:" in out

    events = _audit_events(central_root)
    publish_events = [e for e in events if e["event_type"] == "central_release_publish"]
    assert len(publish_events) == 1
    assert publish_events[0]["tag"] == "v1.3.1"
    assert publish_events[0]["set_current"] is False
    assert publish_events[0]["version_dir"] == str(target_dir)


def test_publish_does_not_flip_current_by_default(
    tmp_path, central_root, stub_install_central
):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo), set_current=False))

    assert rc == 0
    assert not (central_root / "current").exists()
    assert "current NOT flipped" in out


def test_publish_set_current_flips_atomically(
    tmp_path, central_root, stub_install_central
):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo), set_current=True))

    assert rc == 0
    target_dir = central_root / "versions" / "v1.3.1"
    current = central_root / "current"
    assert current.is_symlink()
    assert current.resolve() == target_dir.resolve()
    assert "Activated:" in out

    events = _audit_events(central_root)
    flip_events = [e for e in events if e["event_type"] == "central_install_update"]
    assert any(e["phase"] == "before_flip" for e in flip_events)
    assert any(
        e["phase"] == "after_flip" and e["to_version"] == "v1.3.1" and e["success"] is True
        for e in flip_events
    )


def test_publish_second_run_refused_after_first_publish(
    tmp_path, central_root, stub_install_central, capsys
):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    _, rc = _capture(_publish_args(repo=str(repo)))
    assert rc == 0

    # Re-publishing the same tag must be refused (immutability).
    rc = vnx_release_publish(_publish_args(repo=str(repo)))
    assert rc == 1
    captured = capsys.readouterr()
    assert "immutable" in captured.err


def test_publish_real_install_central_materialize_only(
    tmp_path, central_root, monkeypatch
):
    """Integration with the REAL install-central.sh --materialize-only: the
    version dir is produced, `current` and the shim are NOT touched."""
    monkeypatch.delenv("VNX_INSTALL_CENTRAL_SCRIPT", raising=False)
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    out, rc = _capture(_publish_args(repo=str(repo)))

    assert rc == 0, out
    target_dir = central_root / "versions" / "v1.3.1"
    assert target_dir.is_dir()
    assert (target_dir / "README.md").is_file()  # cloned content, not an empty dir
    assert (target_dir / INSTALL_MODE_MARKER).is_file()
    # Materialize-only: no cutover, no shim, no verify-install artifacts.
    assert not (central_root / "current").exists()
    assert not (central_root / "bin").exists()


# ---------------------------------------------------------------------------
# release dispatcher
# ---------------------------------------------------------------------------

def test_release_requires_subcommand(capsys):
    rc = vnx_release(Namespace(release_subcommand=None))

    assert rc == 1
    captured = capsys.readouterr()
    assert "subcommand" in captured.err


def test_release_dispatches_publish(tmp_path, central_root, monkeypatch):
    repo = _git_repo_with_tag(tmp_path / "origin", "v1.3.1")
    args = Namespace(
        release_subcommand="publish",
        tag="v1.3.1",
        repo=str(repo),
        dry_run=True,
        set_current=False,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = vnx_release(args)

    assert rc == 0
    assert "[dry-run]" in buf.getvalue()
