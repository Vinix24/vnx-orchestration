#!/usr/bin/env python3
"""Tests for folder-scoped manager/worker orchestration layer (Feature 20, PR-1).

Covers:
  1. ScopeType              — enum values and semantics
  2. IsolationViolation     — exception class and message contract
  3. FolderScope            — construction, resolved_path, contains_path, is_*_scope
  4. FolderContext          — is_path_allowed, assert_path_allowed, default sources
  5. Scope factory functions — coding_worktree_scope, business_folder_scope
  6. resolve_scope          — path-to-scope classification with coding roots
  7. assemble_context       — bounded inputs, out-of-scope rejection
  8. Coding worktree isolation — business scopes cannot read coding paths
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


# ---------------------------------------------------------------------------
# 1. ScopeType
# ---------------------------------------------------------------------------

class TestScopeType:

    def test_coding_worktree_value(self) -> None:
        assert ScopeType.CODING_WORKTREE.value == "coding_worktree"

    def test_business_folder_value(self) -> None:
        assert ScopeType.BUSINESS_FOLDER.value == "business_folder"

    def test_unknown_value(self) -> None:
        assert ScopeType.UNKNOWN.value == "unknown"

    def test_coding_and_business_are_distinct(self) -> None:
        assert ScopeType.CODING_WORKTREE != ScopeType.BUSINESS_FOLDER

    def test_all_types_enumerable(self) -> None:
        values = {t.value for t in ScopeType}
        assert "coding_worktree" in values
        assert "business_folder" in values
        assert "unknown" in values


# ---------------------------------------------------------------------------
# 2. IsolationViolation
# ---------------------------------------------------------------------------

class TestIsolationViolation:

    def test_is_exception(self) -> None:
        assert issubclass(IsolationViolation, Exception)

    def test_can_be_raised_with_message(self) -> None:
        with pytest.raises(IsolationViolation, match="outside scope"):
            raise IsolationViolation("outside scope boundary")

    def test_message_includes_path(self) -> None:
        try:
            raise IsolationViolation("Path '/bad/path' is outside scope boundary '/good'.")
        except IsolationViolation as e:
            assert "/bad/path" in str(e)
            assert "/good" in str(e)


# ---------------------------------------------------------------------------
# 3. FolderScope
# ---------------------------------------------------------------------------

class TestFolderScope:

    def _coding_scope(self) -> FolderScope:
        return FolderScope(root="/dev/vnx-wt", scope_type=ScopeType.CODING_WORKTREE)

    def _business_scope(self) -> FolderScope:
        return FolderScope(root="/work/crm", scope_type=ScopeType.BUSINESS_FOLDER)

    def test_root_is_preserved(self) -> None:
        s = self._coding_scope()
        assert s.root == "/dev/vnx-wt"

    def test_scope_type_is_preserved(self) -> None:
        s = self._coding_scope()
        assert s.scope_type == ScopeType.CODING_WORKTREE

    def test_resolved_path_no_subfolder(self) -> None:
        s = FolderScope(root="/work/crm", scope_type=ScopeType.BUSINESS_FOLDER)
        assert s.resolved_path == "/work/crm"

    def test_resolved_path_with_subfolder(self) -> None:
        s = FolderScope(root="/work", scope_type=ScopeType.BUSINESS_FOLDER, subfolder="crm")
        assert s.resolved_path == "/work/crm"

    def test_subfolder_default_empty(self) -> None:
        s = self._business_scope()
        assert s.subfolder == ""

    def test_is_coding_scope_true(self) -> None:
        assert self._coding_scope().is_coding_scope() is True

    def test_is_coding_scope_false_for_business(self) -> None:
        assert self._business_scope().is_coding_scope() is False

    def test_is_business_scope_true(self) -> None:
        assert self._business_scope().is_business_scope() is True

    def test_is_business_scope_false_for_coding(self) -> None:
        assert self._coding_scope().is_business_scope() is False

    def test_contains_path_exact_match(self) -> None:
        s = self._business_scope()
        assert s.contains_path("/work/crm") is True

    def test_contains_path_child(self) -> None:
        s = self._business_scope()
        assert s.contains_path("/work/crm/config.yaml") is True

    def test_contains_path_nested_child(self) -> None:
        s = self._business_scope()
        assert s.contains_path("/work/crm/sub/dir/file.txt") is True

    def test_contains_path_sibling_rejected(self) -> None:
        s = self._business_scope()
        assert s.contains_path("/work/other") is False

    def test_contains_path_parent_rejected(self) -> None:
        s = self._business_scope()
        assert s.contains_path("/work") is False

    def test_contains_path_prefix_not_sufficient(self) -> None:
        """'/work/crm2' should not match scope '/work/crm'."""
        s = self._business_scope()
        assert s.contains_path("/work/crm2") is False

    def test_scope_is_frozen(self) -> None:
        s = self._business_scope()
        with pytest.raises(Exception):
            s.root = "modified"  # type: ignore[misc]

    def test_contains_coding_path_from_business_scope(self) -> None:
        biz = self._business_scope()
        assert biz.contains_path("/dev/vnx-wt/main.py") is False


# ---------------------------------------------------------------------------
# 4. FolderContext
# ---------------------------------------------------------------------------

class TestFolderContext:

    def _biz_context(self) -> FolderContext:
        scope = business_folder_scope("/work/crm")
        return FolderContext(scope=scope,
                             context_sources=frozenset({"/work/crm/config.yaml"}))

    def test_is_path_allowed_within_scope(self) -> None:
        ctx = self._biz_context()
        assert ctx.is_path_allowed("/work/crm/config.yaml") is True

    def test_is_path_allowed_deep_path_within_scope(self) -> None:
        ctx = self._biz_context()
        assert ctx.is_path_allowed("/work/crm/sub/dir/data.json") is True

    def test_is_path_allowed_outside_scope(self) -> None:
        ctx = self._biz_context()
        assert ctx.is_path_allowed("/dev/vnx-wt/main.py") is False

    def test_assert_path_allowed_within_scope_no_raise(self) -> None:
        ctx = self._biz_context()
        ctx.assert_path_allowed("/work/crm/config.yaml")  # no exception

    def test_assert_path_allowed_outside_scope_raises(self) -> None:
        ctx = self._biz_context()
        with pytest.raises(IsolationViolation, match="outside scope boundary"):
            ctx.assert_path_allowed("/dev/vnx-wt/main.py")

    def test_assert_path_allowed_message_includes_paths(self) -> None:
        ctx = self._biz_context()
        with pytest.raises(IsolationViolation) as exc_info:
            ctx.assert_path_allowed("/dev/vnx-wt/main.py")
        msg = str(exc_info.value)
        assert "/dev/vnx-wt/main.py" in msg
        assert "/work/crm" in msg

    def test_context_sources_default_empty(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = FolderContext(scope=scope)
        assert ctx.context_sources == frozenset()

    def test_context_is_frozen(self) -> None:
        ctx = self._biz_context()
        with pytest.raises(Exception):
            ctx.scope = business_folder_scope("/other")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5. Scope factory functions
# ---------------------------------------------------------------------------

class TestScopeFactories:

    def test_coding_worktree_scope_type(self) -> None:
        s = coding_worktree_scope("/dev/vnx-wt")
        assert s.scope_type == ScopeType.CODING_WORKTREE

    def test_coding_worktree_scope_root(self) -> None:
        s = coding_worktree_scope("/dev/vnx-wt")
        assert s.root == "/dev/vnx-wt"

    def test_coding_worktree_scope_with_subfolder(self) -> None:
        s = coding_worktree_scope("/dev/vnx-wt", subfolder="scripts")
        assert s.resolved_path == "/dev/vnx-wt/scripts"
        assert s.is_coding_scope()

    def test_business_folder_scope_type(self) -> None:
        s = business_folder_scope("/work/crm")
        assert s.scope_type == ScopeType.BUSINESS_FOLDER

    def test_business_folder_scope_root(self) -> None:
        s = business_folder_scope("/work/crm")
        assert s.root == "/work/crm"

    def test_business_folder_scope_with_subfolder(self) -> None:
        s = business_folder_scope("/work", subfolder="crm/invoices")
        assert s.resolved_path == "/work/crm/invoices"
        assert s.is_business_scope()


# ---------------------------------------------------------------------------
# 6. resolve_scope
# ---------------------------------------------------------------------------

class TestResolveScope:

    def test_empty_coding_roots_yields_business(self) -> None:
        s = resolve_scope("/work/crm", coding_roots=[])
        assert s.scope_type == ScopeType.BUSINESS_FOLDER

    def test_path_under_coding_root_yields_coding(self) -> None:
        s = resolve_scope("/dev/vnx-wt/scripts", coding_roots=["/dev/vnx-wt"])
        assert s.scope_type == ScopeType.CODING_WORKTREE

    def test_path_equal_to_coding_root_yields_coding(self) -> None:
        s = resolve_scope("/dev/vnx-wt", coding_roots=["/dev/vnx-wt"])
        assert s.scope_type == ScopeType.CODING_WORKTREE

    def test_path_not_under_coding_root_yields_business(self) -> None:
        s = resolve_scope("/work/crm", coding_roots=["/dev/vnx-wt"])
        assert s.scope_type == ScopeType.BUSINESS_FOLDER

    def test_coding_root_assigned_correctly(self) -> None:
        s = resolve_scope("/dev/vnx-wt/scripts", coding_roots=["/dev/vnx-wt"])
        assert s.root == "/dev/vnx-wt"

    def test_subfolder_computed_from_coding_root(self) -> None:
        s = resolve_scope("/dev/vnx-wt/scripts/lib", coding_roots=["/dev/vnx-wt"])
        assert s.subfolder == "scripts/lib"

    def test_no_subfolder_when_path_equals_root(self) -> None:
        s = resolve_scope("/dev/vnx-wt", coding_roots=["/dev/vnx-wt"])
        assert s.subfolder == ""

    def test_business_root_is_input_path(self) -> None:
        s = resolve_scope("/work/crm", coding_roots=["/dev/vnx-wt"])
        assert s.root == "/work/crm"

    def test_multiple_coding_roots_first_match_wins(self) -> None:
        s = resolve_scope("/dev/wt1/src",
                          coding_roots=["/dev/wt1", "/dev/wt2"])
        assert s.root == "/dev/wt1"
        assert s.scope_type == ScopeType.CODING_WORKTREE

    def test_path_prefix_not_sufficient_for_coding_match(self) -> None:
        """'/dev/vnx-wt2' should NOT match coding root '/dev/vnx-wt'."""
        s = resolve_scope("/dev/vnx-wt2", coding_roots=["/dev/vnx-wt"])
        assert s.scope_type == ScopeType.BUSINESS_FOLDER


# ---------------------------------------------------------------------------
# 7. assemble_context
# ---------------------------------------------------------------------------

class TestAssembleContext:

    def test_sources_within_scope_accepted(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = assemble_context(scope, sources=["/work/crm/config.yaml"])
        assert "/work/crm/config.yaml" in ctx.context_sources

    def test_multiple_sources_within_scope(self) -> None:
        scope = business_folder_scope("/work/crm")
        sources = ["/work/crm/a.yaml", "/work/crm/sub/b.json"]
        ctx = assemble_context(scope, sources=sources)
        assert frozenset(sources) == ctx.context_sources

    def test_empty_sources_returns_empty_context(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = assemble_context(scope, sources=[])
        assert ctx.context_sources == frozenset()

    def test_source_outside_scope_raises(self) -> None:
        scope = business_folder_scope("/work/crm")
        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=["/dev/vnx-wt/main.py"])

    def test_mixed_sources_raises_on_first_violation(self) -> None:
        scope = business_folder_scope("/work/crm")
        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=[
                "/work/crm/config.yaml",
                "/dev/vnx-wt/main.py",  # violation
            ])

    def test_coding_scope_rejects_business_sources(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=["/work/crm/config.yaml"])

    def test_assembled_context_scope_is_preserved(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = assemble_context(scope, sources=["/work/crm/config.yaml"])
        assert ctx.scope == scope

    def test_assembled_context_sources_immutable(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = assemble_context(scope, sources=["/work/crm/a.yaml"])
        assert isinstance(ctx.context_sources, frozenset)


# ---------------------------------------------------------------------------
# 8. Coding worktree isolation
# ---------------------------------------------------------------------------

class TestCodingWorktreeIsolation:

    def test_business_context_blocks_coding_path(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = FolderContext(scope=scope)
        assert ctx.is_path_allowed("/dev/vnx-wt/main.py") is False

    def test_business_context_assert_raises_on_coding_path(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = FolderContext(scope=scope)
        with pytest.raises(IsolationViolation):
            ctx.assert_path_allowed("/dev/vnx-wt/scripts/lib/runtime.py")

    def test_coding_context_blocks_business_path(self) -> None:
        scope = coding_worktree_scope("/dev/vnx-wt")
        ctx = FolderContext(scope=scope)
        assert ctx.is_path_allowed("/work/crm/config.yaml") is False

    def test_business_cannot_assemble_coding_sources(self) -> None:
        scope = business_folder_scope("/work")
        with pytest.raises(IsolationViolation):
            assemble_context(scope, sources=["/dev/vnx-wt/main.py"])

    def test_resolve_then_assemble_coding_path_rejected(self) -> None:
        biz_scope = resolve_scope("/work/crm", coding_roots=["/dev/vnx-wt"])
        with pytest.raises(IsolationViolation):
            assemble_context(biz_scope, sources=["/dev/vnx-wt/main.py"])

    def test_two_business_scopes_isolated_from_each_other(self) -> None:
        crm_scope = business_folder_scope("/work/crm")
        hr_ctx = FolderContext(scope=business_folder_scope("/work/hr"))
        assert hr_ctx.is_path_allowed("/work/crm/config.yaml") is False

    def test_coding_and_business_resolved_differently(self) -> None:
        coding = resolve_scope("/dev/vnx-wt/src", coding_roots=["/dev/vnx-wt"])
        business = resolve_scope("/work/crm", coding_roots=["/dev/vnx-wt"])
        assert coding.is_coding_scope()
        assert business.is_business_scope()
        assert coding.scope_type != business.scope_type

    def test_isolation_violation_message_includes_scope_type(self) -> None:
        scope = business_folder_scope("/work/crm")
        ctx = FolderContext(scope=scope)
        with pytest.raises(IsolationViolation) as exc_info:
            ctx.assert_path_allowed("/dev/vnx-wt/main.py")
        assert "business_folder" in str(exc_info.value)
