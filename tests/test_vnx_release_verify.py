#!/usr/bin/env python3
"""`vnx release verify` (dir == tag) and publish's default-branch refusal.

Every test drives the real CLI (``python3 -m vnx_cli.main``) as a subprocess
against temporary git repos and a temporary ``VNX_HOME_ROOT``; nothing imports
the new code, so the red run is "the CLI does not know verify", not ImportError.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _source_repo(path: Path) -> Path:
    """VERSION, the tenant marker, a script, a symlink, a .gitignore, tag v1.0.0."""
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.com", cwd=path)
    _git("config", "user.name", "tester", cwd=path)
    (path / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (path / ".vnx-project-id").write_text("vnx-dev\n", encoding="utf-8")
    (path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (path / "scripts").mkdir()
    (path / "scripts" / "run.py").write_text("print('v1')\n", encoding="utf-8")
    (path / "scripts" / "latest.py").symlink_to("run.py")
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "release 1.0.0", cwd=path)
    _git("tag", "v1.0.0", cwd=path)
    return path


def _published_dir(root: Path, source: Path) -> Path:
    target = root / "versions" / "v1.0.0"
    target.parent.mkdir(parents=True)
    _git("clone", "--quiet", str(source), str(target), cwd=target.parent)
    _git("checkout", "--quiet", "v1.0.0", cwd=target)
    (target / ".vnx-project-id").unlink()
    (target / ".vnx-install-mode").write_text("central\n", encoding="utf-8")
    return target


def _run_cli(root: Path, cwd: Path, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["VNX_HOME_ROOT"] = str(root)
    env["VNX_DATA_DIR"] = str(root.parent / "vnx-data")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-m", "vnx_cli.main", *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True)


def _verify(root, source):
    return _run_cli(root, source, "release", "verify", "v1.0.0")


def _publish_dry(root, source, tag, *extra):
    return _run_cli(root, source, "release", "publish", "--tag", tag,
                    "--repo", str(source), "--dry-run", *extra)


def _side_branch_tag(source: Path, tag: str) -> None:
    """Tag a commit on a branch off main; main does not contain it."""
    _git("checkout", "-q", "-b", f"side-{tag}", cwd=source)
    (source / "VERSION").write_text(f"{tag.lstrip('v')}\n", encoding="utf-8")
    (source / f"side-{tag}.txt").write_text("side work\n", encoding="utf-8")
    _git("add", "-A", cwd=source)
    _git("commit", "-q", "-m", f"side work {tag}", cwd=source)
    _git("tag", tag, cwd=source)
    _git("checkout", "-q", "main", cwd=source)


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "vnx-system"
    path.mkdir()
    return path


@pytest.fixture
def source(tmp_path):
    return _source_repo(tmp_path / "source")


@pytest.fixture
def released(root, source):
    return _published_dir(root, source)


def test_verify_passes_when_only_the_generated_files_differ(root, source, released):
    (released / ".vnx-install-mode").write_text("rewritten\n", encoding="utf-8")
    result = _verify(root, source)
    assert result.returncode == 0, result.stderr
    assert "matches tag 'v1.0.0'" in result.stdout


def test_verify_names_a_changed_file_and_a_moved_head(root, source, released):
    (released / "scripts" / "run.py").write_text("print('edited')\n", encoding="utf-8")
    changed = _verify(root, source)
    assert changed.returncode == 1
    assert "scripts/run.py" in changed.stderr
    _git("checkout", "--", "scripts/run.py", cwd=released)
    _git("-c", "user.name=tester", "-c", "user.email=t@example.com",
         "commit", "--allow-empty", "-q", "-m", "drift", cwd=released)
    moved = _verify(root, source)
    assert moved.returncode == 1
    assert "is not tag 'v1.0.0'" in moved.stderr


def test_verify_fails_on_a_third_deviation(root, source, released):
    """The whitelist is exactly two entries; an extra untracked file is drift."""
    (released / "notes.txt").write_text("stray\n", encoding="utf-8")
    result = _verify(root, source)
    assert result.returncode == 1
    assert "extra notes.txt" in result.stderr


def test_verify_names_a_skip_worktree_file_changed_on_disk(root, source, released):
    """git status hides a skip-worktree file; the bytes on disk do not.

    The index flag is what the tag's blobs are compared without: the file is
    modified, the flag is set, and the only thing that can tell is the content.
    """
    (released / "scripts" / "run.py").write_text("print('edited')\n", encoding="utf-8")
    _git("update-index", "--skip-worktree", "scripts/run.py", cwd=released)
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=str(released), capture_output=True, text=True)
    assert "scripts/run.py" not in status.stdout, "the index flag must hide the change"
    result = _verify(root, source)
    assert result.returncode == 1, result.stdout
    assert "changed scripts/run.py" in result.stderr


def test_verify_names_an_ignored_extra_file(root, source, released):
    """An extra file covered by .gitignore is a file in the release all the same."""
    (released / "debug.log").write_text("ignored\n", encoding="utf-8")
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=str(released), capture_output=True, text=True)
    assert "debug.log" not in status.stdout, ".gitignore must hide the file from git"
    result = _verify(root, source)
    assert result.returncode == 1
    assert "extra debug.log" in result.stderr


def test_verify_names_a_changed_exec_bit_then_a_missing_file(root, source, released):
    """The exec bit is a difference, and an absent tag file is named, not ignored."""
    os.chmod(released / "scripts" / "run.py", 0o755)
    mode = _verify(root, source)
    assert mode.returncode == 1
    assert "mode scripts/run.py" in mode.stderr
    assert "changed scripts/run.py" not in mode.stderr
    os.chmod(released / "scripts" / "run.py", 0o644)
    (released / "scripts" / "run.py").unlink()
    missing = _verify(root, source)
    assert missing.returncode == 1
    assert "missing scripts/run.py" in missing.stderr


def test_verify_names_a_symlink_whose_target_moved(root, source, released):
    """A symlink is compared by its target bytes, never by what it resolves to."""
    link = released / "scripts" / "latest.py"
    link.unlink()
    link.symlink_to("VERSION")
    result = _verify(root, source)
    assert result.returncode == 1
    assert "changed scripts/latest.py" in result.stderr


def test_verify_names_a_gitlink_instead_of_skipping_it(root, tmp_path):
    """A submodule in the tag cannot be compared here, so it is never a pass."""
    path = _source_repo(tmp_path / "with-sub")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path),
                          capture_output=True, text=True, check=True).stdout.strip()
    _git("update-index", "--add", "--cacheinfo", f"160000,{head},vendor", cwd=path)
    _git("commit", "-q", "-m", "vendor gitlink", cwd=path)
    _git("tag", "-f", "v1.0.0", cwd=path)
    published = _published_dir(root, path)
    assert published.is_dir()
    result = _verify(root, path)
    assert result.returncode == 1
    assert "submodule vendor: not verified" in result.stderr


def test_verify_fails_closed_when_the_tag_tree_is_unreadable(root, source, released):
    """A tag pointing at a commit the dir does not have is a problem, not a pass."""
    (source / "later.txt").write_text("later\n", encoding="utf-8")
    _git("add", "-A", cwd=source)
    _git("commit", "-q", "-m", "later work", cwd=source)
    _git("tag", "-f", "v1.0.0", cwd=source)
    result = _verify(root, source)
    assert result.returncode == 1
    assert "cannot read the tree of tag" in result.stderr


def test_publish_refuses_a_tag_outside_the_default_branch(root, source):
    """Ancestry is the rule: a side-branch tag is refused until its commit is
    merged into the default branch, then accepted."""
    _side_branch_tag(source, "v1.0.1")
    refused = _publish_dry(root, source, "v1.0.1")
    assert refused.returncode == 1
    assert "NOT on the default branch" in refused.stderr
    assert "--allow-unmerged-tag" in refused.stderr
    assert not (root / "versions").exists()
    _git("merge", "-q", "--no-ff", "-m", "merge v1.0.1", "side-v1.0.1", cwd=source)
    accepted = _publish_dry(root, source, "v1.0.1")
    assert accepted.returncode == 0, accepted.stderr
    assert "Default-branch guard OK" in accepted.stdout


def test_publish_names_the_override_reason_and_refuses_an_empty_one(root, source):
    _side_branch_tag(source, "v1.0.1")
    reason = "hotfix cut from a side branch, signed off"
    allowed = _publish_dry(root, source, "v1.0.1", "--allow-unmerged-tag", reason)
    assert allowed.returncode == 0, allowed.stderr
    assert reason in allowed.stderr
    empty = _publish_dry(root, source, "v1.0.1", "--allow-unmerged-tag", "   ")
    assert empty.returncode == 1
    assert "non-empty reason" in empty.stderr
    assert not (root / "versions").exists()
