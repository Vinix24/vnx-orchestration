#!/usr/bin/env python3
"""Preference injection into dispatch context.

Provides profile-scoped preference injection for dispatch context assembly.
All injection is bounded to the requested governance profile — cross-profile
entries are never included regardless of domain overlap.

Components:
  InjectionContext       — frozen dataclass capturing what was injected
  PreferenceInjector     — wraps PreferenceStore and performs injection queries
  assert_injection_bounded() — safety guard for callers verifying bounded state

Design invariants:
  - Injection is ALWAYS profile-scoped (bounded=True by definition).
  - Retired entries are never injected.
  - Cross-profile entries are never returned even if domains overlap.
  - Unknown profiles raise ValueError at call time.
  - InjectionContext is immutable after construction (frozen dataclass).

Usage:
    store = preference_store()
    record_entry(store, ScopeKey("regulated_strict", "approval"), ...)

    injector = PreferenceInjector(store)
    ctx = injector.inject_for_dispatch(
        dispatch_id="d-001",
        profile="regulated_strict",
        domains=["approval", "gate"],
        include_lessons=True,
    )
    assert ctx.bounded is True
    assert_injection_bounded(ctx)  # safety guard
    print(injector.format_injection_summary(ctx))
    # [inject] d-001 profile=regulated_strict domains=['approval', 'gate'] entries=2 bounded=True
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from preference_store import (
    KNOWN_PROFILES,
    EntryKind,
    PreferenceEntry,
    PreferenceStore,
    ScopeKey,
)


# ---------------------------------------------------------------------------
# InjectionContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InjectionContext:
    """Immutable record of what was injected for a dispatch.

    Attributes:
        dispatch_id:      The dispatch identifier this injection was built for.
        profile:          The governance profile used to scope the query.
        scope_keys:       Tuple of ScopeKey objects that were queried.
        injected_entries: Tuple of PreferenceEntry objects that were injected.
        injection_count:  Number of injected entries (len(injected_entries)).
        bounded:          True when injection was limited to profile-scoped
                          entries only. Always True by design.
    """
    dispatch_id: str
    profile: str
    scope_keys: tuple
    injected_entries: tuple
    injection_count: int
    bounded: bool

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation of this context."""
        return {
            "dispatch_id": self.dispatch_id,
            "profile": self.profile,
            "scope_keys": [
                {"profile": sk.profile, "domain": sk.domain}
                for sk in self.scope_keys
            ],
            "injected_entries": [e.to_dict() for e in self.injected_entries],
            "injection_count": self.injection_count,
            "bounded": self.bounded,
        }


# ---------------------------------------------------------------------------
# PreferenceInjector
# ---------------------------------------------------------------------------

class PreferenceInjector:
    """Injects profile-scoped preferences and lessons into dispatch context.

    Wraps a PreferenceStore and exposes a dispatch-oriented injection API.
    All queries are bounded to the requested governance profile — entries
    from other profiles are never included.
    """

    def __init__(self, store: PreferenceStore) -> None:
        """Wrap a PreferenceStore for injection queries.

        Args:
            store: The PreferenceStore to source entries from.
        """
        self._store = store

    def inject_for_dispatch(
        self,
        dispatch_id: str,
        profile: str,
        domains: List[str],
        include_lessons: bool = True,
    ) -> InjectionContext:
        """Build an InjectionContext for the given dispatch.

        Queries the store for each domain in domains, filtered strictly to
        profile. Only non-retired entries are included. Entries from other
        profiles are never returned.

        Args:
            dispatch_id:     The dispatch identifier.
            profile:         Governance profile to scope the query.
            domains:         List of domain strings to query within the profile.
            include_lessons: When True (default), include EntryKind.LESSON
                             entries alongside EntryKind.PREFERENCE entries.
                             When False, only PREFERENCE entries are returned.

        Returns:
            InjectionContext with bounded=True.

        Raises:
            ValueError: If profile is not in KNOWN_PROFILES.
        """
        if profile not in KNOWN_PROFILES:
            raise ValueError(
                f"Unknown governance profile {profile!r}. "
                f"Known profiles: {sorted(KNOWN_PROFILES)}"
            )

        scope_keys = tuple(ScopeKey(profile=profile, domain=d) for d in domains)
        collected: List[PreferenceEntry] = []
        seen_ids: set = set()

        for scope_key in scope_keys:
            entries = self._store.query(scope_key, include_retired=False)
            for entry in entries:
                # Cross-profile guard: only accept entries matching the
                # requested profile (store.query already enforces scope,
                # but we are explicit here as a defense-in-depth measure).
                if entry.scope.profile != profile:
                    continue
                if not include_lessons and entry.kind == EntryKind.LESSON:
                    continue
                if entry.entry_id not in seen_ids:
                    seen_ids.add(entry.entry_id)
                    collected.append(entry)

        injected = tuple(collected)
        return InjectionContext(
            dispatch_id=dispatch_id,
            profile=profile,
            scope_keys=scope_keys,
            injected_entries=injected,
            injection_count=len(injected),
            bounded=True,
        )

    def format_injection_summary(self, context: InjectionContext) -> str:
        """Return a human-readable summary line for operator logs.

        Format:
            [inject] d-001 profile=regulated_strict domains=['approval','gate'] entries=3 bounded=True

        Args:
            context: The InjectionContext to summarize.

        Returns:
            A single-line summary string.
        """
        domains = [sk.domain for sk in context.scope_keys]
        return (
            f"[inject] {context.dispatch_id} "
            f"profile={context.profile} "
            f"domains={domains!r} "
            f"entries={context.injection_count} "
            f"bounded={context.bounded}"
        )


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

def assert_injection_bounded(context: InjectionContext) -> None:
    """Assert that the injection context is bounded (profile-scoped only).

    This is a safety check for callers that must verify injection did not
    include cross-profile entries. Since bounded=True is enforced by design,
    this guard catches any hypothetical future misuse.

    Args:
        context: The InjectionContext to check.

    Raises:
        ValueError: If context.bounded is False.
    """
    if not context.bounded:
        raise ValueError(
            f"Injection context for dispatch {context.dispatch_id!r} is not "
            f"bounded to profile {context.profile!r}. Cross-profile contamination "
            f"is a safety violation."
        )
