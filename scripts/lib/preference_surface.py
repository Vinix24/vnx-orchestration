#!/usr/bin/env python3
"""Operator dashboard surface for governance profile preference snapshots.

Provides read-only aggregated views of the preference store, organized by
governance profile, for operator dashboards and monitoring integrations.

Components:
  ProfileSurface          — frozen dataclass with aggregate counts and metadata
  build_profile_surface() — builds a snapshot for one profile
  format_surface_line()   — returns a single-line operator-readable summary
  build_all_surfaces()    — builds snapshots for all KNOWN_PROFILES

Design invariants:
  - Surface builds never mutate the store.
  - Retired entries are counted in total_entries but not active_entries.
  - Unknown profiles raise ValueError.
  - domains is a sorted tuple of distinct domain strings present in the store.
  - ProfileSurface is immutable after construction (frozen dataclass).

Usage:
    store = preference_store()
    record_entry(store, ScopeKey("regulated_strict", "approval"), ...)

    surface = build_profile_surface(store, "regulated_strict")
    print(format_surface_line(surface))
    # [surface] regulated_strict | active=1 retired=0 | lessons=0 prefs=1 | domains=['approval']

    all_surfaces = build_all_surfaces(store)
    for profile, s in all_surfaces.items():
        print(format_surface_line(s))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from preference_store import (
    KNOWN_PROFILES,
    EntryKind,
    PreferenceStore,
    ScopeKey,
)


# ---------------------------------------------------------------------------
# ProfileSurface
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileSurface:
    """Immutable aggregate snapshot of a governance profile's preference data.

    Attributes:
        profile:          The governance profile this surface covers.
        total_entries:    All entries including retired.
        active_entries:   Non-retired entries.
        retired_entries:  Retired entries (total_entries - active_entries).
        lesson_count:     Number of active EntryKind.LESSON entries.
        preference_count: Number of active EntryKind.PREFERENCE entries.
        domains:          Sorted tuple of distinct domain strings with at least
                          one entry (active or retired) in this profile.
    """
    profile: str
    total_entries: int
    active_entries: int
    retired_entries: int
    lesson_count: int
    preference_count: int
    domains: tuple

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of this surface."""
        return {
            "profile": self.profile,
            "total_entries": self.total_entries,
            "active_entries": self.active_entries,
            "retired_entries": self.retired_entries,
            "lesson_count": self.lesson_count,
            "preference_count": self.preference_count,
            "domains": list(self.domains),
        }


# ---------------------------------------------------------------------------
# build_profile_surface
# ---------------------------------------------------------------------------

def build_profile_surface(store: PreferenceStore, profile: str) -> ProfileSurface:
    """Build a ProfileSurface snapshot for the given governance profile.

    Queries the store for all entries in the profile (active and retired),
    and aggregates counts. The store is never mutated.

    Args:
        store:   The PreferenceStore to read from.
        profile: The governance profile to snapshot.

    Returns:
        A frozen ProfileSurface with aggregate counts.

    Raises:
        ValueError: If profile is not in KNOWN_PROFILES.
    """
    if profile not in KNOWN_PROFILES:
        raise ValueError(
            f"Unknown governance profile {profile!r}. "
            f"Known profiles: {sorted(KNOWN_PROFILES)}"
        )

    # Collect distinct domains that have any entry (active or retired)
    # for this profile by inspecting all_scopes.
    profile_domains: set = set()
    for scope_key in store.all_scopes():
        if scope_key.profile == profile:
            profile_domains.add(scope_key.domain)

    # Build aggregate counts by iterating over each domain.
    total_entries = 0
    active_entries = 0
    lesson_count = 0
    preference_count = 0

    for domain in profile_domains:
        scope_key = ScopeKey(profile=profile, domain=domain)

        # Active entries (non-retired)
        active = store.query(scope_key, include_retired=False)
        active_entries += len(active)
        for entry in active:
            if entry.kind == EntryKind.LESSON:
                lesson_count += 1
            else:
                preference_count += 1

        # All entries including retired
        all_in_scope = store.query(scope_key, include_retired=True)
        total_entries += len(all_in_scope)

    retired_entries = total_entries - active_entries
    sorted_domains = tuple(sorted(profile_domains))

    return ProfileSurface(
        profile=profile,
        total_entries=total_entries,
        active_entries=active_entries,
        retired_entries=retired_entries,
        lesson_count=lesson_count,
        preference_count=preference_count,
        domains=sorted_domains,
    )


# ---------------------------------------------------------------------------
# format_surface_line
# ---------------------------------------------------------------------------

def format_surface_line(surface: ProfileSurface) -> str:
    """Return a single-line operator-readable summary of a ProfileSurface.

    Format:
        [surface] regulated_strict | active=5 retired=1 | lessons=2 prefs=3 | domains=['approval','gate']

    Args:
        surface: The ProfileSurface to format.

    Returns:
        A single-line summary string.
    """
    domains_list = list(surface.domains)
    return (
        f"[surface] {surface.profile} | "
        f"active={surface.active_entries} retired={surface.retired_entries} | "
        f"lessons={surface.lesson_count} prefs={surface.preference_count} | "
        f"domains={domains_list!r}"
    )


# ---------------------------------------------------------------------------
# build_all_surfaces
# ---------------------------------------------------------------------------

def build_all_surfaces(store: PreferenceStore) -> Dict[str, ProfileSurface]:
    """Build ProfileSurface snapshots for all known governance profiles.

    Args:
        store: The PreferenceStore to read from.

    Returns:
        Dict mapping each profile name to its ProfileSurface.
    """
    return {
        profile: build_profile_surface(store, profile)
        for profile in sorted(KNOWN_PROFILES)
    }
