"""config_runtime — the runtime-process façade over config_registry (P0 PR 6).

The dashboard wires its own DB resolver explicitly (api_config). The RUNTIME processes — the door,
the intelligence daemon, the headless trigger, the receipt processor — instead read their operator
toggles through this module, which lazily wires config_registry's DB layer for THIS process's project
the first time a value is read. The result: a value an operator flips in the dashboard is honoured by
the runtime, while an un-set flag resolves exactly as the env-only world did (behaviour-preserving).

Single-tenant: one runtime process serves one project. ``autowire()`` binds the resolver to that
project's state dir + sets it as config_registry's default project, so read-sites call ``get_bool``
/ ``get`` with no project_id. Idempotent (wires once) and fail-soft (any resolution error leaves the
registry env-only — the runtime never breaks because the config DB is missing) — but fail-soft is
LOUD (a WARNING naming the reason), not silent: a store that cannot be found reads identically to a
flag the operator explicitly turned off unless the miss is logged (OI-1461).
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import config_registry  # noqa: E402  (after the scripts/lib path guard above)

logger = logging.getLogger(__name__)

# Keyed by (state_dir_str, project_id) so a second project in the same process gets its own store.
# Value is True when that (state_dir, project_id) pair is wired; False / absent otherwise.
_wired_for: "dict[tuple[str, str], bool]" = {}


def _resolve_state_dir(state_dir: "str | Path | None") -> Optional[Path]:
    """Resolve the runtime state dir. Order: explicit arg > ``$VNX_STATE_DIR`` > the fabric's
    canonical resolver (``vnx_paths.resolve_paths()["VNX_STATE_DIR"]``) — the same resolver
    ``orphan_sweep.py`` and ``glm_gate.py``'s ``_resolve_data_dir`` chain fall back to.

    Relying solely on the env var (OI-1461) left every process that starts without an explicit
    ``VNX_STATE_DIR`` export unable to see an operator-set ``project_config`` value even though
    ``vnx_paths.resolve_paths()`` finds the exact same store two lines later — a genuine
    review-gate chain stall on a flag that was correctly set in the DB the whole time.
    """
    if state_dir:
        return Path(state_dir)
    env_sd = os.environ.get("VNX_STATE_DIR")
    if env_sd:
        return Path(env_sd)
    try:
        from vnx_paths import resolve_paths  # type: ignore[import]
        resolved = resolve_paths().get("VNX_STATE_DIR")
    except Exception as exc:
        logger.warning(
            "config_runtime: canonical state-dir resolver (vnx_paths.resolve_paths) failed (%s); "
            "operator config in project_config will NOT be read this process — reads fall back to "
            "env vars / registry defaults only",
            exc,
        )
        return None
    if not resolved:
        logger.warning(
            "config_runtime: canonical state-dir resolver returned no VNX_STATE_DIR; operator "
            "config in project_config will NOT be read this process — reads fall back to env vars "
            "/ registry defaults only"
        )
        return None
    return Path(resolved)


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

    Keyed by (state_dir, project_id): if the same pair is requested again it is a fast no-op
    (idempotent); a DIFFERENT project_id re-wires to that project's store. Fail-soft — any missing
    state dir / project_id / DB leaves the registry env-only and returns False."""
    global _wired_for
    try:
        sd = _resolve_state_dir(state_dir)
        if sd is None:
            # _resolve_state_dir already logged the reason (env-only miss vs. resolver failure).
            return False
        pid = _resolve_project_id(sd, project_id)
        if not pid:
            logger.warning(
                "config_runtime: could not resolve a project_id for state_dir=%s; operator config "
                "in project_config will NOT be read this process — reads fall back to env vars / "
                "registry defaults only",
                sd,
            )
            return False
        wire_key = (str(sd), pid)
        if not _wired_for.get(wire_key):
            # First time for this pair: confirm the DB exists before wiring.
            if not (sd / "runtime_coordination.db").exists():
                logger.warning(
                    "config_runtime: no runtime_coordination.db under state_dir=%s (project_id=%s); "
                    "operator config in project_config will NOT be read this process — reads fall "
                    "back to env vars / registry defaults only",
                    sd, pid,
                )
                return False
        # Always (re)apply the global registry state for the requested pair, so a switch
        # back to a previously-wired project restores ITS resolver + default — a bare cache
        # hit would otherwise leave the registry pointed at the last-wired project.
        import config_store_db
        resolved = sd
        config_registry.set_db_resolver(config_store_db.make_db_resolver(lambda _pid: resolved))
        config_registry.set_default_project_id(pid)
        _wired_for[wire_key] = True
        return True
    except Exception:
        return False


def get(key: str) -> Optional[str]:
    """Resolve a config value for this process's project (autowiring the DB layer on first use)."""
    autowire()
    return config_registry.get(key)


def get_bool(key: str) -> bool:
    """Bool view of get() — true for any truthy spelling (1/true/yes/on), via config_registry.get_bool."""
    autowire()
    return config_registry.get_bool(key)
