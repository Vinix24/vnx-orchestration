"""handover — context-rotation handover detection and markdown writing."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .state_paths import _default_state_dir

logger = logging.getLogger(__name__)


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

    completed_section, remaining_section = _split_handover_sections(handover_text)

    header = (
        "CONTINUATION: Resumed after context rotation.\n\n"
        f"## Completed Work (from handover)\n{completed_section}\n\n"
        f"## Remaining Tasks (from handover)\n{remaining_section}\n\n"
        "---\n\n"
    )
    return header + original_instruction


def _split_handover_sections(handover_text: str) -> tuple[str, str]:
    """Extract ## Status and ## Remaining Tasks sections from handover markdown."""
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

    return completed_section, remaining_section


def _write_rotation_handover(
    terminal_id: str,
    dispatch_id: str,
    tracker: "HeadlessContextTracker",  # type: ignore[name-defined]
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
