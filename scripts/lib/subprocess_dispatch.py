#!/usr/bin/env python3
"""subprocess_dispatch.py — Thin helper for routing dispatch delivery via SubprocessAdapter.

Called from dispatch_deliver.sh when VNX_ADAPTER_T{n}=subprocess is set.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

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


def _inject_skill_context(
    terminal_id: str,
    instruction: str,
    role: str | None = None,
    dispatch_metadata: "dict | None" = None,
) -> str:
    """Compose layered user message context for headless dispatch.

    Uses PromptAssembler (3-layer architecture) when available, with fallback
    to the legacy 3-tier CLAUDE.md resolution for backward compatibility.

    Layer architecture (PromptAssembler path):
      Layer 1 — Base worker context (universal rules, report format)
      Layer 2 — Role context (capabilities, permissions for the role)
      Layer 3 — Dispatch payload (passed through as instruction)

    Legacy fallback (3-tier CLAUDE.md resolution):
      1. agents/{role}/CLAUDE.md        — project-level agent override
      2. .claude/skills/{role}/CLAUDE.md — skill definition
      3. .claude/terminals/{terminal}/CLAUDE.md — terminal fallback

    Args:
        terminal_id:       Terminal identifier (e.g. "T1").
        instruction:       Raw dispatch instruction text.
        role:              Agent role (e.g. "backend-developer").
        dispatch_metadata: Optional metadata dict forwarded to PromptAssembler
                           for L3 enrichments (dispatch_id, gate, pr, track, model,
                           intelligence, historical).  Merged with terminal+role.

    Returns the full pipe_input string ready for `claude -p`.
    """
    try:
        from prompt_assembler import PromptAssembler  # noqa: PLC0415
        assembler = PromptAssembler()
        meta = dict(dispatch_metadata or {})
        meta.setdefault("role", role or "")
        meta.setdefault("terminal", terminal_id)
        assembled = assembler.assemble(
            dispatch_metadata=meta,
            instruction=instruction,
        )
        logger.info(
            "_inject_skill_context: assembler path — role=%s L1=%d L2=%d L3=%d chars",
            assembled.metadata.get("role"),
            assembled.metadata.get("layer1_chars", 0),
            assembled.metadata.get("layer2_chars", 0),
            assembled.metadata.get("layer3_chars", 0),
        )
        return assembled.to_pipe_input()
    except Exception as exc:
        logger.warning(
            "_inject_skill_context: PromptAssembler failed (%s) — falling back to legacy CLAUDE.md resolution",
            exc,
        )

    # Legacy fallback: 3-tier CLAUDE.md resolution
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


class _SubprocessResult(NamedTuple):
    """Return value from deliver_via_subprocess() carrying stats back to the caller."""
    success: bool
    session_id: str | None
    event_count: int
    manifest_path: str | None


def _get_commit_hash() -> str:
    """Return current HEAD commit hash, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[2],
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.debug("_get_commit_hash failed: %s", exc)
        return ""


