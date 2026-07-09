"""delegation_mandate.py — signed batch-delegation mandate for governed dispatches.

A mandate is a scoped, time-boxed, revocable authorization signed by the operator.
When ``VNX_SIGNED_DELEGATION=1`` is enabled, a dispatch covered by a valid mandate
may proceed without a fresh per-dispatch ``approval_id``.  The mandate ID is
recorded in the dispatch receipt for audit.

This module reuses the D1 primitives from ``attestation.py``:
  - ``sign_manifest`` / ``verify_attestation`` for SSH detached signatures
  - ``append_chained_entry`` for the append-only NDJSON ledger

Mandates are persisted in ``<repo_root>/.vnx-attest/mandates.ndjson``.
Revocation entries use ``attestation_type="mandate-revoke"`` and are chained to
the same ledger, so a revoked mandate can never be un-revoked by deleting files.
"""
from __future__ import annotations

import fnmatch
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from attestation import (
    SCHEMA_VERSION,
    sign_manifest,
    verify_attestation,
)
from ndjson_hash_chain import append_chained_entry

MANDATE_ATTESTATION_TYPE = "delegation-mandate"
REVOKE_ATTESTATION_TYPE = "mandate-revoke"
MANDATE_LEDGER_FILE = "mandates.ndjson"

_FEATURE_FLAG = "VNX_SIGNED_DELEGATION"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchContext:
    """The dispatch attributes a mandate may scope over."""

    project_id: str
    dispatch_id: str
    session_id: Optional[str] = None
    task_class: Optional[str] = None


@dataclass(frozen=True)
class DelegationMandate:
    """Parsed, in-memory view of an active mandate."""

    mandate_id: str
    project_id: str
    scope: dict[str, Any]
    issued_at: str
    expires_at: str
    issuer: str
    manifest: dict[str, Any]
    ledger_path: Path


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

def is_signed_delegation_enabled() -> bool:
    """Return True when ``VNX_SIGNED_DELEGATION=1``."""
    return os.environ.get(_FEATURE_FLAG, "0") == "1"


# ---------------------------------------------------------------------------
# Manifest builders
# ---------------------------------------------------------------------------

