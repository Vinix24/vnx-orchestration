#!/usr/bin/env python3
"""Folder-scoped manager/worker orchestration layer (Feature 20, PR-1).

Implements scope resolution, bounded context assembly, and isolation guards
for the business_light governance pilot.  Folder-scoped dispatches derive
context from folder-local sources only and cannot access coding worktrees
by default.

Components:
  ScopeType             — CODING_WORKTREE | BUSINESS_FOLDER | UNKNOWN
  IsolationViolation    — raised when cross-scope access is attempted
  FolderScope           — immutable scope descriptor (root + subfolder + type)
  FolderContext         — bounded context assembled from a scope
  resolve_scope()       — determine scope type from a path + known coding roots
  assemble_context()    — build bounded FolderContext (rejects out-of-scope sources)
  coding_worktree_scope() — factory for coding worktree scopes
  business_folder_scope() — factory for business folder scopes

Design invariants:
  - ScopeType.BUSINESS_FOLDER can never contain a CODING_WORKTREE path.
  - assemble_context() raises IsolationViolation if any source is outside the scope.
  - FolderContext.is_path_allowed() is the canonical gate for runtime access checks.
  - Scope resolution is purely string-based (no filesystem I/O).

Usage (resolve a path and assemble context):
    scope = resolve_scope("/work/crm", coding_roots=["/dev/vnx-wt"])
    ctx = assemble_context(scope, sources=["/work/crm/config.yaml"])
    ctx.assert_path_allowed("/work/crm/config.yaml")  # OK
    ctx.assert_path_allowed("/dev/vnx-wt/main.py")    # raises IsolationViolation

Usage (coding worktree scope):
    scope = coding_worktree_scope("/dev/vnx-wt")
    assert scope.is_coding_scope()

Usage (business folder scope with subfolder):
    scope = business_folder_scope("/work", subfolder="crm")
    assert scope.resolved_path == "/work/crm"
    assert scope.is_business_scope()
"""

from __future__ import annotations

import os
import os.path
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional

# ---------------------------------------------------------------------------
# Delegation to governance_profiles (thin wrapper)
# ---------------------------------------------------------------------------
# governance_profiles is the canonical source for profile config.
# folder_scope re-exports resolve_profile and load_scope_config for backward
# compat, and uses them internally to map ScopeType from config rather than
# from a hardcoded enum.
try:
    _lib_dir = os.path.dirname(os.path.abspath(__file__))
    if _lib_dir not in sys.path:
        sys.path.insert(0, _lib_dir)
    from governance_profiles import (  # noqa: F401  (re-export)
        resolve_profile,
        load_scope_config,
    )
    _GOVERNANCE_PROFILES_AVAILABLE = True
except ImportError:
    _GOVERNANCE_PROFILES_AVAILABLE = False


# ---------------------------------------------------------------------------
# Scope type classification
# ---------------------------------------------------------------------------

class ScopeType(Enum):
    """Classification of a folder scope.

    CODING_WORKTREE — a VNX coding worktree or coding source root.
    BUSINESS_FOLDER — a business-domain folder under business_light governance.
    UNKNOWN         — scope type could not be determined.
    """
    CODING_WORKTREE = "coding_worktree"
    BUSINESS_FOLDER = "business_folder"
    UNKNOWN         = "unknown"


# ---------------------------------------------------------------------------
# Isolation exception
# ---------------------------------------------------------------------------

