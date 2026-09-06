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
#
# EXPLICIT RULE (golf-4 ronde 2 / OI-1640, 2026-09-06): a field may carry
# MORE THAN ONE StoreTarget, and each is measured and reported
# INDEPENDENTLY (see ``build_findings`` — one ``FieldFinding`` per target).
# When a field has several targets, the one the GUARD ITSELF READS is
# authoritative for "is this guard reachable" and MUST be a target here —
# a downstream store that something else WRITES after the guard already
# ran (a persisted column, a derived ledger) may also be listed, but its
# note must say so explicitly, and a healthy fill rate on it is NOT
# evidence the guard is reachable. golf-4r2's own finding was the registry
# doing exactly that: mapping ``track_id`` ONLY against the sqlite column
# ``_persist_track_id`` writes downstream, never against the staged
# ``dispatch-spec.json`` both ``_check_track_link_verdict`` (the door's
# guard, dispatch_cli.py:1030) and ``_persist_track_id`` itself
# (dispatch_cli.py:1423) actually read — a partially-filled downstream
# column reported a clean [OK] on the exact case this detector exists to
# catch (0 of 1115 staged specs had a track_id, measured 2026-09-06).
FIELD_STORE_MAP: Tuple[FieldMapping, ...] = (
    FieldMapping(
        field="track_id",
        targets=(
            StoreTarget(
                kind="json_dir",
                dir_relpath="dispatches",
                glob="dispatch-spec.json",
                note=(
                    "AUTHORITATIVE for guard reachability: this is what both "
                    "guard sites actually read — dispatch_cli.py:1030 "
                    "(_check_track_link_verdict, 'if track_id:' on the local "
                    "assigned from spec.track_id) and dispatch_cli.py:1423 "
                    "(_persist_track_id, 'if not track_id: return'). Measured "
                    "2026-09-06: 0 of 1115 staged dispatch-spec.json bundles "
                    "had a track_id — OI-1640."
                ),
            ),
            StoreTarget(
                kind="sqlite",
                db_relpath="state/runtime_coordination.db",
                table="dispatches",
                column="track",
                note=(
                    "DOWNSTREAM PERSISTENCE ONLY, not guard reachability: "
                    "OI-1632 (#1774, 2026-09-05) fixed registration to write "
                    "dispatches.track; _persist_track_id (dispatch_cli.py) "
                    "UPDATEs this SAME column after the door's guard has "
                    "already run. A nonzero fill rate here only proves "
                    "_persist_track_id executed on a row that already had a "
                    "track_id — it says nothing about whether the guard's "
                    "OWN read of spec.track_id (the json_dir target above) "
                    "is ever reachable. golf-4r2 (OI-1640, 2026-09-06): this "
                    "column alone being partially filled (122/815) is what "
                    "let a genuine zero-fill guard-source (0/1115, above) "
                    "report a false [OK] — never read this target's rate as "
                    "an answer to 'can the guard fire'."
                ),
            ),
        ),
        note=(
            "spec.track_id (DispatchSpec) gates the plan-first-gate "
            "enforcement branch in dispatch_cli._check_track_link_verdict, "
            "and the persistence early-return in _persist_track_id. "
            "VNX_REQUIRE_DISPATCH_TRACK defaults OFF, so a nonzero-but-partial "
            "fill rate on the json_dir target is the designed advisory "
            "state, not a defect — only an EXACT zero is flagged. The "
            "sqlite target is informational only (see its own note)."
        ),
    ),
    FieldMapping(
        field="post_merge_verification",
        targets=(
            StoreTarget(
                kind="json_dir",
                dir_relpath="dispatches",
                glob="dispatch-spec.json",
                note=(
                    "AUTHORITATIVE for guard reachability: dispatch_cli.py:2209 "
                    "reads spec.post_merge_verification directly ('and "
                    "spec.post_merge_verification:'), no local-var indirection. "
                    "Measured 2026-09-06: every staged spec that carries this "
                    "key (365 of 1116) carries it as the literal value False — "
                    "OI-1639, a second, independent instance of the golf-4 "
                    "unreachable-guard shape, found in the same ronde as "
                    "OI-1640 above and deliberately NOT fixed by this dispatch "
                    "(see the dispatch report's Open Items)."
                ),
            ),
        ),
        note=(
            "spec.post_merge_verification (DispatchSpec) gates the "
            "checkout-lag verification-reminder verdict in "
            "run_dispatch (dispatch_cli.py:2209) — a truthy bool test, no "
            "indirection. No caller in the repo sets it True on a staged "
            "bundle (grep dispatch_bridge.py / the stage_spec_bundle CLI), "
            "so that branch is currently unreachable in practice (OI-1639, "
            "tracked separately from this dispatch's OI-1640 fix)."
        ),
    ),
)

# golf-4r2 (2026-09-06): "1364 unmeasured fields" is not itself a defect —
# most guard fields in this repo are unrelated to this bug class — but it
# must never SILENTLY become "0 mapped fields" (a registry that lost every
# entry would report a permanently clean audit and nobody would notice,
# the exact valkuil this whole detector exists to close). Bump this only
# after actually adding a reviewed entry above, never to make room for one.
MIN_MAPPED_FIELDS = 2


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
    mappings, no ``ACCEPTED_GAPS`` entry with an empty/whitespace reason
    (the whole point of this module is that a reason is mandatory and
    reviewable — an empty string is the same failure mode as a doc-comment
    nobody reads), and the mapped-field count never silently drops below
    ``MIN_MAPPED_FIELDS`` (golf-4r2: a registry that lost every entry would
    report a permanently clean audit and look identical to "nothing to fix").
    """
    if len(FIELD_STORE_MAP) < MIN_MAPPED_FIELDS:
        raise ValueError(
            f"FIELD_STORE_MAP has {len(FIELD_STORE_MAP)} entr(y/ies), below "
            f"MIN_MAPPED_FIELDS={MIN_MAPPED_FIELDS} — a mapped-field count "
            "silently dropping is indistinguishable from the registry "
            "losing coverage, not from there being nothing left to map "
            "(golf-4r2, 2026-09-06)"
        )
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
