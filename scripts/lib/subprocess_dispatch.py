#!/usr/bin/env python3
"""subprocess_dispatch.py — Thin helper for routing dispatch delivery via SubprocessAdapter.

Called from dispatch_deliver.sh when VNX_ADAPTER_T{n}=subprocess is set.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from subprocess_adapter import SubprocessAdapter
from worker_health_monitor import WorkerHealthMonitor, HealthStatus, SLOW_THRESHOLD

logger = logging.getLogger(__name__)


def _inject_permission_profile(terminal_id: str, role: str | None, instruction: str) -> str:
    """Prepend permission preamble to instruction if a profile exists for role.

    Resolves the terminal's expected role from terminal_assignments when role
    is None.  Logs a warning on role/terminal mismatch.  Returns instruction
    unchanged when no profile is found or worker_permissions cannot be loaded.
    """
    try:
        from worker_permissions import (
            load_permissions,
            generate_permission_preamble,
            validate_dispatch_permissions,
        )
    except ImportError:
        logger.debug("_inject_permission_profile: worker_permissions not available, skipping")
        return instruction

    # Resolve effective role from terminal assignment when caller didn't specify one
    effective_role = role
    if not effective_role:
        try:
            import yaml
            yaml_path = Path(__file__).resolve().parents[2] / ".vnx" / "worker_permissions.yaml"
            data = yaml.safe_load(yaml_path.read_text()) or {}
            effective_role = data.get("terminal_assignments", {}).get(terminal_id)
        except Exception as exc:
            logger.debug("_inject_permission_profile: could not resolve role for %s: %s", terminal_id, exc)

    if not effective_role:
        return instruction

    # Validate role/terminal assignment
    warnings = validate_dispatch_permissions(
        {"terminal": terminal_id, "role": effective_role}
    )
    for w in warnings:
        logger.warning(w)

    profile = load_permissions(effective_role)
    if not profile.allowed_tools and not profile.denied_tools and not profile.bash_deny_patterns:
        logger.debug("_inject_permission_profile: empty profile for role '%s', skipping preamble", effective_role)
        return instruction

    preamble = generate_permission_preamble(profile)
    logger.info(
        "Permission profile applied: terminal=%s role=%s allowed=%s denied=%s",
        terminal_id, effective_role,
        profile.allowed_tools,
        profile.denied_tools,
    )
    return f"{preamble}\n---\n\n{instruction}"


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
    repo_map: str | None = None,
    lease_generation: int | None = None,
    heartbeat_interval: float = 300.0,
    chunk_timeout: float = 120.0,
    total_deadline: float = 600.0,
    health_monitor: "WorkerHealthMonitor | None" = None,
) -> bool:
    """Deliver a dispatch instruction to terminal_id via SubprocessAdapter.

    Blocks until the subprocess exits, consuming all stream events.
    Events are persisted to EventStore via read_events_with_timeout() internally.

    If role is provided, _inject_skill_context() resolves the matching
    CLAUDE.md via 3-tier lookup and SubprocessAdapter uses the agent dir
    as cwd when available.

    If repo_map is provided (pre-formatted repo map string), it is appended
    to the instruction before skill context injection.  This is the direct-
    caller path; dispatches routed through DispatchDaemon receive repo maps
    via DispatchEnricher instead.

    If lease_generation is provided, a background heartbeat thread renews the
    lease every heartbeat_interval seconds to prevent TTL expiry during long tasks.

    If health_monitor is provided, each streamed event is fed into it.

    Returns True on success, False on failure.
    """
    # Append repo map to instruction before skill context wrapping
    if repo_map:
        instruction = instruction + f"\n\n{repo_map}"

    # Inject skill/terminal CLAUDE.md as skill context for headless agents
    instruction = _inject_skill_context(terminal_id, instruction, role=role)

    # Inject per-terminal permission profile preamble
    instruction = _inject_permission_profile(terminal_id, role, instruction)

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

    success = False
    _last_stuck_log_time = 0.0
    try:
        for _event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            if health_monitor is not None:
                health_monitor.update(_event)
                # Log stuck warning at most once per SLOW_THRESHOLD window
                import time as _time
                _now = _time.monotonic()
                if _now - _last_stuck_log_time >= SLOW_THRESHOLD:
                    h = health_monitor.health_status()
                    if h.status == HealthStatus.STUCK:
                        health_monitor.log_stuck_event()
                        _last_stuck_log_time = _now
        success = True
        return True
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


def _check_commit_since(dispatch_start_ts: str) -> bool:
    """Return True if no commits are found since dispatch_start_ts (commit_missing).

    Uses GovernanceEnforcer.check("receipt_must_have_commit") when available.
    Falls back to a direct git log check when the enforcer cannot be loaded.
    Never raises.
    """
    try:
        from governance_enforcer import GovernanceEnforcer, DEFAULT_CONFIG_PATH  # noqa: PLC0415
        enforcer = GovernanceEnforcer()
        if DEFAULT_CONFIG_PATH.exists():
            enforcer.load_config(DEFAULT_CONFIG_PATH)
        result = enforcer.check(
            "receipt_must_have_commit",
            {"dispatch_timestamp": dispatch_start_ts},
        )
        if not result.passed:
            logger.warning("receipt_must_have_commit: %s", result.message)
            return True
        return False
    except Exception as exc:
        logger.debug("commit check (enforcer path) failed: %s — using git directly", exc)

    # Direct fallback
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"--since={dispatch_start_ts}", "-5"],
            capture_output=True, text=True, timeout=10,
        )
        commits = [l for l in proc.stdout.splitlines() if l.strip()]
        if not commits:
            logger.warning("receipt_must_have_commit: no commits found since %s", dispatch_start_ts)
            return True
    except Exception as exc:
        logger.debug("git log fallback failed: %s", exc)
    return False


def _auto_commit_changes(dispatch_id: str, terminal_id: str, gate: str = "") -> bool:
    """Stage and commit any uncommitted changes after a successful dispatch.

    Returns True if a commit was made, False otherwise.
    Never raises — all exceptions are logged and swallowed.
    """
    try:
        # Check for uncommitted changes
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=Path(__file__).resolve().parents[2],
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            logger.debug("auto_commit: working tree clean for dispatch %s", dispatch_id)
            return False

        # Stage all changes (respects .gitignore — excludes .vnx-data/, .venv/, etc.)
        add_proc = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True, timeout=15,
            cwd=Path(__file__).resolve().parents[2],
        )
        if add_proc.returncode != 0:
            logger.warning("auto_commit: git add failed for %s: %s", dispatch_id, add_proc.stderr)
            return False

        gate_tag = gate or dispatch_id[:12]
        commit_msg = f"feat({gate_tag}): auto-commit from headless worker {terminal_id}"
        commit_proc = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2],
        )
        if commit_proc.returncode == 0:
            logger.info(
                "Auto-committed uncommitted changes from dispatch %s (terminal=%s)",
                dispatch_id, terminal_id,
            )
            return True
        else:
            logger.warning(
                "auto_commit: git commit failed for %s: %s",
                dispatch_id, commit_proc.stderr,
            )
            return False
    except Exception as exc:
        logger.warning("auto_commit: unexpected error for dispatch %s: %s", dispatch_id, exc)
        return False


def _auto_stash_changes(dispatch_id: str, terminal_id: str) -> bool:
    """Stash uncommitted changes after a failed dispatch (preserves but does not commit).

    Returns True if a stash was created, False otherwise.
    Never raises — all exceptions are logged and swallowed.
    """
    try:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=Path(__file__).resolve().parents[2],
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            return False

        stash_name = f"vnx-auto-stash-{dispatch_id}"
        stash_proc = subprocess.run(
            ["git", "stash", "save", stash_name],
            capture_output=True, text=True, timeout=30,
            cwd=Path(__file__).resolve().parents[2],
        )
        if stash_proc.returncode == 0:
            logger.info(
                "Stashed uncommitted changes from failed dispatch %s (terminal=%s, stash=%s)",
                dispatch_id, terminal_id, stash_name,
            )
            return True
        else:
            logger.warning(
                "auto_stash: git stash failed for %s: %s",
                dispatch_id, stash_proc.stderr,
            )
            return False
    except Exception as exc:
        logger.warning("auto_stash: unexpected error for dispatch %s: %s", dispatch_id, exc)
        return False


def _write_receipt(
    dispatch_id: str,
    terminal_id: str,
    status: str,
    *,
    event_count: int = 0,
    session_id: str | None = None,
    attempt: int | None = None,
    failure_reason: str | None = None,
    commit_missing: bool = False,
    committed: bool = False,
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
    if commit_missing:
        receipt["commit_missing"] = True
    if committed:
        receipt["committed"] = True

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
    auto_commit: bool = True,
    gate: str = "",
) -> bool:
    """Deliver with automatic retry on failure.

    On success, writes a receipt with status="done".
    On final failure (budget exhausted), writes a receipt with status="failed".
    Retries use exponential backoff: 30s, 60s, 120s.

    auto_commit: if True (default), auto-commit uncommitted changes on success,
                 auto-stash on failure.  Pass False to disable.
    gate: gate tag used in the auto-commit message (e.g. "f52-pr3").

    Returns True on success, False on failure.
    """
    dispatch_start_ts = datetime.now(timezone.utc).isoformat()

    # Create health monitor for this dispatch
    monitor = WorkerHealthMonitor(terminal_id, dispatch_id)

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
            health_monitor=monitor,
        )
        if success:
            monitor.mark_completed()
            commit_missing = _check_commit_since(dispatch_start_ts)

            # Post-dispatch commit enforcement
            committed = False
            if auto_commit and commit_missing:
                committed = _auto_commit_changes(dispatch_id, terminal_id, gate=gate)
                if committed:
                    commit_missing = False

            _write_receipt(
                dispatch_id, terminal_id, "done",
                attempt=attempt,
                commit_missing=commit_missing,
                committed=committed,
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
            monitor.mark_completed()
            # Stash uncommitted changes from failed dispatch
            if auto_commit:
                _auto_stash_changes(dispatch_id, terminal_id)
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
    parser.add_argument("--no-auto-commit", action="store_true",
                        help="Disable auto-commit of uncommitted changes after dispatch")
    parser.add_argument("--gate", default="", help="Gate tag for auto-commit message")
    args = parser.parse_args()

    ok = deliver_with_recovery(
        terminal_id=args.terminal_id,
        instruction=args.instruction,
        model=args.model,
        dispatch_id=args.dispatch_id,
        role=args.role,
        max_retries=args.max_retries,
        auto_commit=not args.no_auto_commit,
        gate=args.gate,
    )
    sys.exit(0 if ok else 1)
