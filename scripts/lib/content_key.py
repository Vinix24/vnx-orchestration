"""Content-key computation for squash-safe attestation (D2).

The content-key is SHA-256 of the feature diff from merge-base to HEAD,
excluding the .vnx-attest/ metadata directory so writing the attest record
itself does not change the key.

Squash-safety guarantee: squashing or rebasing commits without changing the
final code delta produces the same content-key, because the key is derived
from the resulting diff, not commit history.

References:
  - docs/governance/2026-07-04-governance-attribution-enforce-PLAN.md (D2)
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

_ATTEST_EXCLUDE = ":(exclude).vnx-attest/"


def _resolve_merge_base(base_ref: str, head_ref: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "merge-base", base_ref, head_ref],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git merge-base {base_ref!r} {head_ref!r} failed: "
            f"{result.stderr.strip()}"
        )
    sha = result.stdout.strip()
    if not sha:
        raise RuntimeError(
            f"git merge-base {base_ref!r} {head_ref!r} returned empty output"
        )
    return sha


def _git_diff_bytes(from_ref: str, to_ref: str, cwd: Path) -> bytes:
    """Unified diff bytes excluding .vnx-attest/ for stable content-key hashing."""
    result = subprocess.run(
        ["git", "diff", from_ref, to_ref, "--", ".", _ATTEST_EXCLUDE],
        cwd=str(cwd), capture_output=True,
    )
    # exit 0 = no diff, 1 = diff found; anything else is an error
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"git diff {from_ref} {to_ref} failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def compute_diff_hash(
    *,
    repo_root: "str | Path | None" = None,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
) -> str:
    """SHA-256 of the feature diff from merge-base to head_ref.

    Excludes .vnx-attest/ so writing the attest record does not change the key.
    Stable across squash-merges and rebases that produce the same code delta.

    Args:
        repo_root: Repository root.  Defaults to cwd.
        base_ref: Base branch to merge-base against (default: origin/main).
        head_ref: Branch tip to diff against (default: HEAD).
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()
    merge_base = _resolve_merge_base(base_ref, head_ref, repo_root)
    diff_bytes = _git_diff_bytes(merge_base, head_ref, repo_root)
    return hashlib.sha256(diff_bytes).hexdigest()


def compute_content_key(
    *,
    repo_root: "str | Path | None" = None,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
) -> str:
    """Squash-safe content-key for the current branch.

    The content-key is the diff hash — a SHA-256 of the feature diff from
    merge-base to HEAD, excluding .vnx-attest/.

    Args:
        repo_root: Repository root.  Defaults to cwd.
        base_ref: Base branch to merge-base against (default: origin/main).
        head_ref: Branch tip (default: HEAD).
    """
    return compute_diff_hash(repo_root=repo_root, base_ref=base_ref, head_ref=head_ref)
