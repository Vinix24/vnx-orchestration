"""D3: verify-pr helper — server-side attestation gate logic.

Called by `vnx attest verify-pr` and by the GitHub Action
.github/workflows/attestation-gate.yml.

Checks (for a PR requiring attestation):
  1. Classify changed files against the exempt allowlist (fail-closed).
     Every path not in the exempt allowlist REQUIRES a valid attestation.
     Un-classified paths (e.g. a new top-level dir) are never silently skipped.
  2. Read allowed_signers from the BASE branch (never the PR tree).
  3. Verify: .vnx-attest/<content-key>.json exists, diff_hash binds,
     signature is valid against allowed_signers.

Verifier safety: this file is executed from the BASE branch by the GitHub
Action (extracted via `git show origin/main:scripts/lib/verify_pr.py`).
The PR-tree copy of verify_pr.py is NEVER run for the gate decision, so a
PR that weakens this file cannot affect the gate outcome.

Exempt allowlist — explicit; fail-closed:
  docs/   — documentation directory
  tests/  — test-suite directory
  *.md    — markdown files

Everything else is REQUIRES-ATTESTATION by default.  Any new top-level file
or directory that is not in the exempt allowlist requires a valid attestation.
This is intentionally conservative: prefer a false-positive (unexpected
attestation requirement) over a false-negative (silently ungated change).

Exit codes:
  0 — PASS (valid attest) or EXEMPT (all files in exempt allowlist)
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

# Exempt allowlist — paths exempt from attestation.
# MUST stay in sync with the bash classify step in attestation-gate.yml.
# The gate logic is INVERTED: anything NOT exempt requires attestation.
_EXEMPT_PREFIXES = (
    "docs/",
    "tests/",
)

# Note: _FEATURE_PREFIXES is kept for documentation / future audit use only.
# Classification no longer depends on matching feature prefixes — it depends
# on NOT matching the exempt allowlist.  Any path outside the exempt allowlist
# is treated as requiring attestation (fail-closed).
_FEATURE_PREFIXES = (
    "scripts/",
    "vnx_cli/",
    "dashboard/",
    "schemas/",
    ".vnx/",
    ".github/",
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
    """Return True if this path is in the exempt allowlist.

    Exempt paths: docs/, tests/, *.md
    Everything else is NOT exempt (requires attestation).
    """
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
    """Classify a PR's changed files — fail-closed against the exempt allowlist.

    Classification is EXHAUSTIVE: every changed path is either in the exempt
    allowlist (EXEMPT) or requires attestation (feature).  There is no
    un-classified / silently-skipped category.

    Returns:
      "feature"  — at least one file is NOT in the exempt allowlist → gate fires
      "exempt"   — ALL files are in the exempt allowlist → gate skips
      "empty"    — no changed files (degenerate case → treat as exempt)

    Exempt allowlist: docs/, tests/, *.md
    Anything outside this list — including new top-level files or directories —
    defaults to "feature" (requires attestation).
    """
    if not changed_files:
        return "empty"
    all_exempt = all(_is_exempt_file(f) for f in changed_files)
    if all_exempt:
        return "exempt"
    # At least one file is outside the exempt allowlist → gate fires.
    # This covers feature-code paths (scripts/, vnx_cli/, ...) AND any
    # un-classified path (e.g. setup.py, pyproject.toml, a new top-level dir).
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
    ok = False
    reason = "unknown"
    ov_ok = False
    ov_manifest = None
    _ov_detail = ""
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

        if not ok:
            # Regular attestation failed — check for a valid signed override.
            # An override is diff-bound and signer-pinned to base-branch
            # allowed_signers.  It is NEVER a silent pass: the verdict is
            # "override" and the deviation is permanently recorded in the trail.
            from attest_override import verify_override_record
            ov_ok, _ov_detail, ov_manifest = verify_override_record(
                allowed_signers=allowed_signers_path,
                repo_root=repo_root,
                base_ref=base_ref,
                head_ref=head_ref,
            )
            # Gate-side budget enforcement (round-2 finding 3) + anti-reset
            # (finding 1): a validly-signed override still FAILS the gate if it
            # exceeds the rolling budget, and the PR trail must append-only EXTEND
            # the base-branch trail — deleting rows to free budget is rejected.
            if ov_ok and ov_manifest is not None:
                from attest_override import (
                    ATTEST_DIR as _AO_DIR,
                    OVERRIDE_TRAIL_FILE as _AO_TRAIL,
                    count_overrides_in_window,
                    get_override_budget,
                )
                _pr_trail = repo_root / _AO_DIR / _AO_TRAIL
                _pr_lines = (
                    _pr_trail.read_text(encoding="utf-8").splitlines()
                    if _pr_trail.exists() else []
                )
                _base = subprocess.run(
                    ["git", "show", f"{base_ref}:{_AO_DIR}/{_AO_TRAIL}"],
                    cwd=str(repo_root), capture_output=True,
                )
                if _base.returncode == 0:
                    _base_lines = _base.stdout.decode(
                        "utf-8", "replace"
                    ).splitlines()
                    if _pr_lines[: len(_base_lines)] != _base_lines:
                        ov_ok = False
                        _ov_detail = (
                            "override trail does not append-only extend the base "
                            "branch (budget-reset attempt)"
                        )
                # Trail-membership (round-3 finding): the override RECORD file
                # alone is not enough — a matching, validly-signed entry MUST exist
                # in the append-only trail, else the audit ledger / rolling budget
                # was bypassed by keeping the record but deleting the trail row.
                if ov_ok:
                    from attest_override import ATTESTATION_OVERRIDE as _AO_TYPE
                    from attestation import verify_attestation as _verify
                    from ndjson_hash_chain import walk_chain as _walk
                    _ck = ov_manifest.get("content_key")
                    _in_trail = False
                    if _pr_trail.exists():
                        for _i, _e, _h in _walk(_pr_trail):
                            if not isinstance(_e, dict):
                                continue
                            if _e.get("attestation_type") != _AO_TYPE:
                                continue
                            if _e.get("content_key") != _ck:
                                continue
                            _m = {k: v for k, v in _e.items() if k != "prev_hash"}
                            if _verify(_m, allowed_signers_path):
                                _in_trail = True
                                break
                    if not _in_trail:
                        ov_ok = False
                        _ov_detail = (
                            "override record is not recorded in the append-only "
                            "trail (audit-ledger bypass)"
                        )
                if ov_ok:
                    try:
                        _used = count_overrides_in_window(
                            _pr_trail, allowed_signers=allowed_signers_path
                        )
                    except ValueError as _e:
                        ov_ok = False
                        _ov_detail = str(_e)
                    else:
                        _budget = get_override_budget()
                        if _used > _budget:
                            ov_ok = False
                            _ov_detail = (
                                f"override budget exhausted ({_used}/{_budget})"
                            )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if ok:
        return (0, "PASS: attestation valid")

    if ov_ok and ov_manifest is not None:
        signer = ov_manifest.get("signer_identity", "unknown")
        ck_short = ov_manifest.get("content_key", "?")[:12]
        ov_reason = ov_manifest.get("reason", "")
        return (
            0,
            f"override: RECORDED deviation — signed by {signer!r} "
            f"for {ck_short}\u2026 | reason: {ov_reason!r}",
        )

    # An override was attempted (signed) but rejected by the budget / anti-reset
    # / chain checks \u2014 never a silent pass, never falls back to the generic FAIL.
    if ov_manifest is not None:
        return (1, f"override REJECTED: {_ov_detail}")

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
