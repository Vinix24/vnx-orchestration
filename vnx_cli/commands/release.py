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

# The only deviations a central install is ALLOWED to have against its tag:
# install-central.sh strips the tracked tenant marker (" D .vnx-project-id") and
# writes the install-mode marker ("?? .vnx-install-mode"). Measured on all 16
# published dirs on 2026-10-25; any third deviation is drift and fails.
VERIFY_ALLOWED_DELETED = ".vnx-project-id"
VERIFY_ALLOWED_UNTRACKED = INSTALL_MODE_MARKER
_VERIFY_ALLOWED_DELETE_CODES = (" D", "D ")

# The default branch a release tag must be reachable from. The remote's own
# symbolic HEAD wins (that is what "the default branch" means for a hosted
# repo); the four literals cover a bare local clone (tests, air-gapped boxes)
# and the pre-2020 default spelling.
_DEFAULT_BRANCH_REFS = (
    "refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/heads/main",
    "refs/remotes/origin/master", "refs/heads/master",
)


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
    """Release-notes steps for a release that moved ``current`` (OI-1183, OI-2141).

    ``CHANGELOG.md`` is assembled from the ``changelog.d/`` fragments by
    ``scripts/release/assemble_changelog.py`` on the release PR's branch; bullets
    come only from fragments, and the one thing written by hand is a prose intro
    under the new heading (``changelog.d/README.md``, "The release"). A publish
    that flips ``current`` must state the pin consequence in that intro instead
    of letting operators assume every consumer followed. Returns the assembler
    steps and the intro text naming the mandatory pin-update line, no bullets.
    """
    version = _strip_leading_v(tag)
    return (
        f"Release notes for {tag}: assemble CHANGELOG.md from the fragments "
        f"(changelog.d/README.md, \"The release\"):\n"
        f"  python3 scripts/release/assemble_changelog.py --version {version} --check\n"
        f"  python3 scripts/release/assemble_changelog.py --version {version}\n"
        f"Then paste the paragraph on the line below, and nothing else, under the new "
        f"heading (prose before the first bullet; never a bullet):\n"
        f"{tag} is now the active `current`. Consumers with a tracked "
        f"`.vnx-version` pin are NOT moved by this flip and must update their "
        f"pin to {tag} explicitly with `echo '{tag}' > <project>/.vnx-version`.\n"
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


def _rev_parse(repo: Path, ref: str) -> "str | None":
    """Commit-ish ``ref`` in ``repo``, or None when it does not resolve."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref],
        capture_output=True, text=True,
    )
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _unmerged_tag_message(repo: str, tag: str) -> "str | None":
    """None when the tag is on the default branch, else why ancestry is unproven.

    Fail-closed: a remote URL, an unresolvable default branch and a failing
    ``merge-base`` are "not proven", never a pass.
    """
    if not Path(repo).is_dir():
        return (f"cannot prove tag '{tag}' is on the default branch of {repo}: "
                "not a local checkout, so there are no objects to walk")
    for ref in _DEFAULT_BRANCH_REFS:
        branch_commit = _rev_parse(Path(repo), f"{ref}^{{commit}}")
        if branch_commit:
            break
    else:
        return (f"cannot find the default branch in {repo} (origin/HEAD, origin/main, "
                "main, origin/master, master), so the tag's ancestry is unproven")
    args = ["git", "-C", repo, "merge-base", "--is-ancestor", f"refs/tags/{tag}", branch_commit]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode == 0:
        return None
    if result.returncode == 1:
        return (f"tag '{tag}' is NOT on the default branch ({ref} @ {branch_commit[:12]}): "
                f"its commit is not an ancestor of {ref}; merge it there and retag")
    return (f"git merge-base --is-ancestor {tag} {branch_commit[:12]} failed (exit "
            f"{result.returncode}): {result.stderr.strip() or 'no stderr'}")


def _guard_publish_source(
    repo: str, tag: str, allow_unmerged: "str | None"
) -> "tuple[str | None, str]":
    """Everything that must hold before any materialization; (tree_version, refusal).

    Refuses a missing tag, an OI-1070 mislabeled tree and a tag outside the
    default branch; --allow-unmerged-tag <reason> (non-empty, named in the
    warning) is the only escape for the last. Runs in --dry-run.
    """
    if allow_unmerged is not None and not allow_unmerged.strip():
        return None, "--allow-unmerged-tag requires a non-empty reason"
    try:
        if not _tag_exists(repo, tag):
            return None, f"tag '{tag}' not found in {repo}"
    except FileNotFoundError:
        return None, "git executable not found in PATH"
    except subprocess.CalledProcessError as exc:
        return None, f"git operation failed: {exc}"

    try:
        ok, tree_version, tag_version = _check_version_matches_tag(repo, tag)
    except subprocess.CalledProcessError as exc:
        return None, f"cannot read VERSION from tag '{tag}' in {repo}: {exc.stderr.strip() or exc}"
    except FileNotFoundError:
        return None, "git executable not found in PATH"
    if not ok:
        return None, (f"VERSION mismatch for tag '{tag}': tree VERSION reads "
                      f"'{tree_version}' but tag implies '{tag_version}'. Bump VERSION at "
                      "the repo root and retag. Refusing to ship a mislabeled release.")

    unmerged = _unmerged_tag_message(repo, tag)
    if unmerged is None:
        print(f"Default-branch guard OK: tag '{tag}' is on the default branch")
        return tree_version, ""
    if allow_unmerged is not None:
        print(f"WARNING: {unmerged}", file=sys.stderr)
        print("WARNING: publishing anyway in spite of --allow-unmerged-tag reason: "
              f"{allow_unmerged.strip()}", file=sys.stderr)
        return tree_version, ""
    return None, f"{unmerged}. Or pass --allow-unmerged-tag \"<reason>\" to publish anyway."


def _resolve_release_tag(places, version: str) -> "tuple[str | None, str | None]":
    """``(tag, commit)`` for ``version``; the source checkout wins over the dir."""
    candidates = [version] if version[:1] in ("v", "V") else [version, f"v{version}"]
    for place in places:
        if not Path(place).is_dir():
            continue
        for candidate in candidates:
            commit = _rev_parse(Path(place), f"refs/tags/{candidate}^{{commit}}")
            if commit:
                return candidate, commit
    return None, None


def _verify_differences(version_dir: Path, tag: str, tag_commit: str) -> "list[str]":
    """Every difference between ``version_dir`` and its tag, one line each.

    HEAD must be the tag's commit and the tree clean against it except the two
    generated files; ``--no-optional-locks`` leaves a read-only index alone.
    """
    head = _rev_parse(version_dir, "HEAD")
    problems: "list[str]" = []
    if head != tag_commit:
        problems.append(f"HEAD {head or '?'} is not tag '{tag}' ({tag_commit})")
    cmd = ["git", "--no-optional-locks", "-C", str(version_dir), "status",
           "--porcelain", "--untracked-files=all"]
    status = subprocess.run(cmd, capture_output=True, text=True)
    if status.returncode != 0:
        reason = status.stderr.strip() or f"git status exit {status.returncode}"
        problems.append(f"cannot read the working tree: {reason}")
        return problems
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        code, path = line[:2], line[3:]
        generated = (code == "??" and path == VERIFY_ALLOWED_UNTRACKED) or (
            code in _VERIFY_ALLOWED_DELETE_CODES and path == VERIFY_ALLOWED_DELETED)
        if not generated:
            problems.append(f"{code.strip()} {path}")
    return problems


def vnx_release_verify(args) -> int:
    """`vnx release verify <version>` — exit 1 and name every differing path."""
    try:
        version = _validate_version_name(getattr(args, "version", None) or "")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    version_dir = _resolve_root() / "versions" / version
    if not version_dir.is_dir():
        print(f"Error: release dir not found: {version_dir}. Publish '{version}' first "
              "(vnx release publish --tag ...).", file=sys.stderr)
        return 1
    if _git_toplevel(version_dir) != version_dir:
        print(f"Error: {version_dir} is not a git checkout, so it cannot be compared "
              "against a tag tree. Re-publish the tag.", file=sys.stderr)
        return 1

    tag, tag_commit = _resolve_release_tag([_resolve_repo(args), version_dir], version)
    if tag is None or tag_commit is None:
        print(f"Error: no tag for '{version}' in {version_dir} nor {_resolve_repo(args)}: "
              "without the tag there is nothing to check the release dir against.",
              file=sys.stderr)
        return 1

    problems = _verify_differences(version_dir, tag, tag_commit)
    if problems:
        report = "\n".join(f"  {problem}" for problem in problems)
        print(f"FAIL: {version_dir} is not the tree of tag '{tag}' ({tag_commit[:12]}):\n"
              f"{report}", file=sys.stderr)
        return 1
    print(f"OK: {version_dir} matches tag '{tag}' ({tag_commit[:12]}): every tracked file "
          f"is byte-identical; only the generated {VERIFY_ALLOWED_DELETED} (stripped) and "
          f"{VERIFY_ALLOWED_UNTRACKED} (written) differ.")
    return 0


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

    tree_version, refusal = _guard_publish_source(
        repo, tag, getattr(args, "allow_unmerged_tag", None)
    )
    if refusal:
        print(f"Error: {refusal}", file=sys.stderr)
        return 1

    if dry_run:
        print(f"[dry-run] VNX_HOME_ROOT: {root}")
        print(
            f"[dry-run] Version guard OK: tag '{tag}' tree VERSION '{tree_version}' "
            f"== tag '{_strip_leading_v(tag)}'"
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
    subcommand = getattr(args, "release_subcommand", None)
    if subcommand == "publish":
        return vnx_release_publish(args)
    if subcommand == "verify":
        return vnx_release_verify(args)
    print("Error: a release subcommand is required (publish, verify).", file=sys.stderr)
    return 1
