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
"""

from __future__ import annotations

import os
import re

DEFAULT_PROJECT = "vnx-dev"
ENV_VAR = "VNX_PROJECT_ID"

# Migration plan §3.2 — strict allowlist for project_id values.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


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
