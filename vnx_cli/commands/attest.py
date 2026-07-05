#!/usr/bin/env python3
"""vnx attest {write,verify} — in-repo attestation record (D2)."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile

from vnx_cli import _engine


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_add(path: Path, repo_root: Path) -> None:
    subprocess.run(
        ["git", "add", str(path.relative_to(repo_root))],
        cwd=str(repo_root), check=True, capture_output=True,
    )


def _try_signed_commit(msg: str, repo_root: Path, key_path: Path) -> bool:
    """Attempt `git commit -S` with the SSH key; return True on success."""
    result = subprocess.run(
        [
            "git",
            "-c", "gpg.format=ssh",
            "-c", f"gpg.ssh.signingKey={key_path}",
            "commit", "-S", "-m", msg,
        ],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    return result.returncode == 0


def _plain_commit(msg: str, repo_root: Path) -> bool:
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
    return result.returncode == 0


def vnx_attest_write(args) -> int:
    """Write an in-repo attest record for the current branch."""
    repo_root = Path(getattr(args, "project_dir", ".")).resolve()
    key_path_str = getattr(args, "key", None)
    key_path = Path(key_path_str) if key_path_str else None

    dispatch_id = args.dispatch_id
    deliverable_id = args.deliverable
    track_id = args.track
    plan_gate_ref = getattr(args, "gate_ref", "no-gate-ref")
    signer_identity = getattr(args, "signer", "vnx@local")
    base_ref = getattr(args, "base_ref", "origin/main")
    no_commit = getattr(args, "no_commit", False)

    _engine.ensure_engine_on_path()
    import attest_record as _ar

    try:
        rec = _ar.write_attest_record(
            dispatch_id=dispatch_id,
            deliverable_id=deliverable_id,
            track_id=track_id,
            plan_gate_ref=plan_gate_ref,
            signer_identity=signer_identity,
            timestamp=_utc_now(),
            key_path=key_path,
            repo_root=repo_root,
            base_ref=base_ref,
        )
    except RuntimeError as e:
        print(f"attest write failed: {e}", file=sys.stderr)
        return 1

    signed_str = (
        "yes (detached SSH signature)" if rec.manifest.get("signature")
        else "no (unsigned — advisory phase)"
    )
    print(f"  content-key: {rec.content_key}")
    print(f"  record:      {rec.record_path}")
    print(f"  signed:      {signed_str}")

    if no_commit:
        print("  committed:   skipped (--no-commit)")
        return 0

    try:
        _git_add(rec.record_path, repo_root)
    except subprocess.CalledProcessError as e:
        print(f"  git add failed: {e}", file=sys.stderr)
        return 1

    commit_msg = f"chore(gov): attest record [{dispatch_id}]"
    git_signed = False
    if key_path and key_path.exists():
        git_signed = _try_signed_commit(commit_msg, repo_root, key_path)

    if not git_signed:
        ok = _plain_commit(commit_msg, repo_root)
        if not ok:
            print("  Warning: git commit failed — record staged but not committed.", file=sys.stderr)
            return 1

    print(f"  committed:   {'SSH-signed' if git_signed else 'unsigned'}")
    return 0


def vnx_attest_verify(args) -> int:
    """Verify the attest record for the current branch."""
    repo_root = Path(getattr(args, "project_dir", ".")).resolve()
    allowed_signers_str = getattr(args, "allowed_signers", None)
    base_ref = getattr(args, "base_ref", "origin/main")

    _engine.ensure_engine_on_path()
    import attest_record as _ar

    is_override = bool(allowed_signers_str)
    tmp_path = None

    if is_override:
        # Opt-in explicit override — warn: caller must ensure the path is protected
        print(
            f"  [warn] --allowed-signers override: {allowed_signers_str!r} "
            "(skipping base-branch resolution; ensure this path is CODEOWNERS-protected)",
            file=sys.stderr,
        )
    else:
        # Default: read from base branch — NEVER from the PR working tree.
        # A PR can write .vnx-attest/allowed_signers, so trusting the working-tree
        # copy allows a rogue key to self-verify.
        content = _ar.read_allowed_signers_from_base(repo_root, base_ref)
        if content is not None:
            fd, tmp_path = tempfile.mkstemp(suffix=".allowed_signers")
            try:
                os.write(fd, content)
            finally:
                os.close(fd)
            allowed_signers_str = tmp_path
        else:
            print(
                f"Error: allowed_signers not found in base branch {base_ref!r}. "
                "Add .vnx-attest/allowed_signers at base branch, or pass --allowed-signers.",
                file=sys.stderr,
            )
            return 1

    try:
        ok, reason = _ar.verify_attest_record(
            allowed_signers=allowed_signers_str,
            repo_root=repo_root,
            base_ref=base_ref,
        )
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if ok:
        print(f"  PASS: {reason}")
        return 0
    else:
        print(f"  FAIL: {reason}", file=sys.stderr)
        return 1


def vnx_attest(args) -> int:
    subcommand = getattr(args, "attest_subcommand", None)
    if subcommand == "write":
        return vnx_attest_write(args)
    elif subcommand == "verify":
        return vnx_attest_verify(args)
    elif subcommand == "verify-pr":
        return vnx_attest_verify_pr(args)
    elif subcommand == "override":
        return vnx_attest_override(args)
    else:
        print("  Usage: vnx attest {write,verify,verify-pr,override}", file=sys.stderr)
        return 1


def vnx_attest_verify_pr(args) -> int:
    """Verify attestation for a PR — used by the D3 GitHub Action gate."""
    repo_root = Path(getattr(args, "project_dir", ".")).resolve()
    base_ref = getattr(args, "base_ref", "origin/main")
    head_ref = getattr(args, "head_ref", "HEAD")
    allowed_signers_override = getattr(args, "allowed_signers", None)
    verbose = getattr(args, "verbose", False)

    _engine.ensure_engine_on_path()
    import verify_pr as _vpr

    exit_code, message = _vpr.verify_pr(
        repo_root=repo_root,
        base_ref=base_ref,
        head_ref=head_ref,
        allowed_signers_override=allowed_signers_override or None,
        verbose=verbose,
    )

    if exit_code == 0:
        print(f"  {message}")
    elif exit_code == 2:
        print(f"  {message}", file=sys.stderr)
    else:
        print(f"  {message}", file=sys.stderr)

    return exit_code

def vnx_attest_override(args) -> int:
    """Record a signed, budgeted gate override for the current branch (D4).

    Writes:
      .vnx-attest/override-<content-key>.json  — signed override record
      .vnx-attest/override-trail.ndjson         — append-only audit trail

    The override is diff-bound (content-keyed) and signer-pinned to
    base-branch allowed_signers.  Budget is enforced per rolling 30-day window
    via the append-only trail.
    """
    repo_root = Path(getattr(args, "project_dir", ".")).resolve()
    key_path_str = getattr(args, "key", None)
    if not key_path_str:
        print("  Error: --key is required for override", file=sys.stderr)
        return 1
    key_path = Path(key_path_str)
    if not key_path.exists():
        print(f"  Error: key not found: {key_path}", file=sys.stderr)
        return 1

    reason = getattr(args, "reason", "")
    if not reason or not reason.strip():
        print("  Error: --reason is required and must be non-empty", file=sys.stderr)
        return 1

    dispatch_id = getattr(args, "dispatch_id", "override")
    signer_identity = getattr(args, "signer", "vnx@local")
    base_ref = getattr(args, "base_ref", "origin/main")
    head_ref = getattr(args, "head_ref", "HEAD")
    no_commit = getattr(args, "no_commit", False)

    _engine.ensure_engine_on_path()
    import attest_override as _ao
    import content_key as _ck

    # Compute content-key for this branch
    try:
        ck = _ck.compute_diff_hash(
            repo_root=repo_root, base_ref=base_ref, head_ref=head_ref
        )
    except RuntimeError as e:
        print(f"  Error computing content-key: {e}", file=sys.stderr)
        return 1

    # Budget check — derived from append-only trail, not a mutable counter
    budget = _ao.get_override_budget()
    trail_path = repo_root / _ao.ATTEST_DIR / _ao.OVERRIDE_TRAIL_FILE
    used = _ao.count_overrides_in_window(trail_path)
    if used >= budget:
        print(
            f"  REFUSED: override budget exhausted "
            f"({used}/{budget} used in the last {_ao.OVERRIDE_WINDOW_DAYS} days). "
            "Wait for the window to roll or raise VNX_ATTEST_OVERRIDE_BUDGET.",
            file=sys.stderr,
        )
        return 1

    # Write override record + trail entry
    try:
        rec = _ao.write_override_record(
            content_key=ck,
            reason=reason,
            dispatch_id=dispatch_id,
            signer_identity=signer_identity,
            timestamp=_utc_now(),
            key_path=key_path,
            repo_root=repo_root,
        )
    except (ValueError, RuntimeError) as e:
        print(f"  override write failed: {e}", file=sys.stderr)
        return 1

    remaining_after = budget - (used + 1)
    print(f"  content-key:   {rec.content_key[:12]}\u2026")
    print(f"  record:        {rec.record_path}")
    print(f"  trail:         {rec.trail_path}")
    print(f"  reason:        {rec.manifest['reason']!r}")
    print(
        f"  budget:        {used + 1}/{budget} used in last {_ao.OVERRIDE_WINDOW_DAYS} days "
        f"({remaining_after} remaining)"
    )
    print(f"  ** OVERRIDE RECORDED — this deviation is permanent in the audit trail **")

    if no_commit:
        print("  committed:     skipped (--no-commit)")
        return 0

    try:
        _git_add(rec.record_path, repo_root)
        _git_add(rec.trail_path, repo_root)
    except subprocess.CalledProcessError as e:
        print(f"  git add failed: {e}", file=sys.stderr)
        return 1

    reason_short = reason.strip()[:60]
    commit_msg = f"chore(gov): attest override [{dispatch_id}] — {reason_short}"
    git_signed = False
    if key_path.exists():
        git_signed = _try_signed_commit(commit_msg, repo_root, key_path)

    if not git_signed:
        ok = _plain_commit(commit_msg, repo_root)
        if not ok:
            print(
                "  Warning: git commit failed — records staged but not committed.",
                file=sys.stderr,
            )
            return 1

    print(f"  committed:     {'SSH-signed' if git_signed else 'unsigned'}")
    return 0
