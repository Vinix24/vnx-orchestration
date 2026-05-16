#!/usr/bin/env python3
"""worker_registry.py — YAML-driven worker registry.

Replaces hardcoded CANONICAL_TERMINALS in runtime_facade.py.
Loads .vnx/vnx_workers.yaml (operator-edited), falling back to
.vnx/vnx_workers.default.yaml when no operator override exists.

Public API:
- WORKER_REGISTRY: module-level instance loaded at import time
- list_workers() -> List[Worker]
- by_id(terminal_id) -> Optional[Worker]
- by_role(role) -> List[Worker]
- aliases_for(terminal_id) -> List[str]
- by_pool(pool_id) -> List[Worker]
- resolve_alias(alias) -> Optional[Worker]

Validation:
- terminal_id matches r"^[A-Za-z][A-Za-z0-9]*[0-9]+$" or is a plain letter+digit combo
- role validated against validate_skill.py --list output
- provider in {claude, codex, gemini, litellm:<sub>}
- aliases unique across all workers
- pool_id references a pool defined in pools: section
- scaling_policy in {fixed, queue_aware}
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_VALID_SCALING_POLICIES = frozenset({"fixed", "queue_aware"})
_VALID_PROVIDERS_BASE = frozenset({"claude", "codex", "gemini"})
# System roles that are valid but not listed in validate_skill.py (non-worker roles).
_SYSTEM_ROLES = frozenset({"orchestrator"})

# Hardcoded fallback — mirrors default.yaml, prevents import error during install.
_HARDCODED_FALLBACK = {
    "schema_version": 1,
    "workers": [
        {"terminal_id": "T0", "role": "orchestrator", "provider": "claude",
         "model": "opus", "pool_id": "default", "aliases": []},
        {"terminal_id": "T1", "role": "backend-developer", "provider": "claude",
         "model": "sonnet", "pool_id": "default", "aliases": []},
        {"terminal_id": "T2", "role": "backend-developer", "provider": "claude",
         "model": "sonnet", "pool_id": "default", "aliases": []},
        {"terminal_id": "T3", "role": "reviewer", "provider": "claude",
         "model": "sonnet", "pool_id": "default", "aliases": []},
    ],
    "pools": [
        {"pool_id": "default", "min_workers": 4, "max_workers": 4,
         "scaling_policy": "fixed", "provider_mix": []},
    ],
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Worker:
    terminal_id: str
    role: str
    provider: str
    model: str
    pool_id: str
    aliases: tuple

    def __init__(self, terminal_id: str, role: str, provider: str,
                 model: str, pool_id: str, aliases: list) -> None:
        object.__setattr__(self, "terminal_id", terminal_id)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "pool_id", pool_id)
        object.__setattr__(self, "aliases", tuple(aliases))


@dataclass(frozen=True)
class Pool:
    pool_id: str
    min_workers: int
    max_workers: int
    scaling_policy: str
    provider_mix: tuple

    def __init__(self, pool_id: str, min_workers: int, max_workers: int,
                 scaling_policy: str, provider_mix: list) -> None:
        object.__setattr__(self, "pool_id", pool_id)
        object.__setattr__(self, "min_workers", min_workers)
        object.__setattr__(self, "max_workers", max_workers)
        object.__setattr__(self, "scaling_policy", scaling_policy)
        object.__setattr__(self, "provider_mix", tuple(provider_mix))


# ---------------------------------------------------------------------------
# Role validation — cached once at module import
# ---------------------------------------------------------------------------

def _fetch_valid_roles(repo_root: Path) -> frozenset:
    """Run validate_skill.py --list and parse valid role names."""
    script = repo_root / "scripts" / "validate_skill.py"
    if not script.is_file():
        logger.warning("validate_skill.py not found at %s; skipping role validation", script)
        return frozenset()
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--list"],
            capture_output=True, text=True, timeout=10,
        )
        roles: set = set()
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("@"):
                name = stripped.lstrip("@").split()[0]
                roles.add(name)
        return frozenset(roles)
    except Exception as exc:
        logger.warning("Could not fetch valid roles: %s", exc)
        return frozenset()


# ---------------------------------------------------------------------------
# Provider validation
# ---------------------------------------------------------------------------

def _validate_provider(provider: str) -> None:
    if provider in _VALID_PROVIDERS_BASE:
        return
    if provider.startswith("litellm:") and len(provider) > len("litellm:"):
        return
    raise ValueError(
        f"Invalid provider {provider!r}. "
        f"Must be one of {sorted(_VALID_PROVIDERS_BASE)} or 'litellm:<sub>'."
    )


# ---------------------------------------------------------------------------
# WorkerRegistry
# ---------------------------------------------------------------------------

class WorkerRegistry:
    """Registry of workers and pools loaded from vnx_workers.yaml."""

    def __init__(self, workers: List[Worker], pools: List[Pool]) -> None:
        self._workers: List[Worker] = workers
        self._pools: Dict[str, Pool] = {p.pool_id: p for p in pools}
        self._by_id: Dict[str, Worker] = {w.terminal_id: w for w in workers}
        self._by_alias: Dict[str, Worker] = {}
        for w in workers:
            for alias in w.aliases:
                if alias in self._by_alias:
                    raise ValueError(
                        f"Duplicate alias {alias!r} — already claimed by "
                        f"{self._by_alias[alias].terminal_id}."
                    )
                self._by_alias[alias] = w

    def list_workers(self) -> List[Worker]:
        return list(self._workers)

    def by_id(self, terminal_id: str) -> Optional[Worker]:
        return self._by_id.get(terminal_id)

    def by_role(self, role: str) -> List[Worker]:
        return [w for w in self._workers if w.role == role]

    def aliases_for(self, terminal_id: str) -> List[str]:
        worker = self._by_id.get(terminal_id)
        return list(worker.aliases) if worker else []

    def by_pool(self, pool_id: str) -> List[Worker]:
        return [w for w in self._workers if w.pool_id == pool_id]

    def resolve_alias(self, alias: str) -> Optional[Worker]:
        return self._by_alias.get(alias)

    def pool(self, pool_id: str) -> Optional[Pool]:
        return self._pools.get(pool_id)

    def list_pools(self) -> List[Pool]:
        return list(self._pools.values())


# ---------------------------------------------------------------------------
# YAML file discovery
# ---------------------------------------------------------------------------

def _find_yaml_file(repo_root: Optional[Path] = None) -> Optional[Path]:
    """Search order: operator .vnx/vnx_workers.yaml, then .vnx/vnx_workers.default.yaml."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]
    operator_yaml = repo_root / ".vnx" / "vnx_workers.yaml"
    if operator_yaml.is_file():
        return operator_yaml
    default_yaml = repo_root / ".vnx" / "vnx_workers.default.yaml"
    if default_yaml.is_file():
        return default_yaml
    return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_terminal_id(terminal_id: str) -> None:
    if not _TERMINAL_ID_RE.match(terminal_id):
        raise ValueError(
            f"Invalid terminal_id {terminal_id!r}. "
            "Must start with a letter, followed by alphanumeric chars."
        )