def _get_current_branch() -> str:
    """Return current branch name, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[2],
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.debug("_get_current_branch failed: %s", exc)
        return ""


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
        }
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        logger.info("Manifest written: %s", manifest_path)
        return str(manifest_path)
    except Exception as exc:
        logger.warning("_write_manifest failed for %s: %s", dispatch_id, exc)
        return None


def _promote_manifest(dispatch_id: str) -> str | None:
    """Copy manifest from active/ to completed/ after dispatch finishes.

    Returns the completed manifest path as a string, or None on failure.
    """
    src_dir = _dispatch_manifest_dir("active", dispatch_id)
    dst_dir = _dispatch_manifest_dir("completed", dispatch_id)
    src = src_dir / "manifest.json"
    if not src.exists():
        return None
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "manifest.json"
        shutil.copy2(src, dst)
        logger.info("Manifest promoted: %s -> %s", src, dst)
        return str(dst)
    except Exception as exc:
        logger.warning("_promote_manifest failed for %s: %s", dispatch_id, exc)
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
    commit_hash_before: str = "",
) -> "_SubprocessResult":
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

    Returns _SubprocessResult(success, session_id, event_count, manifest_path).
    """
    # Append repo map to instruction before skill context wrapping
    if repo_map:
        instruction = instruction + f"\n\n{repo_map}"

    # Compose layered user message (L1 base + L2 role + L3 dispatch payload)
    instruction = _inject_skill_context(
        terminal_id,
        instruction,
        role=role,
        dispatch_metadata={
            "dispatch_id": dispatch_id,
            "model": model,
        },
    )

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

    # Write dispatch manifest before launching subprocess
    manifest_path = _write_manifest(
        dispatch_id=dispatch_id,
        terminal_id=terminal_id,
        model=model,
        role=role,
        instruction=instruction,
        commit_hash_before=commit_hash_before,
        branch=_get_current_branch(),
    )

    adapter = SubprocessAdapter()
    result = adapter.deliver(
        terminal_id,
        dispatch_id,
        instruction=instruction,
        model=model,
        cwd=agent_cwd,
    )
    if not result.success:
        return _SubprocessResult(success=False, session_id=None, event_count=0, manifest_path=manifest_path)

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

    event_count = 0
    _last_stuck_log_time = 0.0
    try:
        for _event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            event_count += 1
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
        session_id = adapter.get_session_id(terminal_id)
        completed_manifest = _promote_manifest(dispatch_id)
        return _SubprocessResult(
            success=True,
            session_id=session_id,
            event_count=event_count,
            manifest_path=completed_manifest or manifest_path,
        )
    except Exception:
        logger.exception("deliver_via_subprocess failed for %s", terminal_id)
        return _SubprocessResult(success=False, session_id=None, event_count=event_count, manifest_path=manifest_path)
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)
        # Archive events for this dispatch and clear the live file immediately so
        # the next dispatch starts with an empty file (guarantees 100% archival).
        event_store = adapter._get_event_store()
        if event_store is not None:
            try:
                event_store.clear(terminal_id, archive_dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.debug("deliver_via_subprocess: event archive+clear failed: %s", _exc)
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
        commit_msg = (
            f"feat({gate_tag}): auto-commit from headless worker {terminal_id}\n\n"
            f"Dispatch-ID: {dispatch_id}"
        )
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
    commit_hash_before: str = "",
    commit_hash_after: str = "",
    manifest_path: str | None = None,
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
    if commit_hash_before:
        receipt["commit_hash_before"] = commit_hash_before
    if commit_hash_after:
        receipt["commit_hash_after"] = commit_hash_after
    if commit_hash_before and commit_hash_after:
        receipt["committed"] = committed or (commit_hash_before != commit_hash_after)
    elif committed:
        receipt["committed"] = True
    if manifest_path:
        receipt["manifest_path"] = manifest_path
    if attempt is not None:
        receipt["attempt"] = attempt
    if failure_reason:
        receipt["failure_reason"] = failure_reason
    if commit_missing:
        receipt["commit_missing"] = True

    receipt_path = _default_state_dir() / "t0_receipts.ndjson"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with open(receipt_path, "a") as f:
        f.write(json.dumps(receipt) + "\n")

    logger.info(
        "Receipt written: dispatch=%s terminal=%s status=%s",
        dispatch_id, terminal_id, status,
    )
    return receipt_path


def _capture_dispatch_parameters(
    dispatch_id: str,
    instruction: str,
    terminal_id: str,
    model: str,
    role: str | None,
    repo_map: str | None,
) -> None:
    """Capture DispatchParameters to dispatch_tracker.db. Never raises."""
    try:
        from dispatch_parameter_tracker import (
            DispatchParameterTracker,
            extract_parameters,
        )
        params = extract_parameters(
            instruction=instruction,
            terminal_id=terminal_id,
            model=model,
            role=role,
            repo_map=repo_map,
        )
        tracker = DispatchParameterTracker()
        tracker.capture_parameters(dispatch_id, params)
        logger.debug(
            "Parameter capture: dispatch=%s chars=%d ctx=%d role=%s",
            dispatch_id,
            params.instruction_char_count,
            params.context_item_count,
            params.role,
        )
    except Exception as exc:
        logger.debug("Parameter capture failed for %s: %s", dispatch_id, exc)


def _capture_dispatch_outcome(
    dispatch_id: str,
    success: bool,
    start_ts: str,
    committed: bool,
) -> None:
    """Capture DispatchOutcome after completion. Never raises."""
    try:
        from dispatch_parameter_tracker import (
            DispatchParameterTracker,
            DispatchOutcome,
            _count_lines_changed,
            _lookup_cqs,
        )

        # Compute completion minutes
        try:
            start_dt = datetime.fromisoformat(start_ts)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - start_dt).total_seconds() / 60.0
        except Exception:
            elapsed = 0.0

        outcome = DispatchOutcome(
            cqs=_lookup_cqs(dispatch_id),
            success=success,
            completion_minutes=round(elapsed, 2),
            test_count=0,        # not reliably parseable here
            committed=committed,
            lines_changed=_count_lines_changed(start_ts),
        )
        tracker = DispatchParameterTracker()
        tracker.capture_outcome(dispatch_id, outcome)
        logger.debug(
            "Outcome capture: dispatch=%s success=%s mins=%.1f cqs=%s",
            dispatch_id, success, elapsed, outcome.cqs,
        )
    except Exception as exc:
        logger.debug("Outcome capture failed for %s: %s", dispatch_id, exc)


