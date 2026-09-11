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
    CutoverRefusedError,
    _atomic_symlink_flip,
    _current_target,
    _emit_audit_event,
    _git_toplevel,
    _origin_url,
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


def _read_tag_version(repo: str, tag: str) -> str:
    """Read the ``VERSION`` file content from ``repo``'s tag tree.

    Fetches only the file at the tag (no full checkout), so the version-guard
    check runs before any materialization. Returns the trimmed content.
    """
    result = subprocess.run(
        ["git", "show", f"{tag}:VERSION"],
        cwd=repo if Path(repo).is_dir() else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _strip_leading_v(tag: str) -> str:
    """Strip a single decorative ``v``/``V`` before a digit (``v1.4.5`` -> ``1.4.5``)."""
    if len(tag) > 1 and tag[0] in "vV" and tag[1].isdigit():
        return tag[1:]
    return tag


def _check_version_matches_tag(repo: str, tag: str) -> "tuple[bool, str, str]":
    """Verify the tag tree's ``VERSION`` matches the tag name.

    Returns ``(ok, tree_version, tag_version)`` where ``tag_version`` is the
    tag with its leading ``v`` stripped. On mismatch ``ok`` is False and the
    caller MUST refuse (no escape: a mislabeled release cannot ship).
    """
    tree_version = _read_tag_version(repo, tag)
    tag_version = _strip_leading_v(tag)
    return (tree_version == tag_version, tree_version, tag_version)


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
    _set_origin_remote(target_dir)
    _write_install_marker(target_dir)
    return target_dir


def _set_origin_remote(version_dir: Path) -> None:
    """Point a freshly published version dir's ``origin`` at the canonical remote.

    install-central.sh clones from the temp checkout publish made of the
    source repo, so the materialized dir's origin is a temp path that
    publish deletes right after — every later git fetch/pull in that dir
    would fail against a vanished origin (OI-1711 defect 1). Re-point origin
    at ``VNX_GIT_REMOTE`` (the remote ``vnx update`` clones from and fetches
    against) before the dir is considered published. Goes through the same
    ``writeable_version_dir`` unlock/relock route ``vnx update`` uses — the
    dir is already read-only at this point for pinned versions. A
    materialized dir that is not a git checkout at all (test stubs that do
    not clone) is skipped with a warning instead of failing the publish.
    """
    if _git_toplevel(version_dir) != version_dir:
        print(f"[warn] {version_dir} is not a git checkout — origin left untouched")
        return
    from vnx_cli import _engine
    _engine.ensure_engine_on_path()
    from vnx_version_ro import writeable_version_dir
    with writeable_version_dir(version_dir):
        existing = _origin_url(version_dir)
        if existing is None:
            subprocess.run(
                ["git", "-C", str(version_dir), "remote", "add", "origin", VNX_GIT_REMOTE],
                check=True,
            )
        elif existing != VNX_GIT_REMOTE:
            subprocess.run(
                ["git", "-C", str(version_dir), "remote", "set-url", "origin", VNX_GIT_REMOTE],
                check=True,
            )
        else:
            return
    print(f"Origin set: {version_dir} -> {VNX_GIT_REMOTE}")


def _release_notes_hint(tag: str) -> str:
    """Release-note fragment for a release that moved ``current`` (OI-1183).

    Release notes are written by hand into ``CHANGELOG.md`` (keep-a-changelog);
    there is no automated generator. A publish that flips ``current`` is a
    release that moves ``current``, so it must state the pin consequence in its
    release notes instead of letting operators assume every consumer followed.
    Returns a paste-ready block naming the mandatory pin-update line.
    """
    return (
        f"Release notes (add to CHANGELOG.md under [{tag}]):\n"
        f"  - {tag} is now the active `current`.\n"
        f"  - Consumers with a tracked `.vnx-version` pin are NOT moved by this "
        f"flip and must update their pin to {tag} explicitly:\n"
        f"      echo '{tag}' > <project>/.vnx-version\n"
    )


def _current_flip_warning(tag: str) -> str:
    """Active warning emitted to stderr when a publish flips ``current``.

    A project pinned via ``.vnx-version`` keeps resolving its pin after the
    flip, so the flip alone does not move it. Silently flipping ``current``
    would leave operators assuming every consumer is now on ``tag`` (OI-1183).
    """
    return (
        f"WARNING: `current` now points at {tag}. Consumers with a tracked "
        f"`.vnx-version` pin do not follow `current` and must update their pin "
        f"to {tag} (echo '{tag}' > <project>/.vnx-version)."
    )


def vnx_release_publish(args) -> int:
    tag: "str | None" = getattr(args, "tag", None)
    dry_run: bool = getattr(args, "dry_run", False)
    set_current: bool = getattr(args, "set_current", False)
    cutover_reason: "str | None" = getattr(args, "cutover_reason", None)

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

    # OI-1070: refuse to publish a tag whose tree VERSION disagrees with the
    # tag name. A mislabeled release (tree says 1.4.3, tag says v1.4.4) would
    # otherwise silently ship as the wrong version. No --force escape: this
    # is exactly the accident the guard exists to prevent. --dry-run performs
    # the same check and surfaces it, so the mismatch is visible before any
    # commit to a publish.
    try:
        ok, tree_version, tag_version = _check_version_matches_tag(repo, tag)
    except subprocess.CalledProcessError as exc:
        print(
            f"Error: cannot read VERSION from tag '{tag}' in {repo}: {exc.stderr.strip() or exc}",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError:
        print("Error: git executable not found in PATH", file=sys.stderr)
        return 1
    if not ok:
        print(
            f"Error: VERSION mismatch for tag '{tag}': tree VERSION reads "
            f"'{tree_version}' but tag implies '{tag_version}'. "
            "Bump VERSION at the repo root and retag before publishing. "
            "Refusing to ship a mislabeled release.",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print(f"[dry-run] VNX_HOME_ROOT: {root}")
        print(
            f"[dry-run] Version guard OK: tag '{tag}' tree VERSION '{tree_version}' "
            f"== tag '{tag_version}'"
        )
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
            print(
                f"[dry-run] Would measure how far '{tag}' is behind main and "
                "enforce the VNX_CUTOVER_MAX_BEHIND_COMMITS threshold before "
                "flipping (OI-1718)"
            )
            print(
                f"[dry-run] Would warn: consumers with a tracked .vnx-version "
                f"pin must update their pin to {tag} (the pin does not follow "
                f"`current`)"
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
        # OI-1718: the guard measures the tag's behindness against the repo
        # being published from (the same source the tag came from) and refuses
        # the cutover over the configured threshold without an explicit reason.
        # A refusal leaves the publish itself intact — only the flip is denied.
        try:
            _atomic_symlink_flip(
                root, target_dir, dry_run=False,
                cutover_reason=cutover_reason, behind_source=repo,
            )
        except CutoverRefusedError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            print(
                f"Note: '{tag}' was published to {target_dir}; only the cutover "
                f"was refused. Cut over later with `vnx update --to {tag} "
                "--cutover-reason \"...\"` once the reason exists.",
                file=sys.stderr,
            )
            return 1
        print(_release_notes_hint(tag))
        print(_current_flip_warning(tag), file=sys.stderr)
    else:
        print("current NOT flipped (publish only; pass --set-current to cut over).")

    return 0


def vnx_release(args) -> int:
    if getattr(args, "release_subcommand", None) == "publish":
        return vnx_release_publish(args)
    print("Error: a release subcommand is required (publish).", file=sys.stderr)
    return 1
