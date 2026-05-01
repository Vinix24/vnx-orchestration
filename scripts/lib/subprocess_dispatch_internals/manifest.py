"""manifest — write/promote per-dispatch manifest.json files."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _write_manifest(
    dispatch_id: str,
    terminal_id: str,
    model: str,
    role: str | None,
    instruction: str,
    commit_hash_before: str,
    branch: str,
) -> str | None:
    """Write manifest.json to .vnx-data/dispatches/active/<dispatch_id>/.

    Returns the manifest path as a string, or None on failure.
    """
    import subprocess_dispatch as _sd
    manifest_dir = _sd._dispatch_manifest_dir("active", dispatch_id)
    try:
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "dispatch_id": dispatch_id,
            "commit_hash_before": commit_hash_before,
            "branch": branch,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "terminal": terminal_id,
            "model": model,
            "role": role,
            "instruction_chars": len(instruction),
            "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        }
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        logger.info("Manifest written: %s", manifest_path)
        return str(manifest_path)
    except Exception as exc:
        logger.warning("_write_manifest failed for %s: %s", dispatch_id, exc)
        return None


def _promote_manifest(dispatch_id: str, stage: str = "completed") -> str | None:
    """Move manifest from active/ to <stage>/ after dispatch finishes.

    stage must be one of: "completed", "dead_letter".
    The manifest is moved (not copied) so a failed dispatch never has a
    parallel record in completed/ — there is exactly one terminal location.

    After the manifest move succeeds, the originating ``active/<id>/``
    directory is removed via :func:`_safe_remove_active_dir` so any
    ancillary files (bundle.json, dispatch copies, etc.) that other
    components write alongside the manifest do not orphan the bucket.
    The check_active_drain.py janitor remains a backstop for paths that
    bypass this primary cleanup.

    Returns the destination manifest path as a string, or None on failure.
    """
    if stage not in ("completed", "dead_letter"):
        logger.warning("_promote_manifest: invalid stage %r", stage)
        return None
    import subprocess_dispatch as _sd
    src_dir = _sd._dispatch_manifest_dir("active", dispatch_id)
    dst_dir = _sd._dispatch_manifest_dir(stage, dispatch_id)
    src = src_dir / "manifest.json"
    if not src.exists():
        # Already promoted (or never written) — make the cleanup
        # idempotent: still remove a stray active/<id>/ shell if one
        # remains.  This is what makes re-running cleanup a true no-op.
        _sd._safe_remove_active_dir(src_dir)
        return None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "manifest.json"
        shutil.move(str(src), str(dst))
        # CFX-7: rmtree the active/<id>/ dir so leftover sibling files
        # do not cause check_active_drain to flag the dispatch as
        # in-flight.  Safety-checked via _safe_remove_active_dir.
        _sd._safe_remove_active_dir(src_dir)
        logger.info("Manifest moved to %s: %s -> %s", stage, src, dst)
        return str(dst)
    except Exception as exc:
        logger.warning("_promote_manifest(%s) failed for %s: %s", stage, dispatch_id, exc)
        return None
