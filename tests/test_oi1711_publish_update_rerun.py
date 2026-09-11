#!/usr/bin/env python3
"""OI-1711 — the publication path must work more than once.

Two independent defects in the same flow, kept apart in these tests:

Defect 1 (publish side): ``vnx release publish`` materializes via
install-central.sh from a temp checkout under /var/folders and leaves that
temp path as the published dir's ``origin``. The temp dir is deleted right
after, so the origin points at nothing.

Defect 2 (update side): ``vnx update --to <tag>`` on an existing version dir
takes ``git pull --ff-only``, which can never succeed on a tag clone: the
clone (``--branch <tag> --depth 1``) has a detached HEAD with no upstream.

Acceptance criterion, literally: publish followed by ``update --to`` on the
SAME tag works twice in a row. The second run is the point; the first always
worked (as long as the target dir did not exist yet).

Every measurement carries two controls: something that MUST be found (e.g.
origin == VNX_GIT_REMOTE after publish) and something that must NOT be found
(e.g. a /var/folders temp path as origin, a pull failure on a detached HEAD).
"""

import io
import subprocess
import sys
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import vnx_cli.commands.release as release_module
import vnx_cli.commands.update as update_module
from vnx_cli.commands.release import vnx_release_publish
from vnx_cli.commands.update import (
    INSTALL_MODE_MARKER,
    INSTALL_MODE_VALUE,
    _fetch_version,
    vnx_update,
)

TAG = "v9.9.9"
OLD_TAG = "v9.9.8"


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _git_repo_with_tag(path: Path, tag: "str | None" = TAG) -> Path:
    """A local git repo standing in for the canonical remote (offline).

    Carries a VERSION file agreeing with ``tag`` so the publish version-guard
    passes, and a normalized ``main`` branch so ``edge`` clones work.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=path, check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    version = tag.lstrip("vV") if tag else "0.0.0"
    (path / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", "VERSION"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, check=True)
    if tag:
        subprocess.run(["git", "tag", tag], cwd=path, check=True)
    return path


def _commit_file(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {name}"], cwd=repo, check=True)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def central_root(tmp_path, monkeypatch):
    """Isolated central store, audit log, registry and pin-scan roots.

    Without the registry/pin-scan isolation, ``vnx_update``'s prune sweep
    would read the operator's REAL ~/.vnx/projects.json and scan the REAL
    $HOME for .vnx-version pins.
    """
    root = tmp_path / "vnx-system"
    monkeypatch.setenv("VNX_HOME_ROOT", str(root))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "vnx-data"))
    monkeypatch.setenv("VNX_PIN_SCAN_ROOTS", str(tmp_path / "empty-pin-scan-root"))
    monkeypatch.setenv("VNX_PROJECT_REGISTRY", str(tmp_path / "no-projects.json"))
    return root


@pytest.fixture
def real_install_central(monkeypatch):
    """Use the repo's real install-central.sh (the temp-checkout clone is
    exactly where defect 1 lives — a stub would hide it)."""
    monkeypatch.delenv("VNX_INSTALL_CENTRAL_SCRIPT", raising=False)


@pytest.fixture
def no_fleet_migration(monkeypatch):
    """Stub the post-update fleet store-migration sweep: it is best-effort
    and orthogonal to the fetch path under test, and must never touch real
    per-project stores from a test run."""
    monkeypatch.setattr(
        "vnx_cli.commands.migrate.migrate_all_central_stores", lambda: 0
    )


def _use_as_canonical_remote(monkeypatch, origin: Path) -> None:
    """Patch VNX_GIT_REMOTE in BOTH command modules (release.py binds its own
    imported copy; update.py reads its module global)."""
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))
    monkeypatch.setattr(release_module, "VNX_GIT_REMOTE", str(origin))


def _publish_args(*, tag=TAG, repo=None) -> Namespace:
    return Namespace(tag=tag, repo=repo, dry_run=False, set_current=False)


def _update_args(*, to_version=TAG) -> Namespace:
    return Namespace(
        to_version=to_version,
        keep_last=3,
        dry_run=False,
        rollback=False,
        protect_pins=None,
    )


def _run_publish(args) -> "tuple[str, str, int]":
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = vnx_release_publish(args)
    return out.getvalue(), err.getvalue(), rc


def _run_update(args) -> "tuple[str, str, int]":
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = vnx_update(args)
    return out.getvalue(), err.getvalue(), rc


def _origin_url(version_dir: Path) -> str:
    return _git(version_dir, "remote", "get-url", "origin").stdout.strip()


def _head_sha(version_dir: Path) -> str:
    return _git(version_dir, "rev-parse", "HEAD").stdout.strip()


def _head_branch(version_dir: Path) -> "str | None":
    result = _git(version_dir, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# ---------------------------------------------------------------------------
# Defect 1 — publish must not leave a vanished temp checkout as origin
# ---------------------------------------------------------------------------

def test_publish_sets_origin_to_canonical_remote(
    tmp_path, central_root, real_install_central
):
    """MUST find: origin == VNX_GIT_REMOTE. Must NOT find: the deleted
    /var/folders temp checkout path anywhere in the dir's remotes."""
    origin = _git_repo_with_tag(tmp_path / "origin", TAG)

    out, err, rc = _run_publish(_publish_args(tag=TAG, repo=str(origin)))
    assert rc == 0, err

    target_dir = central_root / "versions" / TAG
    assert target_dir.is_dir()

    remote_v = _git(target_dir, "remote", "-v").stdout
    # Positive control: an origin exists, and it is the canonical remote
    # (this test does NOT patch VNX_GIT_REMOTE, so it must be the real URL).
    assert "origin" in remote_v
    assert _origin_url(target_dir) == release_module.VNX_GIT_REMOTE
    # Negative controls: no vanished publish-temp path left behind.
    assert "vnx-release-" not in remote_v
    assert "/var/folders" not in remote_v


