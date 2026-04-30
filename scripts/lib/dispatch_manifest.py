#!/usr/bin/env python3
"""dispatch_manifest.py — Manifest lifecycle and context-rotation helpers.

Provides manifest write/promote, heartbeat renewal, and context rotation
handover.  Receipt writing and telemetry live in dispatch_receipt.py;
functions are re-exported here for backward compatibility.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from dispatch_context import _default_state_dir
from dispatch_receipt import (
    _write_receipt,
    _capture_dispatch_parameters,
    _capture_dispatch_outcome,
    _update_pattern_confidence,
)

if TYPE_CHECKING:
    from headless_context_tracker import HeadlessContextTracker

logger = logging.getLogger(__name__)

__all__ = [
    "_dispatch_manifest_dir",
    "_write_manifest",
    "_safe_remove_active_dir",
    "_promote_manifest",
    "_heartbeat_loop",
    "_detect_pending_handover",
    "_build_continuation_prompt",
    "_write_rotation_handover",
    # re-exported from dispatch_receipt:
    "_write_receipt",
    "_capture_dispatch_parameters",
    "_capture_dispatch_outcome",
    "_update_pattern_confidence",
]


def _dispatch_manifest_dir(stage: str, dispatch_id: str) -> Path:
    """Resolve .vnx-data/dispatches/<stage>/<dispatch_id>/ for manifest storage."""
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "dispatches" / stage / dispatch_id
    return Path(__file__).resolve().parents[2] / ".vnx-data" / "dispatches" / stage / dispatch_id


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
    manifest_dir = _dispatch_manifest_dir("active", dispatch_id)
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


def _safe_remove_active_dir(src_dir: Path) -> bool:
    """Remove ``src_dir`` recursively iff it lives under ``dispatches/active/``.

    Safety contract (CFX-7):
      * The directory itself must not be a symlink — refuse to follow.
      * ``src_dir.parent.name`` must be ``"active"`` and the grandparent
        must be named ``"dispatches"``.  This anchors the removal to the
        intended layout and rejects any caller-supplied path that escapes
        the active bucket.
      * Missing directory is a successful no-op (idempotent).

    Returns True when the directory was removed (or was already gone),
    False when removal was refused or failed.  Never raises.
    """
    try:
        if src_dir.is_symlink():
            logger.warning(
                "_safe_remove_active_dir: refusing symlinked path %s", src_dir
            )
            return False
        if not src_dir.exists():
            return True
        if not src_dir.is_dir():
            logger.warning(
                "_safe_remove_active_dir: refusing non-directory %s", src_dir
            )
            return False
        parent = src_dir.parent
        grandparent = parent.parent
        if parent.name != "active" or grandparent.name != "dispatches":
            logger.warning(
                "_safe_remove_active_dir: refusing %s — not under dispatches/active/",
                src_dir,
            )
            return False
        shutil.rmtree(src_dir)
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        logger.warning("_safe_remove_active_dir failed for %s: %s", src_dir, exc)
        return False


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
    src_dir = _dispatch_manifest_dir("active", dispatch_id)
    dst_dir = _dispatch_manifest_dir(stage, dispatch_id)
    src = src_dir / "manifest.json"
    if not src.exists():
        # Already promoted (or never written) — make the cleanup
        # idempotent: still remove a stray active/<id>/ shell if one
        # remains.  This is what makes re-running cleanup a true no-op.
        _safe_remove_active_dir(src_dir)
        return None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "manifest.json"
        shutil.move(str(src), str(dst))
        # CFX-7: rmtree the active/<id>/ dir so leftover sibling files
        # do not cause check_active_drain to flag the dispatch as
        # in-flight.  Safety-checked via _safe_remove_active_dir.
        _safe_remove_active_dir(src_dir)
        logger.info("Manifest moved to %s: %s -> %s", stage, src, dst)
        return str(dst)
    except Exception as exc:
        logger.warning("_promote_manifest(%s) failed for %s: %s", stage, dispatch_id, exc)
        return None


def _heartbeat_loop(
    terminal_id: str,
    dispatch_id: str,
    generation: int,
    stop_event: threading.Event,
    state_dir: Path,
    interval: float = 300.0,
) -> None:
    """Renew lease every *interval* seconds until stop_event is set."""
    while not stop_event.wait(timeout=interval):
        try:
            from lease_manager import LeaseManager
            lm = LeaseManager(state_dir=state_dir, auto_init=False)
            lm.renew(terminal_id, generation=generation, actor="heartbeat")
            logger.info("Heartbeat renewed lease for %s (gen %d)", terminal_id, generation)
        except Exception as e:
            logger.warning("Heartbeat renewal failed for %s: %s", terminal_id, e)


def _detect_pending_handover(terminal_id: str, handover_dir: Path) -> Path | None:
    """Find most recent unprocessed handover for terminal_id.

    Scans handover_dir for files matching *{terminal_id}*ROTATION-HANDOVER*.md
    that do NOT have a .processed suffix. Returns most recent by mtime, or None.
    """
    if not handover_dir.exists():
        return None

    candidates = [
        p for p in handover_dir.glob(f"*{terminal_id}*ROTATION-HANDOVER*.md")
        if not p.name.endswith(".processed")
    ]
    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def _build_continuation_prompt(handover_path: Path, original_instruction: str) -> str:
    """Wrap instruction with handover context for seamless continuation.

    Reads the handover markdown and prepends:
    - "CONTINUATION: Resumed after context rotation."
    - Completed work section from handover
    - Remaining tasks section from handover
    - Then the original instruction
    """
    handover_text = handover_path.read_text()

    # Extract ## Status and ## Remaining Tasks sections from handover markdown
    completed_section = ""
    remaining_section = ""

    lines = handover_text.splitlines()
    current_section: str | None = None
    section_lines: list[str] = []

    for line in lines:
        if line.startswith("## Status"):
            if current_section == "status":
                completed_section = "\n".join(section_lines).strip()
            current_section = "status"
            section_lines = []
        elif line.startswith("## Remaining Tasks"):
            if current_section == "status":
                completed_section = "\n".join(section_lines).strip()
            current_section = "remaining"
            section_lines = []
        elif line.startswith("## ") and current_section == "remaining":
            remaining_section = "\n".join(section_lines).strip()
            current_section = None
            section_lines = []
        else:
            section_lines.append(line)

    if current_section == "status":
        completed_section = "\n".join(section_lines).strip()
    elif current_section == "remaining":
        remaining_section = "\n".join(section_lines).strip()

    header = (
        "CONTINUATION: Resumed after context rotation.\n\n"
        f"## Completed Work (from handover)\n{completed_section}\n\n"
        f"## Remaining Tasks (from handover)\n{remaining_section}\n\n"
        "---\n\n"
    )
    return header + original_instruction


def _write_rotation_handover(
    terminal_id: str,
    dispatch_id: str,
    tracker: "HeadlessContextTracker",
) -> None:
    """Write a rotation handover markdown file to .vnx-data/rotation_handovers/."""
    handover_dir = _default_state_dir().parent / "rotation_handovers"
    try:
        handover_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{timestamp}-{terminal_id}-ROTATION-HANDOVER.md"
        snapshot = tracker.snapshot()
        content = (
            f"# {terminal_id} Context Rotation Handover\n"
            f"**Timestamp**: {timestamp}\n"
            f"**Context Used**: {snapshot['context_used_pct']}%\n"
            f"**Dispatch-ID**: {dispatch_id}\n"
            "## Status\n"
            "in-progress\n"
            "## Remaining Tasks\n"
            "[continuation needed]\n"
        )
        (handover_dir / filename).write_text(content)
        logger.info(
            "_write_rotation_handover: handover written to %s",
            handover_dir / filename,
        )
    except Exception as exc:
        logger.warning("_write_rotation_handover: failed to write handover: %s", exc)
