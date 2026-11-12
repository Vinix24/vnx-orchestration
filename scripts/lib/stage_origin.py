"""Origin of a staged dispatch (ADR-071 besluit 3): ``origin_oi`` / ``origin_pr`` fill ``parent_dispatch``
and staging records the declared edge through ``rework_attribution.record_edge``.

Resolution refuses with the reason (ValueError), never an empty edge. The edge write is
best-effort: a missing store or a SQLite error is logged and never blocks staging.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
_LIB_DIR = str(Path(__file__).resolve().parent)
for _p in (_LIB_DIR, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

QI_DB_FILENAME = "quality_intelligence.db"
_DISPATCH_BRANCH_PREFIX = "dispatch/"


def _read_oi_items(state_dir: Path) -> dict:
    """Read the open-items store through ``open_items_manager`` (read API, never the file by hand)."""
    manager = importlib.import_module("open_items_manager")
    return manager.read_items(state_dir)


def _pr_head_branch(pr_number: int, project_root: Path) -> str:
    """Head branch of a PR via ``gh pr view``; raises ValueError with the reason on any failure."""
    from pr_queue_state import _run_gh  # noqa: PLC0415

    data, error = _run_gh(project_root, "pr", "view", str(pr_number), "--json", "headRefName")
    if error:
        raise ValueError(f"origin_pr {pr_number}: cannot read the head branch from gh ({error})")
    branch = data.get("headRefName") if isinstance(data, dict) else None
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError(f"origin_pr {pr_number}: gh returned no head branch")
    return branch.strip()


def _origin_from_oi(origin_oi: str, state_dir: Path) -> str:
    oi_id = (origin_oi or "").strip()
    if not oi_id:
        raise ValueError("origin_oi must be a non-empty OI id")
    try:
        items = _read_oi_items(state_dir).get("items")
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        raise ValueError(f"origin_oi {oi_id}: cannot read the open items ({type(exc).__name__}: {exc})") from exc
    item = next((i for i in items if isinstance(i, dict) and i.get("id") == oi_id), None) \
        if isinstance(items, list) else None
    if item is None:
        raise ValueError(f"origin_oi {oi_id}: no such open item")
    # The store keeps the dispatch an OI came from under ``origin_dispatch_id`` (the
    # ``--dispatch`` flag of open_items_manager add); that is the ``dispatch`` field of ADR-071.
    origin = item.get("origin_dispatch_id")
    if not isinstance(origin, str) or not origin.strip():
        raise ValueError(f"origin_oi {oi_id}: the open item names no dispatch; refusing to stage an empty edge")
    return origin.strip()


def _origin_from_pr(origin_pr: int, project_root: Path) -> str:
    if isinstance(origin_pr, bool) or not isinstance(origin_pr, int) or origin_pr <= 0:
        raise ValueError(f"origin_pr must be a positive PR number, got {origin_pr!r}")
    branch = _pr_head_branch(origin_pr, project_root)
    origin = branch[len(_DISPATCH_BRANCH_PREFIX):] if branch.startswith(_DISPATCH_BRANCH_PREFIX) else ""
    if not origin:
        raise ValueError(
            f"origin_pr {origin_pr}: head branch {branch!r} is not of the form dispatch/<id>"
        )
    return origin


def resolve_origin(
    origin_oi: Optional[str],
    origin_pr: Optional[int],
    *,
    state_dir: Path,
    project_root: Path,
) -> "tuple[Optional[str], Optional[str], dict]":
    """Return ``(origin_dispatch_id, edge_kind, evidence)``; all ``None``/empty without an origin."""
    if origin_oi is not None and origin_pr is not None:
        raise ValueError("origin_oi and origin_pr are mutually exclusive; give at most one")
    if origin_oi is not None:
        return _origin_from_oi(origin_oi, state_dir), "oi_fix", {"origin_oi": origin_oi.strip()}
    if origin_pr is not None:
        return _origin_from_pr(origin_pr, project_root), "pr_fix", {"origin_pr": origin_pr}
    return None, None, {}


def merge_origin_into_parent(dispatch_id: str, origin: Optional[str], parent_dispatch: Optional[str]) -> Optional[str]:
    """The explicit ``parent_dispatch`` to feed ``resolve_parent_dispatch`` once an origin is resolved.

    An origin that contradicts an explicit ``parent_dispatch`` is refused; contradiction with
    ``work_ref`` is refused by ``resolve_parent_dispatch`` itself (the one rule, not a second one).
    """
    explicit = (parent_dispatch or "").strip() or None
    if origin is None:
        return explicit
    if explicit is not None and explicit != origin:
        raise ValueError(
            f"parent_dispatch {explicit!r} conflicts with the origin {origin!r} resolved for "
            f"dispatch_id {dispatch_id!r} — refusing to stage a bundle with two different lineages"
        )
    return origin


def record_declared_edge(
    *,
    data_root: Path,
    project_id: str,
    dispatch_id: str,
    predecessor: Optional[str],
    kind: Optional[str],
    evidence: dict,
) -> bool:
    """Write the declared edge (``oi_fix``/``pr_fix``, else ``round``; all herwerk, ADR-071 besluit 4); best-effort.

    No predecessor writes nothing; the store is never created here.
    """
    if not predecessor or predecessor == dispatch_id:
        return False
    qi_path = data_root / "state" / QI_DB_FILENAME
    if not qi_path.is_file():
        logger.warning("stage_origin: no %s under %s; edge %s -> %s not recorded", QI_DB_FILENAME, qi_path.parent, dispatch_id, predecessor)
        return False
    try:
        from rework_attribution import record_edge  # noqa: PLC0415

        with closing(sqlite3.connect(str(qi_path))) as conn:
            written = record_edge(
                conn, project_id, dispatch_id, predecessor, kind or "round", "rework",
                source="spec", evidence=evidence,
            )
            conn.commit()
        return written
    except (sqlite3.Error, ValueError, ImportError, OSError) as exc:
        logger.warning("stage_origin: edge %s -> %s not recorded (%s: %s)", dispatch_id, predecessor, type(exc).__name__, exc)
        return False
