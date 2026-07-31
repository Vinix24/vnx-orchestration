#!/usr/bin/env python3
"""CI check: detect tests that reference code outside the repository.

Catches two signals:

1. **Tracked symlinks** (git mode 120000) that resolve outside the repo root.
   This is the core check — cheap, deterministic, and catches the case where a
   tracked symlink hides code living in an external install.

2. **Working-tree symlinks** that resolve outside the repo root.  These are
   symlinks present on disk but not tracked by git (install artifacts, build
   outputs).  Known-uninteresting directories (``.git``, ``.venv``,
   ``node_modules``, ``__pycache__``) are skipped.

Exit 0 when clean, exit 1 with violation details when issues are found.

Usable in the source repo AND in consumer repos — the repo-root test works
without an allowlist because legitimate symlinks that stay within the repo
pass automatically.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Directories skipped during working-tree walk (Signal 2).
_SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})


def _get_ignored_paths(paths: list[Path], repo_root: Path) -> set[Path]:
    """Return the subset of *paths* that are git-ignored.

    Uses ``git check-ignore`` in batch mode so we can prune entire subtrees
    in one call per directory level.  Exit 1 ("nothing ignored") is normal and
    means the returned set is empty.
    """
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "check-ignore", *[str(p) for p in paths]],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    # exit 0 = at least one matched, exit 1 = none matched, anything else = error.
    if result.returncode not in (0, 1):
        return set()
    ignored: set[Path] = set()
    for line in result.stdout.strip().split("\n"):
        stripped = line.strip()
        if stripped:
            ignored.add(Path(stripped))
    return ignored


def get_repo_root() -> Path:
    """Return the absolute path of the git repository root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def resolve_symlink_target(symlink: Path) -> Path | None:
    """Resolve a symlink to its absolute target.

    Returns ``None`` when the entry is not a symlink or the target is broken
    (unreachable).
    """
    if not symlink.is_symlink():
        return None
    try:
        raw = os.readlink(str(symlink))
    except OSError:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = (symlink.parent / target).resolve()
    else:
        target = target.resolve()
    return target


def is_within_repo(path: Path, repo_root: Path) -> bool:
    """Return True when *path* is equal to or inside *repo_root*."""
    try:
        path.resolve().relative_to(repo_root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Signal 1 — tracked symlinks (git mode 120000)
# ---------------------------------------------------------------------------


def get_tracked_symlinks(repo_root: Path) -> list[Path]:
    """Return relative paths of all tracked symlinks (git mode 120000)."""
    result = subprocess.run(
        ["git", "ls-files", "-s"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(repo_root),
    )
    symlinks: list[Path] = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split()
        # Format: <mode> <hash> <stage>\t<path>
        # Mode 120000 = symlink
        if len(parts) >= 4 and parts[0] == "120000":
            symlinks.append(Path(parts[3]))
    return symlinks


def check_signal1_tracked_symlinks(repo_root: Path) -> list[str]:
    """Return violation strings for tracked symlinks that escape the repo."""
    violations: list[str] = []
    for symlink_rel in get_tracked_symlinks(repo_root):
        full = repo_root / symlink_rel
        target = resolve_symlink_target(full)
        if target is None:
            continue
        if not is_within_repo(target, repo_root):
            violations.append(f"  {symlink_rel} -> {target}")
    return violations


# ---------------------------------------------------------------------------
# Signal 2 — working-tree symlinks (install artifacts, build outputs, etc.)
# ---------------------------------------------------------------------------


def get_working_tree_symlinks(repo_root: Path) -> list[Path]:
    """Return absolute paths of all symlinks on disk (not just tracked ones).

    Signal 2 exists alongside Signal 1 because untracked symlinks (install
    artifacts, build outputs, vendored tooling) can also reference code outside
    the repo.  Git does not know about them, so ``git ls-files -s`` does not
    see them — a filesystem walk is the only way to catch them.

    Gitignored paths are skipped via ``git check-ignore`` so that ephemeral
    runtime state (``.vnx-data/worktrees/`` and similar) does not drown the
    output in noise.
    """
    symlinks: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(repo_root)):
        # Prune skipped directories in-place.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]

        # Batch-check which directory entries are gitignored and remove them
        # from *dirnames* before os.walk descends into them.
        if dirnames:
            dir_paths = [Path(dirpath) / d for d in dirnames]
            ignored_dirs = _get_ignored_paths(dir_paths, repo_root)
            if ignored_dirs:
                dirnames[:] = [d for d in dirnames if (Path(dirpath) / d) not in ignored_dirs]

        current = Path(dirpath)
        # Check directory entries (rare, but possible)
        for name in dirnames:
            entry = current / name
            if entry.is_symlink():
                symlinks.append(entry)
        # Check file entries — filter gitignored files too (safety net for
        # files in non-ignored directories that match an individual pattern).
        for name in filenames:
            entry = current / name
            if not entry.is_symlink():
                continue
            if _get_ignored_paths([entry], repo_root):
                continue
            symlinks.append(entry)

    return symlinks


def check_signal2_working_tree_symlinks(repo_root: Path) -> list[str]:
    """Return violation strings for working-tree symlinks that escape the repo."""
    violations: list[str] = []
    tracked = set(get_tracked_symlinks(repo_root))

    for symlink in get_working_tree_symlinks(repo_root):
        # Skip symlinks already covered by Signal 1 to avoid double-reporting.
        try:
            rel = symlink.relative_to(repo_root)
        except ValueError:
            continue
        if rel in tracked:
            continue

        target = resolve_symlink_target(symlink)
        if target is None:
            continue
        if not is_within_repo(target, repo_root):
            violations.append(f"  {rel} -> {target}")

    return violations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    repo_root = get_repo_root()

    s1 = check_signal1_tracked_symlinks(repo_root)
    s2 = check_signal2_working_tree_symlinks(repo_root)

    if not s1 and not s2:
        print("OK: No tests reference code outside the repository.")
        return 0

    print("ERROR: Tests reference code outside the repository:\n")
    if s1:
        print("  [Signal 1] Tracked symlinks pointing outside the repo:")
        for line in s1:
            print(line)
        print()
    if s2:
        print("  [Signal 2] Working-tree symlinks pointing outside the repo:")
        for line in s2:
            print(line)
        print()

    print(
        "These references would pass locally (the symlink resolves)"
        " but fail on a clean checkout where the target is absent."
    )
    print("Tests must target code that lives within the repository.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
