#!/usr/bin/env python3
"""Tests for lesson conflict detection and resolution workflow.

Covers:
  - Structural conflict detection (same scope, active lessons)
  - Resolution workflow: ACCEPT, DEFER, RETIRE
  - Audit trail completeness and immutability
  - Guard rails: missing required fields, invalid IDs, silent-overwrite prevention
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from preference_store import EntryKind, ScopeKey, preference_store, record_entry
from lesson_conflict import (
    ConflictPair,
    ConflictResolution,
    ConflictResolutionError,
    InvalidResolutionError,
    ResolutionKind,
    ResolutionLog,
    detect_conflicts,
    resolution_log,
    resolve_conflict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _scope(profile: str = "regulated_strict", domain: str = "approval") -> ScopeKey:
    return ScopeKey(profile=profile, domain=domain)


def _add_lesson(
    store,
    content: str,
    scope: ScopeKey = None,
    evidence_refs: tuple = ("d-001",),
    recorded_by: str = "operator",
) -> object:
    if scope is None:
        scope = _scope()
    return record_entry(
        store,
        scope=scope,
        kind=EntryKind.LESSON,
        content=content,
        evidence_refs=evidence_refs,
        recorded_by=recorded_by,
    )


def _add_preference(store, content: str, scope: ScopeKey = None) -> object:
    if scope is None:
        scope = _scope()
    return record_entry(
        store,
        scope=scope,
        kind=EntryKind.PREFERENCE,
        content=content,
        evidence_refs=("d-001",),
        recorded_by="operator",
    )


def _store_with_conflict():
    """Return a store with two conflicting lessons in the same scope."""
    store = preference_store()
    a = _add_lesson(store, "Always require dual-approval for production dispatches.")
    b = _add_lesson(store, "Single approval is sufficient for low-risk dispatches.")
    return store, a, b


# ---------------------------------------------------------------------------
# ConflictPair unit tests
# ---------------------------------------------------------------------------

class TestConflictPair:

    def test_involves_returns_true_for_member(self) -> None:
        pair = ConflictPair("entry-a", "entry-b", _scope())
        assert pair.involves("entry-a")
        assert pair.involves("entry-b")

    def test_involves_returns_false_for_non_member(self) -> None:
        pair = ConflictPair("entry-a", "entry-b", _scope())
        assert not pair.involves("entry-x")

    def test_other_returns_opposite_id(self) -> None:
        pair = ConflictPair("entry-a", "entry-b", _scope())
        assert pair.other("entry-a") == "entry-b"
        assert pair.other("entry-b") == "entry-a"

    def test_other_raises_for_non_member(self) -> None:
        pair = ConflictPair("entry-a", "entry-b", _scope())
        with pytest.raises(ValueError, match="entry-x"):
            pair.other("entry-x")

    def test_to_dict_is_json_serializable(self) -> None:
        pair = ConflictPair("entry-a", "entry-b", _scope())
        d = pair.to_dict()
        assert d["entry_id_a"] == "entry-a"
        assert d["entry_id_b"] == "entry-b"
        assert d["scope"]["profile"] == "regulated_strict"
        assert d["scope"]["domain"] == "approval"


# ---------------------------------------------------------------------------
# Conflict detection (CL-1)
# ---------------------------------------------------------------------------

class TestDetectConflicts:

    def test_no_conflicts_in_empty_store(self) -> None:
        store = preference_store()
        assert detect_conflicts(store) == []

    def test_no_conflicts_with_single_lesson(self) -> None:
        store = preference_store()
        _add_lesson(store, "Only one lesson here.")
        assert detect_conflicts(store) == []

    def test_two_lessons_same_scope_yields_one_pair(self) -> None:
        store, a, b = _store_with_conflict()
        pairs = detect_conflicts(store)
        assert len(pairs) == 1

    def test_conflict_pair_covers_correct_entries(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        assert {pair.entry_id_a, pair.entry_id_b} == {a.entry_id, b.entry_id}

    def test_conflict_pair_scope_matches_entries(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        assert pair.scope == _scope()

    def test_three_lessons_yields_three_pairs(self) -> None:
        store = preference_store()
        _add_lesson(store, "Lesson A")
        _add_lesson(store, "Lesson B")
        _add_lesson(store, "Lesson C")
        pairs = detect_conflicts(store)
        assert len(pairs) == 3

    def test_preferences_are_excluded_from_conflict_detection(self) -> None:
        store = preference_store()
        _add_lesson(store, "One lesson only.")
        _add_preference(store, "A preference, not a lesson.")
        assert detect_conflicts(store) == []

    def test_retired_lessons_are_excluded(self) -> None:
        store, a, b = _store_with_conflict()
        store.retire(b.entry_id)
        assert detect_conflicts(store) == []

    def test_lessons_in_different_domains_do_not_conflict(self) -> None:
        store = preference_store()
        _add_lesson(store, "Approval lesson.", scope=_scope(domain="approval"))
        _add_lesson(store, "Gate lesson.", scope=_scope(domain="gate"))
        assert detect_conflicts(store) == []

    def test_lessons_in_different_profiles_do_not_conflict(self) -> None:
        store = preference_store()
        _add_lesson(store, "Regulated lesson.", scope=_scope(profile="regulated_strict"))
        _add_lesson(store, "Business lesson.", scope=_scope(profile="business_light"))
        assert detect_conflicts(store) == []

    def test_pair_ids_are_canonically_ordered(self) -> None:
        """entry_id_a should be lexicographically <= entry_id_b."""
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        assert pair.entry_id_a <= pair.entry_id_b

    def test_two_independent_scopes_with_conflicts(self) -> None:
        store = preference_store()
        _add_lesson(store, "A1", scope=_scope(domain="approval"))
        _add_lesson(store, "A2", scope=_scope(domain="approval"))
        _add_lesson(store, "G1", scope=_scope(domain="gate"))
        _add_lesson(store, "G2", scope=_scope(domain="gate"))
        pairs = detect_conflicts(store)
        assert len(pairs) == 2
        scopes = {p.scope for p in pairs}
        assert scopes == {_scope(domain="approval"), _scope(domain="gate")}


# ---------------------------------------------------------------------------
# Resolution workflow — ACCEPT (CL-3)
# ---------------------------------------------------------------------------

class TestResolveAccept:

    def test_accept_retires_other_entry(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.ACCEPT,
            accepted_entry_id=a.entry_id,
            rationale="Dual-approval aligns with policy.",
            resolved_by="operator:alice",
        )
        assert store.get(b.entry_id).retired is True
        assert store.get(a.entry_id).retired is False

    def test_accept_resolution_recorded_in_log(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.ACCEPT,
            accepted_entry_id=a.entry_id,
            rationale="Authoritative guidance.",
            resolved_by="operator",
        )
        assert log.count() == 1
        assert log.get(resolution.resolution_id) is resolution

    def test_accept_resolution_has_correct_fields(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.ACCEPT,
            accepted_entry_id=a.entry_id,
            rationale="Rationale here.",
            resolved_by="operator",
        )
        assert resolution.kind == ResolutionKind.ACCEPT
        assert resolution.accepted_entry_id == a.entry_id
        assert resolution.retired_entry_id == b.entry_id
        assert resolution.rationale == "Rationale here."
        assert resolution.resolved_by == "operator"
        assert resolution.resolution_id.startswith("resolution-")

    def test_accept_with_other_entry_as_accepted(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.ACCEPT,
            accepted_entry_id=b.entry_id,
            rationale="Second lesson is more accurate.",
            resolved_by="operator",
        )
        assert store.get(a.entry_id).retired is True
        assert store.get(b.entry_id).retired is False

    def test_accept_requires_accepted_entry_id(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(InvalidResolutionError, match="accepted_entry_id"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.ACCEPT,
                rationale="Missing accepted_entry_id.",
                resolved_by="operator",
            )

    def test_accept_rejects_non_member_accepted_id(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(InvalidResolutionError, match="not part of"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.ACCEPT,
                accepted_entry_id="some-other-entry",
                rationale="Wrong entry.",
                resolved_by="operator",
            )

    def test_after_accept_conflict_no_longer_detected(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.ACCEPT,
            accepted_entry_id=a.entry_id,
            rationale="Resolved.",
            resolved_by="operator",
        )
        assert detect_conflicts(store) == []


# ---------------------------------------------------------------------------
# Resolution workflow — RETIRE (CL-3)
# ---------------------------------------------------------------------------

class TestResolveRetire:

    def test_retire_retires_specified_entry(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.RETIRE,
            retired_entry_id=b.entry_id,
            rationale="Lesson B is stale.",
            resolved_by="operator",
        )
        assert store.get(b.entry_id).retired is True
        assert store.get(a.entry_id).retired is False

    def test_retire_resolution_recorded_in_log(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.RETIRE,
            retired_entry_id=b.entry_id,
            rationale="Stale.",
            resolved_by="operator",
        )
        assert log.count() == 1
        assert resolution.kind == ResolutionKind.RETIRE
        assert resolution.retired_entry_id == b.entry_id
        assert resolution.accepted_entry_id is None

    def test_retire_requires_retired_entry_id(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(InvalidResolutionError, match="retired_entry_id"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.RETIRE,
                rationale="Stale.",
                resolved_by="operator",
            )

    def test_retire_rejects_non_member_retired_id(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(InvalidResolutionError, match="not part of"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.RETIRE,
                retired_entry_id="not-in-pair",
                rationale="Wrong.",
                resolved_by="operator",
            )

    def test_after_retire_conflict_no_longer_detected(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.RETIRE,
            retired_entry_id=b.entry_id,
            rationale="Stale.",
            resolved_by="operator",
        )
        assert detect_conflicts(store) == []


# ---------------------------------------------------------------------------
# Resolution workflow — DEFER (CL-3)
# ---------------------------------------------------------------------------

class TestResolveDefer:

    def test_defer_does_not_retire_any_entry(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Will revisit in Q2 planning.",
            resolved_by="operator",
        )
        assert store.get(a.entry_id).retired is False
        assert store.get(b.entry_id).retired is False

    def test_defer_conflict_still_detected_afterwards(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Deferred.",
            resolved_by="operator",
        )
        assert len(detect_conflicts(store)) == 1

    def test_defer_resolution_recorded_in_log(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Deferred.",
            resolved_by="operator",
        )
        assert log.count() == 1
        assert resolution.kind == ResolutionKind.DEFER
        assert resolution.accepted_entry_id is None
        assert resolution.retired_entry_id is None


# ---------------------------------------------------------------------------
# Guard rails — no silent overwrite (CL-4)
# ---------------------------------------------------------------------------

class TestGuardRails:

    def test_empty_rationale_raises_value_error(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(ValueError, match="rationale"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.DEFER,
                rationale="",
                resolved_by="operator",
            )

    def test_whitespace_only_rationale_raises(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(ValueError, match="rationale"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.DEFER,
                rationale="   ",
                resolved_by="operator",
            )

    def test_empty_resolved_by_raises_value_error(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        with pytest.raises(ValueError, match="resolved_by"):
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.DEFER,
                rationale="Deferred.",
                resolved_by="",
            )

    def test_duplicate_resolution_id_raises(self) -> None:
        log = resolution_log()
        scope = _scope()
        pair = ConflictPair("entry-a", "entry-b", scope)
        r1 = ConflictResolution(
            resolution_id="resolution-fixed",
            conflict=pair,
            kind=ResolutionKind.DEFER,
            accepted_entry_id=None,
            retired_entry_id=None,
            rationale="First.",
            resolved_by="op",
            resolved_at="2026-04-03T00:00:00.000000Z",
        )
        r2 = ConflictResolution(
            resolution_id="resolution-fixed",
            conflict=pair,
            kind=ResolutionKind.DEFER,
            accepted_entry_id=None,
            retired_entry_id=None,
            rationale="Duplicate.",
            resolved_by="op",
            resolved_at="2026-04-03T00:01:00.000000Z",
        )
        log.append(r1)
        with pytest.raises(ValueError, match="already exists"):
            log.append(r2)


# ---------------------------------------------------------------------------
# Audit trail completeness (CL-2, CL-5)
# ---------------------------------------------------------------------------

class TestAuditTrail:

    def test_resolution_id_format(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Deferred.",
            resolved_by="operator",
        )
        assert resolution.resolution_id.startswith("resolution-")

    def test_multiple_resolutions_accumulate_in_log(self) -> None:
        store = preference_store()
        a = _add_lesson(store, "Lesson A", scope=_scope(domain="approval"))
        b = _add_lesson(store, "Lesson B", scope=_scope(domain="approval"))
        c = _add_lesson(store, "Lesson C", scope=_scope(domain="gate"))
        d = _add_lesson(store, "Lesson D", scope=_scope(domain="gate"))
        log = resolution_log()
        pairs = detect_conflicts(store)
        assert len(pairs) == 2
        for pair in pairs:
            resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.DEFER,
                rationale="Deferred.",
                resolved_by="operator",
            )
        assert log.count() == 2

    def test_log_for_entry_returns_related_resolutions(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Deferred.",
            resolved_by="operator",
        )
        assert resolution in log.for_entry(a.entry_id)
        assert resolution in log.for_entry(b.entry_id)

    def test_log_for_entry_excludes_unrelated_resolutions(self) -> None:
        store = preference_store()
        a = _add_lesson(store, "Lesson A")
        b = _add_lesson(store, "Lesson B")
        log = resolution_log()
        pair = detect_conflicts(store)[0]
        resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Deferred.",
            resolved_by="operator",
        )
        assert log.for_entry("some-other-entry-id") == []

    def test_all_returns_resolutions_in_insertion_order(self) -> None:
        store = preference_store()
        _add_lesson(store, "A1", scope=_scope(domain="approval"))
        _add_lesson(store, "A2", scope=_scope(domain="approval"))
        _add_lesson(store, "G1", scope=_scope(domain="gate"))
        _add_lesson(store, "G2", scope=_scope(domain="gate"))
        log = resolution_log()
        pairs = detect_conflicts(store)
        resolutions = []
        for pair in pairs:
            r = resolve_conflict(
                log=log, store=store, conflict=pair,
                kind=ResolutionKind.DEFER,
                rationale="Deferred.",
                resolved_by="operator",
            )
            resolutions.append(r)
        assert log.all() == resolutions

    def test_resolution_is_immutable(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.DEFER,
            rationale="Deferred.",
            resolved_by="operator",
        )
        with pytest.raises(Exception):
            resolution.rationale = "mutated"  # type: ignore[misc]

    def test_to_dict_is_json_serializable(self) -> None:
        store, a, b = _store_with_conflict()
        pair = detect_conflicts(store)[0]
        log = resolution_log()
        resolution = resolve_conflict(
            log=log, store=store, conflict=pair,
            kind=ResolutionKind.ACCEPT,
            accepted_entry_id=a.entry_id,
            rationale="Accepted.",
            resolved_by="operator",
        )
        d = resolution.to_dict()
        assert d["kind"] == "accept"
        assert d["accepted_entry_id"] == a.entry_id
        assert d["resolution_id"].startswith("resolution-")
        assert "conflict" in d
        assert "rationale" in d
        assert "resolved_by" in d
        assert "resolved_at" in d
