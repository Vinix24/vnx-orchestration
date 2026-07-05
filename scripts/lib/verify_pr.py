"""D3: verify-pr helper — server-side attestation gate logic.

Called by `vnx attest verify-pr` and by the GitHub Action
.github/workflows/attestation-gate.yml.

Checks (for a PR touching feature code):
  1. Is the PR feature-code only or exempt (docs/tests/md only)?
     Exempt PRs exit 0 with a named residual note.
  2. Read allowed_signers from the BASE branch (never the PR tree).
  3. Verify: .vnx-attest/<content-key>.json exists, diff_hash binds,
     signature is valid against allowed_signers.

Feature-code paths  (gate fires on any match):
  scripts/, vnx_cli/, dashboard/, schemas/, .vnx/, .github/

Exempt paths (gate skipped if ALL changed files match these):
  docs/, tests/, *.md

Exit codes:
  0 — PASS (valid attest) or EXEMPT (docs/tests/md only)
  1 — FAIL (unsigned feature PR, bad sig, no record, diff mismatch)
  2 — CONFIG ERROR (allowed_signers not found at base branch)

References:
  - attest_record.py: verify_attest_record, read_allowed_signers_from_base
  - content_key.py:   compute_diff_hash
  - docs/governance/2026-07-04-governance-attribution-enforce-PLAN.md (D3)
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_FEATURE_PREFIXES = (
    "scripts/",
    "vnx_cli/",
    "dashboard/",
    "schemas/",
    ".vnx/",
    ".github/",
)

_EXEMPT_PREFIXES = (
    "docs/",
    "tests/",
)


def _changed_files(
    merge_base: str,
    head_ref: str,
    cwd: Path,
) -> list[str]:
    """Return list of changed file paths between merge_base and head_ref."""
    result = subprocess.run(
        ["git", "diff", "--name-only", merge_base, head_ref],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"git diff --name-only failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return [f for f in result.stdout.splitlines() if f.strip()]


def _is_feature_file(path: str) -> bool:
    for prefix in _FEATURE_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _is_exempt_file(path: str) -> bool:
    for prefix in _EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return True
    if path.endswith(".md"):
        return True
    return False


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
    return result.stdout.strip()


def classify_pr(
    changed_files: list[str],
) -> str:
    """Classify a PR's changed files.

    Returns:
      "feature"  — at least one feature-path file → gate fires
      "exempt"   — all files are docs/tests/md → gate skips
      "empty"    — no changed files (degenerate case → treat as exempt)
    """
    if not changed_files:
        return "empty"
    feature_files = [f for f in changed_files if _is_feature_file(f)]
    if feature_files:
        return "feature"
    # all files must be individually exempt; mixed non-feature non-exempt → feature
    all_exempt = all(_is_exempt_file(f) for f in changed_files)
    if all_exempt:
        return "exempt"
    # files outside feature AND outside exempt prefixes: treat as feature
    return "feature"


def verify_pr(
    *,
    repo_root: "str | Path | None" = None,
    base_ref: str = "origin/main",
    head_ref: str = "HEAD",
    allowed_signers_override: "str | Path | None" = None,
    verbose: bool = False,
) -> "tuple[int, str]":
    """Run the full verify-pr check for a PR.

    Returns (exit_code, message):
      (0, "exempt: docs/tests/md only — un-attributed lane (named residual)")
      (0, "PASS: attestation valid")
      (1, "FAIL: <reason>")
      (2, "CONFIG ERROR: <reason>")

    The caller is responsible for printing the message and exiting.

    Args:
        repo_root: Repository root.  Defaults to cwd.
        base_ref: Base branch to merge-base against (default: origin/main).
        head_ref: Branch tip to verify (default: HEAD).
        allowed_signers_override: Explicit path to allowed_signers — skips
            base-branch resolution.  ONLY for tests; production always uses
            base-branch resolution so a PR cannot supply its own trust anchor.
        verbose: If True, emit diagnostic lines to stderr.
    """
    repo_root = Path(repo_root) if repo_root else Path.cwd()

    try:
        merge_base = _resolve_merge_base(base_ref, head_ref, repo_root)
    except RuntimeError as e:
        return (1, f"FAIL: merge-base resolution failed: {e}")

    try:
        changed = _changed_files(merge_base, head_ref, repo_root)
    except RuntimeError as e:
        return (1, f"FAIL: changed-files detection failed: {e}")

    classification = classify_pr(changed)

    if verbose:
        print(
            f"[verify-pr] base={base_ref} head={head_ref} "
            f"merge_base={merge_base[:12]} changed={len(changed)} "
            f"classification={classification}",
            file=sys.stderr,
        )

    if classification in ("exempt", "empty"):
        return (
            0,
            "exempt: docs/tests/md only — un-attributed lane (named residual, accepted v1)",
        )

    # Feature code in this PR — gate fires.
    # Resolve allowed_signers from base branch (never from PR tree).
    tmp_path = None
    try:
        if allowed_signers_override is not None:
            allowed_signers_path = Path(allowed_signers_override)
        else:
            from attest_record import read_allowed_signers_from_base
            raw = read_allowed_signers_from_base(repo_root, base_ref)
            if raw is None:
                return (
                    2,
                    f"CONFIG ERROR: .vnx-attest/allowed_signers not found at "
                    f"base branch {base_ref!r}. "
                    "Add .vnx-attest/allowed_signers at the base branch "
                    "(see docs/governance/KEY_PROVISIONING.md).",
                )
            fd, tmp_str = tempfile.mkstemp(suffix=".allowed_signers")
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            tmp_path = tmp_str
            allowed_signers_path = Path(tmp_path)

        from attest_record import verify_attest_record
        ok, reason = verify_attest_record(
            allowed_signers=allowed_signers_path,
            repo_root=repo_root,
            base_ref=base_ref,
            head_ref=head_ref,
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if ok:
        return (0, "PASS: attestation valid")
    return (1, f"FAIL: {reason}")


# ---------------------------------------------------------------------------
# CLI entry point (used by the GitHub Action)
# ---------------------------------------------------------------------------

def _cli_main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify attestation for a PR (D3 gate).",
    )
    parser.add_argument("--repo-root", default=".", metavar="DIR",
                        help="repository root (default: current directory)")
    parser.add_argument("--base-ref", default="origin/main", metavar="REF",
                        help="base branch for merge-base (default: origin/main)")
    parser.add_argument("--head-ref", default="HEAD", metavar="REF",
                        help="PR head ref (default: HEAD)")
    parser.add_argument("--allowed-signers", default=None, metavar="PATH",
                        help="override allowed_signers path (base-branch resolution if omitted)")
    parser.add_argument("--verbose", action="store_true",
                        help="emit diagnostic lines to stderr")
    args = parser.parse_args(argv)

    exit_code, message = verify_pr(
        repo_root=args.repo_root,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        allowed_signers_override=args.allowed_signers,
        verbose=args.verbose,
    )
    if exit_code == 0:
        print(message)
    else:
        print(message, file=sys.stderr)
    sys.exit(exit_code)


if __name__ == "__main__":
    _cli_main()
