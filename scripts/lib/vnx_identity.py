#!/usr/bin/env python3
"""VNX Identity Layer — four-tuple {operator, project, orchestrator, agent} resolution.

Phase 6 P2 of the single-system migration. Provides the canonical identity
that callers stamp onto receipts, dispatch register events, worker subprocess
environments, and downstream observability surfaces.

Resolution order::

    1. Environment variables
       VNX_OPERATOR_ID, VNX_PROJECT_ID, VNX_ORCHESTRATOR_ID, VNX_AGENT_ID
    2. Per-repo file ``.vnx-project-id`` at the git root (3 lines)
       project_id, orchestrator_id, agent_id  (last two may be blank)
    3. Per-operator registry ``~/.vnx/projects.json`` (schema_version 2)
       Looked up by current working directory falling under a registered path.
    4. Otherwise raise ``RuntimeError("identity resolution failed")``.

ID format: ``^[a-z][a-z0-9-]{1,31}$``. The reserved id ``_unknown`` is
accepted for migration-only attribution (legacy receipts that pre-date this
layer); regular code MUST NOT mint ``_unknown`` identities.

This module imports nothing project-specific so it can be loaded from any
script (``append_receipt.py``, ``subprocess_dispatch.py``, the migration
scanner) without setting up the full vnx_paths bootstrap chain.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

ID_REGEX = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
RESERVED_UNKNOWN = "_unknown"

ENV_OPERATOR = "VNX_OPERATOR_ID"
ENV_PROJECT = "VNX_PROJECT_ID"
ENV_ORCHESTRATOR = "VNX_ORCHESTRATOR_ID"
ENV_AGENT = "VNX_AGENT_ID"

PROJECT_FILE_NAME = ".vnx-project-id"
REGISTRY_PATH = Path("~/.vnx/projects.json").expanduser()
REGISTRY_SCHEMA_VERSION = 2


class IdentityError(RuntimeError):
    """Raised when an id violates the allowlist regex."""


@dataclass(frozen=True)
class VnxIdentity:
    """Four-tuple identity for a VNX runtime.

    operator_id and project_id are required. orchestrator_id and agent_id
    may be ``None`` when the caller has no orchestrator (e.g. a CLI tool)
    or no worker agent (e.g. an orchestrator process that has not yet
    spawned a subprocess).
    """

    operator_id: str
    project_id: str
    orchestrator_id: Optional[str] = None
    agent_id: Optional[str] = None

    def to_env(self) -> Dict[str, str]:
        """Render the identity as environment variables for child processes."""
        env: Dict[str, str] = {
            ENV_OPERATOR: self.operator_id,
            ENV_PROJECT: self.project_id,
        }
        if self.orchestrator_id:
            env[ENV_ORCHESTRATOR] = self.orchestrator_id
        if self.agent_id:
            env[ENV_AGENT] = self.agent_id
        return env

    def to_dict(self) -> Dict[str, Optional[str]]:
        """Render the identity as a dict suitable for JSON receipt fields."""
        return asdict(self)


def validate_id(value: str, *, allow_unknown: bool = False) -> str:
    """Validate an id against the allowlist regex; return the id unchanged.

    ``allow_unknown=True`` permits the reserved ``_unknown`` literal, which is
    used only by the Phase 4 migration backfill — production code paths must
    leave ``allow_unknown`` False so unattributable rows fail loudly.
    """
    if allow_unknown and value == RESERVED_UNKNOWN:
        return value
    if not isinstance(value, str) or not ID_REGEX.match(value):
        raise IdentityError(
            f"invalid VNX id {value!r}: must match {ID_REGEX.pattern}"
        )
    return value


def _read_project_file(path: Path) -> Optional[Dict[str, str]]:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    project_id = lines[0].strip() if len(lines) > 0 else ""
    orchestrator_id = lines[1].strip() if len(lines) > 1 else ""
    agent_id = lines[2].strip() if len(lines) > 2 else ""
    if not project_id:
        return None
    return {
        "project_id": project_id,
        "orchestrator_id": orchestrator_id,
        "agent_id": agent_id,
    }


def _find_project_file(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for ``.vnx-project-id``."""
    current = start.resolve()
    for ancestor in [current, *current.parents]:
        candidate = ancestor / PROJECT_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


