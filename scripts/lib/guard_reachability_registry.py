#!/usr/bin/env python3
"""guard_reachability_registry.py — the ONE place a guarded field is mapped
to a real store, and the ONE place a zero-fill/missing-column finding may be
marked as a deliberate, reviewed design choice instead of a defect.

Why this file exists instead of a comment at the call site: golf-4
(2026-09-05) found that three of the eight discovered defects were already
written down — as a "Caveat" in ``docs/core/DISPATCH_RULES.md`` or as "an
accepted, intentional no-op" in a module docstring — and that is exactly what
kept them invisible. A prose note near the code is not consulted by the
detector and carries no reviewer signal that it was a DECISION rather than an
unnoticed gap. Every entry below is the opposite: machine-read by
``guard_reachability_audit.py``, and ``validate_registry()`` refuses to load
an ``ACCEPTED_GAPS`` entry that has no reason.

Two registries:

- ``FIELD_STORE_MAP`` — which real store backs a field the scanner found in a
  guard. A field the scanner finds with NO entry here is reported separately
  as "unmeasured" (informational, not a violation) — most guard fields in
  this repo are unrelated to this bug class (feature flags, env toggles,
  ordinary optional config), and only a curated subset is worth measuring
  against a store at all.
- ``ACCEPTED_GAPS`` — a field (optionally scoped to one file) whose
  zero-fill or missing-column finding is a REVIEWED, DELIBERATE design
  choice, not a defect. Every entry requires a non-empty ``reason``,
  ``decided_by``, and ``decided_on``.

A calibration case (``guard_reachability_calibration.py``) must NEVER appear
in ``ACCEPTED_GAPS`` — that module's self-test enforces this directly: a
confirmed historical bug that gets "explained away" here is the exact
Caveat-pattern this detector was built to stop laundering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class StoreTarget:
    """One real store to measure a field's fill rate against."""

    kind: str  # "sqlite" | "ndjson" | "json_dir"
    note: str
    # sqlite
    db_relpath: Optional[str] = None
    table: Optional[str] = None
    column: Optional[str] = None
    # ndjson (relative to the data root; multiple ledgers may share a field)
    ndjson_relpaths: Tuple[str, ...] = ()
    # json_dir (a directory of one-JSON-document-per-file records)
    dir_relpath: Optional[str] = None
    glob: str = "*.json"
    # dict key to probe for ndjson/json_dir stores, when it differs from the
    # FieldMapping's own field name (mirrors sqlite's separate `column`).
    # None means "use the mapping's field name unchanged".
    dict_key: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind == "sqlite":
            if not (self.db_relpath and self.table and self.column):
                raise ValueError(f"sqlite StoreTarget missing db_relpath/table/column: {self!r}")
        elif self.kind == "ndjson":
            if not self.ndjson_relpaths:
                raise ValueError(f"ndjson StoreTarget has no ndjson_relpaths: {self!r}")
        elif self.kind == "json_dir":
            if not self.dir_relpath:
                raise ValueError(f"json_dir StoreTarget missing dir_relpath: {self!r}")
        else:
            raise ValueError(f"unknown StoreTarget.kind={self.kind!r}")


@dataclass(frozen=True)
class FieldMapping:
    field: str
    targets: Tuple[StoreTarget, ...]
    note: str


# Curated, reviewed mappings from a field the scanner can find in a guard to
# the real store that would prove whether it is ever filled. Extending this
# list is the expected way to bring a new guarded field under measurement —
# NOT adding a comment near the guard.
FIELD_STORE_MAP: Tuple[FieldMapping, ...] = (
    FieldMapping(
        field="track_id",
        targets=(
            StoreTarget(
                kind="sqlite",
                db_relpath="state/runtime_coordination.db",
                table="dispatches",
                column="track",
                note=(
                    "OI-1632 (#1774, 2026-09-05): registration writes "
                    "dispatches.track; a nonexistent dispatches.track_id "
                    "column was the pre-fix bug. Reads THIS column, not "
                    "track_id, on purpose — see dispatch_cli.py:_persist_track_id."
                ),
            ),
        ),
        note=(
            "spec.track_id (DispatchSpec) gates the plan-first-gate "
            "enforcement branch in dispatch_cli._check_track_link_verdict. "
            "VNX_REQUIRE_DISPATCH_TRACK defaults OFF, so a nonzero-but-partial "
            "fill rate is the designed advisory state, not a defect — only "
            "an EXACT zero (the pre-#1774 state) is flagged."
        ),
    ),
)


@dataclass(frozen=True)
class AcceptedGap:
    """A reviewed, deliberate exception to a zero-fill/missing-column finding.

    ``file`` scopes the exception to one guard site (repo-relative path);
    ``None`` accepts the field's finding wherever it is found. Every field
    here MUST also still resolve through ``FIELD_STORE_MAP`` — an accepted
    gap does not remove the measurement, it only changes how the audit
    reports it (SUPPRESSED with the reason shown, never silently dropped).
    """

    field: str
    reason: str
    decided_by: str
    decided_on: str
    file: Optional[str] = None


# Empty by design: golf-4's finding was that EVERY zero-fill guard found so
# far turned out to be a genuine defect, not a deliberate choice. An entry
# only belongs here after a human has reviewed a specific zero-fill finding
# and decided it is intentional — never added pre-emptively to keep an audit
# run green.
ACCEPTED_GAPS: Tuple[AcceptedGap, ...] = ()


def validate_registry() -> None:
    """Fail loud on a malformed registry — called before every audit run.

    A ``StoreTarget`` already validates its own shape in ``__post_init__``;
    this validates the registry-level invariants: no duplicate field
    mappings, and no ``ACCEPTED_GAPS`` entry with an empty/whitespace reason
    (the whole point of this module is that a reason is mandatory and
    reviewable — an empty string is the same failure mode as a doc-comment
    nobody reads).
    """
    seen_fields = set()
    for mapping in FIELD_STORE_MAP:
        if not mapping.field or not mapping.field.strip():
            raise ValueError("FIELD_STORE_MAP entry with empty field name")
        if mapping.field in seen_fields:
            raise ValueError(f"duplicate FIELD_STORE_MAP entry for field={mapping.field!r}")
        seen_fields.add(mapping.field)
        if not mapping.targets:
            raise ValueError(f"FIELD_STORE_MAP entry for field={mapping.field!r} has no targets")

    for gap in ACCEPTED_GAPS:
        if not gap.field or not gap.field.strip():
            raise ValueError("ACCEPTED_GAPS entry with empty field name")
        if not gap.reason or not gap.reason.strip():
            raise ValueError(
                f"ACCEPTED_GAPS entry for field={gap.field!r} has an empty reason — "
                "every accepted gap MUST carry a machine-readable reason; a "
                "docstring 'Caveat' or 'accepted, intentional no-op' elsewhere "
                "does not count (2026-09-05 golf-4 finding)"
            )
        if not gap.decided_by or not gap.decided_by.strip():
            raise ValueError(f"ACCEPTED_GAPS entry for field={gap.field!r} missing decided_by")
        if not gap.decided_on or not gap.decided_on.strip():
            raise ValueError(f"ACCEPTED_GAPS entry for field={gap.field!r} missing decided_on")
