#!/usr/bin/env python3
"""subprocess_dispatch.py — Thin helper for routing dispatch delivery via SubprocessAdapter.

Called from dispatch_deliver.sh when VNX_ADAPTER_T{n}=subprocess is set.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import hashlib
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
from headless_context_tracker import HeadlessContextTracker
from worker_health_monitor import WorkerHealthMonitor, HealthStatus, SLOW_THRESHOLD
from cleanup_worker_exit import cleanup_worker_exit

logger = logging.getLogger(__name__)


def _resolve_active_dispatch_file(dispatch_id: str) -> Path | None:
    """Locate the dispatch file in dispatches/active/ for cleanup_worker_exit.

    Returns None when no matching file exists (e.g. file already moved by
    another path).  Used by the deliver_with_recovery exit hooks.
    """
    data_dir = os.environ.get("VNX_DATA_DIR", "")
    base = (
        Path(data_dir) / "dispatches" / "active"
        if data_dir
        else Path(__file__).resolve().parents[2] / ".vnx-data" / "dispatches" / "active"
    )
    if not base.is_dir():
        return None
    for path in base.iterdir():
        if path.is_file() and dispatch_id in path.name:
            return path
    return None


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


def _build_intelligence_section(dispatch_id: str, role: str | None) -> str:
    """Return formatted intelligence items as markdown, or empty string (best-effort).

    Calls IntelligenceSelector to gather antipatterns, success patterns, and
    recent comparable dispatches from quality_intelligence.db.  After selecting,
    emits the coordination event and records the injection (intelligence_injections
    + pattern_usage + dispatch_pattern_offered) so the post-dispatch confidence
    feedback loop has dispatch-scoped rows to update.  Any import or DB failure
    is caught and logged — dispatch proceeds without intelligence.
    """
    try:
        from intelligence_selector import IntelligenceSelector  # noqa: PLC0415
        state_dir = _default_state_dir()
        quality_db_path = state_dir / "quality_intelligence.db"
        selector = IntelligenceSelector(
            quality_db_path=quality_db_path,
            coord_db_state_dir=state_dir,
        )
        try:
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point="dispatch_create",
                skill_name=role or "",
            )
            # Persist the selection so the feedback loop can find which patterns
            # were offered for this dispatch.  Best-effort — never raises.
            try:
                selector.emit_event(result, coord_state_dir=state_dir)
            except Exception as exc:
                logger.debug("emit_event failed for %s: %s", dispatch_id, exc)
            try:
                selector.record_injection(result, coord_state_dir=state_dir)
            except Exception as exc:
                logger.debug("record_injection failed for %s: %s", dispatch_id, exc)
        finally:
            selector.close()
        if not result.items:
            return ""
        by_class: dict[str, list] = {}
        for item in result.items:
            by_class.setdefault(item.item_class, []).append(item)
        parts: list[str] = []
        if "failure_prevention" in by_class:
            parts.append("### Antipatterns to avoid")
            for item in by_class["failure_prevention"]:
                parts.append(f"- **{item.title}**: {item.content}")
            parts.append("")
        if "proven_pattern" in by_class:
            parts.append("### Proven success patterns")
            for item in by_class["proven_pattern"]:
                parts.append(f"- **{item.title}**: {item.content}")
            parts.append("")
        if "recent_comparable" in by_class:
            parts.append("### Tag warnings")
            for item in by_class["recent_comparable"]:
                parts.append(f"- **{item.title}**: {item.content}")
            parts.append("")
        return "\n".join(parts)
    except Exception as exc:
        logger.warning("intelligence injection failed (%s); proceeding without", exc)
        return ""


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
    # Gather intelligence before assembling prompt (best-effort)
    _dispatch_id = (dispatch_metadata or {}).get("dispatch_id") or ""
    intelligence_section = _build_intelligence_section(_dispatch_id, role)

    try:
        from prompt_assembler import PromptAssembler  # noqa: PLC0415
        assembler = PromptAssembler()
        meta = dict(dispatch_metadata or {})
        meta.setdefault("role", role or "")
        meta.setdefault("terminal", terminal_id)
        if intelligence_section:
            meta.setdefault("intelligence", intelligence_section)
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
            if intelligence_section:
                return (
                    f"{context}\n\n---\n\n"
                    f"## Relevant Intelligence (from past dispatches)\n\n"
                    f"{intelligence_section}\n"
                    f"---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"
                )
            return f"{context}\n\n---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"

    if intelligence_section:
        return (
            f"## Relevant Intelligence (from past dispatches)\n\n"
            f"{intelligence_section}\n"
            f"---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"
        )
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
    # Repo-relative paths the worker explicitly wrote/edited via structured tool
    # calls (Write/Edit/MultiEdit/NotebookEdit) during this dispatch.  Used by
    # _auto_commit_changes / _auto_stash_changes to scope staging to *this*
    # worker's writes, even in shared worktrees where concurrent terminals or
    # the operator may produce additional dirty files during the dispatch
    # window.  Empty frozenset() when no structured file writes occurred.
    touched_files: frozenset[str] = frozenset()


# Tool names whose ``input`` block names a file path the worker is modifying.
# Read/Bash/Glob/Grep are deliberately excluded — they do not modify files
# (or, in Bash's case, may modify them but cannot be reliably parsed).  Workers
# that rely on Bash for file modifications must commit those changes manually.
_FILE_WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _normalize_repo_path(path_str: str, repo_root: Path) -> str | None:
    """Convert a tool-event file_path to a repo-relative POSIX string.

    Returns None when ``path_str`` resolves outside ``repo_root`` or is empty
    or unparseable.  The result is suitable for matching against
    ``git status --porcelain`` output, which always uses POSIX-style
    repo-relative paths.

    Symlink-resolves both sides so a path like ``./foo/../bar.py`` collapses
    to ``bar.py`` and a worktree-relative path matches the repo root.
    """
    if not path_str:
        return None
    try:
        p = Path(path_str)
        if not p.is_absolute():
            p = repo_root / p
        try:
            resolved = p.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved = p
        try:
            root_resolved = repo_root.resolve(strict=False)
        except (OSError, RuntimeError):
            root_resolved = repo_root
        rel = resolved.relative_to(root_resolved)
        return rel.as_posix()
    except (ValueError, OSError):
        return None


def _extract_touched_paths_from_event(event: "StreamEvent | object") -> list[str]:  # type: ignore[name-defined]
    """Return raw ``file_path`` / ``notebook_path`` strings from a tool_use event.

    Accepts a ``StreamEvent`` (or any object exposing ``.type`` + ``.data``).
    Returns an empty list for non-tool_use events or tools that do not write
    files.  Path normalization (repo-relative, in-repo filtering) is performed
    by the caller via ``_normalize_repo_path``.
    """
    event_type = getattr(event, "type", None)
    if event_type != "tool_use":
        return []
    data = getattr(event, "data", {}) or {}
    name = data.get("name", "")
    if name not in _FILE_WRITING_TOOLS:
        return []
    tool_input = data.get("input") or {}
    if not isinstance(tool_input, dict):
        return []
    paths: list[str] = []
    if name == "NotebookEdit":
        candidate = tool_input.get("notebook_path") or tool_input.get("file_path")
        if isinstance(candidate, str):
            paths.append(candidate)
    else:
        candidate = tool_input.get("file_path")
        if isinstance(candidate, str):
            paths.append(candidate)
    return paths


def _parse_dirty_files(porcelain_output: str) -> frozenset:
    """Parse 'git status --porcelain' output into a frozenset of relative file paths."""
    files: set[str] = set()
    for line in porcelain_output.splitlines():
        if not line.strip():
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.add(path_part.strip())
    return frozenset(files)


def _get_dirty_files(cwd: Path) -> set[str]:
    """Return the set of dirty (modified/untracked) file paths from git status --porcelain.

    Handles rename lines ("old -> new") by capturing only the destination path.
    Returns an empty set on any failure so callers can safely subtract.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        files: set[str] = set()
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            # git status --porcelain: XY<space>filename (or XY<space>old -> new)
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1]
            files.add(path_part)
        return files
    except Exception as exc:
        logger.debug("_get_dirty_files failed: %s", exc)
        return set()


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
            "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16],
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
    chunk_timeout: float = 300.0,
    total_deadline: float = 900.0,
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
    # Allow runtime override via env vars so operators can tune without code changes.
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
    except (KeyError, ValueError):
        pass

    # Detect pending handover and wrap instruction for seamless continuation.
    # Runs on every attempt (including retries) so continuation is always fresh.
    handover_dir = _default_state_dir().parent / "rotation_handovers"
    pending_handover = _detect_pending_handover(terminal_id, handover_dir)
    if pending_handover is not None:
        logger.info(
            "deliver_via_subprocess: pending handover found for %s: %s",
            terminal_id, pending_handover,
        )
        instruction = _build_continuation_prompt(pending_handover, instruction)

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

    # Load prior session ID for --resume (opt-in via VNX_SESSION_RESUME=1)
    resume_session: str | None = None
    if os.environ.get("VNX_SESSION_RESUME", "0") == "1":
        try:
            from session_store import SessionStore as _SessionStore
            resume_session = _SessionStore().load(terminal_id)
            if resume_session:
                logger.info(
                    "deliver_via_subprocess: resuming %s with session_id=%s",
                    terminal_id, resume_session,
                )
        except Exception as _exc:
            logger.debug("deliver_via_subprocess: session load failed: %s", _exc)

    adapter = SubprocessAdapter()
    result = adapter.deliver(
        terminal_id,
        dispatch_id,
        instruction=instruction,
        model=model,
        cwd=agent_cwd,
        resume_session=resume_session,
    )
    if not result.success:
        return _SubprocessResult(
            success=False,
            session_id=None,
            event_count=0,
            manifest_path=manifest_path,
            touched_files=frozenset(),
        )

    # Resolve repo root once for path normalization; agent_cwd may point into
    # a sub-directory but the repo root anchors all git status output.
    _repo_root = Path(__file__).resolve().parents[2]
    _touched_files: set[str] = set()

    # Wire event_store into health_monitor so STUCK events are persisted to NDJSON
    if health_monitor is not None and health_monitor._event_store is None:
        _es = adapter._get_event_store()
        if _es is not None:
            health_monitor._event_store = _es

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

    tracker = HeadlessContextTracker(model_context_limit=200_000)
    event_count = 0
    _last_stuck_log_time = 0.0
    rotation_triggered = False
    try:
        for _event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            event_count += 1
            # Track files written by this dispatch's structured tool calls so
            # auto-commit / auto-stash can scope to *this* worker's writes,
            # not whatever else became dirty in a shared worktree.
            for raw_path in _extract_touched_paths_from_event(_event):
                norm = _normalize_repo_path(raw_path, _repo_root)
                if norm:
                    _touched_files.add(norm)
            # Update the context tracker.  ``_event`` is a StreamEvent
            # (dataclass with ``.type``/``.data`` attrs); HeadlessContextTracker
            # accepts either a StreamEvent-like object or a plain dict thanks
            # to its internal normalisation, so this call is type-safe even
            # though the unit tests pass plain dicts.
            tracker.update(_event)
            # Detect rotation as soon as the threshold is crossed and write
            # the handover exactly once — checking *inside* the loop avoids
            # the prior bug where a brief late-stream context spike on a
            # normally-completing dispatch flipped the post-loop
            # ``should_rotate`` check and returned success=False.  Once
            # rotation fires we stop the subprocess so the worker can resume
            # via the handover on the next dispatch.
            if not rotation_triggered and tracker.should_rotate:
                rotation_triggered = True
                _write_rotation_handover(terminal_id, dispatch_id, tracker)
                try:
                    adapter.stop(terminal_id)
                except Exception as _exc:
                    logger.debug(
                        "deliver_via_subprocess: adapter.stop after rotation failed: %s",
                        _exc,
                    )
                break
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

        # Rotation is a graceful stop: surface as success=False so
        # deliver_with_recovery's auto-stash protects partial work and the
        # next dispatch (or the immediate retry) sees the handover.  We
        # *only* take this branch when rotation was actually triggered
        # mid-stream — never based on the post-loop tracker state alone.
        if rotation_triggered:
            completed_manifest = _promote_manifest(dispatch_id)
            return _SubprocessResult(
                success=False,
                session_id=session_id,
                event_count=event_count,
                manifest_path=completed_manifest or manifest_path,
                touched_files=frozenset(_touched_files),
            )

        # Fail-closed: non-zero exit code means failure even when events were parsed.
        obs = adapter.observe(terminal_id)
        returncode = obs.transport_state.get("returncode")
        completed_manifest = _promote_manifest(dispatch_id)
        if returncode is not None and returncode != 0:
            logger.warning(
                "deliver_via_subprocess: subprocess exited %d for %s — fail-closed",
                returncode,
                terminal_id,
            )
            return _SubprocessResult(
                success=False,
                session_id=session_id,
                event_count=event_count,
                manifest_path=completed_manifest or manifest_path,
                touched_files=frozenset(_touched_files),
            )
        # Fail-closed: timeout-terminated dispatches must not be classified as success.
        # stop() removes the process from _processes so returncode above is None,
        # but was_timed_out() tracks the authoritative kill-by-timeout signal.
        if adapter.was_timed_out(terminal_id):
            logger.warning(
                "deliver_via_subprocess: timeout-terminated dispatch %s for %s — fail-closed",
                dispatch_id,
                terminal_id,
            )
            return _SubprocessResult(
                success=False,
                session_id=session_id,
                event_count=event_count,
                manifest_path=completed_manifest or manifest_path,
                touched_files=frozenset(_touched_files),
            )

        # Only persist session_id once all fail-closed checks pass, so the next
        # dispatch (with VNX_SESSION_RESUME=1) cannot resume a failed or
        # timeout-killed conversation.
        if session_id and os.environ.get("VNX_SESSION_RESUME", "0") == "1":
            try:
                from session_store import SessionStore as _SessionStore
                _SessionStore().save(terminal_id, session_id, dispatch_id=dispatch_id)
            except Exception as _exc:
                logger.debug("deliver_via_subprocess: session save failed: %s", _exc)

        # Mark handover as processed after successful delivery (not rotation)
        if pending_handover is not None and pending_handover.exists():
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

        return _SubprocessResult(
            success=True,
            session_id=session_id,
            event_count=event_count,
            manifest_path=completed_manifest or manifest_path,
            touched_files=frozenset(_touched_files),
        )
    except Exception:
        logger.exception("deliver_via_subprocess failed for %s", terminal_id)
        return _SubprocessResult(
            success=False,
            session_id=None,
            event_count=event_count,
            manifest_path=manifest_path,
            touched_files=frozenset(_touched_files),
        )
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


