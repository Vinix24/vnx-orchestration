#!/usr/bin/env python3
"""VNX Status and Process UX commands (PR-7).

Provides operator-facing visibility and scoped process controls:
  - status:  Show terminals, claimed dispatches, queue status, open-item summary
  - ps:      Show PID, parent PID, uptime, and health for active VNX processes
  - cleanup: Detect and remove orphan processes without affecting healthy sessions
  - restart: Restart a specific managed process

Exit codes:
  0  - Success
  1  - Command failed or process not found
  10 - Invalid arguments

Usage:
  python vnx_process_ux.py status
  python vnx_process_ux.py ps [--json]
  python vnx_process_ux.py cleanup [--dry-run]
  python vnx_process_ux.py restart <process-name>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_paths import ensure_env

# Known managed processes: name -> script filename
MANAGED_PROCESSES = {
    "dispatcher": "dispatcher_v8_minimal.sh",
    "smart_tap": "smart_tap_v7_json_translator.sh",
    "receipt_processor": "receipt_processor_v4.sh",
    "heartbeat_ack_monitor": "heartbeat_ack_monitor.py",
    "queue_watcher": "queue_popup_watcher.sh",
    "dashboard": "generate_valid_dashboard.sh",
    "state_manager": "unified_state_manager_v2.py",
    "intelligence_daemon": "intelligence_daemon.py",
    "recommendations_engine": "recommendations_engine_daemon.sh",
    "vnx_supervisor": "vnx_supervisor_simple.sh",
}


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# PID metadata
# ---------------------------------------------------------------------------

class PidMetadata:
    """Enhanced PID metadata with parent PID and start timestamp."""

    METADATA_SUFFIX = ".meta.json"

    @staticmethod
    def write(pids_dir: Path, name: str, pid: int) -> Path:
        """Write enhanced PID metadata for a managed process."""
        meta = {
            "name": name,
            "pid": pid,
            "ppid": _get_ppid(pid),
            "started_at": _utc_now_iso(),
            "owner": _get_process_owner(pid),
            "command": _get_process_command(pid),
        }
        meta_path = pids_dir / f"{name}{PidMetadata.METADATA_SUFFIX}"
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        return meta_path

    @staticmethod
    def read(pids_dir: Path, name: str) -> Optional[Dict[str, Any]]:
        """Read enhanced PID metadata if available."""
        meta_path = pids_dir / f"{name}{PidMetadata.METADATA_SUFFIX}"
        if not meta_path.exists():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def remove(pids_dir: Path, name: str) -> None:
        """Remove PID metadata file."""
        meta_path = pids_dir / f"{name}{PidMetadata.METADATA_SUFFIX}"
        meta_path.unlink(missing_ok=True)


def _get_ppid(pid: int) -> Optional[int]:
    """Get parent PID for a process."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        pass
    return None


