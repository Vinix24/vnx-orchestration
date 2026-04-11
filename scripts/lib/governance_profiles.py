#!/usr/bin/env python3
"""Config-driven governance profile system.

Replaces hardcoded "business" / "coding" distinctions with user-configurable
profiles loaded from .vnx/governance_profiles.yaml.

Components:
  GovernanceProfile   — dataclass describing a named profile
  DEFAULT_PROFILES    — built-in profiles always available
  load_profiles()     — merge YAML overrides with defaults
  resolve_profile()   — pick the profile for a given file path
  load_scope_config() — load glob→profile mapping from YAML

Config file (.vnx/governance_profiles.yaml):
  profiles:
    default:
      review_mode: full
      required_gates: [codex_gate, gemini_review, ci]
      max_pr_lines: 300
      auto_merge: false
    light:
      review_mode: exception_only
      required_gates: [ci]
      max_pr_lines: 500
      auto_merge: false
  scopes:
    "agents/*": light
    "scripts/": default
    "*": default

Design invariants:
  - DEFAULT_PROFILES are always present; YAML can add or override.
  - resolve_profile() evaluates glob patterns in declaration order.
  - A "*" catch-all in scopes is the fallback; if absent, "default" is used.
  - No filesystem I/O in GovernanceProfile itself.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Profile dataclass
# ---------------------------------------------------------------------------

@dataclass
class GovernanceProfile:
    """Describes a governance profile.

    Attributes:
        name:           Profile name (e.g. "default", "light", "minimal").
        review_mode:    "full" | "exception_only" | "none".
        required_gates: Ordered list of gates that must pass (e.g. ["codex_gate", "ci"]).
        max_pr_lines:   Maximum allowed PR line count for this profile.
        auto_merge:     Whether the PR may be auto-merged when gates pass.
    """
    name: str
    review_mode: str
    required_gates: list[str] = field(default_factory=list)
    max_pr_lines: int = 300
    auto_merge: bool = False

    def requires_gate(self, gate_name: str) -> bool:
        """True if gate_name is in required_gates."""
        return gate_name in self.required_gates

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "review_mode": self.review_mode,
            "required_gates": list(self.required_gates),
            "max_pr_lines": self.max_pr_lines,
            "auto_merge": self.auto_merge,
        }


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

DEFAULT_PROFILES: dict[str, GovernanceProfile] = {
    "default": GovernanceProfile(
        name="default",
        review_mode="full",
        required_gates=["codex_gate", "gemini_review", "ci"],
        max_pr_lines=300,
        auto_merge=False,
    ),
    "light": GovernanceProfile(
        name="light",
        review_mode="exception_only",
        required_gates=["ci"],
        max_pr_lines=500,
        auto_merge=False,
    ),
    "minimal": GovernanceProfile(
        name="minimal",
        review_mode="none",
        required_gates=[],
        max_pr_lines=1000,
        auto_merge=True,
    ),
}


# ---------------------------------------------------------------------------
# Config loader helpers
# ---------------------------------------------------------------------------

def _find_vnx_dir(project_root: Optional[Path]) -> Optional[Path]:
    """Locate the .vnx/ directory relative to project_root or cwd."""
    if project_root is not None:
        candidate = project_root / ".vnx"
        if candidate.is_dir():
            return candidate
        return None
    # Walk up from cwd
    start = Path.cwd()
    for parent in [start] + list(start.parents):
        candidate = parent / ".vnx"
        if candidate.is_dir():
            return candidate
    return None


def _load_yaml_simple(path: Path) -> dict:
    """Parse a minimal subset of YAML using the stdlib (no PyYAML required).

    Supports:
      - Top-level mappings
      - Nested mappings (2-space or 4-space indent)
      - Inline lists: [a, b, c]
      - Boolean values: true/false
      - Integer values
      - Quoted and unquoted string values

    Not supported: multi-line values, anchors, complex YAML features.
    Falls back to PyYAML if available.
    """
    try:
        import yaml  # type: ignore
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except ImportError:
        pass

    # Minimal built-in parser
    result: dict = {}
    stack: list[tuple[int, dict]] = [(-1, result)]

    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.rstrip()
            if not line or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip())
            content = line.strip()

            # Pop stack to correct depth
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()

            parent = stack[-1][1]

            if ":" in content:
                key_part, _, val_part = content.partition(":")
                key = key_part.strip().strip('"').strip("'")
                val = val_part.strip()

                if val == "" or val.startswith("#"):
                    # Nested mapping follows
                    child: dict = {}
                    parent[key] = child
                    stack.append((indent, child))
                else:
                    val = val.split("#")[0].strip()
                    if val.startswith("[") and val.endswith("]"):
                        inner = val[1:-1]
                        items = [
                            v.strip().strip('"').strip("'")
                            for v in inner.split(",")
                            if v.strip()
                        ]
                        parent[key] = items
                    elif val.lower() == "true":
                        parent[key] = True
                    elif val.lower() == "false":
                        parent[key] = False
                    else:
                        try:
                            parent[key] = int(val)
                        except ValueError:
                            parent[key] = val.strip('"').strip("'")

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_profiles(project_root: Optional[Path] = None) -> dict[str, GovernanceProfile]:
    """Load governance profiles from .vnx/governance_profiles.yaml.

    Merges YAML definitions with DEFAULT_PROFILES. YAML entries override
    defaults; defaults not mentioned in YAML are preserved.

    Args:
        project_root: Optional path to the project root containing .vnx/.
                      When None, walks up from cwd to locate .vnx/.

    Returns:
        Dict mapping profile name to GovernanceProfile.
    """
    profiles = {name: GovernanceProfile(**p.to_dict()) for name, p in DEFAULT_PROFILES.items()}

    vnx_dir = _find_vnx_dir(project_root)
    if vnx_dir is None:
        return profiles

    config_path = vnx_dir / "governance_profiles.yaml"
    if not config_path.is_file():
        return profiles

    try:
        raw = _load_yaml_simple(config_path)
    except Exception:
        return profiles

    yaml_profiles = raw.get("profiles") or {}
    for name, attrs in yaml_profiles.items():
        if not isinstance(attrs, dict):
            continue
        profiles[name] = GovernanceProfile(
            name=name,
            review_mode=attrs.get("review_mode", "full"),
            required_gates=list(attrs.get("required_gates") or []),
            max_pr_lines=int(attrs.get("max_pr_lines", 300)),
            auto_merge=bool(attrs.get("auto_merge", False)),
        )

    return profiles


def load_scope_config(project_root: Optional[Path] = None) -> dict[str, str]:
    """Load glob-pattern → profile-name mapping from .vnx/governance_profiles.yaml.

    Args:
        project_root: Optional path to the project root.

    Returns:
        Ordered dict of {glob_pattern: profile_name}.
        An empty dict means no scope configuration was found.
    """
    vnx_dir = _find_vnx_dir(project_root)
    if vnx_dir is None:
        return {}

    config_path = vnx_dir / "governance_profiles.yaml"
    if not config_path.is_file():
        return {}

    try:
        raw = _load_yaml_simple(config_path)
    except Exception:
        return {}

    scopes = raw.get("scopes") or {}
    return {str(k): str(v) for k, v in scopes.items()}


def resolve_profile(
    path: str,
    scope_config: Optional[dict[str, str]] = None,
    project_root: Optional[Path] = None,
) -> GovernanceProfile:
    """Resolve which governance profile applies to a given file/folder path.

    Patterns are evaluated in declaration order; first match wins.  A bare
    "*" catch-all acts as the fallback.  If no pattern matches, the "default"
    profile is returned.

    Args:
        path:         File or folder path to resolve (can be relative or absolute).
        scope_config: Optional pre-loaded {glob_pattern: profile_name} mapping.
                      When None, load_scope_config() is called automatically.
        project_root: Passed to load_scope_config() when scope_config is None.

    Returns:
        The matching GovernanceProfile.
    """
    if scope_config is None:
        scope_config = load_scope_config(project_root)

    profiles = load_profiles(project_root)

    # Normalise the path for matching
    norm_path = path.replace(os.sep, "/").lstrip("/")

    for pattern, profile_name in scope_config.items():
        norm_pattern = pattern.replace(os.sep, "/").lstrip("/")
        if fnmatch.fnmatch(norm_path, norm_pattern) or fnmatch.fnmatch(
            norm_path, norm_pattern.rstrip("/") + "/*"
        ):
            return profiles.get(profile_name, profiles["default"])

    return profiles.get("default", DEFAULT_PROFILES["default"])
