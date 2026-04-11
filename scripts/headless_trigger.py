#!/usr/bin/env python3
"""F41 Headless Trigger — 3-layer trigger system for headless T0 invocation.

Usage:
    python3 scripts/headless_trigger.py [--watch-dir DIR] [--state-dir DIR] [--dry-run]

Layers:
  1. File watcher    — triggers on new .md files in unified_reports/
  2. Silence watchdog — periodic checks (10 min): stale leases, orphaned/stuck dispatches
  3. LLM triage      — haiku classification before T0 trigger (opt-in via VNX_HAIKU_CLASSIFY=1)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOG = logging.getLogger("headless_trigger")

_DEBOUNCE_SECONDS = 30          # Layer 1: min seconds between T0 triggers
_WATCHDOG_INTERVAL = 600        # Layer 2: silence watchdog interval (10 min)
_STALE_LEASE_SECONDS = 1800     # 30 min without heartbeat → stale
_ORPHANED_DISPATCH_SECONDS = 1200  # 20 min active → orphaned
_STUCK_PENDING_SECONDS = 300    # 5 min pending → stuck
_HAIKU_TIMEOUT = 30             # Layer 3: haiku subprocess timeout
_T0_TIMEOUT = 300               # headless T0 subprocess timeout


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class TriggerState:
    def __init__(self) -> None:
        self.last_trigger_time: float = 0.0
        self.processed_files: set[str] = set()
        self.lock = threading.Lock()
        self.shutdown_event = threading.Event()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _default_state_dir() -> Path:
    if env := os.environ.get("VNX_STATE_DIR"):
        return Path(env)
    if data := os.environ.get("VNX_DATA_DIR"):
        return Path(data) / "state"
    return _REPO_ROOT / ".vnx-data" / "state"


def _default_watch_dir() -> Path:
    if data := os.environ.get("VNX_DATA_DIR"):
        return Path(data) / "unified_reports"
    return _REPO_ROOT / ".vnx-data" / "unified_reports"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Trigger event logging
# ---------------------------------------------------------------------------

def _log_trigger_event(state_dir: Path, reason: str, context: Any, dry_run: bool) -> None:
    events_dir = state_dir.parent / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": _now_utc(),
        "event_type": "headless_trigger",
        "reason": reason,
        "context": str(context)[:500],
        "dry_run": dry_run,
    }
    log_path = events_dir / "headless_triggers.ndjson"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Core trigger
# ---------------------------------------------------------------------------

def trigger_headless_t0(
    reason: str,
    context: Any,
    state_dir: Path,
    dry_run: bool,
    trigger_state: TriggerState,
) -> None:
    """Invoke headless T0; debounce, log, and optionally call claude -p."""
    with trigger_state.lock:
        elapsed = time.monotonic() - trigger_state.last_trigger_time
        if elapsed < _DEBOUNCE_SECONDS:
            _LOG.debug("Debounced trigger (reason=%s, elapsed=%.1fs)", reason, elapsed)
            return
        trigger_state.last_trigger_time = time.monotonic()

    _LOG.info("Triggering headless T0 (reason=%s, dry_run=%s)", reason, dry_run)
    _log_trigger_event(state_dir, reason, context, dry_run)

    if dry_run:
        return

    sys.path.insert(0, str(_REPO_ROOT / "scripts" / "f39"))
    try:
        from context_assembler import (  # noqa: PLC0415
            assemble_t0_context,
            _DEFAULT_FEATURE_PLAN,
            _DEFAULT_SKILL,
            _DEFAULT_CLAUDE_MD,
        )
    except ImportError as exc:
        _LOG.error("Cannot import context_assembler: %s", exc)
        return

    receipt: dict[str, Any] = {
        "dispatch_id": f"trigger-{reason}-{int(time.time())}",
        "event": "headless_trigger",
        "reason": reason,
        "trigger_source": "headless_trigger.py",
        "timestamp": _now_utc(),
        "context": str(context)[:1000],
        "status": "trigger",
        "risk": 0.1,
    }

    try:
        prompt = assemble_t0_context(
            state_path=state_dir / "t0_state.json",
            receipt=receipt,
            feature_plan_path=_DEFAULT_FEATURE_PLAN,
            skill_path=_DEFAULT_SKILL,
            claude_md_path=_DEFAULT_CLAUDE_MD,
        )
    except Exception as exc:
        _LOG.error("context_assembler failed: %s", exc)
        return

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "stream-json", "--verbose", prompt],
            capture_output=True,
            text=True,
            timeout=_T0_TIMEOUT,
            cwd=str(_REPO_ROOT),
        )
        if result.returncode != 0:
            _LOG.warning("claude exited %d: %s", result.returncode, result.stderr[:200])
        else:
            _LOG.info("Headless T0 completed (reason=%s)", reason)
    except subprocess.TimeoutExpired:
        _LOG.error("Headless T0 timed out after %ds (reason=%s)", _T0_TIMEOUT, reason)
    except FileNotFoundError:
        _LOG.error("'claude' CLI not found in PATH")


# ---------------------------------------------------------------------------
# Layer 3: LLM triage
# ---------------------------------------------------------------------------

def _haiku_enabled() -> bool:
    return os.environ.get("VNX_HAIKU_CLASSIFY", "0") not in ("0", "", "false", "False")


def llm_triage(anomalies: list[str]) -> str:
    """Ask haiku to classify: 'stuck' | 'normal' | 'recovering'. Fail-open on timeout."""
    prompt = (
        "You monitor a VNX multi-agent system. Classify the state in ONE word only.\n\n"
        "Anomalies:\n" + "\n".join(f"- {a}" for a in anomalies) + "\n\n"
        "Reply with exactly one of: stuck, normal, recovering\n"
        "stuck=needs intervention  recovering=self-resolving  normal=transient"
    )
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", prompt],
            capture_output=True, text=True, timeout=_HAIKU_TIMEOUT,
        )
        output = result.stdout.strip().lower()
        for word in ("stuck", "normal", "recovering"):
            if word in output:
                return word
        _LOG.warning("Haiku returned unexpected output %r — defaulting to 'stuck'", output[:80])
    except subprocess.TimeoutExpired:
        _LOG.warning("Haiku triage timed out (%ds) — defaulting to 'stuck' (fail-open)", _HAIKU_TIMEOUT)
    except FileNotFoundError:
        _LOG.warning("'claude' not in PATH for haiku triage — defaulting to 'stuck'")
    return "stuck"


# ---------------------------------------------------------------------------
# Layer 2: Silence watchdog
# ---------------------------------------------------------------------------

def _seconds_since(ts: str | None) -> float:
    if not ts:
        return float("inf")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return float("inf")


def check_stale_leases(state_dir: Path) -> list[str]:
    db = state_dir / "runtime_coordination.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db), timeout=5.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT terminal_id, leased_at, last_heartbeat_at FROM terminal_leases WHERE state='leased'"
        ).fetchall()
        conn.close()
        return [
            f"stale_lease: terminal={r['terminal_id']} no-heartbeat {_seconds_since(r['last_heartbeat_at'] or r['leased_at']):.0f}s"
            for r in rows
            if _seconds_since(r["last_heartbeat_at"] or r["leased_at"]) > _STALE_LEASE_SECONDS
        ]
    except Exception as exc:
        _LOG.debug("Lease check error: %s", exc)
        return []


def _scan_old_files(directory: Path, threshold: float) -> list[str]:
    if not directory.exists():
        return []
    now = time.time()
    return [
        f"{directory.name}: {p.name} age {now - p.stat().st_mtime:.0f}s"
        for p in directory.iterdir()
        if p.is_file() and (now - p.stat().st_mtime) > threshold
    ]


def silence_watchdog(
    state_dir: Path, interval: float, trigger_state: TriggerState, dry_run: bool
) -> None:
    """Layer 2: run checks, optionally trigger T0, re-schedule self."""
    if trigger_state.shutdown_event.is_set():
        return

    dispatches_base = state_dir.parent / "dispatches"
    anomalies = (
        check_stale_leases(state_dir)
        + _scan_old_files(dispatches_base / "active", _ORPHANED_DISPATCH_SECONDS)
        + _scan_old_files(dispatches_base / "pending", _STUCK_PENDING_SECONDS)
    )

    if anomalies:
        _LOG.info("Layer 2 anomalies (%d): %s", len(anomalies), anomalies[:3])
        if _haiku_enabled():
            classification = llm_triage(anomalies)
            _LOG.info("Haiku classification: %s", classification)
            if classification == "stuck":
                trigger_headless_t0("silence_anomaly", anomalies, state_dir, dry_run, trigger_state)
        else:
            trigger_headless_t0("silence_anomaly", anomalies, state_dir, dry_run, trigger_state)
    else:
        _LOG.debug("Layer 2: no anomalies detected")

    if not trigger_state.shutdown_event.is_set():
        t = threading.Timer(interval, silence_watchdog, [state_dir, interval, trigger_state, dry_run])
        t.daemon = True
        t.start()


# ---------------------------------------------------------------------------
# Layer 1: File watcher
# ---------------------------------------------------------------------------

class ReportWatcher(FileSystemEventHandler):
    def __init__(self, watch_dir: Path, state_dir: Path, trigger_state: TriggerState, dry_run: bool) -> None:
        self.watch_dir = watch_dir
        self.state_dir = state_dir
        self.trigger_state = trigger_state
        self.dry_run = dry_run

    def on_created(self, event: Any) -> None:
        if event.is_directory or not str(event.src_path).endswith(".md"):
            return
        # Scan ALL unprocessed .md files (idempotency watermark — T3 requirement)
        new_files = [
            str(p) for p in self.watch_dir.rglob("*.md")
            if str(p) not in self.trigger_state.processed_files
        ]
        if not new_files:
            return
        for f in new_files:
            self.trigger_state.processed_files.add(f)
        _LOG.info("Layer 1: %d new report(s) — %s…", len(new_files), new_files[0])
        trigger_headless_t0("new_report", new_files, self.state_dir, self.dry_run, self.trigger_state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="F41 Headless Trigger — 3-layer T0 trigger system")
    parser.add_argument("--watch-dir", default=None, help="Directory to watch for report files")
    parser.add_argument("--state-dir", default=None, help="VNX state dir (contains runtime_coordination.db)")
    parser.add_argument("--dry-run", action="store_true", help="Log triggers but do not invoke claude")
    parser.add_argument("--watchdog-interval", type=float, default=_WATCHDOG_INTERVAL, metavar="SECS")
    args = parser.parse_args()

    state_dir = Path(args.state_dir) if args.state_dir else _default_state_dir()
    watch_dir = Path(args.watch_dir) if args.watch_dir else _default_watch_dir()
    watch_dir.mkdir(parents=True, exist_ok=True)

    trigger_state = TriggerState()

    def _on_signal(signum: int, _frame: Any) -> None:
        _LOG.info("Signal %d received — shutting down", signum)
        trigger_state.shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    _LOG.info("headless_trigger starting (watch=%s state=%s dry_run=%s)", watch_dir, state_dir, args.dry_run)

    # Seed processed-files set so we don't re-trigger on existing reports at startup
    for p in watch_dir.rglob("*.md"):
        trigger_state.processed_files.add(str(p))
    _LOG.info("Layer 1: seeded %d existing files, watching %s", len(trigger_state.processed_files), watch_dir)

    observer = Observer()
    observer.schedule(ReportWatcher(watch_dir, state_dir, trigger_state, args.dry_run), str(watch_dir), recursive=True)
    observer.start()

    t = threading.Timer(args.watchdog_interval, silence_watchdog, [state_dir, args.watchdog_interval, trigger_state, args.dry_run])
    t.daemon = True
    t.start()
    _LOG.info("Layer 2: silence watchdog first run in %.0fs", args.watchdog_interval)

    while not trigger_state.shutdown_event.is_set():
        trigger_state.shutdown_event.wait(timeout=1.0)

    observer.stop()
    observer.join()
    _LOG.info("headless_trigger stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