def _load_registry(path: Path = REGISTRY_PATH) -> Optional[Dict]:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _registry_lookup(
    registry: Dict, cwd: Path
) -> Optional[Dict[str, Optional[str]]]:
    """Resolve identity from a v2 registry given a current working directory."""
    if int(registry.get("schema_version", 1)) < REGISTRY_SCHEMA_VERSION:
        return None
    operator_id = registry.get("operator_id")
    if not operator_id:
        return None
    cwd_resolved = cwd.resolve()
    for entry in registry.get("projects", []) or []:
        project_path = entry.get("path")
        if not project_path:
            continue
        try:
            entry_path = Path(project_path).expanduser().resolve()
        except OSError:
            continue
        try:
            cwd_resolved.relative_to(entry_path)
        except ValueError:
            continue
        return {
            "operator_id": operator_id,
            "project_id": entry.get("project_id") or "",
            "orchestrator_id": (entry.get("agents") or {}).get("orchestrator_id"),
            "agent_id": (entry.get("agents") or {}).get("agent_id"),
        }
    return None


def _from_env() -> Dict[str, Optional[str]]:
    return {
        "operator_id": os.environ.get(ENV_OPERATOR) or None,
        "project_id": os.environ.get(ENV_PROJECT) or None,
        "orchestrator_id": os.environ.get(ENV_ORCHESTRATOR) or None,
        "agent_id": os.environ.get(ENV_AGENT) or None,
    }


def resolve_identity(
    *,
    cwd: Optional[Path] = None,
    registry_path: Optional[Path] = None,
    allow_unknown: bool = False,
) -> VnxIdentity:
    """Resolve the four-tuple identity using the canonical chain.

    ``cwd`` defaults to the current working directory; tests pass an isolated
    temp dir.  ``registry_path`` defaults to ``~/.vnx/projects.json`` and is
    likewise overridable.

    Raises ``RuntimeError("identity resolution failed")`` when neither env,
    project file, nor registry yields an operator+project pair.
    """
    cwd = (cwd or Path.cwd()).resolve()
    registry_path = registry_path or REGISTRY_PATH

    resolved: Dict[str, Optional[str]] = _from_env()

    if not resolved.get("project_id"):
        project_file = _find_project_file(cwd)
        if project_file is not None:
            file_data = _read_project_file(project_file)
            if file_data:
                resolved.setdefault("project_id", None)
                resolved["project_id"] = resolved.get("project_id") or file_data["project_id"]
                if not resolved.get("orchestrator_id") and file_data.get("orchestrator_id"):
                    resolved["orchestrator_id"] = file_data["orchestrator_id"]
                if not resolved.get("agent_id") and file_data.get("agent_id"):
                    resolved["agent_id"] = file_data["agent_id"]

    if not resolved.get("operator_id") or not resolved.get("project_id"):
        registry = _load_registry(registry_path)
        if registry is not None:
            entry = _registry_lookup(registry, cwd)
            if entry:
                resolved["operator_id"] = resolved.get("operator_id") or entry.get("operator_id")
                resolved["project_id"] = resolved.get("project_id") or entry.get("project_id")
                if not resolved.get("orchestrator_id") and entry.get("orchestrator_id"):
                    resolved["orchestrator_id"] = entry["orchestrator_id"]
                if not resolved.get("agent_id") and entry.get("agent_id"):
                    resolved["agent_id"] = entry["agent_id"]

    operator_id = resolved.get("operator_id")
    project_id = resolved.get("project_id")
    if not operator_id or not project_id:
        raise RuntimeError("identity resolution failed")

    validate_id(operator_id, allow_unknown=allow_unknown)
    validate_id(project_id, allow_unknown=allow_unknown)
    if resolved.get("orchestrator_id"):
        validate_id(resolved["orchestrator_id"], allow_unknown=allow_unknown)
    if resolved.get("agent_id"):
        validate_id(resolved["agent_id"], allow_unknown=allow_unknown)

    return VnxIdentity(
        operator_id=operator_id,
        project_id=project_id,
        orchestrator_id=resolved.get("orchestrator_id") or None,
        agent_id=resolved.get("agent_id") or None,
    )


def try_resolve_identity(**kwargs) -> Optional[VnxIdentity]:
    """Best-effort variant: returns ``None`` instead of raising.

    Use this on hot paths (receipt write, dispatch register) where an
    unresolvable identity must NOT break the underlying durability contract.
    The caller is expected to log a warning and continue with absent
    identity fields.
    """
    try:
        return resolve_identity(**kwargs)
    except (RuntimeError, IdentityError):
        return None


__all__ = [
    "ENV_AGENT",
    "ENV_OPERATOR",
    "ENV_ORCHESTRATOR",
    "ENV_PROJECT",
    "ID_REGEX",
    "IdentityError",
    "PROJECT_FILE_NAME",
    "REGISTRY_PATH",
    "REGISTRY_SCHEMA_VERSION",
    "RESERVED_UNKNOWN",
    "VnxIdentity",
    "resolve_identity",
    "try_resolve_identity",
    "validate_id",
]
