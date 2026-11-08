"""The delivery identity ``(project_id, delivery_id)`` for the direct creation paths (ADR-056).

One helper, so ``deliverable add`` and a proposed track dispatch write the same two keys the
merge receipt already writes (``governance_receipts.dispatch_identity_fields``). The round-suffix
rule and the ``unknown`` sentinel live in ``governance_receipts``; nothing here repeats them.
"""
from __future__ import annotations

from typing import Any

from governance_receipts import UNKNOWN_IDENTITY, base_delivery_id


def delivery_identity_fields(dispatch_id: Any, project_id: Any) -> dict:
    """``{"project_id", "delivery_id"}`` for a dispatch. No I/O.

    A ``project_id`` that is ``None``, not a string or blank becomes ``unknown`` (never a
    default project); the ``delivery_id`` is the base id of the dispatch (``unknown`` when blank).
    """
    resolved = project_id.strip() if isinstance(project_id, str) and project_id.strip() else UNKNOWN_IDENTITY
    return {"project_id": resolved, "delivery_id": base_delivery_id(dispatch_id)}
