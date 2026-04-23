#!/usr/bin/env python3
"""Scoped preference and lesson store with provenance tracking.

Provides an in-memory store for operator preferences and learned lessons
scoped by governance profile and subject domain, with full provenance
tracking and cross-profile isolation enforcement.

Components:
  EntryKind              — enum distinguishing preferences from learned lessons
  ScopeKey               — named tuple (profile, domain) as query boundary
  PreferenceEntry        — frozen dataclass with full provenance fields
  PreferenceStore        — mutable store with query, retire, and scope APIs
  record_entry()         — validated factory that creates and adds an entry
  assert_scope_not_contaminated() — cross-profile isolation guard
  preference_store()     — factory returning a fresh empty store

Design invariants:
  - Entries are immutable after creation (frozen dataclass).
  - Scope boundaries are enforced: queries return only the requested profile+domain.
  - Cross-profile contamination is detectable and raises ValueError explicitly.
  - Evidence refs are mandatory — zero-evidence entries are rejected at creation.
  - Retired entries are excluded from default queries but remain in the store.
  - entry_id uniqueness is enforced by the store: duplicate raises ValueError.
  - Known profiles are validated at record_entry time.

Known profiles:
  - regulated_strict
  - coding_strict
  - business_light

Usage (record and query a preference):
    store = preference_store()
    record_entry(
        store,
        scope=ScopeKey(profile="coding_strict", domain="approval"),
        kind=EntryKind.PREFERENCE,
        content="Always require dual-approval for production dispatches.",
        evidence_refs=("dispatch-001", "gate-review-007"),
        recorded_by="operator:alice",
    )
    results = store.query(ScopeKey(profile="coding_strict", domain="approval"))
    assert len(results) == 1

Usage (cross-profile isolation):
    entry = results[0]
    assert_scope_not_contaminated(entry, expected_profile="coding_strict")  # ok
    assert_scope_not_contaminated(entry, expected_profile="regulated_strict")  # raises ValueError
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, NamedTuple, Optional, Tuple
import uuid


# ---------------------------------------------------------------------------
# Known profiles
# ---------------------------------------------------------------------------

KNOWN_PROFILES: frozenset = frozenset({
    "regulated_strict",
    "coding_strict",
    "business_light",
})


# ---------------------------------------------------------------------------
# EntryKind
# ---------------------------------------------------------------------------

class EntryKind(Enum):
    """Classification of a stored entry.

    PREFERENCE — an explicit operator preference for how the system should behave.
    LESSON     — a learned lesson captured from execution experience.
    """
    PREFERENCE = "preference"
    LESSON = "lesson"


# ---------------------------------------------------------------------------
# ScopeKey
# ---------------------------------------------------------------------------

class ScopeKey(NamedTuple):
    """Immutable scope boundary for querying preferences and lessons.

    Attributes:
        profile: Governance profile (e.g., "regulated_strict", "coding_strict",
                 "business_light").
        domain:  Subject area (e.g., "approval", "gate", "dispatch").
    """
    profile: str
    domain: str


# ---------------------------------------------------------------------------
# PreferenceEntry
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


@dataclass(frozen=True)
class PreferenceEntry:
    """Immutable preference or lesson record with full provenance.

    Attributes:
        entry_id:     Unique identifier — format "pref-<uuid4>" or "lesson-<uuid4>".
        scope:        ScopeKey (profile + domain) this entry belongs to.
        kind:         EntryKind.PREFERENCE or EntryKind.LESSON.
        content:      The preference or lesson text.
        evidence_refs: Tuple of pointers to supporting evidence (dispatch IDs,
                       gate IDs, etc.). At least one is required.
        recorded_at:  ISO 8601 timestamp of when this entry was recorded.
        recorded_by:  Operator or agent identity that recorded the entry.
        retired:      Whether this entry has been retired (stale).
        retired_at:   ISO 8601 timestamp of retirement, or None if active.
    """
    entry_id: str
    scope: ScopeKey
    kind: EntryKind
    content: str
    evidence_refs: tuple
    recorded_at: str
    recorded_by: str
    retired: bool = False
    retired_at: Optional[str] = None

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of this entry."""
        return {
            "entry_id": self.entry_id,
            "scope": {
                "profile": self.scope.profile,
                "domain": self.scope.domain,
            },
            "kind": self.kind.value,
            "content": self.content,
            "evidence_refs": list(self.evidence_refs),
            "recorded_at": self.recorded_at,
            "recorded_by": self.recorded_by,
            "retired": self.retired,
            "retired_at": self.retired_at,
        }


# ---------------------------------------------------------------------------
# PreferenceStore
# ---------------------------------------------------------------------------

