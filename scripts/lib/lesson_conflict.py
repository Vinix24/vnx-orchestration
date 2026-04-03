#!/usr/bin/env python3
"""Lesson conflict detection and resolution with audit trail.

Detects conflicts between lessons that share the same governance profile and
subject domain, and provides an explicit resolution workflow so disagreements
are surfaced, governed, and traceable rather than silently overwritten.

Components:
  ConflictPair         — identifies two lessons in structural conflict
  ResolutionKind       — enum of allowed resolution actions (ACCEPT, DEFER, RETIRE)
  ConflictResolution   — immutable record of how a conflict was resolved
  ResolutionLog        — append-only log of all resolutions in a session
  detect_conflicts()   — scan a PreferenceStore and return all ConflictPairs
  resolve_conflict()   — apply a resolution action and record it in the log
  resolution_log()     — factory returning a fresh empty ResolutionLog

Design invariants:
  - CL-1: Conflicts are structural — any two active lessons in the same scope
           are a ConflictPair that must be resolved explicitly.
  - CL-2: Resolutions are immutable and append-only (no overwrite of the log).
  - CL-3: ACCEPT retires all other lessons in the pair; RETIRE retires the
           named entry; DEFER records the conflict without retiring anything.
  - CL-4: No silent overwrite — every resolution requires an explicit rationale.
  - CL-5: resolution_id format is "resolution-<uuid4>".

Resolution kinds:
  - ACCEPT  — accept one lesson as authoritative; retire all others in the pair
  - DEFER   — log the conflict as known but deferred (entries remain active)
  - RETIRE  — retire one specific lesson to resolve the conflict

Usage:
    store = preference_store()
    record_entry(store, scope=ScopeKey("regulated_strict", "approval"),
                 kind=EntryKind.LESSON, content="Always require dual-approval.",
                 evidence_refs=("d-001",), recorded_by="operator")
    record_entry(store, scope=ScopeKey("regulated_strict", "approval"),
                 kind=EntryKind.LESSON, content="Single approval is sufficient.",
                 evidence_refs=("d-002",), recorded_by="operator")

    pairs = detect_conflicts(store)
    # len(pairs) == 1

    log = resolution_log()
    resolution = resolve_conflict(
        log=log, store=store,
        conflict=pairs[0],
        kind=ResolutionKind.ACCEPT,
        accepted_entry_id=pairs[0].entry_id_a,
        rationale="Dual-approval aligns with regulated_strict policy.",
        resolved_by="operator:alice",
    )
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import combinations
from typing import List, Optional, Tuple

from preference_store import EntryKind, PreferenceEntry, PreferenceStore, ScopeKey


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _new_resolution_id() -> str:
    """Generate a unique resolution ID in format 'resolution-<uuid4>'."""
    return f"resolution-{uuid.uuid4()}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConflictResolutionError(Exception):
    """Base error for conflict resolution violations."""


class InvalidResolutionError(ConflictResolutionError):
    """Raised when a resolution action is invalid for the given conflict."""


# ---------------------------------------------------------------------------
# ResolutionKind
# ---------------------------------------------------------------------------

class ResolutionKind(Enum):
    """Allowed resolution actions for a lesson conflict.

    ACCEPT — designate one lesson as authoritative; retire all others in the pair.
    DEFER  — record the conflict as known but defer resolution (entries stay active).
    RETIRE — retire one specific lesson to resolve the conflict.
    """
    ACCEPT = "accept"
    DEFER  = "defer"
    RETIRE = "retire"


# ---------------------------------------------------------------------------
# ConflictPair — structural conflict between two lessons
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConflictPair:
    """Two active lessons in the same scope that are in structural conflict.

    CL-1: any two active lessons sharing the same profile+domain constitute a
    ConflictPair. Both must be resolved explicitly — no silent overwrite.

    Attributes:
        entry_id_a: ID of the first conflicting lesson.
        entry_id_b: ID of the second conflicting lesson.
        scope:      The shared ScopeKey (profile + domain).
    """
    entry_id_a: str
    entry_id_b: str
    scope: ScopeKey

    def involves(self, entry_id: str) -> bool:
        """Return True if this pair involves the given entry_id."""
        return entry_id in (self.entry_id_a, self.entry_id_b)

    def other(self, entry_id: str) -> str:
        """Return the other entry_id in the pair.

        Raises:
            ValueError: If entry_id is not part of this pair.
        """
        if entry_id == self.entry_id_a:
            return self.entry_id_b
        if entry_id == self.entry_id_b:
            return self.entry_id_a
        raise ValueError(
            f"Entry {entry_id!r} is not part of conflict pair "
            f"({self.entry_id_a!r}, {self.entry_id_b!r})."
        )

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return {
            "entry_id_a": self.entry_id_a,
            "entry_id_b": self.entry_id_b,
            "scope": {"profile": self.scope.profile, "domain": self.scope.domain},
        }


# ---------------------------------------------------------------------------
# ConflictResolution — immutable resolution record (CL-2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConflictResolution:
    """Immutable record of how a ConflictPair was resolved.

    CL-2: resolutions are immutable and append-only.
    CL-4: every resolution records an explicit rationale.
    CL-5: resolution_id format is "resolution-<uuid4>".

    Attributes:
        resolution_id:    Unique identifier ("resolution-<uuid4>").
        conflict:         The ConflictPair this resolution addresses.
        kind:             ResolutionKind action taken.
        accepted_entry_id: For ACCEPT: the entry designated as authoritative.
                           None for DEFER/RETIRE.
        retired_entry_id:  For RETIRE/ACCEPT: the entry retired as a result.
                           ACCEPT retires all entries except accepted_entry_id.
                           None for DEFER.
        rationale:        Explicit reason for the resolution decision.
        resolved_by:      Operator or agent identity making the decision.
        resolved_at:      ISO 8601 timestamp of the resolution.
    """
    resolution_id:     str
    conflict:          ConflictPair
    kind:              ResolutionKind
    accepted_entry_id: Optional[str]
    retired_entry_id:  Optional[str]
    rationale:         str
    resolved_by:       str
    resolved_at:       str

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of this resolution."""
        return {
            "resolution_id":     self.resolution_id,
            "conflict":          self.conflict.to_dict(),
            "kind":              self.kind.value,
            "accepted_entry_id": self.accepted_entry_id,
            "retired_entry_id":  self.retired_entry_id,
            "rationale":         self.rationale,
            "resolved_by":       self.resolved_by,
            "resolved_at":       self.resolved_at,
        }


