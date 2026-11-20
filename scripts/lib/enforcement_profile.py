"""enforcement_profile: the one reader of ``VNX_ENFORCEMENT_PROFILE`` (ADR-087; config-een-oppervlak V2.2, V3.1).

Strict side wins whenever the stored value is unknown: a wired store that fails to read gives
``enforced``; ``store_missing``/``resolver_failed`` read layers override, global_config, env and
default with one ERROR per process; ``no_project`` does the same with one WARNING; a value
outside ``shadow``/``enforced`` reads as ``enforced`` with an ERROR. ``VNX_OVERRIDE_ENFORCEMENT_PROFILE
=shadow`` takes one process back. No module-level ``config_runtime`` import, so a bootstrap path can
call :func:`enforced_storeless` (V6.1).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import NamedTuple, Optional

KEY = "VNX_ENFORCEMENT_PROFILE"
VALUES = ("shadow", "enforced")
STORELESS_LAYERS = ("override", "global_config", "env", "default")

_LIB = str(Path(__file__).resolve().parent)
logger = logging.getLogger(__name__)
_LOGGED: set = set()


class Reading(NamedTuple):
    """``source`` is ``project_config_error`` when a store could not be used; ``cause`` says why."""
    enforced: bool
    value: Optional[str]
    source: str
    cause: Optional[str]
    fallback_source: Optional[str]


def _registry():
    if _LIB not in sys.path:
        sys.path.insert(0, _LIB)
    import config_registry
    return config_registry


def _once(level: int, token: tuple, msg: str, *args) -> None:
    if token not in _LOGGED:
        _LOGGED.add(token)
        logger.log(level, msg, *args)


def _strict(value: Optional[str], source: str) -> bool:
    """True for ``enforced``, False for ``shadow``; anything else reads as enforced, loudly."""
    if value in VALUES:
        return value == "enforced"
    logger.error("enforcement_profile: %s=%r from %s is outside %s; reading it as enforced",
                 KEY, value, source, VALUES)
    return True


def enforced_storeless(key: str = KEY) -> "tuple[Optional[str], str]":
    """``(value, source)`` without a store; an unreadable global file gives ``global_config_error``."""
    return _registry().resolve_with_source(
        key, use_default_resolver=False, strict_resolver=True, layers=STORELESS_LAYERS)


def read() -> Reading:
    """The profile for this process, with where it came from (see the module docstring)."""
    registry = _registry()
    import config_runtime

    strict = config_runtime.strict_resolver() if config_runtime.autowire() else None
    if strict is not None:
        resolver, pid = strict
        value, source = registry.resolve_with_source(KEY, pid, resolver, strict_resolver=True)
        if source != "project_config_error":
            return Reading(_strict(value, source), value, source, None, None)
        logger.error("enforcement_profile: %s could not be read from the store of project %s "
                     "(cause read_failed); falling back to enforced", KEY, pid)
        return Reading(True, None, source, "read_failed", None)

    status = config_runtime.wire_status()
    value, fallback = enforced_storeless()
    if status is not None and status.cause == "no_project":
        _once(logging.WARNING, ("no_project",),
              "enforcement_profile: no project for this process; %s=%r from %s "
              "(layers override, global_config, env, default)", KEY, value, fallback)
        return Reading(_strict(value, fallback), value, fallback, "no_project", None)
    cause = status.cause if status is not None and status.cause == "store_missing" else "resolver_failed"
    pid = status.project_id if status is not None else None
    _once(logging.ERROR, (cause, pid),
          "enforcement_profile: %s for project %s: source project_config_error, cause %s; a "
          "stored project value was NOT applied; fallback %r from %s", KEY, pid, cause, value, fallback)
    return Reading(_strict(value, fallback), value, "project_config_error", cause, fallback)


def enforced() -> bool:
    """True when the strict side of the checks on the profile applies."""
    return read().enforced
