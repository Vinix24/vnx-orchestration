#!/usr/bin/env python3
"""VNX Project Scope helpers — multi-tenant project_id resolution (Phase 0).

Phase 0 of the single-VNX migration. Exposes helpers for callers that opt
into reading by project_id. Phase 0 itself does NOT wire these into the
existing selectors; that is Phase 2+ (see migration plan §6).

Usage::

    from project_scope import current_project_id, scoped_query

    pid = current_project_id()                          # 'vnx-dev' default
    sql = "SELECT * FROM success_patterns WHERE 1=1"
    sql = scoped_query(sql)                             # opt-in filter

Filesystem scoping (W4G / OI-1067)
-----------------------------------
Sockets, lock files, and OS tmpfiles must not collide across concurrent
VNX projects on the same host. The helpers below route those state
surfaces through ``VNX_DATA_DIR`` (already project-scoped) or stamp a
short project hash into legacy ``/tmp`` paths::

    sock = project_socket_path("heartbeat_ack_monitor.sock")
    lock = project_lock_path("dispatcher.lock")
    prefix = project_tmpfile_prefix("dispatch")  # → 'vnx-<hash>-dispatch-'
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

DEFAULT_PROJECT = "vnx-dev"
ENV_VAR = "VNX_PROJECT_ID"
FILTER_ENV_VAR = "VNX_PROJECT_FILTER"

# Migration plan §3.2 — strict allowlist for project_id values.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# Truthy/falsy parsing for VNX_PROJECT_FILTER. Default ON (filter applied)
# so that selectors do not bleed cross-tenant data without explicit opt-out.
_FALSY = frozenset({"0", "false", "no", "off"})


def _validate(project_id: str) -> str:
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"Invalid VNX project_id {project_id!r}: "
            r"must match /^[a-z][a-z0-9-]{1,31}$/"
        )
    return project_id


def current_project_id() -> str:
    """Resolve the current project_id from ``VNX_PROJECT_ID`` env or default.

    Returns ``'vnx-dev'`` when the env var is unset or empty. Raises
    ``ValueError`` when the env var is set to a value that violates the
    allowlist regex (Phase 0 contract: identity is attribution, not auth —
    but bad ids are still rejected loudly so cross-project bleed is visible).
    """
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return DEFAULT_PROJECT
    return _validate(raw)


def project_filter_enabled() -> bool:
    """Return True when per-project filtering should be applied to reads.

    Phase 1 contract: the env var ``VNX_PROJECT_FILTER`` controls whether
    selectors restrict reads to ``current_project_id()``. Default is ON
    (filter applied) — multi-tenant safety is the priority. Set to ``0``,
    ``false``, ``no`` or ``off`` to disable, e.g. for cross-tenant analytics
    backfills.
    """
    raw = os.environ.get(FILTER_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSY


def scoped_query(base_sql: str, project_id: str | None = None) -> str:
    """Append a ``AND project_id = '<id>'`` filter to a SELECT.

    The id is validated against the allowlist regex, so direct interpolation
    is safe. Callers that already have a parameterised binding API should
    prefer it; this helper exists so opt-in readers can add scoping without
    rewriting their query construction.

    The base SQL must already contain a ``WHERE`` clause (caller responsibility).
    """
    pid = _validate(project_id) if project_id else current_project_id()
    return base_sql + f" AND project_id = '{pid}'"


# ---------------------------------------------------------------------------
# Filesystem scoping helpers (OI-1067 / W4G)
# ---------------------------------------------------------------------------


def _vnx_data_dir() -> Path:
    """Return the runtime data dir, resolving via vnx_paths if env is unset."""
    env = os.environ.get("VNX_DATA_DIR")
    if env:
        return Path(env).expanduser()
    # Fall back to the resolver — keeps single-process scripts honest even
    # when the caller forgot to source vnx_paths.sh first.
    try:
        from vnx_paths import resolve_paths  # type: ignore
    except Exception:
        return Path.cwd() / ".vnx-data"
    return Path(resolve_paths()["VNX_DATA_DIR"])


def project_hash(seed: str | None = None) -> str:
    """Stable 8-char hash of the project root for namespacing /tmp resources.

    Defaults to the resolved ``PROJECT_ROOT`` (or current working directory
    when unset). Two concurrent VNX projects always produce different
    hashes; the same project always produces the same hash, so cleanup
    (``find /tmp -name 'vnx-<hash>-*'``) is straightforward per-project.
    """
    if seed is None:
        seed = os.environ.get("PROJECT_ROOT") or str(Path.cwd())
    return hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]


def project_socket_path(name: str) -> Path:
    """Return a project-scoped Unix socket path under VNX_DATA_DIR/sockets.

    Concurrent VNX projects never share a socket because VNX_DATA_DIR is
    always project-scoped. Caller is responsible for ``mkdir(parents=True)``
    on the parent before binding.
    """
    if not name or "/" in name:
        raise ValueError(f"socket name must be a bare filename, got {name!r}")
    return _vnx_data_dir() / "sockets" / name


def project_lock_path(name: str) -> Path:
    """Return a project-scoped lock file path under VNX_DATA_DIR/locks."""
    if not name or "/" in name:
        raise ValueError(f"lock name must be a bare filename, got {name!r}")
    env = os.environ.get("VNX_LOCKS_DIR")
    base = Path(env).expanduser() if env else _vnx_data_dir() / "locks"
    return base / name


def project_tmpfile_prefix(prefix: str) -> str:
    """Stamp a short project hash into a tmpfile prefix.

    Use when a caller genuinely needs ``/tmp`` (e.g. shell scripts that
    can't depend on VNX_DATA_DIR being on the same filesystem). The
    returned prefix never collides across concurrent projects::

        >>> project_tmpfile_prefix("dispatch")
        'vnx-3a1f9c0e-dispatch-'
    """
    if not prefix:
        raise ValueError("prefix must be non-empty")
    return f"vnx-{project_hash()}-{prefix}-"
