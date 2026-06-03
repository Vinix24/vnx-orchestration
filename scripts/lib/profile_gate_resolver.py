#!/usr/bin/env python3
"""profile_gate_resolver.py — Resolve the review gate stack from governance profiles.

Activated by VNX_PROFILE_SELECTOR=1 (default off, reversible).  Consults
governance_profiles.yaml scope mappings to resolve the most restrictive
profile across all changed files, then returns that profile's required_gates
as the effective review gate stack.

"Most restrictive" is the profile with the most required gates (longest
required_gates list).  Any file mapping to "default" (e.g. scripts/ or
dashboard/) escalates the whole dispatch to the full gate stack.

Safety invariant: scripts/ and dashboard/ always map to "default" in
governance_profiles.yaml, so this resolver can never weaken gates for those
paths.  On any failure (missing YAML, import error) it returns None so the
caller falls back to DEFAULT_REVIEW_STACK unchanged.

BILLING SAFETY: No Anthropic SDK. No network calls.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Map gate names from governance_profiles.yaml to dispatch gate identifiers.
# The YAML uses "ci" while the dispatch system calls it "ci_gate".
_GATE_NAME_ALIASES: Dict[str, str] = {"ci": "ci_gate"}


def _normalize_gate_name(name: str) -> str:
    return _GATE_NAME_ALIASES.get(name, name)


def resolve_gate_stack(
    changed_files: List[str],
    project_root: Optional[Path] = None,
) -> Optional[List[str]]:
    """Resolve required_gates for the most-restrictive profile across changed_files.

    Returns None when:
      - VNX_PROFILE_SELECTOR != "1" (flag off — caller uses DEFAULT_REVIEW_STACK)
      - changed_files is empty (no scope to resolve)
      - any error occurs (YAML missing, import failure, etc.)

    Returns List[str] of normalized gate names when the flag is on and files
    are provided.  An empty list means the resolved profile has no required
    gates (e.g. "minimal").

    Args:
        changed_files:  Normalized list of changed file paths (relative or absolute).
        project_root:   Optional project root for locating .vnx/governance_profiles.yaml.
                        When None, governance_profiles walks up from cwd.

    Returns:
        List[str] of gate names, or None when disabled/empty/failed.
    """
    if os.environ.get("VNX_PROFILE_SELECTOR", "0") != "1":
        return None

    if not changed_files:
        return None

    _lib_dir = Path(__file__).resolve().parent
    if str(_lib_dir) not in sys.path:
        sys.path.insert(0, str(_lib_dir))

    try:
        from governance_profiles import load_scope_config, resolve_profile  # noqa: PLC0415

        scope_config = load_scope_config(project_root)
        most_restrictive = None

        for fpath in changed_files:
            profile = resolve_profile(
                fpath, scope_config=scope_config, project_root=project_root,
            )
            if (
                most_restrictive is None
                or len(profile.required_gates) > len(most_restrictive.required_gates)
            ):
                most_restrictive = profile

        if most_restrictive is None:
            return None

        return [_normalize_gate_name(g) for g in most_restrictive.required_gates]

    except Exception:
        return None