def _check_commit_since(dispatch_start_ts: str, dispatch_id: str | None = None) -> bool:
    """Return True if no commits attributable to this dispatch are found.

    When ``dispatch_id`` is provided, the check is *dispatch-scoped*: only
    commits whose message contains ``Dispatch-ID: <dispatch_id>`` count as
    "this dispatch committed".  This prevents another terminal's commit
    landing during the dispatch window from being mis-attributed to this
    dispatch — a real correctness risk in shared worktrees where multiple
    headless workers operate concurrently.

    When ``dispatch_id`` is None, falls back to the legacy time-window check
    (any commit since ``dispatch_start_ts``) for backward compatibility with
    callers that haven't been updated.

    Never raises.
    """
    if dispatch_id:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "log",
                    "--all",
                    f"--since={dispatch_start_ts}",
                    f"--grep=Dispatch-ID: {dispatch_id}",
                    "--oneline",
                    "-5",
                ],
                capture_output=True, text=True, timeout=10,
                cwd=Path(__file__).resolve().parents[2],
            )
            dispatch_commits = [l for l in proc.stdout.splitlines() if l.strip()]
            if not dispatch_commits:
                logger.warning(
                    "receipt_must_have_commit: no commits with 'Dispatch-ID: %s' "
                    "found since %s (commit attribution scoped to this dispatch)",
                    dispatch_id, dispatch_start_ts,
                )
                return True
            return False
        except Exception as exc:
            logger.debug(
                "dispatch-scoped commit check failed for %s: %s — falling back to time-window check",
                dispatch_id, exc,
            )

    # Legacy / fallback path: time-window check via GovernanceEnforcer
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