class IsolationViolation(Exception):
    """Raised when a cross-scope access is attempted.

    Indicates that a business-folder workflow tried to access a coding
    worktree path, or that a path was supplied outside the scope boundary.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalize(path: str) -> str:
    """Normalize a path string without filesystem I/O."""
    return os.path.normpath(path).rstrip(os.sep)


def _is_under(path: str, root: str) -> bool:
    """Return True if path equals root or is a descendant of root."""
    np = _normalize(path)
    nr = _normalize(root)
    return np == nr or np.startswith(nr + os.sep)


# ---------------------------------------------------------------------------
# Folder scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FolderScope:
    """Immutable descriptor of a folder-scoped workspace.

    Attributes:
        root:       The root directory of the scope.
        scope_type: Classification — CODING_WORKTREE or BUSINESS_FOLDER.
        subfolder:  Optional relative subfolder within root.
    """
    root: str
    scope_type: ScopeType
    subfolder: str = ""

    def __post_init__(self) -> None:
        if self.subfolder and os.path.isabs(self.subfolder):
            raise ValueError(
                f"FolderScope.subfolder must be relative, "
                f"got absolute path: {self.subfolder!r}"
            )
        if self.subfolder and not _is_under(self.resolved_path, self.root):
            raise ValueError(
                f"FolderScope.subfolder {self.subfolder!r} escapes root {self.root!r}. "
                f"Resolved path {self.resolved_path!r} is outside root boundary."
            )

    @property
    def resolved_path(self) -> str:
        """Absolute resolved path: root/subfolder (or root if no subfolder)."""
        if self.subfolder:
            return os.path.join(self.root, self.subfolder)
        return self.root

    def is_business_scope(self) -> bool:
        """True when scope_type is BUSINESS_FOLDER."""
        return self.scope_type == ScopeType.BUSINESS_FOLDER

    def is_coding_scope(self) -> bool:
        """True when scope_type is CODING_WORKTREE."""
        return self.scope_type == ScopeType.CODING_WORKTREE

    def contains_path(self, path: str) -> bool:
        """True if path is at or below resolved_path."""
        return _is_under(path, self.resolved_path)


# ---------------------------------------------------------------------------
# Folder context
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FolderContext:
    """Bounded context assembled from a folder scope.

    context_sources holds the set of paths that were validated as within
    the scope boundary.  is_path_allowed() and assert_path_allowed() are
    the runtime gates for any subsequent access.

    Attributes:
        scope:           The FolderScope this context belongs to.
        context_sources: Validated source paths within the scope boundary.
    """
    scope: FolderScope
    context_sources: FrozenSet[str] = field(default_factory=frozenset)

    def is_path_allowed(self, path: str) -> bool:
        """True if path is within the scope boundary."""
        return self.scope.contains_path(path)

    def assert_path_allowed(self, path: str) -> None:
        """Raise IsolationViolation if path is outside the scope boundary."""
        if not self.is_path_allowed(path):
            raise IsolationViolation(
                f"Path {path!r} is outside scope boundary {self.scope.resolved_path!r}. "
                f"Scope: {self.scope.scope_type.value}. "
                "Cross-scope access is not permitted."
            )


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def coding_worktree_scope(root: str, subfolder: str = "") -> FolderScope:
    """Return a FolderScope for a coding worktree root."""
    return FolderScope(root=root, scope_type=ScopeType.CODING_WORKTREE,
                       subfolder=subfolder)


def business_folder_scope(root: str, subfolder: str = "") -> FolderScope:
    """Return a FolderScope for a business-light folder."""
    return FolderScope(root=root, scope_type=ScopeType.BUSINESS_FOLDER,
                       subfolder=subfolder)


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

def resolve_scope(path: str, coding_roots: Optional[List[str]] = None) -> FolderScope:
    """Resolve the scope type for a given path.

    A path is classified CODING_WORKTREE if it equals or descends from any
    known coding root.  Otherwise it is BUSINESS_FOLDER.

    Args:
        path:         The path to classify.
        coding_roots: Known coding worktree root paths.  No filesystem I/O.

    Returns:
        FolderScope with the resolved scope type and subfolder filled in.
    """
    if coding_roots is None:
        coding_roots = []
    for coding_root in coding_roots:
        if _is_under(path, coding_root):
            norm_root = _normalize(coding_root)
            norm_path = _normalize(path)
            subfolder = "" if norm_path == norm_root else norm_path[len(norm_root) + 1:]
            return FolderScope(
                root=coding_root,
                scope_type=ScopeType.CODING_WORKTREE,
                subfolder=subfolder,
            )
    return FolderScope(root=path, scope_type=ScopeType.BUSINESS_FOLDER)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def assemble_context(
    scope: FolderScope,
    sources: Optional[List[str]] = None,
) -> FolderContext:
    """Assemble a bounded FolderContext from a scope and source paths.

    Every source must be within the scope boundary.  A source outside the
    boundary raises IsolationViolation — callers must only supply paths they
    own.

    Args:
        scope:   The FolderScope that defines the boundary.
        sources: Paths to include as context sources.

    Returns:
        FolderContext with validated sources.

    Raises:
        IsolationViolation: If any source is outside the scope boundary.
    """
    if sources is None:
        sources = []
    for src in sources:
        if not scope.contains_path(src):
            raise IsolationViolation(
                f"Context source {src!r} is outside scope boundary "
                f"{scope.resolved_path!r} ({scope.scope_type.value}). "
                "Only folder-local sources may be assembled."
            )
    return FolderContext(scope=scope, context_sources=frozenset(sources))
