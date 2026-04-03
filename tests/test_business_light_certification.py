#!/usr/bin/env python3
"""PR-4 certification tests for Feature 20: Business-Light Governance Pilot.

Certifies scope isolation, review-by-exception, profile selection,
cross-profile isolation, and contract alignment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from folder_scope import (
    FolderContext,
    FolderScope,
    IsolationViolation,
    ScopeType,
    assemble_context,
    business_folder_scope,
    coding_worktree_scope,
    resolve_scope,
)
from business_light_policy import (
    AuditArtifact,
    AuditArtifactType,
    AuditRecord,
    BusinessLightReviewPolicy,
    CloseoutDecision,
    GateResult,
    OpenItem,
    OpenItemSeverity,
    ReviewMode,
    business_light_policy,
)
from governance_profile_selector import (
    GovernanceProfile,
    ProfileSelection,
    ProfileVisibility,
    build_visibility,
    select_profile,
)


# Stubs for visibility tests
class _BlockingItem:
    def is_blocking(self) -> bool:
        return True

class _NonBlockingItem:
    def is_blocking(self) -> bool:
        return False


# ===================================================================
# Section 1: Folder-Scoped Orchestration And Isolation
# ===================================================================

class TestFolderScopeIsolation:

    def test_business_folder_scope_created(self) -> None:
        scope = business_folder_scope("/tmp/client-project")
        assert scope.scope_type == ScopeType.BUSINESS_FOLDER

    def test_coding_worktree_scope_created(self) -> None:
        scope = coding_worktree_scope("/tmp/coding-repo")
        assert scope.scope_type == ScopeType.CODING_WORKTREE

    def test_business_scope_blocks_coding_paths(self) -> None:
        scope = business_folder_scope("/tmp/business")
        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=["/tmp/coding-repo/src/main.py"])

    def test_business_scope_allows_own_paths(self) -> None:
        scope = business_folder_scope("/tmp/business")
        ctx = assemble_context(scope, sources=["/tmp/business/brief.md"])
        assert isinstance(ctx, FolderContext)

    def test_resolve_scope_classifies_coding(self) -> None:
        result = resolve_scope("/tmp/coding-repo/src",
                              coding_roots=["/tmp/coding-repo"])
        assert result.scope_type == ScopeType.CODING_WORKTREE

    def test_resolve_scope_classifies_non_coding(self) -> None:
        result = resolve_scope("/tmp/business/docs",
                              coding_roots=["/tmp/coding-repo"])
        assert result.scope_type != ScopeType.CODING_WORKTREE

    def test_scope_objects_are_immutable(self) -> None:
        scope = business_folder_scope("/tmp/test")
        with pytest.raises(AttributeError):
            scope.root = "/tmp/hacked"  # type: ignore[misc]


# ===================================================================
# Section 2: Review-By-Exception And Audit Retention
# ===================================================================

class TestReviewByExceptionPolicy:

    def test_default_policy_is_review_by_exception(self) -> None:
        policy = business_light_policy()
        assert policy.review_mode == ReviewMode.REVIEW_BY_EXCEPTION

    def test_no_blockers_can_proceed(self) -> None:
        policy = business_light_policy()
        items = [OpenItem("i1", "minor note", OpenItemSeverity.INFO)]
        assert policy.can_proceed(items) is True

    def test_blocker_blocks_proceed(self) -> None:
        policy = business_light_policy()
        items = [OpenItem("i1", "critical issue", OpenItemSeverity.BLOCKER)]
        assert policy.can_proceed(items) is False

    def test_warning_does_not_block(self) -> None:
        policy = business_light_policy()
        items = [OpenItem("i1", "attention needed", OpenItemSeverity.WARNING)]
        assert policy.can_proceed(items) is True

    def test_gate_result_captures_blocking_items(self) -> None:
        policy = business_light_policy()
        items = [
            OpenItem("i1", "blocker", OpenItemSeverity.BLOCKER),
            OpenItem("i2", "info", OpenItemSeverity.INFO),
        ]
        record = AuditRecord(task_id="d-cert")
        result = policy.gate_result(items, record)
        assert isinstance(result, GateResult)
        assert result.passed is False

    def test_audit_record_preserves_all_artifacts(self) -> None:
        record = AuditRecord(task_id="d-cert")
        a1 = AuditArtifact("a1", AuditArtifactType.REVIEW_DECISION, note="proceed")
        a2 = AuditArtifact("a2", AuditArtifactType.OPEN_ITEM, note="test")
        record = record.with_artifact(a1)
        record = record.with_artifact(a2)
        assert len(record.artifacts) == 2

    def test_closeout_must_be_explicit(self) -> None:
        with pytest.raises(ValueError):
            CloseoutDecision(task_id="d-cert", decided_by="T0", is_explicit=False)

    def test_closeout_explicit_default(self) -> None:
        decision = CloseoutDecision(task_id="d-cert", decided_by="T0")
        assert decision.is_explicit is True


# ===================================================================
# Section 3: Governance Profile Selector
# ===================================================================

class TestGovernanceProfileSelector:

    def test_coding_scope_always_gets_coding_strict(self) -> None:
        scope = coding_worktree_scope("/tmp/coding")
        selection = select_profile(scope)
        assert selection.profile == GovernanceProfile.CODING_STRICT

    def test_business_scope_gets_business_light_when_requested(self) -> None:
        scope = business_folder_scope("/tmp/business")
        selection = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert selection.profile == GovernanceProfile.BUSINESS_LIGHT

    def test_coding_strict_is_authoritative(self) -> None:
        scope = coding_worktree_scope("/tmp/coding")
        selection = select_profile(scope)
        assert selection.is_authoritative()

    def test_business_light_not_authoritative(self) -> None:
        scope = business_folder_scope("/tmp/business")
        selection = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert not selection.is_authoritative()

    def test_coding_scope_cannot_be_overridden_to_business(self) -> None:
        scope = coding_worktree_scope("/tmp/coding")
        selection = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        assert selection.profile == GovernanceProfile.CODING_STRICT

    def test_selection_is_immutable(self) -> None:
        scope = coding_worktree_scope("/tmp/coding")
        selection = select_profile(scope)
        with pytest.raises(AttributeError):
            selection.profile = GovernanceProfile.BUSINESS_LIGHT  # type: ignore[misc]

    def test_visibility_surface_informative(self) -> None:
        scope = business_folder_scope("/tmp/business")
        selection = select_profile(scope, requested=GovernanceProfile.BUSINESS_LIGHT)
        visibility = build_visibility(selection, open_items=[])
        assert isinstance(visibility, ProfileVisibility)

    def test_selection_produces_audit_line(self) -> None:
        scope = coding_worktree_scope("/tmp/coding")
        selection = select_profile(scope)
        audit = selection.to_audit_line()
        assert isinstance(audit, str)
        assert "coding_strict" in audit


# ===================================================================
# Section 4: Cross-Profile Isolation
# ===================================================================

class TestCrossProfileIsolation:

    def test_business_context_cannot_include_coding_paths(self) -> None:
        scope = business_folder_scope("/tmp/business")
        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=["/tmp/coding/main.py"])

    def test_audit_record_immutable(self) -> None:
        record = AuditRecord(task_id="d-cert")
        a = AuditArtifact("a1", AuditArtifactType.FINDING, note="test")
        new_record = record.with_artifact(a)
        assert len(record.artifacts) == 0
        assert len(new_record.artifacts) == 1

    def test_resolved_blocker_does_not_block(self) -> None:
        policy = business_light_policy()
        items = [OpenItem("i1", "gap", OpenItemSeverity.BLOCKER, is_resolved=True)]
        assert policy.can_proceed(items) is True


# ===================================================================
# Section 5: Contract Alignment
# ===================================================================

class TestContractAlignment:

    def test_two_governance_profiles_exist(self) -> None:
        assert len(list(GovernanceProfile)) == 2

    def test_two_primary_scope_types(self) -> None:
        assert ScopeType.CODING_WORKTREE is not None
        assert ScopeType.BUSINESS_FOLDER is not None

    def test_review_modes_defined(self) -> None:
        assert ReviewMode.FULL_REVIEW is not None
        assert ReviewMode.REVIEW_BY_EXCEPTION is not None

    def test_three_open_item_severities(self) -> None:
        assert OpenItemSeverity.BLOCKER is not None
        assert OpenItemSeverity.WARNING is not None
        assert OpenItemSeverity.INFO is not None
