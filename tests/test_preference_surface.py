#!/usr/bin/env python3
"""Tests for the operator dashboard preference surface.

Covers:
  - ProfileSurface frozen dataclass and to_dict()
  - build_profile_surface() — correct counts, retired handling, unknown profile
  - format_surface_line() — output format
  - build_all_surfaces() — all KNOWN_PROFILES covered
  - Store is not mutated by surface builds
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from preference_store import (
    KNOWN_PROFILES,
    EntryKind,
    PreferenceEntry,
    PreferenceStore,
    ScopeKey,
    preference_store,
    record_entry,
)
from preference_surface import (
    ProfileSurface,
    build_all_surfaces,
    build_profile_surface,
    format_surface_line,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entry(
    entry_id: str = "pref-00000000-0000-0000-0000-000000000001",
    profile: str = "coding_strict",
    domain: str = "approval",
    kind: EntryKind = EntryKind.PREFERENCE,
    content: str = "Require dual-approval for production.",
    evidence_refs: tuple = ("dispatch-001",),
    recorded_at: str = "2026-04-03T10:00:00.000000Z",
    recorded_by: str = "operator:alice",
    retired: bool = False,
    retired_at=None,
) -> PreferenceEntry:
    return PreferenceEntry(
        entry_id=entry_id,
        scope=ScopeKey(profile=profile, domain=domain),
        kind=kind,
        content=content,
        evidence_refs=evidence_refs,
        recorded_at=recorded_at,
        recorded_by=recorded_by,
        retired=retired,
        retired_at=retired_at,
    )


def _store_with_entries(*entries: PreferenceEntry) -> PreferenceStore:
    store = preference_store()
    for e in entries:
        store.add(e)
    return store


# ---------------------------------------------------------------------------
# ProfileSurface — frozen dataclass
# ---------------------------------------------------------------------------

class TestProfileSurface:
    def test_basic_construction(self):
        surface = ProfileSurface(
            profile="coding_strict",
            total_entries=5,
            active_entries=4,
            retired_entries=1,
            lesson_count=2,
            preference_count=2,
            domains=("approval", "gate"),
        )
        assert surface.profile == "coding_strict"
        assert surface.total_entries == 5
        assert surface.active_entries == 4
        assert surface.retired_entries == 1
        assert surface.lesson_count == 2
        assert surface.preference_count == 2
        assert surface.domains == ("approval", "gate")

    def test_frozen_immutable(self):
        surface = ProfileSurface(
            profile="coding_strict",
            total_entries=0,
            active_entries=0,
            retired_entries=0,
            lesson_count=0,
            preference_count=0,
            domains=(),
        )
        with pytest.raises((AttributeError, TypeError)):
            surface.profile = "changed"  # type: ignore[misc]

    def test_to_dict_structure(self):
        surface = ProfileSurface(
            profile="regulated_strict",
            total_entries=6,
            active_entries=5,
            retired_entries=1,
            lesson_count=2,
            preference_count=3,
            domains=("approval", "gate"),
        )
        d = surface.to_dict()
        assert d["profile"] == "regulated_strict"
        assert d["total_entries"] == 6
        assert d["active_entries"] == 5
        assert d["retired_entries"] == 1
        assert d["lesson_count"] == 2
        assert d["preference_count"] == 3
        assert d["domains"] == ["approval", "gate"]

    def test_to_dict_domains_is_list(self):
        surface = ProfileSurface(
            profile="coding_strict",
            total_entries=0,
            active_entries=0,
            retired_entries=0,
            lesson_count=0,
            preference_count=0,
            domains=("approval",),
        )
        d = surface.to_dict()
        assert isinstance(d["domains"], list)

    def test_to_dict_is_json_serializable(self):
        import json
        surface = ProfileSurface(
            profile="business_light",
            total_entries=1,
            active_entries=1,
            retired_entries=0,
            lesson_count=0,
            preference_count=1,
            domains=("dispatch",),
        )
        json.dumps(surface.to_dict())  # must not raise


# ---------------------------------------------------------------------------
# build_profile_surface()
# ---------------------------------------------------------------------------

class TestBuildProfileSurface:
    def test_empty_store_returns_zero_counts(self):
        store = preference_store()
        surface = build_profile_surface(store, "coding_strict")
        assert surface.total_entries == 0
        assert surface.active_entries == 0
        assert surface.retired_entries == 0
        assert surface.lesson_count == 0
        assert surface.preference_count == 0
        assert surface.domains == ()

    def test_single_active_preference(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        surface = build_profile_surface(store, "coding_strict")
        assert surface.total_entries == 1
        assert surface.active_entries == 1
        assert surface.retired_entries == 0
        assert surface.preference_count == 1
        assert surface.lesson_count == 0

    def test_single_active_lesson(self):
        e = _make_entry(
            entry_id="lesson-001",
            profile="coding_strict",
            domain="gate",
            kind=EntryKind.LESSON,
            content="Gate lesson.",
        )
        store = _store_with_entries(e)
        surface = build_profile_surface(store, "coding_strict")
        assert surface.lesson_count == 1
        assert surface.preference_count == 0

    def test_retired_counted_in_total_not_active(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = preference_store()
        store.add(e)
        store.retire("pref-001")
        surface = build_profile_surface(store, "coding_strict")
        assert surface.total_entries == 1
        assert surface.active_entries == 0
        assert surface.retired_entries == 1

    def test_mix_active_and_retired(self):
        e_active = _make_entry(
            entry_id="pref-active",
            profile="regulated_strict",
            domain="approval",
        )
        e_retired = _make_entry(
            entry_id="pref-retired",
            profile="regulated_strict",
            domain="approval",
        )
        store = preference_store()
        store.add(e_active)
        store.add(e_retired)
        store.retire("pref-retired")
        surface = build_profile_surface(store, "regulated_strict")
        assert surface.total_entries == 2
        assert surface.active_entries == 1
        assert surface.retired_entries == 1

    def test_domains_sorted(self):
        e1 = _make_entry(entry_id="pref-001", profile="coding_strict", domain="gate")
        e2 = _make_entry(entry_id="pref-002", profile="coding_strict", domain="approval")
        e3 = _make_entry(entry_id="pref-003", profile="coding_strict", domain="dispatch")
        store = _store_with_entries(e1, e2, e3)
        surface = build_profile_surface(store, "coding_strict")
        assert surface.domains == ("approval", "dispatch", "gate")

    def test_domains_distinct(self):
        e1 = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        e2 = _make_entry(entry_id="pref-002", profile="coding_strict", domain="approval")
        store = _store_with_entries(e1, e2)
        surface = build_profile_surface(store, "coding_strict")
        assert surface.domains == ("approval",)

    def test_domains_include_retired_entry_domain(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = preference_store()
        store.add(e)
        store.retire("pref-001")
        surface = build_profile_surface(store, "coding_strict")
        assert "approval" in surface.domains

    def test_cross_profile_entries_not_counted(self):
        e_cs = _make_entry(entry_id="pref-cs", profile="coding_strict", domain="approval")
        e_rs = _make_entry(entry_id="pref-rs", profile="regulated_strict", domain="approval")
        store = _store_with_entries(e_cs, e_rs)
        surface = build_profile_surface(store, "coding_strict")
        assert surface.total_entries == 1
        assert surface.active_entries == 1

    def test_unknown_profile_raises_value_error(self):
        store = preference_store()
        with pytest.raises(ValueError, match="Unknown governance profile"):
            build_profile_surface(store, "fantasy_profile")

    def test_unknown_profile_error_mentions_profile(self):
        store = preference_store()
        with pytest.raises(ValueError, match="fantasy_profile"):
            build_profile_surface(store, "fantasy_profile")

    def test_profile_field_matches_requested(self):
        store = preference_store()
        surface = build_profile_surface(store, "business_light")
        assert surface.profile == "business_light"

    def test_regulated_strict_accepted(self):
        store = preference_store()
        surface = build_profile_surface(store, "regulated_strict")
        assert surface.profile == "regulated_strict"

    def test_multiple_domains_all_counted(self):
        e1 = _make_entry(entry_id="pref-001", profile="regulated_strict", domain="approval")
        e2 = _make_entry(entry_id="pref-002", profile="regulated_strict", domain="gate")
        e3 = _make_entry(entry_id="pref-003", profile="regulated_strict", domain="dispatch")
        store = _store_with_entries(e1, e2, e3)
        surface = build_profile_surface(store, "regulated_strict")
        assert surface.total_entries == 3
        assert surface.active_entries == 3
        assert len(surface.domains) == 3

    def test_lesson_count_only_counts_active_lessons(self):
        e_lesson_active = _make_entry(
            entry_id="lesson-active",
            profile="coding_strict",
            domain="gate",
            kind=EntryKind.LESSON,
            content="Active lesson.",
        )
        e_lesson_retired = _make_entry(
            entry_id="lesson-retired",
            profile="coding_strict",
            domain="gate",
            kind=EntryKind.LESSON,
            content="Retired lesson.",
        )
        store = preference_store()
        store.add(e_lesson_active)
        store.add(e_lesson_retired)
        store.retire("lesson-retired")
        surface = build_profile_surface(store, "coding_strict")
        assert surface.lesson_count == 1
        assert surface.total_entries == 2

    def test_preference_count_only_counts_active_preferences(self):
        e_pref_active = _make_entry(
            entry_id="pref-active",
            profile="coding_strict",
            domain="approval",
            kind=EntryKind.PREFERENCE,
        )
        e_pref_retired = _make_entry(
            entry_id="pref-retired",
            profile="coding_strict",
            domain="approval",
            kind=EntryKind.PREFERENCE,
        )
        store = preference_store()
        store.add(e_pref_active)
        store.add(e_pref_retired)
        store.retire("pref-retired")
        surface = build_profile_surface(store, "coding_strict")
        assert surface.preference_count == 1
        assert surface.total_entries == 2


# ---------------------------------------------------------------------------
# Store not mutated by build_profile_surface
# ---------------------------------------------------------------------------

class TestSurfaceDoesNotMutateStore:
    def test_entry_count_unchanged_after_surface_build(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        count_before = store.entry_count(include_retired=True)
        build_profile_surface(store, "coding_strict")
        assert store.entry_count(include_retired=True) == count_before

    def test_retired_state_unchanged_after_surface_build(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = preference_store()
        store.add(e)
        build_profile_surface(store, "coding_strict")
        fetched = store.get("pref-001")
        assert fetched is not None
        assert fetched.retired is False

    def test_all_scopes_unchanged_after_surface_build(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        scopes_before = store.all_scopes()
        build_profile_surface(store, "coding_strict")
        assert store.all_scopes() == scopes_before

    def test_build_all_surfaces_does_not_mutate_store(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        count_before = store.entry_count(include_retired=True)
        build_all_surfaces(store)
        assert store.entry_count(include_retired=True) == count_before


# ---------------------------------------------------------------------------
# format_surface_line()
# ---------------------------------------------------------------------------

class TestFormatSurfaceLine:
    def _make_surface(
        self,
        profile="regulated_strict",
        total_entries=6,
        active_entries=5,
        retired_entries=1,
        lesson_count=2,
        preference_count=3,
        domains=("approval", "gate"),
    ):
        return ProfileSurface(
            profile=profile,
            total_entries=total_entries,
            active_entries=active_entries,
            retired_entries=retired_entries,
            lesson_count=lesson_count,
            preference_count=preference_count,
            domains=domains,
        )

    def test_starts_with_surface_prefix(self):
        surface = self._make_surface()
        line = format_surface_line(surface)
        assert line.startswith("[surface]")

    def test_includes_profile(self):
        surface = self._make_surface(profile="regulated_strict")
        line = format_surface_line(surface)
        assert "regulated_strict" in line

    def test_includes_active_count(self):
        surface = self._make_surface(active_entries=5)
        line = format_surface_line(surface)
        assert "active=5" in line

    def test_includes_retired_count(self):
        surface = self._make_surface(retired_entries=1)
        line = format_surface_line(surface)
        assert "retired=1" in line

    def test_includes_lesson_count(self):
        surface = self._make_surface(lesson_count=2)
        line = format_surface_line(surface)
        assert "lessons=2" in line

    def test_includes_preference_count(self):
        surface = self._make_surface(preference_count=3)
        line = format_surface_line(surface)
        assert "prefs=3" in line

    def test_includes_domains(self):
        surface = self._make_surface(domains=("approval", "gate"))
        line = format_surface_line(surface)
        assert "approval" in line
        assert "gate" in line

    def test_includes_domains_label(self):
        surface = self._make_surface()
        line = format_surface_line(surface)
        assert "domains=" in line

    def test_returns_single_line(self):
        surface = self._make_surface()
        line = format_surface_line(surface)
        assert "\n" not in line

    def test_empty_domains(self):
        surface = self._make_surface(domains=())
        line = format_surface_line(surface)
        assert "domains=" in line

    def test_zero_counts(self):
        surface = ProfileSurface(
            profile="business_light",
            total_entries=0,
            active_entries=0,
            retired_entries=0,
            lesson_count=0,
            preference_count=0,
            domains=(),
        )
        line = format_surface_line(surface)
        assert "active=0" in line
        assert "retired=0" in line
        assert "lessons=0" in line
        assert "prefs=0" in line


# ---------------------------------------------------------------------------
# build_all_surfaces()
# ---------------------------------------------------------------------------

class TestBuildAllSurfaces:
    def test_returns_all_known_profiles(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        assert set(surfaces.keys()) == KNOWN_PROFILES

    def test_regulated_strict_present(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        assert "regulated_strict" in surfaces

    def test_coding_strict_present(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        assert "coding_strict" in surfaces

    def test_business_light_present(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        assert "business_light" in surfaces

    def test_all_values_are_profile_surface_instances(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        for surface in surfaces.values():
            assert isinstance(surface, ProfileSurface)

    def test_profile_field_matches_key(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        for profile, surface in surfaces.items():
            assert surface.profile == profile

    def test_returns_dict(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        assert isinstance(surfaces, dict)

    def test_counts_reflect_store_contents(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        surfaces = build_all_surfaces(store)
        assert surfaces["coding_strict"].active_entries == 1
        assert surfaces["regulated_strict"].active_entries == 0
        assert surfaces["business_light"].active_entries == 0

    def test_empty_store_all_zero_counts(self):
        store = preference_store()
        surfaces = build_all_surfaces(store)
        for surface in surfaces.values():
            assert surface.total_entries == 0
            assert surface.active_entries == 0

    def test_retired_entries_counted_in_correct_profile(self):
        e = _make_entry(entry_id="pref-001", profile="regulated_strict", domain="gate")
        store = preference_store()
        store.add(e)
        store.retire("pref-001")
        surfaces = build_all_surfaces(store)
        assert surfaces["regulated_strict"].total_entries == 1
        assert surfaces["regulated_strict"].retired_entries == 1
        assert surfaces["coding_strict"].total_entries == 0
