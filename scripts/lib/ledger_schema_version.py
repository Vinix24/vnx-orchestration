"""ledger_schema_version.py — direction-aware receipt-schema version discipline.

The receipt ledger (``t0_receipts.ndjson``) is append-only and mixed-version by
construction (ADR-005, ADR-035 §7): a v0/v1-shaped prefix followed by a
``schema_version: 2`` suffix. The WRITE side already stamps a monotonic
``schema_version`` on every new receipt — ``append_receipt_internals/payload.py``
does ``receipt.setdefault("schema_version", CURRENT_SCHEMA_VERSION)`` (Path 2)
and ``receipt_schema.ReceiptV2`` forces ``RECEIPT_V2_SCHEMA_VERSION`` (Path 1),
both sourced from the single constant below. The READ side is the gap this
module closes: a reader must refuse what it does not know, convert what it
does, and never rewrite history.

Deterministic read rules — pure version arithmetic plus a fixed migration
ladder, no model judgment anywhere:

  - ``schema_version`` absent  -> version 0 (the oldest generation; not an error)
  - ``schema_version`` == CURRENT -> read as-is
  - ``schema_version`` <  CURRENT -> convert in memory, read on; the conversion
    is NEVER written back — it only lands on disk when the receipt is
    continued (re-emitted through the writer, which stamps the current version)
  - ``schema_version`` >  CURRENT -> refuse loudly (``ReceiptSchemaTooNewError``):
    the reader does not know what it does not know, and silently reading a
    newer shape produces a wrong interpretation

Migration path for the existing ~27k mixed-version lines: none of them is ever
rewritten (ADR-005 append-only). A backfill that edits history is explicitly
not an option. The in-memory conversion in ``read_receipt``/``iter_receipts``
is the migration — lazy, per-line, and durable only when a line is re-emitted.

Two "absent" conventions coexist on purpose. The WRITE-side validator
(``append_receipt_internals/validation.py::_resolve_schema_version``) resolves
an absent stamp to ``1`` for its legacy-v1 tolerance rules (ADR-035 §3.2.1).
This READ-side module resolves an absent stamp to ``0`` for the migration
ladder. Both agree that absent means "legacy"; the numbers differ because the
validator's floor is the v1 write-rule set, while the reader's floor is the
true oldest generation that predates any explicit stamp.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Union

# Single source of truth for the current receipt-schema version. Bump here —
# never at the individual stamp sites — so a future v3 lands monotonically on
# every new receipt and the reader's ladder walks up to it in one place.
CURRENT_SCHEMA_VERSION = 2

# A receipt with no ``schema_version`` key is the oldest generation (version 0),
# not an error. ``1`` is the legacy v1 stamp (ADR-035: "absent or 1 means the
# legacy shape"); both are older than CURRENT and convert in memory.
ABSENT_SCHEMA_VERSION = 0


class ReceiptSchemaTooNewError(RuntimeError):
    """A receipt carries a schema_version newer than this reader knows.

    Refuse loudly: reading a newer shape with an older reader yields a wrong
    interpretation, so there is no safe fallback.
    """


class ReceiptSchemaVersionError(RuntimeError):
    """A receipt carries a schema_version the reader cannot parse at all."""


class ReceiptSchemaMigrationMissingError(RuntimeError):
    """No migration step is registered for a version in the read ladder."""


def receipt_schema_version(receipt: Dict[str, Any]) -> int:
    """Resolve a receipt's schema version.

    Absent/None -> ``ABSENT_SCHEMA_VERSION`` (0, oldest generation). A present
    integer-parseable scalar -> that integer. Anything else that is present but
    cannot be parsed -> ``ReceiptSchemaVersionError``: the reader cannot
    determine whether it is safe to read, so it refuses loudly.
    """
    raw = receipt.get("schema_version")
    if raw is None:
        return ABSENT_SCHEMA_VERSION
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ReceiptSchemaVersionError(
            f"receipt schema_version={raw!r} is not an integer"
        ) from exc


def _migrate_0_to_1(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """v0 -> v1: give the legacy shape its explicit stamp.

    v0 (no stamp) and v1 (``schema_version: 1``) are the SAME legacy shape
    (ADR-035 §7: "absent or 1 means the legacy v1 shape"). The step exists so
    the ladder is uniform — every step is ``source -> source + 1`` — and a
    future conversion between the two can slot in here without changing
    ``read_receipt``. Returns a new dict; the input is never mutated.
    """
    migrated = dict(receipt)
    migrated["schema_version"] = 1
    return migrated


def _migrate_1_to_2(receipt: Dict[str, Any]) -> Dict[str, Any]:
    """v1 -> v2: stamp the current version and canonicalize the event-name key.

    v2 drops the legacy ``event`` alias (ADR-035 §3.2.1 r3 HIGH-2): for a
    ``schema_version >= 2`` record only ``event_type`` is consulted. An
    in-memory upgrade therefore promotes ``event`` to ``event_type`` when the
    canonical key is absent, so a downstream v2 reader sees the field it
    expects. Non-lossy: the ``event`` key is left in place (history is never
    rewritten; a reader may still want it). This is schema canonicalization,
    NOT the status-vocabulary cleanup (that is a separate track).

    Returns a new dict; the input is never mutated.
    """
    migrated = dict(receipt)
    migrated["schema_version"] = 2
    if "event_type" not in migrated and migrated.get("event") is not None:
        migrated["event_type"] = migrated["event"]
    return migrated


# Migration ladder: source version -> (dict -> dict) upgrade to source + 1.
# Each step is a PURE function (new dict out, input never mutated) so it is
# independently unit-testable and ``read_receipt`` can chain them blindly.
SCHEMA_MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    0: _migrate_0_to_1,
    1: _migrate_1_to_2,
}

PathLike = Union[str, Path]


def read_receipt(
    raw: Dict[str, Any],
    *,
    current_version: int = CURRENT_SCHEMA_VERSION,
) -> Dict[str, Any]:
    """Read one receipt dict, converting it in memory to ``current_version``.

    Direction-aware:
      - newer than ``current_version`` -> ``ReceiptSchemaTooNewError`` (refuse)
      - older, or absent -> walked through ``SCHEMA_MIGRATIONS`` in memory
      - equal -> returned as a copy (never the caller's dict, never mutated)

    Pure and I/O-free: it returns a new dict and writes nothing, so a read
    round can never touch the ledger. Only a subsequent WRITE (through the
    normal append path) persists the converted form.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"receipt must be a dict, got {type(raw).__name__}")

    version = receipt_schema_version(raw)
    if version > current_version:
        raise ReceiptSchemaTooNewError(
            f"receipt schema_version={version} is newer than this reader "
            f"(current_version={current_version}); refusing to read a shape "
            f"this reader does not know"
        )

    migrated: Dict[str, Any] = dict(raw)
    for v in range(version, current_version):
        step = SCHEMA_MIGRATIONS.get(v)
        if step is None:
            raise ReceiptSchemaMigrationMissingError(
                f"no migration registered from schema_version={v} to {v + 1}; "
                f"cannot convert a version-{v} receipt to {current_version}"
            )
        migrated = step(migrated)
    return migrated


