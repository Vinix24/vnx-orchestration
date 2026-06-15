"""dispatch_internal.py — ExecutionPermit: the un-evadable in-process gate.

A lane adapter must hold a valid ExecutionPermit to execute. The permit is
unforgeable outside this module because _PERMIT_SENTINEL is module-private.

Does NOT import dispatch_plan or ExecutionPlan (those live in PR-2).
Any plan-like object that satisfies PlanLike is accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Module-private sentinel. Cannot be reconstructed from outside — construction
# via ExecutionPermit(...) without access to this object yields a broken permit
# that require_permit() will reject.
_PERMIT_SENTINEL = object()

# P0-3 (PR-4c) / PR-4d: a plan's instruction hash must be EXACTLY a 64-char
# lowercase hex sha256. An empty / short / non-hex value disables TOCTOU
# verification (fail-OPEN), so it is refused at permit-mint time, at the gate, and
# again at the executor (defense-in-depth).
#
# PR-4d: the earlier `^[0-9a-f]{64}$` accepted a trailing newline — `$` matches
# just before a final `\n`, so a `<64hex>\n` value passed. `fullmatch` anchors
# both ends with no newline exception.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def is_valid_instruction_hash(value: object) -> bool:
    """True iff *value* is exactly a 64-char lowercase hex sha256 (no trailing newline)."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class PlanLike(Protocol):
    """Structural type accepted by issue_permit / require_permit.

    Any object with a dispatch_id, a 64-hex instruction_sha256, and a digest()
    method qualifies. PR-4d makes instruction_sha256 mandatory (defense-in-depth):
    a hashless plan disables the executors' TOCTOU verification, so the permit
    layer refuses it at both mint and check time — execution can never be reached
    with a plan whose content hash is missing or malformed.
    """

    @property
    def dispatch_id(self) -> str: ...

    instruction_sha256: str

    def digest(self) -> str: ...


# ---------------------------------------------------------------------------
# Permit dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExecutionPermit:
    """Proof that a lane adapter was launched through the validated dispatch core.

    Do not construct directly — use issue_permit(). The _sentinel field defaults
    to None (deliberately a non-matching value), so any permit built via the public
    constructor without explicit _sentinel access is rejected by require_permit().
    Only issue_permit() attaches the real _PERMIT_SENTINEL.
    """

    dispatch_id: str
    plan_digest: str
    _sentinel: object = field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def issue_permit(plan: PlanLike) -> ExecutionPermit:
    """Mint a permit for a validated plan. Called ONLY by the envelope/executor.

    PR-4d defense-in-depth: instruction_sha256 is MANDATORY. Refuse to mint a
    permit for a plan whose instruction_sha256 is missing or not a valid 64-hex
    digest — an empty/invalid/absent hash would let the executor's TOCTOU guard
    fall open. There is no back-compat exception: a hashless plan never gets a
    permit, so it can never be spawned.
    """
    sha = getattr(plan, "instruction_sha256", None)
    if not is_valid_instruction_hash(sha):
        raise PermissionError(
            "cannot mint permit: plan.instruction_sha256 is missing or not a valid "
            f"64-hex digest (got {sha!r}) — a hashless plan disables TOCTOU verification"
        )
    return ExecutionPermit(
        dispatch_id=plan.dispatch_id,
        plan_digest=plan.digest(),
        _sentinel=_PERMIT_SENTINEL,
    )


def require_permit(plan: PlanLike, permit: ExecutionPermit) -> None:
    """Raise PermissionError unless the permit was minted by issue_permit for THIS plan.

    PR-4d: the plan's own instruction_sha256 must be a valid 64-hex digest —
    checked here as well, so a hashless plan is rejected at the gate even on a
    path that somehow holds a sentinel-bearing permit (defense-in-depth).
    """
    sha = getattr(plan, "instruction_sha256", None)
    if not is_valid_instruction_hash(sha):
        raise PermissionError(
            "lane execution requires a plan with a valid 64-hex instruction_sha256 "
            f"(got {sha!r}) — a hashless/invalid plan disables TOCTOU verification"
        )
    if (
        permit._sentinel is not _PERMIT_SENTINEL
        or permit.plan_digest != plan.digest()
        or permit.dispatch_id != plan.dispatch_id
    ):
        raise PermissionError("lane execution requires a validated dispatch plan permit")