def mandate_manifest(
    *,
    mandate_id: str,
    project_id: str,
    scope: dict[str, Any],
    issued_at: str,
    expires_at: str,
    signer_identity: str,
) -> dict:
    """Build an unsigned delegation-mandate manifest.

    Args:
        mandate_id: Stable identifier for this mandate.
        project_id: Project the mandate is bound to.
        scope: Scope dictionary.  Supported keys (all optional but at least one
            should normally be present):
              - session_id: exact session identifier
              - allowed_task_classes: list of task-class strings
              - dispatch_id_glob: shell glob matched against dispatch_id
        issued_at: ISO-8601 UTC timestamp.
        expires_at: ISO-8601 UTC timestamp.  Mandatory — a mandate without an
            expiry covers nothing.
        signer_identity: Operator identity matching an allowed_signers entry.

    Returns:
        Manifest dict ready for ``sign_manifest``.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "attestation_type": MANDATE_ATTESTATION_TYPE,
        "mandate_id": mandate_id,
        "project_id": project_id,
        "scope": dict(scope),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "signer_identity": signer_identity,
    }


def revoke_mandate_manifest(
    *,
    mandate_id: str,
    revoked_at: str,
    signer_identity: str,
) -> dict:
    """Build an unsigned mandate-revocation manifest."""
    return {
        "schema_version": SCHEMA_VERSION,
        "attestation_type": REVOKE_ATTESTATION_TYPE,
        "mandate_id": mandate_id,
        "revoked_at": revoked_at,
        "signer_identity": signer_identity,
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _mandate_ledger_path(repo_root: "str | Path") -> Path:
    return Path(repo_root) / ".vnx-attest" / MANDATE_LEDGER_FILE


def emit_mandate(
    manifest: dict,
    key_path: "str | Path",
    *,
    repo_root: "str | Path | None" = None,
) -> "DelegationMandate":
    """Sign a mandate manifest and append it to the mandate ledger.

    Returns a ``DelegationMandate`` view of the persisted record.
    """
    signed = sign_manifest(manifest, key_path)
    ledger_path = _mandate_ledger_path(repo_root or Path.cwd())
    append_chained_entry(ledger_path, signed)
    return DelegationMandate(
        mandate_id=manifest["mandate_id"],
        project_id=manifest["project_id"],
        scope=manifest["scope"],
        issued_at=manifest["issued_at"],
        expires_at=manifest["expires_at"],
        issuer=manifest["signer_identity"],
        manifest=signed,
        ledger_path=ledger_path,
    )


def emit_mandate_revocation(
    *,
    mandate_id: str,
    signer_identity: str,
    timestamp: str,
    key_path: "str | Path",
    repo_root: "str | Path | None" = None,
) -> dict:
    """Sign and append a revocation record for ``mandate_id``.

    Returns the signed revocation manifest.
    """
    manifest = revoke_mandate_manifest(
        mandate_id=mandate_id,
        revoked_at=timestamp,
        signer_identity=signer_identity,
    )
    signed = sign_manifest(manifest, key_path)
    ledger_path = _mandate_ledger_path(repo_root or Path.cwd())
    append_chained_entry(ledger_path, signed)
    return signed


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Loading and coverage
# ---------------------------------------------------------------------------

def _is_expired(manifest: dict, now: datetime) -> bool:
    expires_at = manifest.get("expires_at")
    if not expires_at:
        return True  # Mandates MUST expire; absent expiry is treated as expired.
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    return now >= expires


def _revoked_mandate_ids(
    ledger_path: Path,
    allowed_signers: "str | Path",
) -> set[str]:
    """Return the set of mandate IDs that have a valid revocation record."""
    revoked: set[str] = set()
    if not ledger_path.exists():
        return revoked
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("attestation_type") != REVOKE_ATTESTATION_TYPE:
            continue
        if not verify_attestation(entry, allowed_signers):
            continue
        revoked.add(entry.get("mandate_id", ""))
    return revoked


def load_mandates(
    ledger_path: "str | Path",
    allowed_signers: "str | Path",
    *,
    project_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Load active, unrevoked, unexpired mandates from the ledger.

    Entries with invalid signatures, missing expiry, or that have been revoked
    are silently skipped.  Conservative: any doubt is dropped.
    """
    ledger_path = Path(ledger_path)
    if not ledger_path.exists():
        return []

    now = now or datetime.now(timezone.utc)
    revoked = _revoked_mandate_ids(ledger_path, allowed_signers)

    active: list[dict] = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("attestation_type") != MANDATE_ATTESTATION_TYPE:
            continue
        if entry.get("mandate_id") in revoked:
            continue
        if not verify_attestation(entry, allowed_signers):
            continue
        if _is_expired(entry, now):
            continue
        if project_id is not None and entry.get("project_id") != project_id:
            continue
        active.append(entry)
    return active


def _scope_covers(scope: dict[str, Any], ctx: DispatchContext) -> bool:
    """Return True iff the dispatch context falls inside the mandate scope.

    Conservative: a scope key that is present and malformed causes a mismatch.
    """
    if not scope:
        return False  # Empty scope covers nothing.

    if "session_id" in scope:
        if ctx.session_id != scope["session_id"]:
            return False

    if "allowed_task_classes" in scope:
        allowed = scope["allowed_task_classes"]
        if not isinstance(allowed, list):
            return False
        if ctx.task_class is None or ctx.task_class not in allowed:
            return False

    if "dispatch_id_glob" in scope:
        glob = scope["dispatch_id_glob"]
        if not isinstance(glob, str):
            return False
        if not fnmatch.fnmatch(ctx.dispatch_id, glob):
            return False

    return True


def mandate_covers(
    manifest: dict,
    ctx: DispatchContext,
    *,
    allowed_signers: "str | Path",
    now: Optional[datetime] = None,
    repo_root: "str | Path | None" = None,
) -> bool:
    """Return True iff ``manifest`` is a valid, active mandate covering ``ctx``.

    When ``repo_root`` is supplied the ledger is consulted and revoked mandates
    are rejected.  Callers that already loaded the mandate via ``load_mandates``
    may omit ``repo_root``.
    """
    if manifest.get("attestation_type") != MANDATE_ATTESTATION_TYPE:
        return False
    if not verify_attestation(manifest, allowed_signers):
        return False
    if _is_expired(manifest, now or datetime.now(timezone.utc)):
        return False
    if manifest.get("project_id") != ctx.project_id:
        return False
    if repo_root is not None:
        ledger_path = _mandate_ledger_path(repo_root)
        revoked = _revoked_mandate_ids(ledger_path, allowed_signers)
        if manifest.get("mandate_id") in revoked:
            return False
    return _scope_covers(manifest.get("scope", {}), ctx)


