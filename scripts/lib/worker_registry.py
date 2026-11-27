#!/usr/bin/env python3
"""worker_registry.py — YAML-driven worker registry.

Replaces hardcoded CANONICAL_TERMINALS in runtime_facade.py.
Loads the project's .vnx/vnx_workers.yaml (operator-edited), falling back to
scripts/lib/defaults/vnx_workers.default.yaml from the installed package.

Public API:
- WORKER_REGISTRY: module-level instance loaded on first legacy access
- list_workers() -> List[Worker]
- by_id(terminal_id) -> Optional[Worker]
- by_role(role) -> List[Worker]
- aliases_for(terminal_id) -> List[str]
- by_pool(pool_id) -> List[Worker]
- resolve_alias(alias) -> Optional[Worker]

Validation:
- terminal_id matches r"^T[0-9]+$" (T followed by one or more digits)
- role validated against the fabric's validate_skill.py --list output
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
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

if __package__:
    from .project_root import resolve_project_root
else:
    from project_root import resolve_project_root

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TERMINAL_ID_RE = re.compile(r"^T[0-9]+$")
_VALID_SCALING_POLICIES = frozenset({"fixed", "queue_aware"})
_VALID_PROVIDERS_BASE = frozenset({"claude", "codex", "gemini", "kimi"})
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
    default_pair: Optional[tuple] = None

    def __init__(self, pool_id: str, min_workers: int, max_workers: int,
                 scaling_policy: str, provider_mix: list, default_pair=None) -> None:
        object.__setattr__(self, "pool_id", pool_id)
        object.__setattr__(self, "min_workers", min_workers)
        object.__setattr__(self, "max_workers", max_workers)
        object.__setattr__(self, "scaling_policy", scaling_policy)
        object.__setattr__(self, "provider_mix", tuple(provider_mix))
        object.__setattr__(self, "default_pair", default_pair)


# ---------------------------------------------------------------------------
# Role validation — cached once at module import
# ---------------------------------------------------------------------------

def _fetch_valid_roles(repo_root: Path) -> Optional[frozenset]:
    """Run validate_skill.py --list and parse valid role names.

    Returns:
        frozenset of role names — use for validation (may be empty).
        None — tool unavailable or failed; caller skips role validation.

    Never raises — import-time reliability over strict validation.
    """
    script = repo_root / "scripts" / "validate_skill.py"
    if not script.is_file():
        logger.warning("validate_skill.py not found at %s; skipping role validation", script)
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(script), "--list"],
            check=True,
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(
            "validate_skill.py failed (exit %d), role validation disabled. stderr: %s",
            e.returncode, (e.stderr or "")[:200],
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning("validate_skill.py timed out, role validation disabled")
        return None
    roles: set = set()
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("@"):
            name = stripped.lstrip("@").split()[0]
            roles.add(name)
    return frozenset(roles)


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
        seen_ids: set = set()
        for w in workers:
            if w.terminal_id in seen_ids:
                raise ValueError(f"duplicate terminal_id: {w.terminal_id!r}")
            seen_ids.add(w.terminal_id)
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
    """Find the project override, then the packaged default; root errors propagate."""
    if repo_root is None:
        repo_root = resolve_project_root()
    operator_yaml = repo_root / ".vnx" / "vnx_workers.yaml"
    if operator_yaml.is_file():
        return operator_yaml
    default_yaml = Path(__file__).resolve().parent / "defaults" / "vnx_workers.default.yaml"
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
            "Must match ^T[0-9]+$ (e.g. T0, T1, T2, T99)."
        )


def _validate_role(role: str, valid_roles: Optional[frozenset]) -> None:
    if role in _SYSTEM_ROLES:
        return
    if valid_roles is None:
        return  # Tool unavailable — skip validation
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

def _build_registry(data: dict, valid_roles: Optional[FrozenSet[str]] = None) -> WorkerRegistry:
    """Parse, validate, and construct WorkerRegistry from raw YAML dict."""
    raw_workers = data.get("workers", [])
    raw_pools = data.get("pools", [])

    pools: List[Pool] = []
    for p in raw_pools:
        policy = p.get("scaling_policy", "fixed")
        _validate_scaling_policy(policy)
        pair = p.get("default_pair")
        if "default_pair" in p:
            if not isinstance(pair, dict) or not all(pair.get(k) for k in ("provider", "model")):
                raise ValueError(f"Invalid default_pair for pool {p['pool_id']!r}")
            pair = _worker_pair(pair["provider"], pair["model"])
        pools.append(Pool(
            pool_id=p["pool_id"],
            min_workers=int(p.get("min_workers", 1)),
            max_workers=int(p.get("max_workers", 1)),
            scaling_policy=policy,
            provider_mix=list(p.get("provider_mix", [])),
            default_pair=pair,
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

    Falls back to hardcoded defaults only when neither YAML file exists.
    With no explicit root, project-root resolution errors propagate.
    The default validator root comes from this module, independent of the
    project YAML root. An explicit repo_root binds both roots for test callers.
    """
    import yaml  # Deferred import — yaml guaranteed present but keeps startup fast

    if repo_root is None:
        fabric_root = Path(__file__).resolve().parents[2]
        repo_root = resolve_project_root()
    else:
        fabric_root = repo_root

    valid_roles = _fetch_valid_roles(fabric_root)
    yaml_path = _find_yaml_file(repo_root)

    if yaml_path is None:
        logger.warning(
            "Project .vnx/vnx_workers.yaml and packaged defaults/vnx_workers.default.yaml not found; "
            "using hardcoded fallback (T0-T3)."
        )
        return _build_registry(_HARDCODED_FALLBACK, valid_roles)

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    return _build_registry(data, valid_roles)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

