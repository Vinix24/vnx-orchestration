"""report_path.py — canonical report path SSOT and resolver.

Single source of truth for where worker completion reports live.  Every reader
(receipt converter, dispatch_govern, report parser, benchmark scorer) uses
``resolve_report_path()`` instead of hand-rolling a path from a dispatch_id.

The canonical path is: ``$VNX_DATA_DIR/unified_reports/<dispatch_id>.md``.

Before this module existed (OI-989, OI-993), the worker prompt prescribed three
different report paths in the same assembled message.  This module is the single
place that defines the canonical form; the prompt sources now read it from here
so a second copy can never drift again.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonical form — the one truth every prompt source must agree on
# ---------------------------------------------------------------------------

# The three filename forms that workers have historically produced because the
# prompt prescribed all three at once:
#   1. <dispatch_id>.md          — canonical (governance_emit, dispatch_govern)
#   2. dispatch-<dispatch_id>.md — legacy (some tmux workers, OI-989 pattern)
#   3. <dispatch_id>_report.md   — legacy (base_worker.md until this dispatch)
_FILENAME_FORMS = (
    "{dispatch_id}.md",
    "dispatch-{dispatch_id}.md",
    "{dispatch_id}_report.md",
)

# Fragment for use in prompt text — env-var notation so the path is correct
# regardless of central-vs-local store.
_CANONICAL_FRAGMENT = "$VNX_DATA_DIR/unified_reports"


# ---------------------------------------------------------------------------
# Public API — canonical path
# ---------------------------------------------------------------------------


def canonical_report_path(dispatch_id: str, data_dir: "Optional[Path]" = None) -> Path:
    """Return the canonical report path: ``$VNX_DATA_DIR/unified_reports/<id>.md``.

    Args:
        dispatch_id: The dispatch identifier string.
        data_dir:    Override ``VNX_DATA_DIR`` (resolved from env when *None*).

    Returns:
        Absolute ``Path`` to the canonical report file.
    """
    dd = data_dir or _resolve_data_dir()
    return dd / "unified_reports" / f"{dispatch_id}.md"


def report_path_fragment() -> str:
    """Return the path fragment for use in worker prompt text.

    Returns the literal ``$VNX_DATA_DIR/unified_reports`` so workers see the
    correct env-var form regardless of where VNX_DATA_DIR points.
    """
    return _CANONICAL_FRAGMENT


# ---------------------------------------------------------------------------
# Public API — resolver
# ---------------------------------------------------------------------------


@dataclass
class ResolvedReport:
    """Outcome of ``resolve_report_path()``.

    Attributes:
        path:             The resolved report file (highest priority candidate).
        candidates_found: Every candidate that existed, in priority order.
        ambiguous:        ``True`` when more than one candidate existed. The
                          resolver picks the first by priority; the caller must
                          record the ambiguity in its own audit data.
    """

    path: Path
    candidates_found: List[Path] = field(default_factory=list)
    ambiguous: bool = False

    @property
    def candidate_sizes(self) -> Dict[str, int]:
        """Return ``{path_str: size_bytes}`` for every candidate found.

        Returns -1 for any candidate that no longer exists on disk.
        """
        result: Dict[str, int] = {}
        for c in self.candidates_found:
            try:
                result[str(c)] = c.stat().st_size
            except OSError:
                result[str(c)] = -1
        return result


def resolve_report_path(
    dispatch_id: str,
    data_dir: "Optional[Path]" = None,
    repo_root: "Optional[Path]" = None,
) -> Optional[ResolvedReport]:
    """Resolve the real report file for *dispatch_id*.

    Priority order (central store first, then repo-local; within each store
    the canonical form ``<id>.md`` first, then the two legacy forms)::

        1.  ``<data_dir>/unified_reports/<id>.md``
        2.  ``<data_dir>/unified_reports/dispatch-<id>.md``
        3.  ``<data_dir>/unified_reports/<id>_report.md``
        4.  ``<repo_root>/.vnx-data/unified_reports/<id>.md``
        5.  ``<repo_root>/.vnx-data/unified_reports/dispatch-<id>.md``
        6.  ``<repo_root>/.vnx-data/unified_reports/<id>_report.md``

    The repo-local store is only consulted when *repo_root* is provided AND its
    ``.vnx-data/unified_reports/`` directory resolves to a different path than
    the central store (protects against the double-resolve case where both
    point at the same directory).

    When multiple candidates exist, **the first by priority order is returned**
    and ``ResolvedReport.ambiguous`` is set to ``True``.  Every candidate is
    recorded in ``candidates_found`` with its size.  The caller is responsible
    for persisting the ambiguity — the resolver does NOT silently hide it.

    Returns ``None`` when no candidate file exists at any location.
    """
    dd = data_dir or _resolve_data_dir()
    central_dir = dd / "unified_reports"

    candidates: List[Path] = []

    # Central store — try all three filename forms in priority order.
    for form in _FILENAME_FORMS:
        p = central_dir / form.format(dispatch_id=dispatch_id)
        if p.is_file():
            candidates.append(p)

    # Repo-local store — only when explicitly provided and different from central.
    if repo_root is not None:
        local_dir = (repo_root / ".vnx-data" / "unified_reports").resolve()
        if local_dir != central_dir.resolve():
            for form in _FILENAME_FORMS:
                p = local_dir / form.format(dispatch_id=dispatch_id)
                if p.is_file():
                    candidates.append(p)

    if not candidates:
        return None

    ambiguous = len(candidates) > 1
    if ambiguous:
        sizes = _format_candidate_sizes(candidates)
        logger.warning(
            "resolve_report_path: ambiguous report for dispatch=%s "
            "— %d candidates: %s",
            dispatch_id, len(candidates), sizes,
        )

    return ResolvedReport(
        path=candidates[0],
        candidates_found=candidates,
        ambiguous=ambiguous,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_data_dir() -> Path:
    """Resolve ``VNX_DATA_DIR`` from the environment.

    Falls back to repo-local ``.vnx-data`` when the env var is unset
    (headless / CI contexts).
    """
    env_val = os.environ.get("VNX_DATA_DIR")
    if env_val:
        return Path(env_val).expanduser()
    # Last resort: CWD-relative .vnx-data (repo-local mode).
    return Path.cwd() / ".vnx-data"


def _resolve_repo_root() -> Optional[Path]:
    """Return the git repository root, or *None* when unresolvable."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _format_candidate_sizes(candidates: List[Path]) -> str:
    """Return a human-readable string of ``(path, size)`` pairs."""
    parts: List[str] = []
    for c in candidates:
        try:
            size = c.stat().st_size
        except OSError:
            size = -1
        parts.append(f"{c} ({size}B)")
    return ", ".join(parts)
