#!/usr/bin/env python3
"""Fallback reconciler for terminal_state.json.

Read-only by default. In repair mode it can rewrite terminal_state.json from
fallback evidence (receipts, tmux activity, process health).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from terminal_state_shadow import (
    SCHEMA_VERSION,
    TERMINAL_STATE_FILENAME,
    validate_terminal_state_document,
)

TERMINALS = ("T1", "T2", "T3")
RECEIPTS_FILENAME = "t0_receipts.ndjson"
PANES_FILENAME = "panes.json"

WORKING_EVENTS = {"task_started", "task_ack", "dispatch_sent", "task_claimed"}
COMPLETE_EVENTS = {"task_complete", "completion", "task_finished", "done"}
FAIL_EVENTS = {"task_timeout", "timeout", "task_failed", "failed", "error"}
AGENT_RUNTIME_COMMANDS = {"claude", "codex", "gemini", "node", "python", "python3", "zsh", "bash", "sh"}
AGENT_RUNTIME_SUBSTRINGS = ("claude", "codex", "gemini")


@dataclass
class ReconcilerConfig:
    stale_after_seconds: int = 180
    receipt_scan_lines: int = 600
    active_window_seconds: int = 240
    allow_tmux_probe: bool = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _max_ts(*items: Optional[datetime]) -> Optional[datetime]:
    values = [item for item in items if item is not None]
    return max(values) if values else None


def _read_recent_receipts(path: Path, max_lines: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    out: List[Dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _normalize_receipt_event(receipt: Dict[str, Any]) -> str:
    event = str(receipt.get("event_type") or receipt.get("event") or receipt.get("type") or "").strip().lower()
    status = str(receipt.get("status") or "").strip().lower()

    if event in WORKING_EVENTS:
        return "working"
    if event in COMPLETE_EVENTS:
        return "idle"
    if event in FAIL_EVENTS:
        if status == "no_confirmation":
            return "blocked"
        return "idle"

    if status in {"pending", "working", "active", "claimed", "in_progress"}:
        return "working"
    if status in {"success", "complete", "completed", "done", "idle"}:
        return "idle"
    if status == "no_confirmation":
        return "blocked"
    if status in {"blocked", "failed", "error", "timeout"}:
        return "idle"

    return "unknown"


def _load_primary(path: Path) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    health = {
        "exists": path.exists(),
        "valid": False,
        "stale": False,
        "error": None,
        "mtime": None,
        "age_seconds": None,
    }
    if path.exists():
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        health["mtime"] = _iso(mtime)
        health["age_seconds"] = int((_now_utc() - mtime).total_seconds())

    if not path.exists():
        health["error"] = "primary_missing"
        return None, health

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        validate_terminal_state_document(payload)
        health["valid"] = True
        return payload, health
    except Exception as exc:
        health["error"] = f"primary_corrupt:{exc.__class__.__name__}"
        return None, health


def _probe_tmux(terminals: Iterable[str], state_dir: Path, allow_tmux_probe: bool) -> Dict[str, Dict[str, Any]]:
    panes_file = state_dir / PANES_FILENAME
    pane_map: Dict[str, str] = {}
    if panes_file.exists():
        try:
            panes = json.loads(panes_file.read_text(encoding="utf-8"))
            for term in terminals:
                key = term.lower()
                pane_map[term] = (
                    panes.get(key, {}).get("pane_id")
                    or panes.get(term, {}).get("pane_id")
                    or ""
                )
        except Exception:
            pane_map = {}

    tmux_table: Dict[str, Dict[str, Any]] = {}
    pane_runtime: Dict[str, Dict[str, str]] = {}

    if allow_tmux_probe:
        try:
            output = subprocess.check_output(
                [
                    "tmux",
                    "list-panes",
                    "-a",
                    "-F",
                    "#{pane_id}\t#{pane_current_command}\t#{pane_pid}\t#{pane_dead}\t#{pane_title}\t#{pane_active}",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for row in output.splitlines():
                parts = row.split("\t", 5)
                if len(parts) != 6:
                    continue
                pane_runtime[parts[0]] = {
                    "command": parts[1],
                    "pid": parts[2],
                    "dead": parts[3],
                    "title": parts[4],
                    "active": parts[5],
                }
        except Exception:
            pane_runtime = {}

    now = _now_utc()
    for term in terminals:
        log_path = state_dir / f"{term.lower()}_conversation.log"
        log_activity: Optional[datetime] = None
        if log_path.exists():
            try:
                log_activity = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                log_activity = None

        pane_id = pane_map.get(term) or ""
        pane = pane_runtime.get(pane_id, {})
        cmd = pane.get("command")
        dead = pane.get("dead") == "1"
        pane_alive = bool(pane_id and pane and not dead)

        tmux_table[term] = {
            "pane_id": pane_id or None,
            "pane_alive": pane_alive,
            "current_command": cmd or None,
            "pane_title": pane.get("title") or None,
            "pane_active": pane.get("active") == "1",
            "recent_log_activity": _iso(log_activity),
            "seconds_since_log_activity": (
                int((now - log_activity).total_seconds()) if log_activity else None
            ),
        }
    return tmux_table


def _probe_process_health() -> Dict[str, Any]:
    proc_patterns = {
        "dispatcher": r"dispatcher_v8_minimal|dispatcher_v7_compilation",
        "receipt_processor": r"receipt_processor_v4",
        "heartbeat_monitor": r"heartbeat_ack_monitor",
    }

    processes: Dict[str, bool] = {}
    for name, pattern in proc_patterns.items():
        try:
            code = subprocess.call(
                ["pgrep", "-f", pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes[name] = code == 0
        except Exception:
            processes[name] = False

    running_count = sum(1 for ok in processes.values() if ok)
    return {
        "processes": processes,
        "core_pipeline_healthy": running_count >= 2,
    }


def _detect_split_brain(records: Dict[str, Dict[str, Any]]) -> List[str]:
    claim_to_terminals: Dict[str, List[str]] = {}
    for term, record in records.items():
        claim = record.get("claimed_by")
        if isinstance(claim, str) and claim and claim.lower() != "unknown":
            claim_to_terminals.setdefault(claim, []).append(term)

    conflicts: List[str] = []
    for claim, terminals in claim_to_terminals.items():
        if len(terminals) > 1:
            conflicts.append(f"split_brain_conflicting_claim:{claim}:{','.join(sorted(terminals))}")
    return conflicts


def _status_from_signals(
    base_status: Optional[str],
    receipt_status: Optional[str],
    claim_active: bool,
    tmux_info: Dict[str, Any],
    process_health: Dict[str, Any],
    active_window_seconds: int,
) -> str:
    if receipt_status in {"working", "blocked"}:
        return receipt_status
    receipt_idle = receipt_status == "idle"

    log_age = tmux_info.get("seconds_since_log_activity")
    pane_alive = bool(tmux_info.get("pane_alive"))
    cmd = str(tmux_info.get("current_command") or "").lower()
    pane_title = str(tmux_info.get("pane_title") or "")
    base = str(base_status or "").strip().lower()
    is_agent_runtime = cmd in AGENT_RUNTIME_COMMANDS or any(token in cmd for token in AGENT_RUNTIME_SUBSTRINGS)
    title_shows_activity = bool(re.match(r"^\s*[\u2800-\u28FF]", pane_title))

    if pane_alive and title_shows_activity:
        return "active"

    if pane_alive and is_agent_runtime:
        if log_age is not None and log_age <= active_window_seconds:
            return "active"
        # Terminal can be waiting for user confirmation while still owning claim.
        if claim_active:
            return "working"
        return "idle"

    # Support wrapped runtimes that report a generic command while primary state still says working.
    if pane_alive and log_age is not None and log_age <= active_window_seconds and base in {"working", "claimed", "active", "in_progress"}:
        return "active"

    if pane_alive and claim_active and base in {"working", "claimed", "active", "in_progress"}:
        return "working"

    if not pane_alive and process_health.get("core_pipeline_healthy"):
        if base in {"working", "claimed", "active"}:
            return "unknown"
        return "idle"

    if receipt_idle:
        return "idle"

    return base_status or "unknown"


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    validate_terminal_state_document(payload)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def reconcile_terminal_state(
    state_dir: str | Path,
    *,
    config: Optional[ReconcilerConfig] = None,
    repair: bool = False,
    now: Optional[datetime] = None,
    tmux_probe: Optional[Callable[[Iterable[str], Path, bool], Dict[str, Dict[str, Any]]]] = None,
    process_probe: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    cfg = config or ReconcilerConfig()
    now_utc = now.astimezone(timezone.utc) if now else _now_utc()
    state_root = Path(state_dir)

    primary_path = state_root / TERMINAL_STATE_FILENAME
    receipts_path = state_root / RECEIPTS_FILENAME

    primary_doc, primary_health = _load_primary(primary_path)
    if primary_health.get("valid") and isinstance(primary_health.get("age_seconds"), int):
        if int(primary_health["age_seconds"]) > cfg.stale_after_seconds:
            primary_health["stale"] = True
            primary_health["error"] = "primary_stale"

    receipts = _read_recent_receipts(receipts_path, cfg.receipt_scan_lines)
    tmux_info = (tmux_probe or _probe_tmux)(TERMINALS, state_root, cfg.allow_tmux_probe)
    process_health = (process_probe or _probe_process_health)()

    by_terminal_receipt: Dict[str, Dict[str, Any]] = {}
    for entry in receipts:
        term = str(entry.get("terminal") or entry.get("from") or "").upper()
        if term not in TERMINALS:
            continue
        ts = _parse_iso(entry.get("timestamp"))
        prev = by_terminal_receipt.get(term)
        prev_ts = prev.get("_ts") if isinstance(prev, dict) else None
        # Prefer latest appended receipt for each terminal.
        # Prefer receipts with timestamps over those without.
        # If both have timestamps, use the later one.
        should_replace = (
            prev is None
            or (ts is not None and prev_ts is None)  # Current has timestamp, prev doesn't
            or (ts is not None and prev_ts is not None and ts >= prev_ts)  # Both have timestamps, current is newer
            or (ts is None and prev_ts is None)  # Neither has timestamp, use current (order in file)
        )
        if should_replace:
            by_terminal_receipt[term] = {
                "_ts": ts,
                "status": _normalize_receipt_event(entry),
                "raw_status": str(entry.get("status") or "").strip().lower(),
                "dispatch_id": entry.get("dispatch_id"),
                "task_id": entry.get("task_id"),
            }

    records: Dict[str, Dict[str, Any]] = {}
    for term in TERMINALS:
        primary_record = (primary_doc or {}).get("terminals", {}).get(term, {})
        receipt_record = by_terminal_receipt.get(term, {})
        receipt_ts = receipt_record.get("_ts")

        claimed_by = primary_record.get("claimed_by")
        if (not claimed_by) and receipt_record.get("status") in {"working", "blocked"}:
            claimed_by = receipt_record.get("dispatch_id") or receipt_record.get("task_id")
        lease_expires_at = primary_record.get("lease_expires_at")
        lease_expires_ts = _parse_iso(lease_expires_at)
        claim_active = bool(claimed_by) and (lease_expires_ts is None or lease_expires_ts > now_utc)

        base_last_activity = _parse_iso(primary_record.get("last_activity"))
        tmux_last = _parse_iso((tmux_info.get(term) or {}).get("recent_log_activity"))
        last_activity = _max_ts(base_last_activity, receipt_ts, tmux_last)

        status = _status_from_signals(
            str(primary_record.get("status") or "") or None,
            receipt_record.get("status"),
            claim_active,
            tmux_info.get(term, {}),
            process_health,
            cfg.active_window_seconds,
        )

        source = "primary"
        if not primary_doc or primary_health.get("stale"):
            source = "fallback"
        elif receipt_record or tmux_info.get(term):
            source = "hybrid"

        record = {
            "terminal_id": term,
            "status": status,
            "claimed_by": claimed_by,
            "claimed_at": primary_record.get("claimed_at"),
            "lease_expires_at": lease_expires_at,
            "last_activity": _iso(last_activity) or _iso(now_utc),
            "version": int(primary_record.get("version", 1)) if isinstance(primary_record, dict) else 1,
            "reconciled_source": source,
        }

        # Receipt completion/failure should clear stale claims. Explicit
        # no_confirmation blocks keep ownership on the current dispatch.
        if receipt_record.get("status") == "idle":
            record["claimed_by"] = None
            record["claimed_at"] = None
            record["lease_expires_at"] = None

        # Clear expired leases - if claim is not active, remove ownership
        if not claim_active and claimed_by:
            record["claimed_by"] = None
            record["claimed_at"] = None
            record["lease_expires_at"] = None

        records[term] = record

    degraded_reasons: List[str] = []
    if primary_health.get("error"):
        degraded_reasons.append(str(primary_health["error"]))
    if not process_health.get("core_pipeline_healthy"):
        degraded_reasons.append("process_health_degraded")

    split_brain = _detect_split_brain(records)
    degraded_reasons.extend(split_brain)

    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now_utc),
        "mode": "repair" if repair else "read_only",
        "degraded": len(degraded_reasons) > 0,
        "degraded_reasons": degraded_reasons,
        "primary_health": primary_health,
        "process_health": process_health,
        "terminals": records,
        "evidence": {
            "receipts_scanned": len(receipts),
            "receipt_file": str(receipts_path),
            "tmux": tmux_info,
        },
    }

    if repair:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "terminals": {term: {k: v for k, v in rec.items() if k in {
                "terminal_id",
                "status",
                "claimed_by",
                "claimed_at",
                "lease_expires_at",
                "last_activity",
                "version",
            }} for term, rec in records.items()},
        }
        _atomic_write(primary_path, payload)
        result["repair_written"] = str(primary_path)

    return result
