#!/usr/bin/env python3
"""Tests for governance profile selector and visibility surface (Feature 20, PR-3).

Covers:
  1. GovernanceProfile    — enum values and semantics
  2. ProfileSelection     — construction, is_authoritative, to_audit_line, to_dict
  3. ProfileVisibility    — counts, to_summary, is_blocked
  4. ProfileSelector      — select() for coding/business scopes
  5. Stateless helpers    — select_profile(), build_visibility()
  6. Factory functions    — coding_strict_selection(), business_light_selection()
  7. Coding-strict default — CODING_STRICT is default for all unspecified cases
  8. Isolation guarantees — coding scopes always get CODING_STRICT
  9. Business-light visibility — business scope can get BUSINESS_LIGHT
  10. Auditability         — selections produce auditable output
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from folder_scope import (
    ScopeType,
    business_folder_scope,
    coding_worktree_scope,
)
from governance_profile_selector import (
    GovernanceProfile,
    ProfileSelection,
    ProfileSelector,
    ProfileVisibility,
    build_visibility,
    business_light_selection,
    coding_strict_selection,
    select_profile,
)


# ---------------------------------------------------------------------------
# Minimal open item stub for visibility tests
# ---------------------------------------------------------------------------

class _BlockingItem:
    def is_blocking(self) -> bool:
        return True


class _NonBlockingItem:
    def is_blocking(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# 1. GovernanceProfile
# ---------------------------------------------------------------------------

class TestGovernanceProfile:

    def test_coding_strict_value(self) -> None:
        assert GovernanceProfile.CODING_STRICT.value == "coding_strict"

    def test_business_light_value(self) -> None:
        assert GovernanceProfile.BUSINESS_LIGHT.value == "business_light"

    def test_profiles_are_distinct(self) -> None:
        assert GovernanceProfile.CODING_STRICT != GovernanceProfile.BUSINESS_LIGHT

    def test_exactly_two_profiles(self) -> None:
        assert len(list(GovernanceProfile)) == 2


# ---------------------------------------------------------------------------
# 2. ProfileSelection
# ---------------------------------------------------------------------------

class TestProfileSelection:

    def _coding_sel(self) -> ProfileSelection:
        return ProfileSelection(
            profile=GovernanceProfile.CODING_STRICT,
            scope_type=ScopeType.CODING_WORKTREE,
            selection_id="s-001",
            note="test",
        )

    def _biz_sel(self) -> ProfileSelection:
        return ProfileSelection(
            profile=GovernanceProfile.BUSINESS_LIGHT,
            scope_type=ScopeType.BUSINESS_FOLDER,
            selection_id="s-002",
        )

    def test_profile_preserved(self) -> None:
        assert self._coding_sel().profile == GovernanceProfile.CODING_STRICT

    def test_scope_type_preserved(self) -> None:
        assert self._coding_sel().scope_type == ScopeType.CODING_WORKTREE

    def test_selection_id_preserved(self) -> None:
        assert self._coding_sel().selection_id == "s-001"

    def test_note_preserved(self) -> None:
        assert self._coding_sel().note == "test"

    def test_note_default_empty(self) -> None:
        s = ProfileSelection(profile=GovernanceProfile.CODING_STRICT,
                             scope_type=ScopeType.CODING_WORKTREE)
        assert s.note == ""

    def test_is_authoritative_true_for_coding_strict(self) -> None:
        assert self._coding_sel().is_authoritative() is True

    def test_is_authoritative_false_for_business_light(self) -> None:
        assert self._biz_sel().is_authoritative() is False

    def test_is_business_light_true(self) -> None:
        assert self._biz_sel().is_business_light() is True

    def test_is_business_light_false_for_coding(self) -> None:
        assert self._coding_sel().is_business_light() is False

    def test_to_audit_line_contains_profile(self) -> None:
        line = self._coding_sel().to_audit_line()
        assert "coding_strict" in line

    def test_to_audit_line_contains_scope_type(self) -> None:
        line = self._coding_sel().to_audit_line()
        assert "coding_worktree" in line

    def test_to_audit_line_contains_authoritative(self) -> None:
        line = self._coding_sel().to_audit_line()
        assert "authoritative=True" in line

    def test_to_dict_structure(self) -> None:
        d = self._coding_sel().to_dict()
        assert d["profile"] == "coding_strict"
        assert d["scope_type"] == "coding_worktree"
        assert d["is_authoritative"] is True
        assert d["selection_id"] == "s-001"

    def test_selection_is_frozen(self) -> None:
        s = self._coding_sel()
        with pytest.raises(Exception):
            s.profile = GovernanceProfile.BUSINESS_LIGHT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 3. ProfileVisibility
# ---------------------------------------------------------------------------

class TestProfileVisibility:

    def _sel(self, profile: GovernanceProfile = GovernanceProfile.BUSINESS_LIGHT) -> ProfileSelection:
        return ProfileSelection(profile=profile, scope_type=ScopeType.BUSINESS_FOLDER)

    def test_profile_name_returns_value(self) -> None:
        vis = ProfileVisibility(selection=self._sel())
        assert vis.profile_name() == "business_light"

    def test_open_item_count_zero_default(self) -> None:
        vis = ProfileVisibility(selection=self._sel())
        assert vis.open_item_count() == 0

    def test_open_item_count_with_items(self) -> None:
        vis = ProfileVisibility(selection=self._sel(),
                                open_items=(_BlockingItem(), _NonBlockingItem()))
        assert vis.open_item_count() == 2

    def test_blocking_item_count_zero_when_none(self) -> None:
        vis = ProfileVisibility(selection=self._sel(),
                                open_items=(_NonBlockingItem(),))
        assert vis.blocking_item_count() == 0

    def test_blocking_item_count_counts_blockers(self) -> None:
        vis = ProfileVisibility(selection=self._sel(),
                                open_items=(_BlockingItem(), _BlockingItem(),
                                            _NonBlockingItem()))
        assert vis.blocking_item_count() == 2

    def test_is_blocked_false_when_no_blockers(self) -> None:
        vis = ProfileVisibility(selection=self._sel())
        assert vis.is_blocked() is False

    def test_is_blocked_true_when_blocker_present(self) -> None:
        vis = ProfileVisibility(selection=self._sel(),
                                open_items=(_BlockingItem(),))
        assert vis.is_blocked() is True

    def test_to_summary_structure(self) -> None:
        vis = ProfileVisibility(selection=self._sel(), note="sprint review")
        summary = vis.to_summary()
        assert summary["profile"] == "business_light"
        assert summary["scope_type"] == "business_folder"
        assert summary["is_authoritative"] is False
        assert summary["open_item_count"] == 0
        assert summary["blocking_item_count"] == 0
        assert summary["is_blocked"] is False
        assert summary["note"] == "sprint review"

    def test_to_summary_coding_strict_is_authoritative(self) -> None:
        sel = self._sel(GovernanceProfile.CODING_STRICT)
        vis = ProfileVisibility(selection=sel)
        assert vis.to_summary()["is_authoritative"] is True

    def test_visibility_is_frozen(self) -> None:
        vis = ProfileVisibility(selection=self._sel())
        with pytest.raises(Exception):
            vis.note = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. ProfileSelector
# ---------------------------------------------------------------------------

class TestProfileSelector:

    def setup_method(self) -> None:
        self.selector = ProfileSelector()
        self.coding_scope = coding_worktree_scope("/dev/vnx-wt")
        self.biz_scope = business_folder_scope("/work/crm")

    def test_default_profile_is_coding_strict(self) -> None:
        assert self.selector.default_profile() == GovernanceProfile.CODING_STRICT

    def test_coding_scope_no_request_returns_coding_strict(self) -> None:
        sel = self.selector.select(self.coding_scope)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_coding_scope_request_business_light_returns_coding_strict(self) -> None:
        sel = self.selector.select(self.coding_scope,
                                   requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_coding_scope_always_authoritative(self) -> None:
        sel = self.selector.select(self.coding_scope,
                                   requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.is_authoritative() is True

    def test_business_scope_default_returns_coding_strict(self) -> None:
        sel = self.selector.select(self.biz_scope)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_business_scope_request_business_light_returns_business_light(self) -> None:
        sel = self.selector.select(self.biz_scope,
                                   requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.profile == GovernanceProfile.BUSINESS_LIGHT

    def test_business_scope_business_light_not_authoritative(self) -> None:
        sel = self.selector.select(self.biz_scope,
                                   requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.is_authoritative() is False

    def test_is_overrideable_true_for_business_scope(self) -> None:
        assert self.selector.is_overrideable(self.biz_scope) is True

    def test_is_overrideable_false_for_coding_scope(self) -> None:
        assert self.selector.is_overrideable(self.coding_scope) is False

    def test_selection_id_preserved(self) -> None:
        sel = self.selector.select(self.biz_scope, selection_id="sel-42")
        assert sel.selection_id == "sel-42"

    def test_coding_override_note_mentions_business_light(self) -> None:
        sel = self.selector.select(self.coding_scope,
                                   requested=GovernanceProfile.BUSINESS_LIGHT)
        assert "business_light" in sel.note.lower()


# ---------------------------------------------------------------------------
# 5. Stateless helpers
# ---------------------------------------------------------------------------

class TestStatelessHelpers:

    def test_select_profile_coding_scope(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = select_profile(scope)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_select_profile_business_light_for_biz_scope(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.profile == GovernanceProfile.BUSINESS_LIGHT

    def test_select_profile_coding_scope_override_blocked(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_build_visibility_no_items(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        vis = build_visibility(sel)
        assert vis.open_item_count() == 0

    def test_build_visibility_with_items(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        vis = build_visibility(sel, open_items=[_BlockingItem(), _NonBlockingItem()])
        assert vis.open_item_count() == 2
        assert vis.blocking_item_count() == 1

    def test_build_visibility_note_preserved(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope)
        vis = build_visibility(sel, note="operator check")
        assert vis.note == "operator check"

    def test_build_visibility_none_items_empty_tuple(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope)
        vis = build_visibility(sel, open_items=None)
        assert vis.open_item_count() == 0


# ---------------------------------------------------------------------------
# 6. Factory functions
# ---------------------------------------------------------------------------

class TestFactoryFunctions:

    def test_coding_strict_selection_profile(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = coding_strict_selection(scope)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_coding_strict_selection_scope_type(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = coding_strict_selection(scope)
        assert sel.scope_type == ScopeType.CODING_WORKTREE

    def test_coding_strict_selection_for_business_scope(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = coding_strict_selection(scope)
        assert sel.profile == GovernanceProfile.CODING_STRICT
        assert sel.scope_type == ScopeType.BUSINESS_FOLDER

    def test_business_light_selection_for_business_scope(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = business_light_selection(scope)
        assert sel.profile == GovernanceProfile.BUSINESS_LIGHT

    def test_business_light_selection_scope_type(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = business_light_selection(scope)
        assert sel.scope_type == ScopeType.BUSINESS_FOLDER

    def test_business_light_selection_for_coding_scope_raises(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        with pytest.raises(ValueError, match="coding_strict"):
            business_light_selection(scope)

    def test_business_light_error_message_mentions_coding_scope(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        with pytest.raises(ValueError) as exc_info:
            business_light_selection(scope)
        assert "/dev/vnx-wt" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. Coding-strict default
# ---------------------------------------------------------------------------

class TestCodingStrictDefault:

    def test_selector_default_is_coding_strict(self) -> None:
        assert ProfileSelector().default_profile() == GovernanceProfile.CODING_STRICT

    def test_no_request_yields_coding_strict_for_coding_scope(self) -> None:
        sel = select_profile(coding_worktree_scope("/dev/vnx-wt"))
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_no_request_yields_coding_strict_for_business_scope(self) -> None:
        """Without a specific request, business scopes also default to coding_strict."""
        sel = select_profile(business_folder_scope("/work/crm"))
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_coding_strict_is_always_authoritative(self) -> None:
        for scope in [coding_worktree_scope("/dev/vnx-wt"),
                      business_folder_scope("/work/crm")]:
            sel = coding_strict_selection(scope)
            assert sel.is_authoritative()


# ---------------------------------------------------------------------------
# 8. Isolation guarantees
# ---------------------------------------------------------------------------

class TestIsolationGuarantees:

    def test_coding_scope_cannot_get_business_light_via_selector(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = ProfileSelector().select(scope,
                                       requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_coding_scope_cannot_get_business_light_via_helper(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.profile == GovernanceProfile.CODING_STRICT

    def test_coding_scope_cannot_get_business_light_via_factory(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        with pytest.raises(ValueError):
            business_light_selection(scope)

    def test_business_light_selection_is_not_authoritative(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert sel.is_authoritative() is False

    def test_two_scopes_independent_profiles(self) -> None:
        coding_sel = select_profile(coding_worktree_scope("/dev/vnx-wt"))
        biz_sel = select_profile(business_folder_scope("/work/crm"),
                                  requested=GovernanceProfile.BUSINESS_LIGHT)
        assert coding_sel.profile == GovernanceProfile.CODING_STRICT
        assert biz_sel.profile == GovernanceProfile.BUSINESS_LIGHT

    def test_is_overrideable_coding_scope_false(self) -> None:
        assert not ProfileSelector().is_overrideable(coding_worktree_scope("/dev/vnx-wt"))

    def test_is_overrideable_business_scope_true(self) -> None:
        assert ProfileSelector().is_overrideable(business_folder_scope("/work/crm"))


# ---------------------------------------------------------------------------
# 9. Business-light visibility
# ---------------------------------------------------------------------------

class TestBusinessLightVisibility:

    def setup_method(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        self.vis = build_visibility(sel, note="operator view")

    def test_profile_is_business_light(self) -> None:
        assert self.vis.profile_name() == "business_light"

    def test_is_not_authoritative(self) -> None:
        assert self.vis.to_summary()["is_authoritative"] is False

    def test_scope_type_is_business_folder(self) -> None:
        assert self.vis.to_summary()["scope_type"] == "business_folder"

    def test_note_in_summary(self) -> None:
        assert self.vis.to_summary()["note"] == "operator view"

    def test_no_blocker_items_not_blocked(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        vis = build_visibility(sel, open_items=[_NonBlockingItem()])
        assert vis.is_blocked() is False

    def test_blocker_item_shows_blocked(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        vis = build_visibility(sel, open_items=[_BlockingItem()])
        assert vis.is_blocked() is True


# ---------------------------------------------------------------------------
# 10. Auditability
# ---------------------------------------------------------------------------

class TestAuditability:

    def test_audit_line_produced_for_coding_strict(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = select_profile(scope, selection_id="s-100")
        line = sel.to_audit_line()
        assert "s-100" in line
        assert "coding_strict" in line

    def test_audit_line_produced_for_business_light(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT,
                              selection_id="s-200")
        line = sel.to_audit_line()
        assert "s-200" in line
        assert "business_light" in line

    def test_audit_line_shows_authoritative_false_for_business_light(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert "authoritative=False" in sel.to_audit_line()

    def test_to_dict_is_serializable(self) -> None:
        scope = business_folder_scope("/work/crm")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT,
                              selection_id="s-42")
        d = sel.to_dict()
        # All values are JSON-serializable types
        for v in d.values():
            assert isinstance(v, (str, bool))

    def test_coding_override_attempt_is_auditable(self) -> None:
        """Even a blocked override attempt produces an auditable selection."""
        scope = coding_worktree_scope("/dev/vnx-wt")
        sel = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT,
                              selection_id="blocked-001")
        line = sel.to_audit_line()
        assert "blocked-001" in line
        assert "coding_strict" in line  # override was rejected