def _worker_pair(provider, model):
    """Validate/infer a pair using the fabric catalogue, never another source."""
    if __package__:
        from .providers import provider_registry as catalogue
        from .provider_costs import registry_key_for_provider
    else:
        from providers import provider_registry as catalogue
        from provider_costs import registry_key_for_provider
    if not isinstance(provider, (str, type(None))):
        raise ValueError(f"worker_pair_conflict: invalid provider {provider!r}")
    configs = catalogue.load()
    provider_key = registry_key_for_provider(provider) if provider else None
    if model in (None, ""):
        config = configs.get(provider_key)
        if config is None or not config.default_model:
            raise ValueError(f"worker_model_unresolved: {provider!r}")
        model = config.default_model
    if not isinstance(model, str):
        raise ValueError(f"worker_model_unknown: {model!r}")
    owners = set()
    for key in configs:
        try:
            catalogue.resolve_model_identity(key, model)
        except catalogue.RegistryLookupError:
            continue  # A section miss is not success; require one owner below.
        owners.add(key)
    if not owners:
        raise ValueError(f"worker_model_unknown: {model!r}")
    if (provider and provider_key not in owners) or (not provider and len(owners) != 1):
        raise ValueError(f"worker_pair_conflict: provider={provider!r} model={model!r}")
    key = provider_key if provider else owners.pop()
    inferred = configs[key].dispatch_enum or next(
        (p for p in _VALID_PROVIDERS_BASE if registry_key_for_provider(p) == key), f"litellm:{key}")
    return provider or inferred, model


def _registry_pair(path, terminal_id, pool_id):
    """Read a selected registry strictly; absent targets may use the next file."""
    import yaml
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"Unsupported schema_version in {path}")
    if not isinstance(data.get("workers"), list):
        raise ValueError(f"workers must be a list in {path}")
    registry = _build_registry(data)
    if terminal_id is not None:
        worker = registry.by_id(terminal_id)
        return _worker_pair(worker.provider, worker.model) if worker else None
    pool = registry.pool(pool_id)
    if pool is None:
        return None
    pairs = {(w.provider, w.model) for w in registry.by_pool(pool_id) if w.role not in _SYSTEM_ROLES}
    if not pairs:
        raise ValueError(f"worker_model_unresolved: pool {pool_id!r} has no builders")
    if pool.default_pair is not None:
        return pool.default_pair
    if len(pairs) != 1:
        raise ValueError(f"pool_default_ambiguous: {pool_id}")
    return _worker_pair(*pairs.pop())