def iter_receipts(
    path: PathLike,
    *,
    current_version: int = CURRENT_SCHEMA_VERSION,
) -> Iterator[Dict[str, Any]]:
    """Stream a ledger through the direction-aware reader.

    Yields each receipt converted in memory to ``current_version``. Built on
    ``ndjson_io.iter_ndjson`` (torn-tail-safe), so a partial final line from a
    crash mid-append is skipped, not raised. Never writes: the ledger bytes are
    untouched by iterating.
    """
    # Lazy sibling import keeps this module importable with zero side effects
    # (it is imported by append_receipt_internals/payload.py at module scope).
    from ndjson_io import iter_ndjson  # noqa: PLC0415

    for record in iter_ndjson(path):
        yield read_receipt(record, current_version=current_version)


def read_receipts(
    path: PathLike,
    *,
    current_version: int = CURRENT_SCHEMA_VERSION,
) -> List[Dict[str, Any]]:
    """Eager list form of :func:`iter_receipts` (torn-tail-safe, non-writing)."""
    return list(iter_receipts(path, current_version=current_version))


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "ABSENT_SCHEMA_VERSION",
    "SCHEMA_MIGRATIONS",
    "ReceiptSchemaTooNewError",
    "ReceiptSchemaVersionError",
    "ReceiptSchemaMigrationMissingError",
    "receipt_schema_version",
    "read_receipt",
    "iter_receipts",
    "read_receipts",
]