def _get_process_owner(pid: int) -> Optional[str]:
    """Get process owner username."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "user="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _get_process_command(pid: int) -> Optional[str]:
    """Get process command line."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _get_process_etime(pid: int) -> Optional[str]:
    """Get process elapsed time (uptime)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etime="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _read_pid_file(pid_file: Path) -> Optional[int]:
    """Read a PID from a .pid file."""
    try:
        content = pid_file.read_text(encoding="utf-8").strip()
        return int(content) if content else None
    except (OSError, ValueError):
        return None


def _read_fingerprint(pid_file: Path) -> Optional[str]:
    """Read the fingerprint file associated with a PID file."""
    fp_file = pid_file.parent / f"{pid_file.name}.fingerprint"
    try:
        return fp_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------

def cmd_status(paths: Dict[str, str]) -> int:
    """Show terminals, claimed dispatches, queue status, and open-item summary."""
    state_dir = Path(paths["VNX_STATE_DIR"])
    project_root = Path(paths["PROJECT_ROOT"])
    session_name = f"vnx-{project_root.name}"

    # tmux session check
    tmux_running = False
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=5,
        )
        tmux_running = result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    print(f"\n  VNX Status — {project_root.name}")
    print(f"  {'=' * 50}")
    print(f"  Session: {session_name}  {'[ACTIVE]' if tmux_running else '[STOPPED]'}")
    print()

    # Terminal state
    ts_file = state_dir / "terminal_state.json"
    if ts_file.exists():
        try:
            ts_data = json.loads(ts_file.read_text(encoding="utf-8"))
            terminals = ts_data.get("terminals", {})
            print("  Terminals:")
            for tid in sorted(terminals.keys()):
                t = terminals[tid]
                status = t.get("status", "unknown")
                claimed = t.get("claimed_by")
                icon = {"idle": ".", "working": ">", "blocked": "!"}.get(status, "?")
                claim_str = f"  <- {claimed}" if claimed else ""
                wt = t.get("worktree_path")
                wt_str = f"  [{Path(wt).name}]" if wt else ""
                print(f"    [{icon}] {tid}: {status}{claim_str}{wt_str}")
        except (json.JSONDecodeError, OSError):
            print("  Terminals: (state unavailable)")
    else:
        print("  Terminals: (no state file)")

    print()

    # Dispatches summary
    dispatch_dir = Path(paths["VNX_DISPATCH_DIR"])
    for sub in ("pending", "active"):
        d = dispatch_dir / sub
        if d.is_dir():
            count = len(list(d.glob("*.md")))
            if count:
                print(f"  Dispatches ({sub}): {count}")

    # Queue status
    queue_file = state_dir / "pr_queue_state.json"
    if queue_file.exists():
        try:
            q = json.loads(queue_file.read_text(encoding="utf-8"))
            prs = q.get("prs", [])
            completed = [p for p in prs if p.get("status") == "completed"]
            active = q.get("active", [])
            total = len(prs)
            pct = int(len(completed) / total * 100) if total else 0
            bar_filled = pct // 10
            bar = f"{'█' * bar_filled}{'░' * (10 - bar_filled)}"
            print(f"\n  Queue: {bar} {pct}% ({len(completed)}/{total} PRs)")
            if active:
                print(f"  Active: {', '.join(active)}")
        except (json.JSONDecodeError, OSError):
            pass

    # Open items summary
    oi_file = state_dir / "open_items.json"
    if oi_file.exists():
        try:
            oi = json.loads(oi_file.read_text(encoding="utf-8"))
            items = oi.get("items", [])
            open_items = [i for i in items if i.get("status") == "open"]
            blockers = sum(1 for i in open_items if i.get("severity") == "blocker")
            warns = sum(1 for i in open_items if i.get("severity") == "warn")
            infos = sum(1 for i in open_items if i.get("severity") == "info")
            if open_items:
                print(f"\n  Open Items: {len(open_items)} total", end="")
                parts = []
                if blockers:
                    parts.append(f"{blockers} blocker")
                if warns:
                    parts.append(f"{warns} warn")
                if infos:
                    parts.append(f"{infos} info")
                if parts:
                    print(f" ({', '.join(parts)})", end="")
                print()
            else:
                print("\n  Open Items: none")
        except (json.JSONDecodeError, OSError):
            pass

    print()
    return 0


# ---------------------------------------------------------------------------
# Command: ps
# ---------------------------------------------------------------------------

def cmd_ps(paths: Dict[str, str], json_output: bool = False) -> int:
    """Show PID, parent PID, uptime, and health for active VNX processes."""
    pids_dir = Path(paths["VNX_PIDS_DIR"])

    if not pids_dir.is_dir():
        if json_output:
            print(json.dumps({"processes": []}, indent=2))
        else:
            print("  No PID directory found.")
        return 0

    processes: List[Dict[str, Any]] = []

    for pid_file in sorted(pids_dir.glob("*.pid")):
        name = pid_file.stem
        pid = _read_pid_file(pid_file)
        if pid is None:
            continue

        alive = _is_pid_alive(pid)
        fingerprint = _read_fingerprint(pid_file)
        meta = PidMetadata.read(pids_dir, name)

        entry: Dict[str, Any] = {
            "name": name,
            "pid": pid,
            "ppid": meta.get("ppid") if meta else _get_ppid(pid) if alive else None,
            "alive": alive,
            "uptime": _get_process_etime(pid) if alive else None,
            "started_at": meta.get("started_at") if meta else None,
            "owner": meta.get("owner") if meta else (_get_process_owner(pid) if alive else None),
            "fingerprint": fingerprint,
            "health": "running" if alive else "dead",
        }
        processes.append(entry)

    if json_output:
        print(json.dumps({"processes": processes, "checked_at": _utc_now_iso()}, indent=2))
        return 0

    if not processes:
        print("  No managed processes found.")
        return 0

    print(f"\n  {'NAME':<28s} {'PID':>7s} {'PPID':>7s} {'UPTIME':>12s} {'HEALTH':>8s}")
    print(f"  {'-' * 28} {'-' * 7} {'-' * 7} {'-' * 12} {'-' * 8}")

    for p in processes:
        health_icon = "ok" if p["alive"] else "DEAD"
        ppid_str = str(p["ppid"]) if p["ppid"] else "-"
        uptime_str = p["uptime"] or "-"
        print(f"  {p['name']:<28s} {p['pid']:>7d} {ppid_str:>7s} {uptime_str:>12s} {health_icon:>8s}")

    alive_count = sum(1 for p in processes if p["alive"])
    dead_count = sum(1 for p in processes if not p["alive"])
    print(f"\n  Total: {len(processes)}  Running: {alive_count}  Dead: {dead_count}")
    print()
    return 0


# ---------------------------------------------------------------------------
# Command: cleanup
# ---------------------------------------------------------------------------

def cmd_cleanup(paths: Dict[str, str], dry_run: bool = False) -> int:
    """Detect and remove orphan processes without affecting healthy scoped sessions."""
    pids_dir = Path(paths["VNX_PIDS_DIR"])
    locks_dir = Path(paths["VNX_LOCKS_DIR"])

    if not pids_dir.is_dir():
        print("  No PID directory found — nothing to clean.")
        return 0

    cleaned_pids = 0
    cleaned_locks = 0

    # Clean stale PID files
    for pid_file in sorted(pids_dir.glob("*.pid")):
        name = pid_file.stem
        pid = _read_pid_file(pid_file)
        if pid is None:
            if dry_run:
                print(f"  [dry-run] Would remove invalid PID file: {name}")
            else:
                pid_file.unlink(missing_ok=True)
                fp_file = pid_file.parent / f"{pid_file.name}.fingerprint"
                fp_file.unlink(missing_ok=True)
                PidMetadata.remove(pids_dir, name)
                cleaned_pids += 1
            continue

        if not _is_pid_alive(pid):
            if dry_run:
                print(f"  [dry-run] Would clean orphan: {name} (PID {pid} is dead)")
            else:
                pid_file.unlink(missing_ok=True)
                fp_file = pid_file.parent / f"{pid_file.name}.fingerprint"
                fp_file.unlink(missing_ok=True)
                PidMetadata.remove(pids_dir, name)
                cleaned_pids += 1

    # Clean stale lock directories
    if locks_dir.is_dir():
        for lock_dir in sorted(locks_dir.iterdir()):
            if not lock_dir.is_dir() or not lock_dir.name.endswith(".lock"):
                continue
            lock_pid_file = lock_dir / "pid"
            if lock_pid_file.exists():
                try:
                    lock_pid = int(lock_pid_file.read_text(encoding="utf-8").strip())
                    if _is_pid_alive(lock_pid):
                        continue  # Lock is held by a live process
                except (ValueError, OSError):
                    pass

            if dry_run:
                print(f"  [dry-run] Would remove stale lock: {lock_dir.name}")
            else:
                import shutil
                shutil.rmtree(lock_dir, ignore_errors=True)
                cleaned_locks += 1

    if dry_run:
        print("  (dry-run mode — no changes made)")
    else:
        print(f"  Cleaned: {cleaned_pids} orphan PID(s), {cleaned_locks} stale lock(s)")

    return 0


# ---------------------------------------------------------------------------
# Command: restart
# ---------------------------------------------------------------------------

def cmd_restart(paths: Dict[str, str], process_name: str) -> int:
    """Restart a specific managed process."""
    pids_dir = Path(paths["VNX_PIDS_DIR"])
    scripts_dir = Path(paths["VNX_HOME"]) / "scripts"
    logs_dir = Path(paths["VNX_LOGS_DIR"])

    if process_name not in MANAGED_PROCESSES:
        print(f"  Unknown process: {process_name}")
        print(f"  Known processes: {', '.join(sorted(MANAGED_PROCESSES.keys()))}")
        return 1

    script_name = MANAGED_PROCESSES[process_name]
    script_path = scripts_dir / script_name
    pid_file = pids_dir / f"{process_name}.pid"
    log_file = logs_dir / f"{process_name}.log"

    if not script_path.exists():
        print(f"  Script not found: {script_path}")
        return 1

    # Stop existing process if running
    old_pid = _read_pid_file(pid_file)
    if old_pid and _is_pid_alive(old_pid):
        print(f"  Stopping {process_name} (PID {old_pid})...")
        try:
            os.kill(old_pid, signal.SIGTERM)
            for _ in range(10):
                if not _is_pid_alive(old_pid):
                    break
                time.sleep(0.5)
            else:
                if _is_pid_alive(old_pid):
                    os.kill(old_pid, signal.SIGKILL)
                    time.sleep(1)
        except (OSError, ProcessLookupError):
            pass

        # Clean up old PID files
        pid_file.unlink(missing_ok=True)
        fp_file = pids_dir / f"{process_name}.pid.fingerprint"
        fp_file.unlink(missing_ok=True)
        PidMetadata.remove(pids_dir, process_name)

    # Start new process
    print(f"  Starting {process_name}...")
    pids_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    if script_name.endswith(".py"):
        cmd = [sys.executable, str(script_path)]
    else:
        cmd = ["bash", str(script_path)]

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            cmd,
            cwd=str(scripts_dir),
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    new_pid = proc.pid

    # Write PID file + fingerprint + metadata
    pid_file.write_text(str(new_pid) + "\n", encoding="utf-8")
    fp_file = pids_dir / f"{process_name}.pid.fingerprint"
    fp_file.write_text(str(script_path.resolve()) + "\n", encoding="utf-8")
    PidMetadata.write(pids_dir, process_name, new_pid)

    # Health check
    time.sleep(2)
    if _is_pid_alive(new_pid):
        print(f"  {process_name} started (PID {new_pid})")
        return 0
    else:
        print(f"  {process_name} crashed immediately after start")
        pid_file.unlink(missing_ok=True)
        fp_file.unlink(missing_ok=True)
        PidMetadata.remove(pids_dir, process_name)
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="VNX Status and Process UX"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show session, terminals, queue, and open items")

    ps_parser = sub.add_parser("ps", help="Show managed process status")
    ps_parser.add_argument("--json", action="store_true", default=False, help="JSON output")

    cleanup_parser = sub.add_parser("cleanup", help="Remove orphan PIDs and stale locks")
    cleanup_parser.add_argument("--dry-run", action="store_true", default=False, help="Preview without changes")

    restart_parser = sub.add_parser("restart", help="Restart a managed process")
    restart_parser.add_argument("process", help=f"Process name ({', '.join(sorted(MANAGED_PROCESSES.keys()))})")

    args = parser.parse_args()
    paths = ensure_env()

    if args.command == "status":
        return cmd_status(paths)
    elif args.command == "ps":
        return cmd_ps(paths, json_output=args.json)
    elif args.command == "cleanup":
        return cmd_cleanup(paths, dry_run=args.dry_run)
    elif args.command == "restart":
        return cmd_restart(paths, args.process)

    return 0


if __name__ == "__main__":
    sys.exit(main())
