#!/usr/bin/env python3
"""dispatch_paths.py — Dispatch-scoped path manifest helper.

Workers declare the repository paths they intend to mutate during a dispatch
via a manifest written at dispatch start.  Subsequent auto-commit / auto-stash
operations restrict their file scope to the manifest, preventing cross-dispatch
contamination on shared worktrees.

Manifest schema:
    {
      "dispatch_id": "<id>",
      "allowed_paths": ["scripts/", "tests/foo.py", ...]
    }

Stored at:
    <state_dir>/dispatch_paths/<dispatch_id>.json
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


def _manifest_path(state_dir: Path, dispatch_id: str) -> Path:
    return state_dir / "dispatch_paths" / f"{dispatch_id}.json"


def write_manifest(
    state_dir: Path, dispatch_id: str, allowed_paths: List[str]
) -> Path:
    """Write manifest of paths this dispatch is allowed to mutate.

    Returns the manifest file path.  Caller is responsible for ensuring
    state_dir is writable.
    """
    p = _manifest_path(state_dir, dispatch_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {"dispatch_id": dispatch_id, "allowed_paths": list(allowed_paths)},
            indent=2,
        )
    )
    return p


def read_manifest(state_dir: Path, dispatch_id: str) -> Optional[List[str]]:
    """Return the manifest's allowed_paths list, or None when no manifest exists.

    Returns an empty list when the manifest exists but declares no paths
    (semantically: dispatch declared its scope is empty — fail-safe).
    Returns None on parse errors so callers fall back to legacy behavior.
    """
    p = _manifest_path(state_dir, dispatch_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "dispatch_paths: failed to read manifest %s: %s — falling back to legacy scope",
            p,
            exc,
        )
        return None
    paths = data.get("allowed_paths")
    if not isinstance(paths, list):
        return None
    return [str(x) for x in paths]


def paths_for_dispatch(dispatch_id: str) -> Optional[List[str]]:
    """Convenience wrapper: read manifest using the default VNX state dir.

    Returns None when no manifest exists or it is unreadable; an empty
    list when the manifest exists but declares no paths.
    """
    try:
        from subprocess_dispatch_internals.state_paths import _default_state_dir
    except Exception as exc:  # pragma: no cover - import-time safety net
        logger.warning("dispatch_paths: state-dir resolver unavailable: %s", exc)
        return None
    return read_manifest(_default_state_dir(), dispatch_id)


def filter_paths(files: List[str], allowed_paths: List[str]) -> List[str]:
    """Return only those files that match any allowed path.

    Matching rules:
      - exact match: file == allowed
      - directory match: file starts with allowed.rstrip("/") + "/"

    A trailing slash on an allowed entry is optional; both forms work.
    Pass-through when allowed_paths is empty list -> empty result (fail-safe).
    """
    if not allowed_paths:
        return []
    normalized = [p.rstrip("/") for p in allowed_paths if p]
    out: List[str] = []
    for f in files:
        for p in normalized:
            if f == p or f.startswith(p + "/"):
                out.append(f)
                break
    return out
