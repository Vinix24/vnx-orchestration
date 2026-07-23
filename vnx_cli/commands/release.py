#!/usr/bin/env python3
"""vnx release publish — materialize an immutable central version from a git tag.

Given a git tag, produce ``<root>/versions/<tag>/`` via install-central.sh
(materialize-only: no shim install, no ``current`` flip), stamp the
install-mode marker, and — only with explicit ``--set-current`` — atomically
flip ``current -> <tag>``. Published versions are immutable: publishing a tag
whose version dir already exists is refused, never overwritten.

The live cutover (``--set-current``) is an operator action; the default is
publish-without-cutover so a release can be staged and verified first.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from vnx_cli.commands.update import (
    INSTALL_MODE_MARKER,
    VNX_GIT_REMOTE,
    _atomic_symlink_flip,
    _current_target,
    _emit_audit_event,
    _git_toplevel,
    _resolve_root,
    _validate_version_name,
    _write_install_marker,
)

INSTALL_CENTRAL_SCRIPT = Path(__file__).resolve().parent.parent.parent / "install-central.sh"


def _resolve_install_central() -> Path:
    env = os.environ.get("VNX_INSTALL_CENTRAL_SCRIPT")
    if env:
        return Path(env).expanduser().resolve()
    return INSTALL_CENTRAL_SCRIPT


def _resolve_repo(args) -> str:
    repo = getattr(args, "repo", None)
    if repo:
        return repo
    toplevel = _git_toplevel(Path.cwd())
    if toplevel is not None:
        return str(toplevel)
    return VNX_GIT_REMOTE


def _tag_exists(repo: str, tag: str) -> bool:
    """True when ``refs/tags/<tag>`` exists in ``repo`` (local path or remote URL)."""
    if Path(repo).is_dir():
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            capture_output=True,
        )
        return result.returncode == 0
    result = subprocess.run(
        ["git", "ls-remote", "--tags", repo, tag],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def _materialize_from_tag(root: Path, repo: str, tag: str) -> Path:
    """Materialize ``versions/<tag>/`` from ``repo``'s tag via install-central.sh.

    Clones the source repo into a temp dir, checks out the tag, then runs
    ``install-central.sh --materialize-only`` so the version dir is produced
    by the same code path as a normal central install (immutable layout,
    tenant-marker strip) — without touching ``current`` or the shim.
    """
    target_dir = root / "versions" / tag
    install_central = _resolve_install_central()
    if not install_central.is_file():
        raise FileNotFoundError(f"install-central.sh not found: {install_central}")

    tmp = Path(tempfile.mkdtemp(prefix="vnx-release-"))
    try:
        checkout = tmp / "checkout"
        print(f"Cloning {repo} -> {checkout} ...")
        subprocess.run(["git", "clone", "--quiet", repo, str(checkout)], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", tag], check=True
        )
        subprocess.run(
            [
                "bash", str(install_central),
                "--version", tag,
                "--source", str(checkout),
                "--target", str(root),
                "--materialize-only",
            ],
            check=True,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not target_dir.is_dir():
        raise RuntimeError(
            f"materialization reported success but {target_dir} does not exist"
        )
    _write_install_marker(target_dir)
    return target_dir


def vnx_release_publish(args) -> int:
    tag: "str | None" = getattr(args, "tag", None)
    dry_run: bool = getattr(args, "dry_run", False)
    set_current: bool = getattr(args, "set_current", False)

    if not tag:
        print("Error: --tag <vX.Y.Z> is required.", file=sys.stderr)
        return 1
    try:
        tag = _validate_version_name(tag)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    root = _resolve_root()
    target_dir = root / "versions" / tag

    # Immutability: a published version is never overwritten.
    if target_dir.exists():
        print(
            f"Error: version '{tag}' already exists at {target_dir} — "
            "published versions are immutable; refusing to overwrite.",
            file=sys.stderr,
        )
        return 1

    repo = _resolve_repo(args)

    try:
        if not _tag_exists(repo, tag):
            print(f"Error: tag '{tag}' not found in {repo}", file=sys.stderr)
            return 1
    except FileNotFoundError:
        print("Error: git executable not found in PATH", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: git operation failed: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] VNX_HOME_ROOT: {root}")
        print(
            f"[dry-run] Would materialize tag '{tag}' from {repo} -> {target_dir} "
            f"(install-central.sh --version {tag} --materialize-only)"
        )
        print(f"[dry-run] Would write {INSTALL_MODE_MARKER}=central into {target_dir}")
        if set_current:
            current = _current_target(root)
            current_name = current.name if current else None
            print(
                f"[dry-run] Would flip current ({current_name}) -> {tag} "
                "(--set-current passed)"
            )
        else:
            print("[dry-run] current NOT flipped (publish only; pass --set-current to cut over)")
        return 0

    try:
        target_dir = _materialize_from_tag(root, repo, tag)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: materialization failed: {exc}", file=sys.stderr)
        return 1
    except (RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _emit_audit_event(
        "central_release_publish",
        {"tag": tag, "version_dir": str(target_dir), "set_current": set_current},
    )
    print(f"Published: {target_dir}")

    if set_current:
        _atomic_symlink_flip(root, target_dir, dry_run=False)
    else:
        print("current NOT flipped (publish only; pass --set-current to cut over).")

    return 0


def vnx_release(args) -> int:
    if getattr(args, "release_subcommand", None) == "publish":
        return vnx_release_publish(args)
    print("Error: a release subcommand is required (publish).", file=sys.stderr)
    return 1
