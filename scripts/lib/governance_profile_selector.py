#!/usr/bin/env python3
"""Governance profile selector and operator visibility surface (Feature 20, PR-3).

Exposes business_light profile selection and status visibility for non-coding
scopes while preserving coding_strict as the default authoritative profile.
Coding scopes cannot be overridden to business_light by construction.

Components:
  GovernanceProfileEnum — CODING_STRICT | BUSINESS_LIGHT (enum; renamed from GovernanceProfile, OI-1160)
  ProfileSelection      — immutable result of a governance profile selection
  ProfileVisibility     — operator-readable status surface for a selection
  ProfileSelector       — core profile selection engine
  select_profile()      — stateless helper for one-off selection
  build_visibility()    — build operator-visible surface from a selection
  coding_strict_selection()   — factory for coding_strict selections
  business_light_selection()  — factory for business_light selections (non-coding only)

Design invariants:
  - CODING_STRICT is the default and authoritative profile.
  - Coding scopes (CODING_WORKTREE) always receive CODING_STRICT regardless of
    the requested profile.  This is enforced at selection time, not at usage.
  - Business scopes (BUSINESS_FOLDER) may select BUSINESS_LIGHT.
  - ProfileSelection.is_authoritative() is True only for CODING_STRICT.
  - Profile selection is always auditable: to_audit_line() produces a log entry.
  - ProfileVisibility never exposes coding_strict state to a business_light surface.

Usage (select profile for a scope):
    scope = business_folder_scope("/work/crm")
    sel = select_profile(scope, requested=GovernanceProfileEnum.BUSINESS_LIGHT)
    assert sel.profile == GovernanceProfileEnum.BUSINESS_LIGHT

Usage (coding scope ignores requested profile):
    scope = coding_worktree_scope("/dev/vnx-wt")
    sel = select_profile(scope, requested=GovernanceProfileEnum.BUSINESS_LIGHT)
    assert sel.profile == GovernanceProfileEnum.CODING_STRICT  # override blocked

Usage (operator visibility):
    vis = build_visibility(sel, open_items=[...], note="sprint review")
    print(vis.to_summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from folder_scope import FolderScope, ScopeType


# ---------------------------------------------------------------------------
# Governance profile
# ---------------------------------------------------------------------------

class GovernanceProfileEnum(Enum):
    """Governance profile classification.

    CODING_STRICT  — the default authoritative profile for all coding scopes.
                     Full review gates, strict closeout requirements.
    BUSINESS_LIGHT — softer review-by-exception profile for business folders.
                     Only valid for non-coding scopes.
    """
    CODING_STRICT  = "coding_strict"
    BUSINESS_LIGHT = "business_light"


# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileSelection:
    """Immutable result of a governance profile selection.

    Attributes:
        profile:     The effective profile assigned to this scope.
        scope_type:  The ScopeType of the scope that was evaluated.
        selection_id: Identifier for this selection event (for audit trail).
        note:        Human-readable note explaining the selection.
    """
    profile: GovernanceProfileEnum
    scope_type: ScopeType
    selection_id: str = ""
    note: str = ""

    def is_authoritative(self) -> bool:
        """True when profile is CODING_STRICT."""
        return self.profile == GovernanceProfileEnum.CODING_STRICT

    def is_business_light(self) -> bool:
        """True when profile is BUSINESS_LIGHT."""
        return self.profile == GovernanceProfileEnum.BUSINESS_LIGHT

    def to_audit_line(self) -> str:
        """Produce a single-line audit string for this selection."""
        return (
            f"profile_selection id={self.selection_id!r} "
            f"profile={self.profile.value} "
            f"scope_type={self.scope_type.value} "
            f"authoritative={self.is_authoritative()} "
            f"note={self.note!r}"
        )

    def to_dict(self) -> dict:
        return {
            "selection_id": self.selection_id,
            "profile": self.profile.value,
            "scope_type": self.scope_type.value,
            "is_authoritative": self.is_authoritative(),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Profile visibility surface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileVisibility:
    """Operator-readable status surface for a governance profile selection.

    Presents profile status, open-item counts, and audit record size.
    Does not expose coding_strict internals to a business_light surface.

    Attributes:
        selection:   The ProfileSelection this visibility is built from.
        open_items:  Tuple of open items associated with this profile.
        note:        Optional operator note for this visibility snapshot.
    """
    selection: ProfileSelection
    open_items: tuple = field(default_factory=tuple)
    note: str = ""

    def profile_name(self) -> str:
        """Human-readable profile name."""
        return self.selection.profile.value

    def open_item_count(self) -> int:
        """Total number of open items."""
        return len(self.open_items)

    def blocking_item_count(self) -> int:
        """Number of open items that are currently blocking."""
        return sum(1 for item in self.open_items
                   if getattr(item, "is_blocking", lambda: False)())

    def is_blocked(self) -> bool:
        """True if any open item is blocking."""
        return self.blocking_item_count() > 0

    def to_summary(self) -> dict:
        """Operator-readable summary of this visibility snapshot."""
        return {
            "profile": self.profile_name(),
            "scope_type": self.selection.scope_type.value,
            "is_authoritative": self.selection.is_authoritative(),
            "open_item_count": self.open_item_count(),
            "blocking_item_count": self.blocking_item_count(),
            "is_blocked": self.is_blocked(),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Profile selector
# ---------------------------------------------------------------------------

_CODING_SCOPE_NOTE = (
    "Coding scopes always use coding_strict profile. "
    "business_light cannot override coding governance."
)
_DEFAULT_NOTE = "Default coding_strict profile applied."
_BUSINESS_LIGHT_NOTE = "business_light profile selected for non-coding scope."


@dataclass(frozen=True)
class ProfileSelector:
    """Core governance profile selection engine.

    Coding scopes (CODING_WORKTREE) always receive CODING_STRICT.
    Business scopes (BUSINESS_FOLDER) may select BUSINESS_LIGHT.
    """

    def select(
        self,
        scope: FolderScope,
        requested: Optional[GovernanceProfileEnum] = None,
        selection_id: str = "",
    ) -> ProfileSelection:
        """Select the effective governance profile for a scope.

        Coding scopes always receive CODING_STRICT, regardless of requested.
        """
        if scope.is_coding_scope():
            return ProfileSelection(
                profile=GovernanceProfileEnum.CODING_STRICT,
                scope_type=scope.scope_type,
                selection_id=selection_id,
                note=_CODING_SCOPE_NOTE if requested == GovernanceProfileEnum.BUSINESS_LIGHT
                     else _DEFAULT_NOTE,
            )
        if requested == GovernanceProfileEnum.BUSINESS_LIGHT:
            return ProfileSelection(
                profile=GovernanceProfileEnum.BUSINESS_LIGHT,
                scope_type=scope.scope_type,
                selection_id=selection_id,
                note=_BUSINESS_LIGHT_NOTE,
            )
        return ProfileSelection(
            profile=GovernanceProfileEnum.CODING_STRICT,
            scope_type=scope.scope_type,
            selection_id=selection_id,
            note=_DEFAULT_NOTE,
        )

    def is_overrideable(self, scope: FolderScope) -> bool:
        """True only for non-coding scopes (business_light can be requested)."""
        return scope.is_business_scope()

    def default_profile(self) -> GovernanceProfileEnum:
        """Return the default governance profile."""
        return GovernanceProfileEnum.CODING_STRICT


# ---------------------------------------------------------------------------
# Stateless helpers
# ---------------------------------------------------------------------------

def select_profile(
    scope: FolderScope,
    requested: Optional[GovernanceProfileEnum] = None,
    selection_id: str = "",
) -> ProfileSelection:
    """Stateless helper: select the effective profile for a scope."""
    return ProfileSelector().select(scope, requested=requested,
                                    selection_id=selection_id)


def build_visibility(
    selection: ProfileSelection,
    open_items: Optional[List] = None,
    note: str = "",
) -> ProfileVisibility:
    """Build an operator-visible surface for a profile selection."""
    return ProfileVisibility(
        selection=selection,
        open_items=tuple(open_items) if open_items else (),
        note=note,
    )


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def coding_strict_selection(
    scope: FolderScope,
    selection_id: str = "",
) -> ProfileSelection:
    """Return a CODING_STRICT ProfileSelection for the given scope."""
    return ProfileSelection(
        profile=GovernanceProfileEnum.CODING_STRICT,
        scope_type=scope.scope_type,
        selection_id=selection_id,
        note=_DEFAULT_NOTE,
    )


def business_light_selection(
    scope: FolderScope,
    selection_id: str = "",
) -> ProfileSelection:
    """Return a BUSINESS_LIGHT ProfileSelection for a non-coding scope.

    Raises ValueError if scope is a coding worktree — coding scopes cannot
    receive business_light governance.
    """
    if scope.is_coding_scope():
        raise ValueError(
            f"Cannot assign business_light profile to coding worktree scope: "
            f"{scope.root!r}. Coding scopes always use coding_strict."
        )
    return ProfileSelection(
        profile=GovernanceProfileEnum.BUSINESS_LIGHT,
        scope_type=scope.scope_type,
        selection_id=selection_id,
        note=_BUSINESS_LIGHT_NOTE,
    )
