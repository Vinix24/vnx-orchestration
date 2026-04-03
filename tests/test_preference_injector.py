#!/usr/bin/env python3
"""Tests for preference injection into dispatch context.

Covers:
  - InjectionContext frozen dataclass and to_dict()
  - PreferenceInjector.inject_for_dispatch() — profile scoping, cross-profile
    exclusion, retired entry exclusion, multi-domain, include_lessons flag
  - PreferenceInjector.format_injection_summary() — output format
  - assert_injection_bounded() — safety guard
  - Unknown profile raises ValueError
  - bounded is always True
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from preference_store import (
    EntryKind,
    PreferenceEntry,
    PreferenceStore,
    ScopeKey,
    preference_store,
    record_entry,
)
from preference_injector import (
    InjectionContext,
    PreferenceInjector,
    assert_injection_bounded,
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
# InjectionContext — frozen dataclass
# ---------------------------------------------------------------------------

class TestInjectionContext:
    def test_basic_construction(self):
        entry = _make_entry()
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(ScopeKey("coding_strict", "approval"),),
            injected_entries=(entry,),
            injection_count=1,
            bounded=True,
        )
        assert ctx.dispatch_id == "d-001"
        assert ctx.profile == "coding_strict"
        assert ctx.injection_count == 1
        assert ctx.bounded is True

    def test_frozen_immutable(self):
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(),
            injected_entries=(),
            injection_count=0,
            bounded=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            ctx.bounded = False  # type: ignore[misc]

    def test_to_dict_structure(self):
        entry = _make_entry()
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(ScopeKey("coding_strict", "approval"),),
            injected_entries=(entry,),
            injection_count=1,
            bounded=True,
        )
        d = ctx.to_dict()
        assert d["dispatch_id"] == "d-001"
        assert d["profile"] == "coding_strict"
        assert d["injection_count"] == 1
        assert d["bounded"] is True
        assert isinstance(d["scope_keys"], list)
        assert isinstance(d["injected_entries"], list)

    def test_to_dict_scope_keys_format(self):
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(ScopeKey("coding_strict", "approval"),),
            injected_entries=(),
            injection_count=0,
            bounded=True,
        )
        d = ctx.to_dict()
        assert d["scope_keys"] == [{"profile": "coding_strict", "domain": "approval"}]

    def test_to_dict_empty_injection(self):
        ctx = InjectionContext(
            dispatch_id="d-empty",
            profile="business_light",
            scope_keys=(),
            injected_entries=(),
            injection_count=0,
            bounded=True,
        )
        d = ctx.to_dict()
        assert d["injection_count"] == 0
        assert d["injected_entries"] == []

    def test_to_dict_injected_entries_are_dicts(self):
        entry = _make_entry()
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(ScopeKey("coding_strict", "approval"),),
            injected_entries=(entry,),
            injection_count=1,
            bounded=True,
        )
        d = ctx.to_dict()
        assert isinstance(d["injected_entries"][0], dict)
        assert "entry_id" in d["injected_entries"][0]

    def test_to_dict_is_json_serializable(self):
        import json
        entry = _make_entry()
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(ScopeKey("coding_strict", "approval"),),
            injected_entries=(entry,),
            injection_count=1,
            bounded=True,
        )
        # Should not raise
        json.dumps(ctx.to_dict())


# ---------------------------------------------------------------------------
# PreferenceInjector — inject_for_dispatch()
# ---------------------------------------------------------------------------

class TestInjectForDispatch:
    def test_returns_matching_profile_entry(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        assert ctx.injection_count == 1
        assert ctx.injected_entries[0].entry_id == "pref-001"

    def test_cross_profile_entry_excluded(self):
        e_cs = _make_entry(entry_id="pref-cs", profile="coding_strict", domain="approval")
        e_rs = _make_entry(entry_id="pref-rs", profile="regulated_strict", domain="approval")
        store = _store_with_entries(e_cs, e_rs)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        ids = [e.entry_id for e in ctx.injected_entries]
        assert "pref-cs" in ids
        assert "pref-rs" not in ids

    def test_all_three_profiles_isolated(self):
        e_cs = _make_entry(entry_id="pref-cs", profile="coding_strict", domain="gate")
        e_rs = _make_entry(entry_id="pref-rs", profile="regulated_strict", domain="gate")
        e_bl = _make_entry(entry_id="pref-bl", profile="business_light", domain="gate")
        store = _store_with_entries(e_cs, e_rs, e_bl)
        injector = PreferenceInjector(store)

        ctx_cs = injector.inject_for_dispatch("d-cs", "coding_strict", ["gate"])
        ctx_rs = injector.inject_for_dispatch("d-rs", "regulated_strict", ["gate"])
        ctx_bl = injector.inject_for_dispatch("d-bl", "business_light", ["gate"])

        assert ctx_cs.injection_count == 1
        assert ctx_cs.injected_entries[0].entry_id == "pref-cs"
        assert ctx_rs.injection_count == 1
        assert ctx_rs.injected_entries[0].entry_id == "pref-rs"
        assert ctx_bl.injection_count == 1
        assert ctx_bl.injected_entries[0].entry_id == "pref-bl"

    def test_retired_entry_excluded(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = preference_store()
        store.add(e)
        store.retire("pref-001")
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        assert ctx.injection_count == 0

    def test_active_entry_included_retired_excluded(self):
        e_active = _make_entry(entry_id="pref-active", profile="coding_strict", domain="approval")
        e_retired = _make_entry(entry_id="pref-retired", profile="coding_strict", domain="approval")
        store = preference_store()
        store.add(e_active)
        store.add(e_retired)
        store.retire("pref-retired")
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        assert ctx.injection_count == 1
        assert ctx.injected_entries[0].entry_id == "pref-active"

    def test_bounded_always_true(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        assert ctx.bounded is True

    def test_bounded_true_with_entries(self):
        e = _make_entry(entry_id="pref-001", profile="regulated_strict", domain="gate")
        store = _store_with_entries(e)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "regulated_strict", ["gate"])
        assert ctx.bounded is True

    def test_unknown_profile_raises_value_error(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        with pytest.raises(ValueError, match="Unknown governance profile"):
            injector.inject_for_dispatch("d-001", "fantasy_profile", ["approval"])

    def test_unknown_profile_error_mentions_profile(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        with pytest.raises(ValueError, match="fantasy_profile"):
            injector.inject_for_dispatch("d-001", "fantasy_profile", ["approval"])

    def test_multi_domain_returns_entries_from_all_domains(self):
        e_approval = _make_entry(
            entry_id="pref-approval",
            profile="regulated_strict",
            domain="approval",
        )
        e_gate = _make_entry(
            entry_id="pref-gate",
            profile="regulated_strict",
            domain="gate",
        )
        store = _store_with_entries(e_approval, e_gate)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "regulated_strict", ["approval", "gate"]
        )
        ids = [e.entry_id for e in ctx.injected_entries]
        assert "pref-approval" in ids
        assert "pref-gate" in ids
        assert ctx.injection_count == 2

    def test_multi_domain_no_duplication(self):
        # Entry in approval only — gate domain query returns nothing
        e = _make_entry(
            entry_id="pref-approval",
            profile="regulated_strict",
            domain="approval",
        )
        store = _store_with_entries(e)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "regulated_strict", ["approval", "gate"]
        )
        assert ctx.injection_count == 1

    def test_empty_domains_returns_empty(self):
        e = _make_entry(profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", [])
        assert ctx.injection_count == 0

    def test_scope_keys_match_requested_domains(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "coding_strict", ["approval", "gate"]
        )
        domains_in_keys = {sk.domain for sk in ctx.scope_keys}
        assert domains_in_keys == {"approval", "gate"}

    def test_scope_keys_all_use_requested_profile(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "regulated_strict", ["approval", "gate", "dispatch"]
        )
        for sk in ctx.scope_keys:
            assert sk.profile == "regulated_strict"

    def test_dispatch_id_preserved_in_context(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("dispatch-xyz-789", "business_light", [])
        assert ctx.dispatch_id == "dispatch-xyz-789"

    def test_profile_preserved_in_context(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "business_light", [])
        assert ctx.profile == "business_light"

    def test_include_lessons_true_returns_lessons(self):
        e_lesson = _make_entry(
            entry_id="lesson-001",
            profile="coding_strict",
            domain="approval",
            kind=EntryKind.LESSON,
            content="Gate timeouts correlate with provider saturation.",
        )
        store = _store_with_entries(e_lesson)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "coding_strict", ["approval"], include_lessons=True
        )
        assert ctx.injection_count == 1
        assert ctx.injected_entries[0].entry_id == "lesson-001"

    def test_include_lessons_false_excludes_lessons(self):
        e_lesson = _make_entry(
            entry_id="lesson-001",
            profile="coding_strict",
            domain="approval",
            kind=EntryKind.LESSON,
            content="Gate timeouts correlate with provider saturation.",
        )
        e_pref = _make_entry(
            entry_id="pref-001",
            profile="coding_strict",
            domain="approval",
            kind=EntryKind.PREFERENCE,
        )
        store = _store_with_entries(e_lesson, e_pref)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "coding_strict", ["approval"], include_lessons=False
        )
        ids = [e.entry_id for e in ctx.injected_entries]
        assert "lesson-001" not in ids
        assert "pref-001" in ids
        assert ctx.injection_count == 1

    def test_include_lessons_default_is_true(self):
        e_lesson = _make_entry(
            entry_id="lesson-001",
            profile="coding_strict",
            domain="approval",
            kind=EntryKind.LESSON,
            content="Some lesson.",
        )
        store = _store_with_entries(e_lesson)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        assert ctx.injection_count == 1

    def test_multiple_entries_same_domain(self):
        e1 = _make_entry(entry_id="pref-001", profile="regulated_strict", domain="gate")
        e2 = _make_entry(entry_id="pref-002", profile="regulated_strict", domain="gate")
        e3 = _make_entry(entry_id="pref-003", profile="regulated_strict", domain="gate")
        store = _store_with_entries(e1, e2, e3)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "regulated_strict", ["gate"])
        assert ctx.injection_count == 3

    def test_injection_count_equals_len_injected_entries(self):
        e1 = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        e2 = _make_entry(entry_id="pref-002", profile="coding_strict", domain="gate")
        store = _store_with_entries(e1, e2)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch(
            "d-001", "coding_strict", ["approval", "gate"]
        )
        assert ctx.injection_count == len(ctx.injected_entries)

    def test_empty_store_returns_empty_context(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "regulated_strict", ["approval"])
        assert ctx.injection_count == 0
        assert ctx.injected_entries == ()
        assert ctx.bounded is True

    def test_regulated_strict_profile_accepted(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "regulated_strict", [])
        assert ctx.profile == "regulated_strict"

    def test_business_light_profile_accepted(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "business_light", [])
        assert ctx.profile == "business_light"

    def test_injected_entries_are_preference_entry_instances(self):
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store = _store_with_entries(e)
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        for entry in ctx.injected_entries:
            assert isinstance(entry, PreferenceEntry)


# ---------------------------------------------------------------------------
# PreferenceInjector — format_injection_summary()
# ---------------------------------------------------------------------------

class TestFormatInjectionSummary:
    def _make_ctx(
        self,
        dispatch_id="d-001",
        profile="regulated_strict",
        domains=None,
        injection_count=3,
        bounded=True,
    ):
        if domains is None:
            domains = ["approval", "gate"]
        scope_keys = tuple(ScopeKey(profile=profile, domain=d) for d in domains)
        return InjectionContext(
            dispatch_id=dispatch_id,
            profile=profile,
            scope_keys=scope_keys,
            injected_entries=(),
            injection_count=injection_count,
            bounded=bounded,
        )

    def test_includes_dispatch_id(self):
        ctx = self._make_ctx(dispatch_id="d-007")
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "d-007" in summary

    def test_includes_profile(self):
        ctx = self._make_ctx(profile="regulated_strict")
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "regulated_strict" in summary

    def test_includes_entry_count(self):
        ctx = self._make_ctx(injection_count=5)
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "entries=5" in summary

    def test_includes_bounded(self):
        ctx = self._make_ctx(bounded=True)
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "bounded=True" in summary

    def test_includes_inject_prefix(self):
        ctx = self._make_ctx()
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert summary.startswith("[inject]")

    def test_includes_domains(self):
        ctx = self._make_ctx(domains=["approval", "gate"])
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "approval" in summary
        assert "gate" in summary

    def test_zero_entries(self):
        ctx = self._make_ctx(injection_count=0, domains=[])
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "entries=0" in summary

    def test_returns_single_line(self):
        ctx = self._make_ctx()
        injector = PreferenceInjector(preference_store())
        summary = injector.format_injection_summary(ctx)
        assert "\n" not in summary


# ---------------------------------------------------------------------------
# assert_injection_bounded()
# ---------------------------------------------------------------------------

class TestAssertInjectionBounded:
    def test_bounded_true_does_not_raise(self):
        ctx = InjectionContext(
            dispatch_id="d-001",
            profile="coding_strict",
            scope_keys=(),
            injected_entries=(),
            injection_count=0,
            bounded=True,
        )
        assert_injection_bounded(ctx)  # must not raise

    def test_bounded_false_raises_value_error(self):
        # Create via object.__setattr__ to bypass frozen enforcement
        ctx = object.__new__(InjectionContext)
        object.__setattr__(ctx, "dispatch_id", "d-001")
        object.__setattr__(ctx, "profile", "coding_strict")
        object.__setattr__(ctx, "scope_keys", ())
        object.__setattr__(ctx, "injected_entries", ())
        object.__setattr__(ctx, "injection_count", 0)
        object.__setattr__(ctx, "bounded", False)
        with pytest.raises(ValueError):
            assert_injection_bounded(ctx)

    def test_bounded_false_error_mentions_dispatch_id(self):
        ctx = object.__new__(InjectionContext)
        object.__setattr__(ctx, "dispatch_id", "d-suspect")
        object.__setattr__(ctx, "profile", "coding_strict")
        object.__setattr__(ctx, "scope_keys", ())
        object.__setattr__(ctx, "injected_entries", ())
        object.__setattr__(ctx, "injection_count", 0)
        object.__setattr__(ctx, "bounded", False)
        with pytest.raises(ValueError, match="d-suspect"):
            assert_injection_bounded(ctx)

    def test_bounded_false_error_mentions_profile(self):
        ctx = object.__new__(InjectionContext)
        object.__setattr__(ctx, "dispatch_id", "d-001")
        object.__setattr__(ctx, "profile", "regulated_strict")
        object.__setattr__(ctx, "scope_keys", ())
        object.__setattr__(ctx, "injected_entries", ())
        object.__setattr__(ctx, "injection_count", 0)
        object.__setattr__(ctx, "bounded", False)
        with pytest.raises(ValueError, match="regulated_strict"):
            assert_injection_bounded(ctx)

    def test_inject_for_dispatch_result_always_passes_guard(self):
        store = preference_store()
        injector = PreferenceInjector(store)
        ctx = injector.inject_for_dispatch("d-001", "coding_strict", ["approval"])
        # Should never raise — inject_for_dispatch always sets bounded=True
        assert_injection_bounded(ctx)
