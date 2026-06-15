"""dispatch_internal.py — ExecutionPermit: the un-evadable in-process gate.

A lane adapter must hold a valid ExecutionPermit to execute. The permit is
unforgeable outside this module because _PERMIT_SENTINEL is module-private.

Does NOT import dispatch_plan or ExecutionPlan (those live in PR-2).
Any plan-like object that satisfies PlanLike is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Module-private sentinel. Cannot be reconstructed from outside — construction
# via ExecutionPermit(...) without access to this object yields a broken permit
# that require_permit() will reject.
_PERMIT_SENTINEL = object()


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class PlanLike(Protocol):
    """Structural type accepted by issue_permit / require_permit.

    Any object with a dispatch_id property and a digest() method qualifies.
    Intentionally loose so PR-1 is not coupled to the concrete ExecutionPlan
    type introduced in PR-2.
    """

    @property
    def dispatch_id(self) -> str: ...

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
    """Mint a permit for a validated plan. Called ONLY by the envelope/executor."""
    return ExecutionPermit(
        dispatch_id=plan.dispatch_id,
        plan_digest=plan.digest(),
        _sentinel=_PERMIT_SENTINEL,
    )


def require_permit(plan: PlanLike, permit: ExecutionPermit) -> None:
    """Raise PermissionError unless the permit was minted by issue_permit for THIS plan."""
    if (
        permit._sentinel is not _PERMIT_SENTINEL
        or permit.plan_digest != plan.digest()
        or permit.dispatch_id != plan.dispatch_id
    ):
        raise PermissionError("lane execution requires a validated dispatch plan permit")