# ---------------------------------------------------------------------------
# ResolutionLog — append-only audit trail (CL-2)
# ---------------------------------------------------------------------------

class ResolutionLog:
    """Append-only log of ConflictResolution records.

    CL-2: resolutions are immutable and append-only. Records can be inspected
    but never deleted or modified.

    Entries are stored in insertion order and keyed by resolution_id for
    fast lookup.
    """

    def __init__(self) -> None:
        self._records: dict[str, ConflictResolution] = {}
        self._order: list[str] = []

    def append(self, resolution: ConflictResolution) -> None:
        """Append a ConflictResolution to the log.

        Raises:
            ValueError: If a resolution with the same ID already exists.
        """
        if resolution.resolution_id in self._records:
            raise ValueError(
                f"Resolution {resolution.resolution_id!r} already exists in the log."
            )
        self._records[resolution.resolution_id] = resolution
        self._order.append(resolution.resolution_id)

    def get(self, resolution_id: str) -> Optional[ConflictResolution]:
        """Return the resolution with the given ID, or None."""
        return self._records.get(resolution_id)

    def all(self) -> List[ConflictResolution]:
        """Return all resolutions in insertion order."""
        return [self._records[rid] for rid in self._order]

    def for_entry(self, entry_id: str) -> List[ConflictResolution]:
        """Return all resolutions that involve the given entry_id."""
        return [
            r for r in self.all()
            if r.conflict.involves(entry_id)
        ]

    def count(self) -> int:
        """Return the number of resolutions recorded."""
        return len(self._order)


# ---------------------------------------------------------------------------
# Conflict detection (CL-1)
# ---------------------------------------------------------------------------

def detect_conflicts(store: PreferenceStore) -> List[ConflictPair]:
    """Scan a PreferenceStore and return all structural ConflictPairs.

    CL-1: any two active lessons sharing the same scope (profile + domain)
    are a ConflictPair. Retired lessons and PREFERENCE entries are excluded.

    Args:
        store: The PreferenceStore to scan.

    Returns:
        List of ConflictPair instances. Empty list if no conflicts exist.
        Pairs are ordered deterministically (entry_id_a < entry_id_b
        lexicographically).
    """
    # Group active lessons by scope
    scope_to_lessons: dict[ScopeKey, list[PreferenceEntry]] = {}
    for scope in store.all_scopes():
        lessons = store.query(scope, kind=EntryKind.LESSON, include_retired=False)
        if len(lessons) >= 2:
            scope_to_lessons[scope] = lessons

    pairs: list[ConflictPair] = []
    for scope, lessons in scope_to_lessons.items():
        for a, b in combinations(lessons, 2):
            # Canonical ordering: lexicographically smaller ID first
            id_a, id_b = sorted([a.entry_id, b.entry_id])
            pairs.append(ConflictPair(entry_id_a=id_a, entry_id_b=id_b, scope=scope))

    return pairs