def _update_pattern_confidence(
    dispatch_id: str,
    status: str,
    db_path: "Path",
) -> int:
    """Update confidence for patterns that were injected in this dispatch.

    Looks up pattern_usage rows where dispatch_id matches, then:
    - success: boosts success_patterns.confidence_score + 0.05 (cap 1.0) and
               increments pattern_usage.success_count + used_count
    - failure: decays success_patterns.confidence_score - 0.10 (floor 0.0) and
               increments pattern_usage.failure_count + used_count

    Linkage is by title: pattern_usage.pattern_title → success_patterns.title.
    Returns count of pattern_usage rows updated.  Never raises.
    """
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timezone as _tz

    if not db_path.exists():
        return 0

    is_success = (status == "success")
    now = _dt.now(_tz.utc).isoformat()
    updated = 0

    try:
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row

        injected = conn.execute(
            "SELECT pattern_id, pattern_title FROM pattern_usage WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchall()

        for row in injected:
            pattern_id = row["pattern_id"]
            title = row["pattern_title"]

            if is_success:
                conn.execute(
                    """
                    UPDATE success_patterns
                    SET confidence_score = MIN(confidence_score + 0.05, 1.0),
                        usage_count      = usage_count + 1,
                        last_used        = ?
                    WHERE title = ?
                    """,
                    (now, title),
                )
                conn.execute(
                    """
                    UPDATE pattern_usage
                    SET used_count    = used_count + 1,
                        success_count = success_count + 1,
                        last_used     = ?,
                        updated_at    = ?
                    WHERE dispatch_id = ? AND pattern_id = ?
                    """,
                    (now, now, dispatch_id, pattern_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE success_patterns
                    SET confidence_score = MAX(confidence_score - 0.10, 0.0)
                    WHERE title = ?
                    """,
                    (title,),
                )
                conn.execute(
                    """
                    UPDATE pattern_usage
                    SET used_count     = used_count + 1,
                        failure_count  = failure_count + 1,
                        last_used      = ?,
                        updated_at     = ?
                    WHERE dispatch_id = ? AND pattern_id = ?
                    """,
                    (now, now, dispatch_id, pattern_id),
                )
            updated += 1

        conn.commit()
        conn.close()
    except Exception as exc:
        logger.debug("_update_pattern_confidence failed for %s: %s", dispatch_id, exc)

    return updated


def deliver_with_recovery(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    *,
    role: str | None = None,
    repo_map: str | None = None,
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
    repo_map: optional repo map string, forwarded to parameter tracker only.

    Returns True on success, False on failure.
    """
    dispatch_start_ts = datetime.now(timezone.utc).isoformat()
    commit_hash_before = _get_commit_hash()

    # Capture dispatch parameters before execution
    _capture_dispatch_parameters(
        dispatch_id=dispatch_id,
        instruction=instruction,
        terminal_id=terminal_id,
        model=model,
        role=role,
        repo_map=repo_map,
    )

    # Create health monitor for this dispatch
    monitor = WorkerHealthMonitor(terminal_id, dispatch_id)

    for attempt in range(max_retries + 1):
        sub_result = deliver_via_subprocess(
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
            commit_hash_before=commit_hash_before,
        )
        if sub_result.success:
            monitor.mark_completed()
            commit_hash_after = _get_commit_hash()
            commit_missing = _check_commit_since(dispatch_start_ts)

            # Post-dispatch commit enforcement
            committed = commit_hash_before != commit_hash_after
            if auto_commit and commit_missing and not committed:
                committed = _auto_commit_changes(dispatch_id, terminal_id, gate=gate)
                if committed:
                    commit_missing = False
                    commit_hash_after = _get_commit_hash()

            _write_receipt(
                dispatch_id, terminal_id, "done",
                event_count=sub_result.event_count,
                session_id=sub_result.session_id,
                attempt=attempt,
                commit_missing=commit_missing,
                committed=committed,
                commit_hash_before=commit_hash_before,
                commit_hash_after=commit_hash_after,
                manifest_path=sub_result.manifest_path,
            )

            # Feedback loop: boost pattern confidence for successful dispatch
            _quality_db = _default_state_dir() / "quality_intelligence.db"
            _patt_updated = _update_pattern_confidence(dispatch_id, "success", _quality_db)
            logger.debug(
                "Feedback boost: dispatch=%s patterns_updated=%d", dispatch_id, _patt_updated
            )
            try:
                from intelligence_persist import update_confidence_from_outcome as _upcf
                _upcf(_quality_db, dispatch_id, terminal_id, "success")
            except Exception as _exc:
                logger.debug("update_confidence_from_outcome(success) failed: %s", _exc)

            # Capture outcome after receipt is written
            _capture_dispatch_outcome(
                dispatch_id=dispatch_id,
                success=True,
                start_ts=dispatch_start_ts,
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
                event_count=sub_result.event_count,
                session_id=sub_result.session_id,
                attempt=attempt,
                failure_reason=f"Exhausted {max_retries} retries",
                commit_hash_before=commit_hash_before,
                manifest_path=sub_result.manifest_path,
            )

            # Feedback loop: decay pattern confidence for failed dispatch
            _quality_db = _default_state_dir() / "quality_intelligence.db"
            _patt_updated = _update_pattern_confidence(dispatch_id, "failure", _quality_db)
            logger.debug(
                "Feedback decay: dispatch=%s patterns_updated=%d", dispatch_id, _patt_updated
            )
            try:
                from intelligence_persist import update_confidence_from_outcome as _upcf
                _upcf(_quality_db, dispatch_id, terminal_id, "failure")
            except Exception as _exc:
                logger.debug("update_confidence_from_outcome(failure) failed: %s", _exc)

            # Capture failed outcome
            _capture_dispatch_outcome(
                dispatch_id=dispatch_id,
                success=False,
                start_ts=dispatch_start_ts,
                committed=False,
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
