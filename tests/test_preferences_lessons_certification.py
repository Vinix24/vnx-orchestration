#!/usr/bin/env python3
"""PR-4 certification for Feature 22: Preferences/Lessons Surface Generalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from preference_store import (
    EntryKind, KNOWN_PROFILES, PreferenceEntry, PreferenceStore,
    ScopeKey, assert_scope_not_contaminated, preference_store, record_entry,
)
from preference_injector import (
    InjectionContext, PreferenceInjector, assert_injection_bounded,
)
from preference_surface import (
    ProfileSurface, build_all_surfaces, build_profile_surface, format_surface_line,
)
from lesson_conflict import (
    ConflictPair, ResolutionKind, ResolutionLog,
    detect_conflicts, resolution_log, resolve_conflict,
)


def _store_with_entries() -> PreferenceStore:
    store = preference_store()
    record_entry(store, scope=ScopeKey("coding_strict", "quality"),
                 kind=EntryKind.PREFERENCE, content="Use pytest -q",
                 evidence_refs=("d-1",), recorded_by="operator")
    record_entry(store, scope=ScopeKey("coding_strict", "runtime"),
                 kind=EntryKind.LESSON, content="Codex timeouts at 600s",
                 evidence_refs=("sig-1", "sig-2"), recorded_by="T0")
    record_entry(store, scope=ScopeKey("business_light", "process"),
                 kind=EntryKind.PREFERENCE, content="Review contracts folder",
                 evidence_refs=("d-2",), recorded_by="operator")
    return store


class TestScopeIsolation:

    def test_cross_profile_query_empty(self) -> None:
        store = _store_with_entries()
        results = store.query(ScopeKey("business_light", "quality"))
        assert len(results) == 0

    def test_same_profile_query_returns_entries(self) -> None:
        store = _store_with_entries()
        results = store.query(ScopeKey("coding_strict", "quality"))
        assert len(results) >= 1

    def test_cross_profile_contamination(self) -> None:
        store = _store_with_entries()
        coding = store.query(ScopeKey("coding_strict", "quality"))
        business = store.query(ScopeKey("business_light", "process"))
        coding_ids = {e.entry_id for e in coding}
        business_ids = {e.entry_id for e in business}
        assert coding_ids.isdisjoint(business_ids)

    def test_assert_scope_not_contaminated_correct(self) -> None:
        store = preference_store()
        entry = record_entry(store, scope=ScopeKey("coding_strict", "test"),
                             kind=EntryKind.PREFERENCE, content="test",
                             evidence_refs=("d",), recorded_by="op")
        assert_scope_not_contaminated(entry, "coding_strict")

    def test_assert_scope_not_contaminated_wrong(self) -> None:
        store = preference_store()
        entry = record_entry(store, scope=ScopeKey("coding_strict", "test"),
                             kind=EntryKind.PREFERENCE, content="test",
                             evidence_refs=("d",), recorded_by="op")
        with pytest.raises(ValueError):
            assert_scope_not_contaminated(entry, "business_light")


class TestInjectionAndConflict:

    def test_injection_scoped(self) -> None:
        store = _store_with_entries()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-1", "coding_strict", ["quality", "runtime"])
        assert isinstance(ctx, InjectionContext)
        assert all(e.scope.profile == "coding_strict" for e in ctx.injected_entries)

    def test_injection_excludes_other_profiles(self) -> None:
        store = _store_with_entries()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-1", "coding_strict", ["quality", "runtime"])
        for entry in ctx.injected_entries:
            assert entry.scope.profile != "business_light"

    def test_injection_bounded(self) -> None:
        store = _store_with_entries()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-1", "coding_strict", ["quality"])
        assert ctx.bounded is True
        assert_injection_bounded(ctx)

    def test_injection_immutable(self) -> None:
        store = _store_with_entries()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-1", "coding_strict", ["quality"])
        with pytest.raises(AttributeError):
            ctx.dispatch_id = "hacked"  # type: ignore[misc]

    def test_conflict_detection(self) -> None:
        store = preference_store()
        record_entry(store, scope=ScopeKey("coding_strict", "runtime"),
                     kind=EntryKind.LESSON, content="lesson A",
                     evidence_refs=("s1",), recorded_by="T0")
        record_entry(store, scope=ScopeKey("coding_strict", "runtime"),
                     kind=EntryKind.LESSON, content="lesson B",
                     evidence_refs=("s2",), recorded_by="T0")
        conflicts = detect_conflicts(store)
        assert len(conflicts) >= 1

    def test_conflict_resolution_audited(self) -> None:
        store = preference_store()
        record_entry(store, scope=ScopeKey("coding_strict", "runtime"),
                     kind=EntryKind.LESSON, content="lesson A",
                     evidence_refs=("s1",), recorded_by="T0")
        record_entry(store, scope=ScopeKey("coding_strict", "runtime"),
                     kind=EntryKind.LESSON, content="lesson B",
                     evidence_refs=("s2",), recorded_by="T0")
        conflicts = detect_conflicts(store)
        assert len(conflicts) >= 1
        log = resolution_log()
        resolve_conflict(log, store, conflicts[0], kind=ResolutionKind.DEFER,
                         rationale="Needs more evidence", resolved_by="operator")
        assert log.count() == 1


class TestOperatorSurface:

    def test_surface_created(self) -> None:
        store = _store_with_entries()
        surface = build_profile_surface(store, "coding_strict")
        assert isinstance(surface, ProfileSurface)

    def test_all_surfaces(self) -> None:
        store = _store_with_entries()
        surfaces = build_all_surfaces(store)
        for profile in KNOWN_PROFILES:
            assert profile in surfaces

    def test_surface_line(self) -> None:
        store = _store_with_entries()
        surface = build_profile_surface(store, "coding_strict")
        line = format_surface_line(surface)
        assert "coding_strict" in line

    def test_surface_immutable(self) -> None:
        store = _store_with_entries()
        surface = build_profile_surface(store, "coding_strict")
        with pytest.raises(AttributeError):
            surface.profile = "hacked"  # type: ignore[misc]

    def test_retired_tracking(self) -> None:
        store = _store_with_entries()
        entries = store.query(ScopeKey("coding_strict", "quality"))
        store.retire(entries[0].entry_id)
        surface = build_profile_surface(store, "coding_strict")
        assert surface.retired_entries >= 1


class TestContractAlignment:

    def test_three_profiles(self) -> None:
        assert len(KNOWN_PROFILES) == 3

    def test_two_entry_kinds(self) -> None:
        assert EntryKind.PREFERENCE is not None
        assert EntryKind.LESSON is not None

    def test_three_resolution_kinds(self) -> None:
        assert len(list(ResolutionKind)) == 3

    def test_entry_immutable(self) -> None:
        store = preference_store()
        entry = record_entry(store, scope=ScopeKey("coding_strict", "test"),
                             kind=EntryKind.PREFERENCE, content="test",
                             evidence_refs=("d",), recorded_by="op")
        with pytest.raises(AttributeError):
            entry.content = "changed"  # type: ignore[misc]