def _commit_belongs_to_dispatch(commit_hash: str, dispatch_id: str) -> bool:
    """Return True if the given commit's message contains the dispatch_id marker.

    Used by deliver_with_recovery to determine whether a HEAD change between
    pre-dispatch and post-dispatch was actually produced by THIS dispatch
    (vs. a concurrent commit from another terminal in a shared worktree).

    Never raises.  Returns False on any error or empty input.
    """
    if not commit_hash or not dispatch_id:
        return False
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--pretty=%B", commit_hash],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[2],
        )
        if proc.returncode != 0:
            return False
        return f"Dispatch-ID: {dispatch_id}" in proc.stdout
    except Exception as exc:
        logger.debug(
            "_commit_belongs_to_dispatch failed for %s/%s: %s",
            commit_hash, dispatch_id, exc,
        )
        return False


def _auto_commit_changes(
    dispatch_id: str,
    terminal_id: str,
    gate: str = "",
    pre_dispatch_dirty: "set[str] | None" = None,
    dispatch_touched_files: "frozenset[str] | set[str] | None" = None,
) -> bool:
    """Stage and commit changes introduced by this dispatch.

    Two safety filters compose to determine the file set staged:

    1. ``pre_dispatch_dirty`` — files dirty *before* the dispatch started.
       Excluded from staging so pre-existing operator/agent edits are never
       swept into this worker's commit.
    2. ``dispatch_touched_files`` — files this dispatch's worker explicitly
       wrote via structured tool calls (Write/Edit/MultiEdit/NotebookEdit).
       In a *shared* or *concurrently-edited* worktree, files that became
       dirty during the dispatch window may have been written by another
       terminal or by the operator, not by this worker.  Intersecting with
       this set prevents auto-commit from sweeping those concurrent edits.

    Both kwargs are REQUIRED.  Passing ``None`` for either causes the helper
    to refuse to commit (fail-safe) — better to leave changes uncommitted
    than to sweep unrelated work into this worker's commit.  An empty set is
    treated as "no eligible files" and is therefore also a no-op (correct: a
    worker that performed no structured file writes should not auto-commit).

    Returns True if a commit was made, False otherwise.
    Never raises — all exceptions are logged and swallowed.
    """
    if pre_dispatch_dirty is None:
        logger.warning(
            "auto_commit: pre_dispatch_dirty=None — refusing to commit for dispatch %s "
            "(would otherwise sweep unrelated dirty files via git add -A)",
            dispatch_id,
        )
        return False
    if dispatch_touched_files is None:
        logger.warning(
            "auto_commit: dispatch_touched_files=None — refusing to commit for dispatch %s "
            "(cannot distinguish this worker's writes from concurrent edits in a shared worktree)",
            dispatch_id,
        )
        return False
    try:
        cwd = Path(__file__).resolve().parents[2]
        # Check for uncommitted changes
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            logger.debug("auto_commit: working tree clean for dispatch %s", dispatch_id)
            return False

        # Scope staging to (files that became dirty during this dispatch) ∩
        # (files this dispatch's worker explicitly wrote).  The intersection
        # is the deepest scoping signal available: it filters out both
        # pre-existing dirty files and concurrent-terminal edits that happen
        # to land within the dispatch window.
        current_dirty = _get_dirty_files(cwd)
        new_during_dispatch = current_dirty - pre_dispatch_dirty
        touched = set(dispatch_touched_files)
        files_to_stage = sorted(new_during_dispatch & touched)
        if not files_to_stage:
            ignored_dispatch_dirty = sorted(new_during_dispatch - touched)
            if ignored_dispatch_dirty:
                logger.warning(
                    "auto_commit: %d dispatch-window dirty file(s) not in "
                    "touched_files — refusing to commit (likely concurrent "
                    "edits from another terminal). dispatch=%s files=%s",
                    len(ignored_dispatch_dirty),
                    dispatch_id,
                    ignored_dispatch_dirty[:10],
                )
            else:
                logger.debug(
                    "auto_commit: no dispatch-touched files dirty for dispatch %s "
                    "(all dirty files pre-existed the dispatch)",
                    dispatch_id,
                )
            return False
        add_cmd = ["git", "add", "--"] + files_to_stage

        add_proc = subprocess.run(
            add_cmd,
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
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
            cwd=cwd,
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


def _auto_stash_changes(
    dispatch_id: str,
    terminal_id: str,
    pre_dispatch_dirty: "set[str] | None" = None,
    dispatch_touched_files: "frozenset[str] | set[str] | None" = None,
) -> bool:
    """Stash changes introduced by this dispatch after a failure (preserves but does not commit).

    Two safety filters compose to determine the file set stashed:

    1. ``pre_dispatch_dirty`` — files dirty *before* the dispatch started.
       Excluded from the stash so pre-existing edits remain in the worktree
       and are not hidden from the operator or other terminals.
    2. ``dispatch_touched_files`` — files this dispatch's worker explicitly
       wrote via structured tool calls.  In a shared worktree, files that
       became dirty during the dispatch window may have been written by
       another terminal — those must NOT be stashed under this dispatch's
       name.

    Both kwargs are REQUIRED.  Passing ``None`` for either causes the helper
    to refuse to stash (fail-safe).  An empty ``dispatch_touched_files`` is
    a legitimate "no structured writes happened" signal and also yields a
    no-op stash.

    Returns True if a stash was created, False otherwise.
    Never raises — all exceptions are logged and swallowed.
    """
    if pre_dispatch_dirty is None:
        logger.warning(
            "auto_stash: pre_dispatch_dirty=None — refusing to stash for dispatch %s "
            "(would otherwise sweep unrelated dirty files into a global stash)",
            dispatch_id,
        )
        return False
    if dispatch_touched_files is None:
        logger.warning(
            "auto_stash: dispatch_touched_files=None — refusing to stash for dispatch %s "
            "(cannot distinguish this worker's writes from concurrent edits in a shared worktree)",
            dispatch_id,
        )
        return False
    try:
        cwd = Path(__file__).resolve().parents[2]
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            return False

        stash_name = f"vnx-auto-stash-{dispatch_id}"

        current_dirty = _get_dirty_files(cwd)
        new_during_dispatch = current_dirty - pre_dispatch_dirty
        touched = set(dispatch_touched_files)
        files_to_stash = sorted(new_during_dispatch & touched)
        if not files_to_stash:
            ignored_dispatch_dirty = sorted(new_during_dispatch - touched)
            if ignored_dispatch_dirty:
                logger.warning(
                    "auto_stash: %d dispatch-window dirty file(s) not in "
                    "touched_files — refusing to stash (likely concurrent "
                    "edits from another terminal). dispatch=%s files=%s",
                    len(ignored_dispatch_dirty),
                    dispatch_id,
                    ignored_dispatch_dirty[:10],
                )
            else:
                logger.debug(
                    "auto_stash: no dispatch-touched files dirty for dispatch %s "
                    "(all dirty files pre-existed the dispatch)",
                    dispatch_id,
                )
            return False
        # -u includes untracked files matching the specified paths so
        # newly-created files from the failed dispatch are also captured.
        stash_cmd = ["git", "stash", "push", "-u", "-m", stash_name, "--"] + files_to_stash

        stash_proc = subprocess.run(
            stash_cmd,
            capture_output=True, text=True, timeout=30,
            cwd=cwd,
        )
        if stash_proc.returncode == 0:
            logger.info(
                "Stashed %d dispatch-produced file(s) from failed dispatch %s "
                "(terminal=%s, stash=%s)",
                len(files_to_stash), dispatch_id, terminal_id, stash_name,
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
    stuck_event_count: int = 0,
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
    if stuck_event_count:
        receipt["stuck_event_count"] = stuck_event_count

    _scripts_dir = Path(__file__).resolve().parents[1]
    try:
        sys.path.insert(0, str(_scripts_dir))
        from append_receipt import append_receipt_payload
        result = append_receipt_payload(receipt)
        receipt_path = result.receipts_file
        if result.status == "duplicate":
            logger.debug(
                "Receipt already appended (idempotent skip): dispatch=%s", dispatch_id
            )
        else:
            logger.info(
                "Receipt written: dispatch=%s terminal=%s status=%s",
                dispatch_id, terminal_id, status,
            )
        return receipt_path
    except Exception as exc:
        # Fallback: bare write to prevent receipt loss on import error (e.g. circular import)
        logger.warning(
            "append_receipt_payload failed (%s); falling back to bare write", exc
        )
        receipt_path = _default_state_dir() / "t0_receipts.ndjson"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(receipt_path, "a") as f:
            f.write(json.dumps(receipt) + "\n")
        logger.info(
            "Receipt written (bare): dispatch=%s terminal=%s status=%s",
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
    """Update confidence for patterns that were OFFERED in this dispatch.

    Looks up dispatch_pattern_offered (or legacy pattern_usage.dispatch_id) rows
    matching this dispatch, then:
    - success: boosts success_patterns.confidence_score + 0.05 (cap 1.0) and
               touches pattern_usage.last_used + updated_at
    - failure: decays success_patterns.confidence_score - 0.10 (floor 0.0) and
               touches pattern_usage.last_used + updated_at

    NOTE: pattern_usage.used_count must NOT be incremented here.  Existing
    consumers treat used_count > 0 as evidence that a worker actually consumed
    a pattern, not merely that it was offered.  Likewise success_count and
    failure_count are reserved for confirmed worker usage outcomes.  The
    legacy fallback only increments success_count when usage is unknown; the
    offered-only feedback loop touches timestamps and the confidence_score in
    success_patterns instead.

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

        # Query dispatch_pattern_offered (isolated per-dispatch junction table) so that
        # patterns offered to multiple concurrent dispatches are not misattributed.
        # Falls back to pattern_usage.dispatch_id for DBs that predate the junction table.
        offered_table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='dispatch_pattern_offered'"
        ).fetchone()

        if offered_table_exists:
            injected = conn.execute(
                "SELECT pattern_id, pattern_title FROM dispatch_pattern_offered "
                "WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchall()
        else:
            injected = conn.execute(
                "SELECT pattern_id, pattern_title FROM pattern_usage "
                "WHERE dispatch_id = ?",
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
                        last_used        = ?
                    WHERE title = ?
                    """,
                    (now, title),
                )
                # Offered-only path: do NOT touch used_count, success_count, or
                # failure_count here.  Those are reserved for confirmed worker
                # usage signals (see learning_loop.update_confidence_scores).
                conn.execute(
                    """
                    UPDATE pattern_usage
                    SET last_used  = ?,
                        updated_at = ?
                    WHERE pattern_id = ?
                    """,
                    (now, now, pattern_id),
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
                    SET last_used  = ?,
                        updated_at = ?
                    WHERE pattern_id = ?
                    """,
                    (now, now, pattern_id),
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
    chunk_timeout: float = 300.0,
    total_deadline: float = 900.0,
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
    # Allow runtime override via env vars so operators can tune without code changes.
    try:
        chunk_timeout = float(os.environ["VNX_CHUNK_TIMEOUT"])
    except (KeyError, ValueError):
        pass
    try:
        total_deadline = float(os.environ["VNX_TOTAL_DEADLINE"])
    except (KeyError, ValueError):
        pass

    dispatch_start_ts = datetime.now(timezone.utc).isoformat()
    commit_hash_before = _get_commit_hash()
    # Snapshot dirty files before dispatch so auto-commit/stash can scope to
    # changes introduced by this dispatch only (not pre-existing dirty state).
    _repo_cwd = Path(__file__).resolve().parents[2]
    pre_dispatch_dirty = _get_dirty_files(_repo_cwd)

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
            commit_missing = _check_commit_since(dispatch_start_ts, dispatch_id=dispatch_id)

            # Post-dispatch commit enforcement.
            #
            # ``committed`` must mean "this dispatch produced a commit", not
            # "HEAD moved during the dispatch window" — in a shared worktree
            # another terminal can commit concurrently, which would otherwise
            # be mis-attributed here.  We require BOTH (a) HEAD changed and
            # (b) the new commit message references this dispatch_id.
            head_moved = bool(
                commit_hash_before and commit_hash_after and commit_hash_before != commit_hash_after
            )
            committed = head_moved and _commit_belongs_to_dispatch(
                commit_hash_after, dispatch_id
            )
            if auto_commit and commit_missing and not committed:
                committed = _auto_commit_changes(
                    dispatch_id, terminal_id, gate=gate,
                    pre_dispatch_dirty=pre_dispatch_dirty,
                    dispatch_touched_files=sub_result.touched_files,
                )
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
                stuck_event_count=monitor.stuck_count,
            )

            # Feedback loop: boost pattern confidence for successful dispatch
            # update_confidence_from_outcome is handled by append_receipt_payload (VNX-R4)
            _quality_db = _default_state_dir() / "quality_intelligence.db"
            _patt_updated = _update_pattern_confidence(dispatch_id, "success", _quality_db)
            logger.debug(
                "Feedback boost: dispatch=%s patterns_updated=%d", dispatch_id, _patt_updated
            )

            # Capture outcome after receipt is written
            _capture_dispatch_outcome(
                dispatch_id=dispatch_id,
                success=True,
                start_ts=dispatch_start_ts,
                committed=committed,
            )

            # Single-owner post-exit cleanup (SUP-PR1).  Idempotent — the bash
            # caller's rc_release_lease may have already released the lease,
            # which is fine; this records the worker state transition and
            # dispatch_register audit event regardless.
            cleanup_worker_exit(
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                exit_status="success",
                lease_generation=lease_generation,
                dispatch_file=_resolve_active_dispatch_file(dispatch_id),
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
                _auto_stash_changes(
                    dispatch_id,
                    terminal_id,
                    pre_dispatch_dirty=pre_dispatch_dirty,
                    dispatch_touched_files=sub_result.touched_files,
                )
            _write_receipt(
                dispatch_id, terminal_id, "failed",
                event_count=sub_result.event_count,
                session_id=sub_result.session_id,
                attempt=attempt,
                failure_reason=f"Exhausted {max_retries} retries",
                commit_hash_before=commit_hash_before,
                manifest_path=sub_result.manifest_path,
                stuck_event_count=monitor.stuck_count,
            )

            # Feedback loop: decay pattern confidence for failed dispatch
            # update_confidence_from_outcome is handled by append_receipt_payload (VNX-R4)
            _quality_db = _default_state_dir() / "quality_intelligence.db"
            _patt_updated = _update_pattern_confidence(dispatch_id, "failure", _quality_db)
            logger.debug(
                "Feedback decay: dispatch=%s patterns_updated=%d", dispatch_id, _patt_updated
            )

            # Capture failed outcome
            _capture_dispatch_outcome(
                dispatch_id=dispatch_id,
                success=False,
                start_ts=dispatch_start_ts,
                committed=False,
            )

            # Single-owner post-exit cleanup (SUP-PR1).  Routes failure-path
            # disposition (move to rejected/failure/, transition worker to
            # exited_bad, audit) through the same helper as the bash caller.
            cleanup_worker_exit(
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                exit_status="failure",
                lease_generation=lease_generation,
                dispatch_file=_resolve_active_dispatch_file(dispatch_id),
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
