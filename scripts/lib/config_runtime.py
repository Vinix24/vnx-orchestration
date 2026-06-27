"""config_runtime — the runtime-process façade over config_registry (P0 PR 6).

The dashboard wires its own DB resolver explicitly (api_config). The RUNTIME processes — the door,
the intelligence daemon, the headless trigger, the receipt processor — instead read their operator
toggles through this module, which lazily wires config_registry's DB layer for THIS process's project
the first time a value is read. The result: a value an operator flips in the dashboard is honoured by
the runtime, while an un-set flag resolves exactly as the env-only world did (behaviour-preserving).

Single-tenant: one runtime process serves one project. ``autowire()`` binds the resolver to that
project's state dir + sets it as config_registry's default project, so read-sites call ``get_bool``
/ ``get`` with no project_id. Idempotent (wires once) and fail-soft (any resolution error leaves the
registry env-only — the runtime never breaks because the config DB is missing).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import config_registry  # noqa: E402  (after the scripts/lib path guard above)

_wired = False


def _resolve_state_dir(state_dir: "str | Path | None") -> Optional[Path]:
    if state_dir:
        return Path(state_dir)
    env_sd = os.environ.get("VNX_STATE_DIR")
    return Path(env_sd) if env_sd else None


def _resolve_project_id(state_dir: Path, project_id: Optional[str]) -> Optional[str]:
    if project_id:
        return project_id
    env_pid = os.environ.get("VNX_PROJECT_ID")
    if env_pid:
        return env_pid
    try:
        from vnx_paths import project_id_from_state_dir  # type: ignore[import]
        return project_id_from_state_dir(state_dir)
    except Exception:
        return None


def autowire(state_dir: "str | Path | None" = None, project_id: Optional[str] = None) -> bool:
    """Wire config_registry's DB resolver + default project for this runtime process.

    Returns True once wired. Idempotent (subsequent calls are O(1)); fail-soft — any missing state
    dir / project_id / DB leaves the registry env-only and returns False (it may succeed on a later
    call once the runtime state exists)."""
    global _wired
    if _wired:
        return True
    try:
        sd = _resolve_state_dir(state_dir)
        if sd is None:
            return False
        pid = _resolve_project_id(sd, project_id)
        if not pid:
            return False
        db = sd / "runtime_coordination.db"
        if not db.exists():
            return False
        import config_store_db
        resolved = sd
        config_registry.set_db_resolver(config_store_db.make_db_resolver(lambda _pid: resolved))
        config_registry.set_default_project_id(pid)
        _wired = True
        return True
    except Exception:
        return False


def get(key: str) -> Optional[str]:
    """Resolve a config value for this process's project (autowiring the DB layer on first use)."""
    autowire()
    return config_registry.get(key)


def get_bool(key: str) -> bool:
    """Bool view of get() — true only for the canonical "1" (matches the read-sites)."""
    autowire()
    return config_registry.get_bool(key)