def test_publish_sets_origin_exactly_to_vnx_git_remote(
    tmp_path, central_root, real_install_central, monkeypatch
):
    """The published dir's origin is re-pointed at the canonical remote so a
    later ``vnx update`` fetches from the same place it clones from."""
    origin = _git_repo_with_tag(tmp_path / "origin", TAG)
    _use_as_canonical_remote(monkeypatch, origin)

    out, err, rc = _run_publish(_publish_args(tag=TAG, repo=str(origin)))
    assert rc == 0, err

    target_dir = central_root / "versions" / TAG
    # Positive control.
    assert _origin_url(target_dir) == str(origin)
    # Negative control.
    assert "vnx-release-" not in _git(target_dir, "remote", "-v").stdout


# ---------------------------------------------------------------------------
# Acceptance — publish, then ``update --to`` the SAME tag, twice
# ---------------------------------------------------------------------------

def test_publish_then_update_to_same_tag_works_twice(
    tmp_path, central_root, real_install_central, no_fleet_migration, monkeypatch
):
    """The literal acceptance criterion. On the broken code the FIRST update
    already fails: publish left a deleted temp path as origin (defect 1) and
    the pull branch cannot work on the tag's detached HEAD anyway (defect 2).
    """
    origin = _git_repo_with_tag(tmp_path / "origin", TAG)
    _use_as_canonical_remote(monkeypatch, origin)

    out, err, rc = _run_publish(_publish_args(tag=TAG, repo=str(origin)))
    assert rc == 0, err
    target_dir = central_root / "versions" / TAG
    assert target_dir.is_dir()

    # First update --to the same tag: the target dir already exists.
    out1, err1, rc1 = _run_update(_update_args(to_version=TAG))
    assert rc1 == 0, f"first update failed: {err1}"
    assert "Error" not in err1

    # Second update --to the same tag: the whole point of OI-1711. A
    # tag-pinned dir already at the target tag is DONE, not broken.
    out2, err2, rc2 = _run_update(_update_args(to_version=TAG))
    assert rc2 == 0, f"second update failed: {err2}"
    assert "already at" in out2
    assert "Error" not in err2

    # Controls: origin is the canonical remote, never the publish temp dir.
    # (The fixture remote itself lives under pytest's tmp_path — which on
    # macOS is also /var/folders — so the meaningful negative here is the
    # publish temp-checkout prefix, not the folder root. The unprefixed
    # /var/folders negative lives in the unpatched-remote publish test.)
    assert _origin_url(target_dir) == str(origin)
    remote_v = _git(target_dir, "remote", "-v").stdout
    assert "vnx-release-" not in remote_v
    # The marker self-heal still ran on the no-op path.
    marker = target_dir / INSTALL_MODE_MARKER
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == INSTALL_MODE_VALUE


# ---------------------------------------------------------------------------
# Defect 2 — the update pull branch on a tag-pinned (detached HEAD) dir
# ---------------------------------------------------------------------------