def _validate_role(role: str, valid_roles: frozenset) -> None:
    if role in _SYSTEM_ROLES:
        return
    if not valid_roles:
        return  # Skip validation when validate_skill.py unavailable
    if role not in valid_roles:
        raise ValueError(
            f"Invalid role {role!r}. Run 'python3 scripts/validate_skill.py --list' "
            "for the full list."
        )


def _validate_scaling_policy(policy: str) -> None:
    if policy not in _VALID_SCALING_POLICIES:
        raise ValueError(
            f"Invalid scaling_policy {policy!r}. "
            f"Must be one of {sorted(_VALID_SCALING_POLICIES)}."
        )


def _validate_pool_ref(worker: Worker, pool_ids: frozenset) -> None:
    if worker.pool_id not in pool_ids:
        raise ValueError(
            f"Worker {worker.terminal_id!r} references pool {worker.pool_id!r} "
            "which is not defined in pools:."
        )


# ---------------------------------------------------------------------------
# Load registry from data dict
# ---------------------------------------------------------------------------

def _build_registry(data: dict, valid_roles: frozenset) -> WorkerRegistry:
    """Parse, validate, and construct WorkerRegistry from raw YAML dict."""
    raw_workers = data.get("workers", [])
    raw_pools = data.get("pools", [])

    pools: List[Pool] = []
    for p in raw_pools:
        policy = p.get("scaling_policy", "fixed")
        _validate_scaling_policy(policy)
        pools.append(Pool(
            pool_id=p["pool_id"],
            min_workers=int(p.get("min_workers", 1)),
            max_workers=int(p.get("max_workers", 1)),
            scaling_policy=policy,
            provider_mix=list(p.get("provider_mix", [])),
        ))

    pool_ids = frozenset(p.pool_id for p in pools)
    workers: List[Worker] = []
    for w in raw_workers:
        tid = w["terminal_id"]
        role = w["role"]
        provider = w["provider"]
        _validate_terminal_id(tid)
        _validate_role(role, valid_roles)
        _validate_provider(provider)
        worker = Worker(
            terminal_id=tid,
            role=role,
            provider=provider,
            model=w.get("model", ""),
            pool_id=w.get("pool_id", "default"),
            aliases=list(w.get("aliases", [])),
        )
        _validate_pool_ref(worker, pool_ids)
        workers.append(worker)

    return WorkerRegistry(workers, pools)


# ---------------------------------------------------------------------------
# Module-level loader
# ---------------------------------------------------------------------------

def _load_registry(repo_root: Optional[Path] = None) -> WorkerRegistry:
    """Resolve YAML path, parse, validate, and return WorkerRegistry.

    Falls back to hardcoded defaults when .vnx/ is absent or no yaml found.
    """
    import yaml  # Deferred import — yaml guaranteed present but keeps startup fast

    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]

    valid_roles = _fetch_valid_roles(repo_root)
    yaml_path = _find_yaml_file(repo_root)

    if yaml_path is None:
        logger.warning(
            ".vnx/vnx_workers.yaml and .vnx/vnx_workers.default.yaml not found; "
            "using hardcoded fallback (T0-T3)."
        )
        return _build_registry(_HARDCODED_FALLBACK, valid_roles)

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return _build_registry(data, valid_roles)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

WORKER_REGISTRY: WorkerRegistry = _load_registry()


# ---------------------------------------------------------------------------
# Convenience module-level functions
# ---------------------------------------------------------------------------

def list_workers() -> List[Worker]:
    return WORKER_REGISTRY.list_workers()


def by_id(terminal_id: str) -> Optional[Worker]:
    return WORKER_REGISTRY.by_id(terminal_id)


def by_role(role: str) -> List[Worker]:
    return WORKER_REGISTRY.by_role(role)


def aliases_for(terminal_id: str) -> List[str]:
    return WORKER_REGISTRY.aliases_for(terminal_id)


def by_pool(pool_id: str) -> List[Worker]:
    return WORKER_REGISTRY.by_pool(pool_id)


def resolve_alias(alias: str) -> Optional[Worker]:
    return WORKER_REGISTRY.resolve_alias(alias)