class PreferenceStore:
    """Mutable in-memory store for PreferenceEntry records.

    Entries are keyed by entry_id. Scope-based queries return only entries
    matching the requested profile+domain combination.

    This class is intentionally NOT frozen — it accumulates entries over time.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, PreferenceEntry] = {}

    def add(self, entry: PreferenceEntry) -> None:
        """Add an entry to the store.

        Raises:
            ValueError: If an entry with the same entry_id already exists.
        """
        if entry.entry_id in self._entries:
            raise ValueError(
                f"Entry with entry_id {entry.entry_id!r} already exists in the store."
            )
        self._entries[entry.entry_id] = entry

    def query(
        self,
        scope: ScopeKey,
        kind: Optional[EntryKind] = None,
        include_retired: bool = False,
    ) -> List[PreferenceEntry]:
        """Return entries matching the given scope.

        Args:
            scope:           Profile+domain boundary to filter on.
            kind:            Optional EntryKind filter. None returns all kinds.
            include_retired: When False (default), retired entries are excluded.

        Returns:
            List of matching PreferenceEntry instances in insertion order.
        """
        results = []
        for entry in self._entries.values():
            if entry.scope != scope:
                continue
            if not include_retired and entry.retired:
                continue
            if kind is not None and entry.kind != kind:
                continue
            results.append(entry)
        return results

    def get(self, entry_id: str) -> Optional[PreferenceEntry]:
        """Return the entry with the given entry_id, or None if not found."""
        return self._entries.get(entry_id)

    def retire(
        self,
        entry_id: str,
        retired_at: Optional[str] = None,
    ) -> PreferenceEntry:
        """Mark an entry as retired and return the updated frozen entry.

        The existing entry is replaced in the store with a new frozen entry
        that has retired=True and retired_at set.

        Args:
            entry_id:   The ID of the entry to retire.
            retired_at: Optional explicit retirement timestamp. Defaults to now.

        Returns:
            The new retired PreferenceEntry.

        Raises:
            KeyError: If no entry with the given entry_id exists.
        """
        if entry_id not in self._entries:
            raise KeyError(f"Entry not found: {entry_id!r}")
        ts = retired_at if retired_at is not None else _now_utc()
        retired_entry = replace(
            self._entries[entry_id],
            retired=True,
            retired_at=ts,
        )
        self._entries[entry_id] = retired_entry
        return retired_entry

    def all_scopes(self) -> List[ScopeKey]:
        """Return the list of distinct ScopeKey values present in the store.

        Order is stable (insertion order of first occurrence).
        """
        seen = []
        for entry in self._entries.values():
            if entry.scope not in seen:
                seen.append(entry.scope)
        return seen

    def entry_count(self, include_retired: bool = False) -> int:
        """Return the total number of entries in the store.

        Args:
            include_retired: When False (default), retired entries are excluded.
        """
        if include_retired:
            return len(self._entries)
        return sum(1 for e in self._entries.values() if not e.retired)


# ---------------------------------------------------------------------------
# record_entry factory
# ---------------------------------------------------------------------------

def record_entry(
    store: PreferenceStore,
    scope: ScopeKey,
    kind: EntryKind,
    content: str,
    evidence_refs: Tuple[str, ...],
    recorded_by: str,
    recorded_at: Optional[str] = None,
) -> PreferenceEntry:
    """Validate inputs, create a PreferenceEntry, and add it to the store.

    Validation rules:
      - content must be non-empty.
      - recorded_by must be non-empty.
      - evidence_refs must contain at least one item.
      - scope.profile must be one of the known profiles.

    Args:
        store:        The PreferenceStore to add the entry to.
        scope:        ScopeKey (profile + domain) for the new entry.
        kind:         EntryKind.PREFERENCE or EntryKind.LESSON.
        content:      The preference or lesson text.
        evidence_refs: Tuple of evidence pointers. At least one required.
        recorded_by:  Operator or agent identity. Must be non-empty.
        recorded_at:  Optional explicit timestamp. Defaults to current UTC time.

    Returns:
        The newly created and stored PreferenceEntry.

    Raises:
        ValueError: If any validation rule is violated.
    """
    if not content or not content.strip():
        raise ValueError("content must be non-empty.")
    if not recorded_by or not recorded_by.strip():
        raise ValueError("recorded_by must be non-empty.")
    if not evidence_refs:
        raise ValueError(
            "evidence_refs must contain at least one evidence pointer."
        )
    if scope.profile not in KNOWN_PROFILES:
        raise ValueError(
            f"Unknown governance profile {scope.profile!r}. "
            f"Known profiles: {sorted(KNOWN_PROFILES)}"
        )

    prefix = "pref" if kind == EntryKind.PREFERENCE else "lesson"
    entry_id = f"{prefix}-{uuid.uuid4()}"
    ts = recorded_at if recorded_at is not None else _now_utc()

    entry = PreferenceEntry(
        entry_id=entry_id,
        scope=scope,
        kind=kind,
        content=content,
        evidence_refs=tuple(evidence_refs),
        recorded_at=ts,
        recorded_by=recorded_by,
    )
    store.add(entry)
    return entry


# ---------------------------------------------------------------------------
# Cross-profile isolation guard
# ---------------------------------------------------------------------------

def assert_scope_not_contaminated(
    entry: PreferenceEntry,
    expected_profile: str,
) -> None:
    """Assert that an entry belongs to the expected governance profile.

    Raises:
        ValueError: If entry.scope.profile does not match expected_profile.
    """
    if entry.scope.profile != expected_profile:
        raise ValueError(
            f"Scope contamination detected: entry {entry.entry_id!r} belongs to "
            f"profile {entry.scope.profile!r} but expected {expected_profile!r}."
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def preference_store() -> PreferenceStore:
    """Return a fresh empty PreferenceStore."""
    return PreferenceStore()