# ---------------------------------------------------------------------------
# Resolution helpers (CL-3)
# ---------------------------------------------------------------------------

def _apply_accept(
    store: PreferenceStore,
    conflict: ConflictPair,
    accepted_entry_id: Optional[str],
    ts: str,
) -> str:
    """Validate ACCEPT inputs, retire the non-accepted entry, return retired ID."""
    if not accepted_entry_id:
        raise InvalidResolutionError("ACCEPT resolution requires accepted_entry_id.")
    if not conflict.involves(accepted_entry_id):
        raise InvalidResolutionError(
            f"accepted_entry_id {accepted_entry_id!r} is not part of the "
            f"conflict pair ({conflict.entry_id_a!r}, {conflict.entry_id_b!r})."
        )
    other_id = conflict.other(accepted_entry_id)
    entry = store.get(other_id)
    if entry is None:
        raise InvalidResolutionError(
            f"Entry {other_id!r} not found in store — cannot retire it."
        )
    if not entry.retired:
        store.retire(other_id, retired_at=ts)
    return other_id


def _apply_retire(
    store: PreferenceStore,
    conflict: ConflictPair,
    retired_entry_id: Optional[str],
    ts: str,
) -> str:
    """Validate RETIRE inputs, retire the named entry, return retired ID."""
    if not retired_entry_id:
        raise InvalidResolutionError("RETIRE resolution requires retired_entry_id.")
    if not conflict.involves(retired_entry_id):
        raise InvalidResolutionError(
            f"retired_entry_id {retired_entry_id!r} is not part of the "
            f"conflict pair ({conflict.entry_id_a!r}, {conflict.entry_id_b!r})."
        )
    entry = store.get(retired_entry_id)
    if entry is None:
        raise InvalidResolutionError(
            f"Entry {retired_entry_id!r} not found in store — cannot retire it."
        )
    if not entry.retired:
        store.retire(retired_entry_id, retired_at=ts)
    return retired_entry_id


# ---------------------------------------------------------------------------
# Resolution workflow (CL-3, CL-4)
# ---------------------------------------------------------------------------

def resolve_conflict(
    log: ResolutionLog,
    store: PreferenceStore,
    conflict: ConflictPair,
    kind: ResolutionKind,
    rationale: str,
    resolved_by: str,
    accepted_entry_id: Optional[str] = None,
    retired_entry_id: Optional[str] = None,
    resolved_at: Optional[str] = None,
) -> ConflictResolution:
    """Apply a resolution action to a ConflictPair and record it in the log.

    CL-3 semantics by kind:
      - ACCEPT: accepted_entry_id required; other entry in the pair is retired.
      - RETIRE: retired_entry_id required; that entry is retired.
      - DEFER:  neither accepted nor retired; conflict is logged as deferred.

    CL-4: rationale and resolved_by must be non-empty.

    Raises:
        ValueError:             If rationale or resolved_by is empty.
        InvalidResolutionError: If required IDs are missing/wrong or not found.
    """
    if not rationale or not rationale.strip():
        raise ValueError("rationale must be non-empty.")
    if not resolved_by or not resolved_by.strip():
        raise ValueError("resolved_by must be non-empty.")

    ts = resolved_at if resolved_at is not None else _now_utc()
    effective_retired: Optional[str] = None

    if kind == ResolutionKind.ACCEPT:
        effective_retired = _apply_accept(store, conflict, accepted_entry_id, ts)
    elif kind == ResolutionKind.RETIRE:
        effective_retired = _apply_retire(store, conflict, retired_entry_id, ts)
    # DEFER: no entries retired; conflict is explicitly deferred

    resolution = ConflictResolution(
        resolution_id=_new_resolution_id(),
        conflict=conflict,
        kind=kind,
        accepted_entry_id=accepted_entry_id if kind == ResolutionKind.ACCEPT else None,
        retired_entry_id=effective_retired,
        rationale=rationale,
        resolved_by=resolved_by,
        resolved_at=ts,
    )
    log.append(resolution)
    return resolution


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def resolution_log() -> ResolutionLog:
    """Return a fresh empty ResolutionLog."""
    return ResolutionLog()
