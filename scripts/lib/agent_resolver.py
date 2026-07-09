#!/usr/bin/env python3
"""Agent-folder resolver (ADR-028 Phase 1).

Resolves an agent by name from the local ``agents/`` tree, project ``examples/``,
or the packaged engine ``examples/`` fallback. Parses the extended ``config.yaml``
schema into a typed, backward-compatible result.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

VNX_AGENT_FOLDERS_ENV = "VNX_AGENT_FOLDERS"


@dataclass(frozen=True)
class AgentConfig:
    """Typed view of an agent folder's config.yaml with safe defaults."""

    name: str
    claude_md: Path
    provider: str = "claude"
    model: str | None = None
    governance_profile: str = "default"
    default_instruction: str | None = None
    isolation: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)


def agent_folders_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True unless ``VNX_AGENT_FOLDERS=0`` is set (default: ON)."""
    env = env if env is not None else os.environ
    return env.get(VNX_AGENT_FOLDERS_ENV, "1") != "0"


def _resolve_agent_claude_md(
    name: str,
    project_dir: Path,
    engine_root: Path | None = None,
) -> Path | None:
    """Find ``<name>/CLAUDE.md`` using project agents/, examples/, then engine examples/."""
    candidates = [
        project_dir / "agents" / name / "CLAUDE.md",
        project_dir / "examples" / name / "CLAUDE.md",
    ]
    if engine_root is not None:
        candidates.append(engine_root / "examples" / name / "CLAUDE.md")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return str(value).strip() or None


def _coerce_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _coerce_pattern_list(value: Any) -> list[str]:
    """Preserve regex whitespace, unlike ``_coerce_str_list``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _normalize_permissions(raw: Any) -> dict[str, Any]:
    """Return a permissions dict with predictable keys and list values."""
    if not isinstance(raw, dict):
        return {
            "allowed_tools": [],
            "denied_tools": [],
            "bash_allow_patterns": [],
            "bash_deny_patterns": [],
        }
    return {
        "allowed_tools": _coerce_str_list(raw.get("allowed_tools")),
        "denied_tools": _coerce_str_list(raw.get("denied_tools")),
        "bash_allow_patterns": _coerce_pattern_list(raw.get("bash_allow_patterns")),
        "bash_deny_patterns": _coerce_pattern_list(raw.get("bash_deny_patterns")),
    }


def resolve_agent(
    name: str,
    project_dir: Path | str,
    engine_root: Path | str | None = None,
) -> AgentConfig | None:
    """Resolve an agent folder and parse its extended ``config.yaml``.

    Returns ``None`` when no ``CLAUDE.md`` is found. Missing optional fields
    resolve to safe defaults so old configs keep working.
    """
    project_dir = Path(project_dir)
    engine_root = Path(engine_root) if engine_root is not None else None

    claude_md = _resolve_agent_claude_md(name, project_dir, engine_root)
    if claude_md is None:
        return None

    config_path = claude_md.parent / "config.yaml"
    raw: dict[str, Any] = {}
    try:
        if config_path.is_file():
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
    except (OSError, yaml.YAMLError):
        raw = {}

    return AgentConfig(
        name=name,
        claude_md=claude_md,
        provider=_coerce_str(raw.get("provider")) or "claude",
        model=_coerce_str(raw.get("model")),
        governance_profile=_coerce_str(raw.get("governance_profile")) or "default",
        default_instruction=_coerce_str(raw.get("default_instruction")),
        isolation=raw.get("isolation") if isinstance(raw.get("isolation"), dict) else {},
        permissions=_normalize_permissions(raw.get("permissions")),
        skills=_coerce_str_list(raw.get("skills")),
    )