def _find_active_mandate(
    mandate_id: str,
    ctx: DispatchContext,
    allowed_signers: "str | Path",
    repo_root: "str | Path",
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Load the ledger and return the active mandate matching ``mandate_id``.

    Returns None if not found, revoked, expired, or out-of-scope.
    """
    ledger_path = _mandate_ledger_path(repo_root)
    mandates = load_mandates(
        ledger_path,
        allowed_signers,
        project_id=ctx.project_id,
        now=now,
    )
    for manifest in mandates:
        if manifest.get("mandate_id") == mandate_id and mandate_covers(
            manifest, ctx, allowed_signers=allowed_signers, now=now
        ):
            return manifest
    return None


# ---------------------------------------------------------------------------
# Runtime trust-anchor resolution
# ---------------------------------------------------------------------------

def _write_allowed_signers_temp(content: bytes) -> Path:
    fd, tmp_path = tempfile.mkstemp(suffix=".allowed_signers")
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    return Path(tmp_path)


def resolve_allowed_signers_for_runtime(
    repo_root: "str | Path",
    base_ref: str = "origin/main",
) -> Optional[Path]:
    """Resolve a trust-anchor allowed_signers file for mandate verification.

    Priority:
      1. ``VNX_ALLOWED_SIGNERS`` env override (operator explicit).
      2. ``.vnx-attest/allowed_signers`` at ``base_ref`` (base-branch trust).
      3. ``.vnx-attest/allowed_signers`` in the working tree (fallback).
      4. ``.vnx/allowed_signers`` in the working tree (fallback).

    Returns a path to a temporary file when reading from base branch; the file
    is intentionally not deleted so the short-lived dispatch process can use it.
    """
    env_override = os.environ.get("VNX_ALLOWED_SIGNERS")
    if env_override:
        path = Path(env_override)
        if path.exists():
            return path

    repo_root = Path(repo_root)

    # Try base branch first so a PR cannot plant its own signer set.
    try:
        from attest_record import read_allowed_signers_from_base  # noqa: PLC0415
    except ImportError:
        read_allowed_signers_from_base = None  # type: ignore[assignment,misc]

    if read_allowed_signers_from_base is not None:
        content = read_allowed_signers_from_base(repo_root, base_ref)
        if content:
            return _write_allowed_signers_temp(content)

    for candidate in (
        repo_root / ".vnx-attest" / "allowed_signers",
        repo_root / ".vnx" / "allowed_signers",
    ):
        if candidate.exists():
            return candidate

    return None


# ---------------------------------------------------------------------------
# Approval / mandate resolution
# ---------------------------------------------------------------------------

def resolve_signed_delegation(
    ctx: DispatchContext,
    approval_id: Optional[str],
    mandate_id: Optional[str],
    allowed_signers: "str | Path",
    repo_root: "str | Path",
    *,
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[str], str]:
    """Decide whether a dispatch may proceed under the current delegation policy.

    Returns ``(ok, recorded_mandate_id, reason)``.  ``recorded_mandate_id`` is
    the mandate ID to stamp on the receipt (None when a per-dispatch approval_id
    is used or when the feature is disabled).

    Behavior:
      - Feature OFF: mandates are never consulted; approval_id is required.
      - Feature ON: approval_id OR a valid covering mandate is accepted.
    """
    if not is_signed_delegation_enabled():
        if approval_id and approval_id.strip():
            return True, None, "signed delegation disabled; per-dispatch approval accepted"
        return False, None, "signed delegation disabled; per-dispatch approval_id required"

    if approval_id and approval_id.strip():
        return True, None, "per-dispatch approval accepted"

    if mandate_id and mandate_id.strip():
        manifest = _find_active_mandate(
            mandate_id.strip(), ctx, allowed_signers, repo_root, now=now
        )
        if manifest is not None:
            return True, mandate_id.strip(), f"mandate {mandate_id} covers dispatch"
        return False, None, f"mandate {mandate_id} not active or does not cover dispatch"

    return False, None, "no approval_id or valid mandate provided"
