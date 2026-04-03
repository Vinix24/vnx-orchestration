#!/usr/bin/env python3
"""Tests for the scoped preference/lesson store with provenance tracking.

Covers:
  - EntryKind enum values
  - ScopeKey construction and equality
  - PreferenceEntry frozen dataclass and to_dict()
  - PreferenceStore.add() — basic add and duplicate detection
  - PreferenceStore.query() — scope filtering, kind filtering, retired exclusion
  - PreferenceStore.get() — retrieval by ID
  - PreferenceStore.retire() — mutation, timestamp, KeyError on missing
  - PreferenceStore.all_scopes() — distinct scopes in insertion order
  - PreferenceStore.entry_count() — with and without retired
  - record_entry() — validated factory, prefix format, profile validation
  - assert_scope_not_contaminated() — isolation guard
  - preference_store() — factory returns fresh empty store
  - Cross-profile isolation — query returns only matching profile
  - Evidence refs retained on entries
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
    assert_scope_not_contaminated,
    preference_store,
    record_entry,
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


# ---------------------------------------------------------------------------
# EntryKind
# ---------------------------------------------------------------------------

class TestEntryKind:
    def test_preference_value(self):
        assert EntryKind.PREFERENCE.value == "preference"

    def test_lesson_value(self):
        assert EntryKind.LESSON.value == "lesson"

    def test_two_members(self):
        assert set(EntryKind) == {EntryKind.PREFERENCE, EntryKind.LESSON}

    def test_from_value_preference(self):
        assert EntryKind("preference") == EntryKind.PREFERENCE

    def test_from_value_lesson(self):
        assert EntryKind("lesson") == EntryKind.LESSON


# ---------------------------------------------------------------------------
# ScopeKey
# ---------------------------------------------------------------------------

class TestScopeKey:
    def test_construction(self):
        sk = ScopeKey(profile="coding_strict", domain="approval")
        assert sk.profile == "coding_strict"
        assert sk.domain == "approval"

    def test_equality_same(self):
        a = ScopeKey(profile="coding_strict", domain="gate")
        b = ScopeKey(profile="coding_strict", domain="gate")
        assert a == b

    def test_inequality_different_profile(self):
        a = ScopeKey(profile="coding_strict", domain="gate")
        b = ScopeKey(profile="regulated_strict", domain="gate")
        assert a != b

    def test_inequality_different_domain(self):
        a = ScopeKey(profile="coding_strict", domain="approval")
        b = ScopeKey(profile="coding_strict", domain="dispatch")
        assert a != b

    def test_hashable(self):
        sk = ScopeKey(profile="business_light", domain="approval")
        s = {sk}
        assert sk in s

    def test_usable_as_dict_key(self):
        d = {ScopeKey("coding_strict", "gate"): "value"}
        assert d[ScopeKey("coding_strict", "gate")] == "value"


# ---------------------------------------------------------------------------
# PreferenceEntry
# ---------------------------------------------------------------------------

class TestPreferenceEntry:
    def test_basic_construction(self):
        entry = _make_entry()
        assert entry.entry_id == "pref-00000000-0000-0000-0000-000000000001"
        assert entry.scope == ScopeKey("coding_strict", "approval")
        assert entry.kind == EntryKind.PREFERENCE
        assert entry.content == "Require dual-approval for production."

    def test_default_retired_false(self):
        entry = _make_entry()
        assert entry.retired is False
        assert entry.retired_at is None

    def test_evidence_refs_preserved(self):
        refs = ("dispatch-001", "gate-review-007", "audit-123")
        entry = _make_entry(evidence_refs=refs)
        assert entry.evidence_refs == refs

    def test_frozen_immutable(self):
        entry = _make_entry()
        with pytest.raises((AttributeError, TypeError)):
            entry.content = "changed"  # type: ignore[misc]

    def test_to_dict_structure(self):
        entry = _make_entry(evidence_refs=("ev-1", "ev-2"))
        d = entry.to_dict()
        assert d["entry_id"] == "pref-00000000-0000-0000-0000-000000000001"
        assert d["scope"] == {"profile": "coding_strict", "domain": "approval"}
        assert d["kind"] == "preference"
        assert d["content"] == "Require dual-approval for production."
        assert d["evidence_refs"] == ["ev-1", "ev-2"]
        assert d["recorded_at"] == "2026-04-03T10:00:00.000000Z"
        assert d["recorded_by"] == "operator:alice"
        assert d["retired"] is False
        assert d["retired_at"] is None

    def test_to_dict_retired_entry(self):
        entry = _make_entry(
            retired=True,
            retired_at="2026-04-03T11:00:00.000000Z",
        )
        d = entry.to_dict()
        assert d["retired"] is True
        assert d["retired_at"] == "2026-04-03T11:00:00.000000Z"

    def test_to_dict_lesson_kind(self):
        entry = _make_entry(kind=EntryKind.LESSON)
        d = entry.to_dict()
        assert d["kind"] == "lesson"

    def test_to_dict_evidence_refs_is_list(self):
        entry = _make_entry(evidence_refs=("ev-only",))
        d = entry.to_dict()
        assert isinstance(d["evidence_refs"], list)


# ---------------------------------------------------------------------------
# PreferenceStore — add()
# ---------------------------------------------------------------------------

class TestPreferenceStoreAdd:
    def test_add_single_entry(self):
        store = preference_store()
        entry = _make_entry()
        store.add(entry)
        assert store.entry_count(include_retired=True) == 1

    def test_add_duplicate_raises_value_error(self):
        store = preference_store()
        entry = _make_entry()
        store.add(entry)
        with pytest.raises(ValueError, match="already exists"):
            store.add(entry)

    def test_add_different_ids_succeeds(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-aaa")
        e2 = _make_entry(entry_id="pref-bbb")
        store.add(e1)
        store.add(e2)
        assert store.entry_count(include_retired=True) == 2

    def test_add_multiple_scopes(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        e2 = _make_entry(entry_id="pref-002", profile="regulated_strict", domain="gate")
        store.add(e1)
        store.add(e2)
        assert store.entry_count(include_retired=True) == 2


# ---------------------------------------------------------------------------
# PreferenceStore — get()
# ---------------------------------------------------------------------------

class TestPreferenceStoreGet:
    def test_get_existing(self):
        store = preference_store()
        entry = _make_entry(entry_id="pref-xyz")
        store.add(entry)
        result = store.get("pref-xyz")
        assert result is entry

    def test_get_missing_returns_none(self):
        store = preference_store()
        assert store.get("pref-nonexistent") is None

    def test_get_after_retire(self):
        store = preference_store()
        entry = _make_entry(entry_id="pref-r1")
        store.add(entry)
        store.retire("pref-r1")
        result = store.get("pref-r1")
        assert result is not None
        assert result.retired is True


# ---------------------------------------------------------------------------
# PreferenceStore — query()
# ---------------------------------------------------------------------------

class TestPreferenceStoreQuery:
    def test_query_matching_scope(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store.add(e)
        results = store.query(ScopeKey("coding_strict", "approval"))
        assert len(results) == 1
        assert results[0].entry_id == "pref-001"

    def test_query_wrong_profile_returns_empty(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store.add(e)
        results = store.query(ScopeKey("regulated_strict", "approval"))
        assert results == []

    def test_query_wrong_domain_returns_empty(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        store.add(e)
        results = store.query(ScopeKey("coding_strict", "gate"))
        assert results == []

    def test_query_excludes_retired_by_default(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        store.retire("pref-001")
        results = store.query(ScopeKey("coding_strict", "approval"))
        assert results == []

    def test_query_includes_retired_when_requested(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        store.retire("pref-001")
        results = store.query(ScopeKey("coding_strict", "approval"), include_retired=True)
        assert len(results) == 1

    def test_query_kind_filter_preference(self):
        store = preference_store()
        ep = _make_entry(entry_id="pref-001", kind=EntryKind.PREFERENCE)
        el = _make_entry(entry_id="lesson-001", kind=EntryKind.LESSON)
        store.add(ep)
        store.add(el)
        results = store.query(ScopeKey("coding_strict", "approval"), kind=EntryKind.PREFERENCE)
        assert len(results) == 1
        assert results[0].kind == EntryKind.PREFERENCE

    def test_query_kind_filter_lesson(self):
        store = preference_store()
        ep = _make_entry(entry_id="pref-001", kind=EntryKind.PREFERENCE)
        el = _make_entry(entry_id="lesson-001", kind=EntryKind.LESSON)
        store.add(ep)
        store.add(el)
        results = store.query(ScopeKey("coding_strict", "approval"), kind=EntryKind.LESSON)
        assert len(results) == 1
        assert results[0].kind == EntryKind.LESSON

    def test_query_no_kind_filter_returns_all(self):
        store = preference_store()
        ep = _make_entry(entry_id="pref-001", kind=EntryKind.PREFERENCE)
        el = _make_entry(entry_id="lesson-001", kind=EntryKind.LESSON)
        store.add(ep)
        store.add(el)
        results = store.query(ScopeKey("coding_strict", "approval"))
        assert len(results) == 2

    def test_cross_profile_isolation(self):
        store = preference_store()
        e_cs = _make_entry(entry_id="pref-cs", profile="coding_strict", domain="gate")
        e_rs = _make_entry(entry_id="pref-rs", profile="regulated_strict", domain="gate")
        e_bl = _make_entry(entry_id="pref-bl", profile="business_light", domain="gate")
        store.add(e_cs)
        store.add(e_rs)
        store.add(e_bl)

        cs_results = store.query(ScopeKey("coding_strict", "gate"))
        rs_results = store.query(ScopeKey("regulated_strict", "gate"))
        bl_results = store.query(ScopeKey("business_light", "gate"))

        assert len(cs_results) == 1 and cs_results[0].entry_id == "pref-cs"
        assert len(rs_results) == 1 and rs_results[0].entry_id == "pref-rs"
        assert len(bl_results) == 1 and bl_results[0].entry_id == "pref-bl"

    def test_query_empty_store(self):
        store = preference_store()
        results = store.query(ScopeKey("coding_strict", "approval"))
        assert results == []


# ---------------------------------------------------------------------------
# PreferenceStore — retire()
# ---------------------------------------------------------------------------

class TestPreferenceStoreRetire:
    def test_retire_marks_entry(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        retired = store.retire("pref-001")
        assert retired.retired is True

    def test_retire_sets_retired_at(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        retired = store.retire("pref-001")
        assert retired.retired_at is not None

    def test_retire_custom_timestamp(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        ts = "2026-04-03T15:00:00.000000Z"
        retired = store.retire("pref-001", retired_at=ts)
        assert retired.retired_at == ts

    def test_retire_preserves_other_fields(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001", content="Keep this content.")
        store.add(e)
        retired = store.retire("pref-001")
        assert retired.content == "Keep this content."
        assert retired.entry_id == "pref-001"

    def test_retire_returns_frozen_entry(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        retired = store.retire("pref-001")
        with pytest.raises((AttributeError, TypeError)):
            retired.retired = False  # type: ignore[misc]

    def test_retire_missing_raises_key_error(self):
        store = preference_store()
        with pytest.raises(KeyError):
            store.retire("pref-nonexistent")

    def test_retire_entry_persisted_in_store(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        store.retire("pref-001")
        fetched = store.get("pref-001")
        assert fetched is not None
        assert fetched.retired is True


# ---------------------------------------------------------------------------
# PreferenceStore — all_scopes()
# ---------------------------------------------------------------------------

class TestPreferenceStoreAllScopes:
    def test_empty_store_returns_empty(self):
        store = preference_store()
        assert store.all_scopes() == []

    def test_single_scope(self):
        store = preference_store()
        e = _make_entry(profile="coding_strict", domain="approval")
        store.add(e)
        scopes = store.all_scopes()
        assert scopes == [ScopeKey("coding_strict", "approval")]

    def test_multiple_distinct_scopes(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        e2 = _make_entry(entry_id="pref-002", profile="regulated_strict", domain="gate")
        e3 = _make_entry(entry_id="pref-003", profile="business_light", domain="dispatch")
        store.add(e1)
        store.add(e2)
        store.add(e3)
        scopes = store.all_scopes()
        assert len(scopes) == 3

    def test_duplicate_scope_appears_once(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-001", profile="coding_strict", domain="approval")
        e2 = _make_entry(entry_id="pref-002", profile="coding_strict", domain="approval")
        store.add(e1)
        store.add(e2)
        scopes = store.all_scopes()
        assert len(scopes) == 1
        assert scopes[0] == ScopeKey("coding_strict", "approval")

    def test_includes_retired_entry_scope(self):
        store = preference_store()
        e = _make_entry(entry_id="pref-001")
        store.add(e)
        store.retire("pref-001")
        scopes = store.all_scopes()
        assert ScopeKey("coding_strict", "approval") in scopes


# ---------------------------------------------------------------------------
# PreferenceStore — entry_count()
# ---------------------------------------------------------------------------

class TestPreferenceStoreEntryCount:
    def test_empty_store(self):
        store = preference_store()
        assert store.entry_count() == 0
        assert store.entry_count(include_retired=True) == 0

    def test_count_active_only(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-001")
        e2 = _make_entry(entry_id="pref-002")
        store.add(e1)
        store.add(e2)
        assert store.entry_count() == 2

    def test_count_excludes_retired_by_default(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-001")
        e2 = _make_entry(entry_id="pref-002")
        store.add(e1)
        store.add(e2)
        store.retire("pref-001")
        assert store.entry_count() == 1

    def test_count_includes_retired_when_requested(self):
        store = preference_store()
        e1 = _make_entry(entry_id="pref-001")
        e2 = _make_entry(entry_id="pref-002")
        store.add(e1)
        store.add(e2)
        store.retire("pref-001")
        assert store.entry_count(include_retired=True) == 2


# ---------------------------------------------------------------------------
# record_entry()
# ---------------------------------------------------------------------------

class TestRecordEntry:
    def test_basic_record_preference(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("coding_strict", "approval"),
            kind=EntryKind.PREFERENCE,
            content="Require dual-approval.",
            evidence_refs=("dispatch-001",),
            recorded_by="operator:alice",
        )
        assert entry.kind == EntryKind.PREFERENCE
        assert entry.content == "Require dual-approval."

    def test_basic_record_lesson(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("coding_strict", "gate"),
            kind=EntryKind.LESSON,
            content="Gate timeouts above 30s correlate with provider saturation.",
            evidence_refs=("gate-ev-001",),
            recorded_by="agent:t0",
        )
        assert entry.kind == EntryKind.LESSON

    def test_entry_id_prefix_preference(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("coding_strict", "approval"),
            kind=EntryKind.PREFERENCE,
            content="Some preference.",
            evidence_refs=("ev-001",),
            recorded_by="operator:alice",
        )
        assert entry.entry_id.startswith("pref-")

    def test_entry_id_prefix_lesson(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("coding_strict", "approval"),
            kind=EntryKind.LESSON,
            content="Some lesson.",
            evidence_refs=("ev-001",),
            recorded_by="agent:t0",
        )
        assert entry.entry_id.startswith("lesson-")

    def test_entry_added_to_store(self):
        store = preference_store()
        record_entry(
            store,
            scope=ScopeKey("coding_strict", "approval"),
            kind=EntryKind.PREFERENCE,
            content="Keep approvals strict.",
            evidence_refs=("ev-001",),
            recorded_by="operator:alice",
        )
        assert store.entry_count() == 1

    def test_evidence_refs_preserved(self):
        store = preference_store()
        refs = ("dispatch-001", "gate-002", "audit-003")
        entry = record_entry(
            store,
            scope=ScopeKey("regulated_strict", "dispatch"),
            kind=EntryKind.PREFERENCE,
            content="Evidence rich preference.",
            evidence_refs=refs,
            recorded_by="operator:bob",
        )
        assert entry.evidence_refs == refs

    def test_rejects_empty_content(self):
        store = preference_store()
        with pytest.raises(ValueError, match="content"):
            record_entry(
                store,
                scope=ScopeKey("coding_strict", "approval"),
                kind=EntryKind.PREFERENCE,
                content="",
                evidence_refs=("ev-001",),
                recorded_by="operator:alice",
            )

    def test_rejects_whitespace_only_content(self):
        store = preference_store()
        with pytest.raises(ValueError, match="content"):
            record_entry(
                store,
                scope=ScopeKey("coding_strict", "approval"),
                kind=EntryKind.PREFERENCE,
                content="   ",
                evidence_refs=("ev-001",),
                recorded_by="operator:alice",
            )

    def test_rejects_empty_recorded_by(self):
        store = preference_store()
        with pytest.raises(ValueError, match="recorded_by"):
            record_entry(
                store,
                scope=ScopeKey("coding_strict", "approval"),
                kind=EntryKind.PREFERENCE,
                content="Valid content.",
                evidence_refs=("ev-001",),
                recorded_by="",
            )

    def test_rejects_whitespace_only_recorded_by(self):
        store = preference_store()
        with pytest.raises(ValueError, match="recorded_by"):
            record_entry(
                store,
                scope=ScopeKey("coding_strict", "approval"),
                kind=EntryKind.PREFERENCE,
                content="Valid content.",
                evidence_refs=("ev-001",),
                recorded_by="   ",
            )

    def test_rejects_empty_evidence_refs(self):
        store = preference_store()
        with pytest.raises(ValueError, match="evidence_refs"):
            record_entry(
                store,
                scope=ScopeKey("coding_strict", "approval"),
                kind=EntryKind.PREFERENCE,
                content="Valid content.",
                evidence_refs=(),
                recorded_by="operator:alice",
            )

    def test_rejects_unknown_profile(self):
        store = preference_store()
        with pytest.raises(ValueError, match="Unknown governance profile"):
            record_entry(
                store,
                scope=ScopeKey("unknown_profile", "approval"),
                kind=EntryKind.PREFERENCE,
                content="Valid content.",
                evidence_refs=("ev-001",),
                recorded_by="operator:alice",
            )

    def test_accepts_regulated_strict_profile(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("regulated_strict", "gate"),
            kind=EntryKind.LESSON,
            content="Regulated gate lesson.",
            evidence_refs=("ev-001",),
            recorded_by="agent:t3",
        )
        assert entry.scope.profile == "regulated_strict"

    def test_accepts_business_light_profile(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("business_light", "dispatch"),
            kind=EntryKind.PREFERENCE,
            content="Business light preference.",
            evidence_refs=("ev-001",),
            recorded_by="operator:charlie",
        )
        assert entry.scope.profile == "business_light"

    def test_custom_recorded_at_preserved(self):
        store = preference_store()
        ts = "2026-01-01T00:00:00.000000Z"
        entry = record_entry(
            store,
            scope=ScopeKey("coding_strict", "gate"),
            kind=EntryKind.PREFERENCE,
            content="Preference with fixed timestamp.",
            evidence_refs=("ev-001",),
            recorded_by="operator:alice",
            recorded_at=ts,
        )
        assert entry.recorded_at == ts

    def test_default_recorded_at_is_set(self):
        store = preference_store()
        entry = record_entry(
            store,
            scope=ScopeKey("coding_strict", "gate"),
            kind=EntryKind.PREFERENCE,
            content="Some preference.",
            evidence_refs=("ev-001",),
            recorded_by="operator:alice",
        )
        assert entry.recorded_at is not None
        assert len(entry.recorded_at) > 0


# ---------------------------------------------------------------------------
# assert_scope_not_contaminated()
# ---------------------------------------------------------------------------

class TestAssertScopeNotContaminated:
    def test_matching_profile_passes(self):
        entry = _make_entry(profile="coding_strict")
        assert_scope_not_contaminated(entry, expected_profile="coding_strict")  # no raise

    def test_mismatched_profile_raises(self):
        entry = _make_entry(profile="coding_strict")
        with pytest.raises(ValueError, match="contamination"):
            assert_scope_not_contaminated(entry, expected_profile="regulated_strict")

    def test_contamination_message_includes_entry_id(self):
        entry = _make_entry(entry_id="pref-suspect", profile="business_light")
        with pytest.raises(ValueError, match="pref-suspect"):
            assert_scope_not_contaminated(entry, expected_profile="coding_strict")

    def test_contamination_message_includes_profiles(self):
        entry = _make_entry(profile="business_light")
        with pytest.raises(ValueError) as exc_info:
            assert_scope_not_contaminated(entry, expected_profile="coding_strict")
        msg = str(exc_info.value)
        assert "business_light" in msg
        assert "coding_strict" in msg

    def test_regulated_strict_entry_vs_coding_strict_expected(self):
        entry = _make_entry(profile="regulated_strict")
        with pytest.raises(ValueError):
            assert_scope_not_contaminated(entry, expected_profile="coding_strict")

    def test_business_light_entry_vs_regulated_strict_expected(self):
        entry = _make_entry(profile="business_light")
        with pytest.raises(ValueError):
            assert_scope_not_contaminated(entry, expected_profile="regulated_strict")


# ---------------------------------------------------------------------------
# preference_store() factory
# ---------------------------------------------------------------------------

class TestPreferenceStoreFactory:
    def test_returns_preference_store_instance(self):
        store = preference_store()
        assert isinstance(store, PreferenceStore)

    def test_fresh_store_is_empty(self):
        store = preference_store()
        assert store.entry_count(include_retired=True) == 0

    def test_factory_returns_independent_stores(self):
        store_a = preference_store()
        store_b = preference_store()
        e = _make_entry(entry_id="pref-001")
        store_a.add(e)
        assert store_b.entry_count(include_retired=True) == 0


# ---------------------------------------------------------------------------
# KNOWN_PROFILES constant
# ---------------------------------------------------------------------------

class TestKnownProfiles:
    def test_regulated_strict_known(self):
        assert "regulated_strict" in KNOWN_PROFILES

    def test_coding_strict_known(self):
        assert "coding_strict" in KNOWN_PROFILES

    def test_business_light_known(self):
        assert "business_light" in KNOWN_PROFILES

    def test_unknown_not_present(self):
        assert "fantasy_profile" not in KNOWN_PROFILES