def resolve_worker(terminal_id=None, *, pool_id="default", spec=None, repo_root=None, project_id=None):
    """Return provider/model/source/overridden; ValueError refuses invalid choices.

    None/empty fields are absent. Provider-only uses catalogue default_model.
    A pool request has no terminal env. Overridden lists populated lower layers
    (excluding the shipped baseline); files are only parsed when selected.
    File/config errors propagate; the legacy loader's code fallback is untouched.
    """
    if __package__:
        from . import config_registry, config_runtime
    else:
        import config_registry
        import config_runtime
    if terminal_id is not None:
        _validate_terminal_id(terminal_id)
    spec = {} if spec is None else spec
    if not isinstance(spec, dict):
        raise ValueError("worker_pair_conflict: spec must be a mapping")
    resolver = None
    if terminal_id is not None:
        if config_runtime.autowire(project_id=project_id):
            resolver, project_id = config_runtime.strict_resolver()
        else:
            status = config_runtime.wire_status()
            if status is None or status.cause not in {"no_project", "store_missing"}:
                raise ValueError("worker_config_unreadable: project_config_error")
    env = {}
    for field_name in ("provider", "model") if terminal_id is not None else ():
        value, source = config_registry.resolve_with_source(
            f"VNX_{terminal_id}_{field_name.upper()}", project_id, resolver,
            use_default_resolver=False, strict_resolver=True)
        if source in config_registry.ERROR_SOURCES:
            raise ValueError(f"worker_config_unreadable: {source}")
        env[field_name] = value
    root = Path(repo_root) if repo_root is not None else resolve_project_root()
    project = root / ".vnx" / "vnx_workers.yaml"
    default = Path(__file__).resolve().parent / "defaults" / "vnx_workers.default.yaml"
    sources = [("spec", spec), ("env", env)]
    sources = [(name, value) for name, value in sources
               if any(value.get(k) not in (None, "") for k in ("provider", "model"))]
    if project.exists():
        sources.append(("registry", project))
    sources.append(("default", default))
    for index, (source, value) in enumerate(sources):
        pair = (_registry_pair(value, terminal_id, pool_id) if isinstance(value, Path)
                else _worker_pair(value.get("provider"), value.get("model")))
        if pair is None:
            continue
        overridden = [name for name, _ in sources[index + 1:] if name != "default"]
        result = dict(provider=pair[0], model=pair[1], source=source, overridden=overridden)
        logger.info("worker_resolution: provider=%s model=%s source=%s overridden=[%s]",
                    *pair, source, ", ".join(overridden))
        return result
    raise ValueError(f"worker_model_unresolved: terminal={terminal_id!r} pool={pool_id!r}")


_REGISTRY_LOCK = threading.RLock()


def __getattr__(name: str):
    """Keep legacy access lazy so importing resolve_worker never reads a CWD registry."""
    if name != "WORKER_REGISTRY":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    with _REGISTRY_LOCK:
        registry = globals().get(name)
        if registry is None:
            registry = _load_registry()
            globals()[name] = registry
        return registry


# ---------------------------------------------------------------------------
# Convenience module-level functions
# ---------------------------------------------------------------------------

def list_workers() -> List[Worker]:
    return __getattr__("WORKER_REGISTRY").list_workers()


def by_id(terminal_id: str) -> Optional[Worker]:
    return __getattr__("WORKER_REGISTRY").by_id(terminal_id)


def by_role(role: str) -> List[Worker]:
    return __getattr__("WORKER_REGISTRY").by_role(role)


def aliases_for(terminal_id: str) -> List[str]:
    return __getattr__("WORKER_REGISTRY").aliases_for(terminal_id)


def by_pool(pool_id: str) -> List[Worker]:
    return __getattr__("WORKER_REGISTRY").by_pool(pool_id)


def resolve_alias(alias: str) -> Optional[Worker]:
    return __getattr__("WORKER_REGISTRY").resolve_alias(alias)
