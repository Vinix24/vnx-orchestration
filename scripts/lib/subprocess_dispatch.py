#!/usr/bin/env python3
"""subprocess_dispatch.py — Thin helper for routing dispatch delivery via SubprocessAdapter.

Called from dispatch_deliver.sh when VNX_ADAPTER_T{n}=subprocess is set.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from subprocess_adapter import SubprocessAdapter
from headless_context_tracker import HeadlessContextTracker

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


def _inject_skill_context(terminal_id: str, instruction: str, role: str | None = None) -> str:
    """Prepend skill/terminal CLAUDE.md content to instruction for context.

    3-tier resolution (first hit wins):
      1. agents/{role}/CLAUDE.md        — project-level agent override
      2. .claude/skills/{role}/CLAUDE.md — skill definition
      3. .claude/terminals/{terminal}/CLAUDE.md — terminal fallback

    Tiers 1 and 2 require role to be provided.  Tier 3 is always attempted
    when the first two are absent or role is None.

    Returns the instruction unchanged if no CLAUDE.md is found in any tier.
    """
    project_root = Path(__file__).resolve().parents[2]

    candidates: list[Path] = []
    if role:
        candidates.append(project_root / "agents" / role / "CLAUDE.md")
        candidates.append(project_root / ".claude" / "skills" / role / "CLAUDE.md")
    candidates.append(project_root / ".claude" / "terminals" / terminal_id / "CLAUDE.md")

    for path in candidates:
        if path.exists():
            context = path.read_text()
            return f"{context}\n\n---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"

    return instruction


def _default_state_dir() -> Path:
    """Resolve VNX state directory from environment."""
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    if data_dir:
        return Path(data_dir) / "state"
    return Path(__file__).resolve().parent.parent.parent / ".vnx-data" / "state"


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


def _resolve_agent_cwd(role: str | None) -> Path | None:
    """Return agents/{role}/ as Path if the directory exists, else None."""
    if not role:
        return None
    candidate = Path(__file__).resolve().parents[2] / "agents" / role
    return candidate if candidate.is_dir() else None


def _load_agent_profile(config_path: Path) -> str:
    """Load governance_profile from agent config.yaml.

    Uses a simple line-scan so no yaml dependency is required.
    Returns 'default' when the key is absent or the file cannot be read.
    """
    try:
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("governance_profile:"):
                return line.split(":", 1)[1].strip()
    except Exception as _exc:
        logger.warning("Failed to read agent config %s: %s", config_path, _exc)
    return "default"


def deliver_via_subprocess(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    *,
    role: str | None = None,
    lease_generation: int | None = None,
    heartbeat_interval: float = 300.0,
    chunk_timeout: float = 120.0,
    total_deadline: float = 600.0,
) -> bool:
    """Deliver a dispatch instruction to terminal_id via SubprocessAdapter.

    Blocks until the subprocess exits, consuming all stream events.
    Events are persisted to EventStore via read_events_with_timeout() internally.

    If role is provided, _inject_skill_context() resolves the matching
    CLAUDE.md via 3-tier lookup and SubprocessAdapter uses the agent dir
    as cwd when available.

    If lease_generation is provided, a background heartbeat thread renews the
    lease every heartbeat_interval seconds to prevent TTL expiry during long tasks.

    Returns True on success, False on failure.
    """
    # Detect pending handover and wrap instruction for seamless continuation
    project_root = Path(__file__).resolve().parents[2]
    handover_dir = project_root / ".vnx-data" / "rotation_handovers"
    pending_handover = _detect_pending_handover(terminal_id, handover_dir)
    if pending_handover is not None:
        logger.info(
            "deliver_via_subprocess: pending handover found for %s: %s",
            terminal_id, pending_handover,
        )
        instruction = _build_continuation_prompt(pending_handover, instruction)

    # Inject skill/terminal CLAUDE.md as skill context for headless agents
    instruction = _inject_skill_context(terminal_id, instruction, role=role)

    # Resolve agent cwd: agents/{role}/ dir takes precedence when it exists
    agent_cwd = _resolve_agent_cwd(role)

    # Load and log governance profile from agent config.yaml
    if agent_cwd is not None:
        config_path = agent_cwd / "config.yaml"
        if config_path.exists():
            profile = _load_agent_profile(config_path)
            logger.info("Agent %s using governance profile: %s", role, profile)

    adapter = SubprocessAdapter()
    result = adapter.deliver(
        terminal_id,
        dispatch_id,
        instruction=instruction,
        model=model,
        cwd=agent_cwd,
    )
    if not result.success:
        return False

    # Start heartbeat thread if lease generation is known
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None

    if lease_generation is not None:
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(terminal_id, dispatch_id, lease_generation, heartbeat_stop, _default_state_dir()),
            kwargs={"interval": heartbeat_interval},
            daemon=True,
        )
        heartbeat_thread.start()

    tracker = HeadlessContextTracker()
    state_dir = _default_state_dir()

    success = False
    try:
        for _event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
            context_tracker=tracker,
            state_dir=state_dir,
        ):
            pass
        success = True
    except Exception:
        logger.exception("deliver_via_subprocess failed for %s", terminal_id)
        return False
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        # Archive events for this dispatch before the next dispatch clears them
        event_store = adapter._get_event_store()
        if event_store is not None:
            try:
                event_store.archive(terminal_id, dispatch_id)
            except Exception as _exc:
                logger.debug("deliver_via_subprocess: event archive failed: %s", _exc)
        # Trigger auto-report pipeline on completion (gated by VNX_AUTO_REPORT=1)
        adapter.trigger_report_pipeline(
            terminal_id,
            dispatch_id,
            cwd=str(agent_cwd) if agent_cwd is not None else None,
        )

    # Handle context rotation: write handover markdown and return False so
    # deliver_with_recovery treats this as a graceful stop, not a failure.
    if tracker.should_rotate:
        _write_rotation_handover(terminal_id, dispatch_id, tracker)
        return False

    # Mark handover as processed after successful delivery (not rotation)
    if success and pending_handover is not None and pending_handover.exists():
        processed_path = pending_handover.with_suffix(pending_handover.suffix + ".processed")
        try:
            pending_handover.rename(processed_path)
            logger.info(
                "deliver_via_subprocess: handover marked processed: %s",
                processed_path,
            )
        except Exception as exc:
            logger.warning(
                "deliver_via_subprocess: failed to mark handover processed: %s", exc
            )

    return success


def _write_rotation_handover(
    terminal_id: str,
    dispatch_id: str,
    tracker: "HeadlessContextTracker",
) -> None:
    """Write a rotation handover markdown file to .vnx-data/rotation_handovers/."""
    project_root = Path(__file__).resolve().parents[2]
    handover_dir = project_root / ".vnx-data" / "rotation_handovers"
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


def _write_receipt(
    dispatch_id: str,
    terminal_id: str,
    status: str,
    *,
    event_count: int = 0,
    session_id: str | None = None,
    attempt: int | None = None,
    failure_reason: str | None = None,
) -> Path:
    """Append a subprocess completion receipt to t0_receipts.ndjson.

    Returns the path to the receipt file.
    """
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": "subprocess_completion",
        "dispatch_id": dispatch_id,
        "terminal": terminal_id,
        "status": status,
        "event_count": event_count,
        "session_id": session_id,
        "source": "subprocess",
    }
    if attempt is not None:
        receipt["attempt"] = attempt
    if failure_reason:
        receipt["failure_reason"] = failure_reason

    receipt_path = _default_state_dir() / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with open(receipt_path, "a") as f:
        f.write(json.dumps(receipt) + "\n")

    logger.info(
        "Receipt written: dispatch=%s terminal=%s status=%s",
        dispatch_id, terminal_id, status,
    )
    return receipt_path


def deliver_with_recovery(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    *,
    role: str | None = None,
    max_retries: int = 3,
    lease_generation: int | None = None,
    heartbeat_interval: float = 300.0,
    chunk_timeout: float = 120.0,
    total_deadline: float = 600.0,
) -> bool:
    """Deliver with automatic retry on failure.

    On success, writes a receipt with status="done".
    On final failure (budget exhausted), writes a receipt with status="failed".
    Retries use exponential backoff: 30s, 60s, 120s.

    Returns True on success, False on failure.
    """
    for attempt in range(max_retries + 1):
        success = deliver_via_subprocess(
            terminal_id,
            instruction,
            model,
            dispatch_id,
            role=role,
            lease_generation=lease_generation,
            heartbeat_interval=heartbeat_interval,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        )
        if success:
            _write_receipt(
                dispatch_id, terminal_id, "done",
                attempt=attempt,
            )
            return True

        if attempt < max_retries:
            backoff = 30 * (2 ** attempt)  # 30s, 60s, 120s
            logger.warning(
                "Delivery failed for %s, retry %d/%d in %ds",
                dispatch_id, attempt + 1, max_retries, backoff,
            )
            time.sleep(backoff)
        else:
            _write_receipt(
                dispatch_id, terminal_id, "failed",
                attempt=attempt,
                failure_reason=f"Exhausted {max_retries} retries",
            )

    return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deliver dispatch via SubprocessAdapter")
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--role", default=None, help="Agent role for skill context inlining")
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    ok = deliver_with_recovery(
        terminal_id=args.terminal_id,
        instruction=args.instruction,
        model=args.model,
        dispatch_id=args.dispatch_id,
        role=args.role,
        max_retries=args.max_retries,
    )
    sys.exit(0 if ok else 1)