def test_update_existing_tag_dir_at_target_is_noop(tmp_path, central_root, capsys, monkeypatch):
    """Second ``_fetch_version`` for the same tag: HEAD already sits on the
    tag commit, so the right outcome is a no-op with a clear message — no
    pull, no error. On the broken code ``git pull --ff-only`` on the detached
    HEAD raises CalledProcessError here."""
    origin = _git_repo_with_tag(tmp_path / "origin", TAG)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))
    root = central_root

    target_dir = _fetch_version(root, TAG, dry_run=False)
    assert _head_branch(target_dir) is None  # tag clone => detached HEAD
    sha_before = _head_sha(target_dir)

    target_again = _fetch_version(root, TAG, dry_run=False)

    assert target_again == target_dir
    out = capsys.readouterr().out
    assert "already at" in out
    assert "nothing to pull" in out
    # No-op means no-op: HEAD untouched, still detached at the same commit.
    assert _head_sha(target_dir) == sha_before
    assert _head_branch(target_dir) is None


def test_update_detached_dir_below_target_fetches_and_checks_out_tag(
    tmp_path, central_root, capsys, monkeypatch
):
    """A tag dir whose HEAD is NOT at the target ref is re-pinned by fetch +
    explicit checkout of the tag — the move that works for a tag, instead of
    a ``pull --ff-only`` that only ever worked for a branch."""
    origin = _git_repo_with_tag(tmp_path / "origin", None)
    old_sha = _git(origin, "rev-parse", "HEAD").stdout.strip()
    subprocess.run(["git", "tag", OLD_TAG], cwd=origin, check=True)
    new_sha = _commit_file(origin, "NEWER.md", "newer\n")
    subprocess.run(["git", "tag", TAG], cwd=origin, check=True)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))

    # Simulate a dir pinned BELOW the target: cloned at the older tag under
    # the target's name.
    target_dir = central_root / "versions" / TAG
    subprocess.run(
        ["git", "clone", "--quiet", "--branch", OLD_TAG, "--depth", "1",
         str(origin), str(target_dir)],
        check=True,
    )
    assert _head_sha(target_dir) == old_sha
    assert _head_branch(target_dir) is None

    result = _fetch_version(central_root, TAG, dry_run=False)

    assert result == target_dir
    out = capsys.readouterr().out
    assert "checkout" in out.lower() or "Fetching" in out
    # Re-pinned at the target tag, detached as a tag clone should be.
    assert _head_sha(target_dir) == new_sha
    assert _head_branch(target_dir) is None


def test_update_repairs_origin_pointing_at_vanished_path(
    tmp_path, central_root, capsys, monkeypatch
):
    """An origin that points at a deleted path (exactly what a pre-fix
    publish left behind) is repaired to the canonical remote with a clear
    message — not git's opaque ``fatal: ... does not appear to be a git
    repository`` surfacing as an unexplained failure."""
    origin = _git_repo_with_tag(tmp_path / "origin", TAG)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))

    target_dir = _fetch_version(central_root, TAG, dry_run=False)

    # Break the origin the way defect 1 did: point it at a path that does
    # not exist. (Dir is read-only after the pin lock — unlock to tamper.)
    vanished = tmp_path / "vanished-checkout"
    subprocess.run(["chmod", "-R", "u+w", str(target_dir)], check=True)
    _git(target_dir, "remote", "set-url", "origin", str(vanished))
    assert _origin_url(target_dir) == str(vanished)
    assert not vanished.exists()

    result = _fetch_version(central_root, TAG, dry_run=False)

    assert result == target_dir
    out = capsys.readouterr().out
    # Positive control: the repair is named, the canonical remote is set.
    assert "Repaired" in out
    assert _origin_url(target_dir) == str(origin)
    # Negative control: no unexplained git fatal, and HEAD still on the tag.
    assert "does not appear to be a git repository" not in out
    assert _head_branch(target_dir) is None


# ---------------------------------------------------------------------------
# Regression guard — the ``edge`` (branch) path must keep working as before
# ---------------------------------------------------------------------------

def test_update_edge_existing_dir_still_pulls_new_commits(
    tmp_path, central_root, monkeypatch
):
    """``--to edge`` clones branch main with an attached HEAD; the existing
    ``pull --ff-only`` behaviour for that case is correct and must survive
    the tag fix. This guard passes on the old code too — it exists to prove
    the fix does not break the branch path."""
    origin = _git_repo_with_tag(tmp_path / "origin", None)
    monkeypatch.setattr(update_module, "VNX_GIT_REMOTE", str(origin))

    target_dir = _fetch_version(central_root, "edge", dry_run=False)
    assert _head_branch(target_dir) == "main"

    new_sha = _commit_file(origin, "SECOND.md", "second\n")

    target_again = _fetch_version(central_root, "edge", dry_run=False)

    assert target_again == target_dir
    assert _head_sha(target_dir) == new_sha
    assert (target_dir / "SECOND.md").is_file()
    # edge stays attached to its branch — the pull path, not a detach.
    assert _head_branch(target_dir) == "main"
