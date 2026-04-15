#!/usr/bin/env python3
"""headless_orchestrator.py — Single entry point for the autonomous loop.

Combines ReceiptWatcher, DispatchDaemon, and Silence Watchdog into one
managed process with health monitoring and DecisionRouter wiring.

CLI:
    python3 scripts/headless_orchestrator.py [--dry-run] [--log-level DEBUG]

BILLING SAFETY: No Anthropic SDK. CLI-only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

logger = logging.getLogger("headless_orchestrator")

_HEALTH_INTERVAL = 30.0         # seconds between health file writes
_SHUTDOWN_JOIN_TIMEOUT = 10.0   # max seconds to wait per thread on shutdown
_WATCHDOG_INTERVAL = 600.0      # silence watchdog period (10 min)
_LOOP_LOG_NAME = "autonomous_loop.ndjson"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_data_dir() -> Path:
    env = os.environ.get("VNX_DATA_DIR", "")
    return Path(env) if env else _REPO_ROOT / ".vnx-data"


def _default_state_dir() -> Path:
    env = os.environ.get("VNX_STATE_DIR", "")
    return Path(env) if env else _default_data_dir() / "state"


# ---------------------------------------------------------------------------
# Event bus record
# ---------------------------------------------------------------------------

@dataclass
class LoopEvent:
    reason: str
    context: Any
    timestamp: str = field(default_factory=_now_utc)


# ---------------------------------------------------------------------------
# Orchestrated receipt watcher — routes through event bus
# ---------------------------------------------------------------------------

class _OrchestratedReceiptWatcher:
    """Thin wrapper around ReceiptWatcher that emits events to the orchestrator bus."""

    def __init__(
        self,
        state_dir: Path,
        shutdown_event: threading.Event,
        event_bus: "queue.Queue[LoopEvent]",
        dry_run: bool,
        poll_interval: float = 2.0,
    ) -> None:
        from headless_trigger import TriggerState, _refresh_t0_state  # noqa: PLC0415
        self._state_dir = state_dir
        self._shutdown = shutdown_event
        self._bus = event_bus
        self._dry_run = dry_run
        self._poll_interval = poll_interval
        self._receipts_path = state_dir / "t0_receipts.ndjson"
        self._file_pos = 0
        self._thread: Optional[threading.Thread] = None
        self._refresh_t0_state = _refresh_t0_state

        _ACTIONABLE = frozenset({
            "subprocess_completion", "task_complete", "gate_pass", "gate_fail",
            "quality_gate_verification", "dispatch_complete", "dispatch_failure",
        })
        self._ACTIONABLE = _ACTIONABLE

    def start(self) -> None:
        if self._receipts_path.exists():
            self._file_pos = self._receipts_path.stat().st_size
        logger.info("ReceiptWatcher started (pos=%d path=%s)", self._file_pos, self._receipts_path)
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="receipt-watcher")
        self._thread.start()

    def _poll_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._check_new_lines()
            except Exception as exc:
                logger.debug("ReceiptWatcher poll error: %s", exc)
            self._shutdown.wait(timeout=self._poll_interval)

    def _check_new_lines(self) -> None:
        if not self._receipts_path.exists():
            return
        current_size = self._receipts_path.stat().st_size
        if current_size <= self._file_pos:
            return
        try:
            with open(self._receipts_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._file_pos)
                new_data = f.read()
                self._file_pos = f.tell()
        except Exception as exc:
            logger.debug("ReceiptWatcher read error: %s", exc)
            return

        actionable = []
        for line in new_data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                receipt = json.loads(line)
                if (receipt.get("event_type") or receipt.get("event", "")) in self._ACTIONABLE:
                    actionable.append(receipt)
            except json.JSONDecodeError:
                continue

        if not actionable:
            return

        logger.info("ReceiptWatcher: %d actionable receipt(s)", len(actionable))
        self._refresh_t0_state(self._state_dir)

        latest = actionable[-1]
        self._bus.put(LoopEvent(
            reason="receipt",
            context={
                "receipt_count": len(actionable),
                "latest_event": latest.get("event_type") or latest.get("event"),
                "latest_dispatch_id": latest.get("dispatch_id"),
                "latest_terminal": latest.get("terminal"),
                "receipt_status": latest.get("status"),
            },
        ))


# ---------------------------------------------------------------------------
# Silence watchdog thread
# ---------------------------------------------------------------------------

def _run_silence_watchdog(
    state_dir: Path,
    shutdown_event: threading.Event,
    event_bus: "queue.Queue[LoopEvent]",
    interval: float,
) -> None:
    """Run silence watchdog in a loop, emitting to event bus on anomalies."""
    from headless_trigger import check_stale_leases, _scan_old_files  # noqa: PLC0415

    logger.info("SilenceWatchdog started (interval=%.0fs)", interval)
    while not shutdown_event.is_set():
        shutdown_event.wait(timeout=interval)
        if shutdown_event.is_set():
            break
        dispatches_base = state_dir.parent / "dispatches"
        anomalies = (
            check_stale_leases(state_dir)
            + _scan_old_files(dispatches_base / "active", 1200)
            + _scan_old_files(dispatches_base / "pending", 300)
        )
        if anomalies:
            logger.info("SilenceWatchdog: %d anomaly(ies)", len(anomalies))
            event_bus.put(LoopEvent(reason="silence_anomaly", context={"anomalies": anomalies}))
        else:
            logger.debug("SilenceWatchdog: no anomalies")
    logger.info("SilenceWatchdog stopped")


# ---------------------------------------------------------------------------
# Loop event logger
# ---------------------------------------------------------------------------

def _log_loop_event(data_dir: Path, record: Dict[str, Any]) -> None:
    events_dir = data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    with open(events_dir / _LOOP_LOG_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# HeadlessOrchestrator
# ---------------------------------------------------------------------------

class HeadlessOrchestrator:
    """Single entry point for the autonomous loop.

    Starts ReceiptWatcher, DispatchDaemon, and Silence Watchdog as managed
    threads.  Wires DecisionRouter between receipt events and dispatch
    execution.  Writes headless_health.json every 30 s.
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        dry_run: bool = False,
    ) -> None:
        self.data_dir = data_dir or _default_data_dir()
        self.state_dir = state_dir or _default_state_dir()
        self.dry_run = dry_run

        self._shutdown = threading.Event()
        self._decisions_made = 0
        self._started_at: Optional[datetime] = None
        self._event_bus: "queue.Queue[LoopEvent]" = queue.Queue()

        # Daemon references (set during start)
        self._receipt_watcher: Optional[_OrchestratedReceiptWatcher] = None
        self._dispatch_daemon = None
        self._decision_router = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._health_thread: Optional[threading.Thread] = None
        self._decision_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Startup validation
    # ------------------------------------------------------------------

    def validate_startup(self) -> None:
        """Raise RuntimeError with a clear message if preconditions are not met."""
        errors: List[str] = []

        t0_state = self.state_dir / "t0_state.json"
        if not t0_state.exists():
            errors.append(f"t0_state.json not found: {t0_state}")

        db_path = self.state_dir / "quality_intelligence.db"
        if db_path.exists():
            if not os.access(db_path, os.R_OK):
                errors.append(f"quality_intelligence.db not readable: {db_path}")
        # db absence is non-fatal — warn only
        elif not self.dry_run:
            logger.warning("quality_intelligence.db not found at %s — continuing", db_path)

        pending_dir = self.data_dir / "dispatches" / "pending"
        if not pending_dir.exists():
            try:
                pending_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Created dispatches/pending/: %s", pending_dir)
            except OSError as exc:
                errors.append(f"Cannot create dispatches/pending/: {exc}")

        if not shutil.which("claude"):
            errors.append("'claude' CLI not found in PATH")

        if errors:
            raise RuntimeError("Startup validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

        logger.info("Startup validation passed (state=%s data=%s)", self.state_dir, self.data_dir)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Validate, then launch all daemons and monitoring threads."""
        self.validate_startup()
        self._started_at = datetime.now(timezone.utc)

        # Import daemons
        from headless_dispatch_daemon import DispatchDaemon  # noqa: PLC0415
        from llm_decision_router import DecisionRouter        # noqa: PLC0415

        self._decision_router = DecisionRouter(data_dir=self.data_dir)

        # 1. Receipt watcher
        self._receipt_watcher = _OrchestratedReceiptWatcher(
            state_dir=self.state_dir,
            shutdown_event=self._shutdown,
            event_bus=self._event_bus,
            dry_run=self.dry_run,
        )
        self._receipt_watcher.start()

        # 2. Dispatch daemon
        self._dispatch_daemon = DispatchDaemon(
            data_dir=self.data_dir,
            state_dir=self.state_dir,
        )
        self._dispatch_daemon.start()

        # 3. Silence watchdog
        self._watchdog_thread = threading.Thread(
            target=_run_silence_watchdog,
            args=(self.state_dir, self._shutdown, self._event_bus, _WATCHDOG_INTERVAL),
            daemon=True,
            name="silence-watchdog",
        )
        self._watchdog_thread.start()

        # 4. Decision loop
        self._decision_thread = threading.Thread(
            target=self._decision_loop, daemon=True, name="decision-loop"
        )
        self._decision_thread.start()

        # 5. Health file writer
        self._health_thread = threading.Thread(
            target=self._health_loop, daemon=True, name="health-writer"
        )
        self._health_thread.start()

        logger.info(
            "HeadlessOrchestrator started (dry_run=%s data=%s)", self.dry_run, self.data_dir
        )

    def stop(self) -> None:
        """Signal shutdown and wait for all threads to stop (max 10s each)."""
        logger.info("HeadlessOrchestrator stopping...")
        self._shutdown.set()

        # Stop DispatchDaemon (has its own shutdown event)
        if self._dispatch_daemon is not None:
            self._dispatch_daemon.stop()

        threads = [
            ("receipt_watcher", self._receipt_watcher._thread if self._receipt_watcher else None),
            ("dispatch_daemon", self._find_thread("dispatch-daemon")),
            ("silence_watchdog", self._watchdog_thread),
            ("health_writer", self._health_thread),
            ("decision_loop", self._decision_thread),
        ]
        for name, thread in threads:
            if thread is not None and thread.is_alive():
                thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT)
                if thread.is_alive():
                    logger.warning("%s did not stop within %.0fs", name, _SHUTDOWN_JOIN_TIMEOUT)
                else:
                    logger.debug("%s stopped", name)

        logger.info("HeadlessOrchestrator stopped")

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    def _check_feature_completion_and_trigger(self, event: LoopEvent) -> None:
        """After a receipt event: check if feature is complete and auto-trigger gates."""
        if self.dry_run:
            return
        ctx = _flatten_context(event.context)
        dispatch_id = ctx.get("latest_dispatch_id", "")
        if not dispatch_id:
            return

        # Derive feature_id from dispatch_id (e.g. "f51-pr2-..." → "F51")
        m = re.search(r"f(\d+)-pr", dispatch_id, re.IGNORECASE)
        if not m:
            return
        feature_id = f"F{m.group(1)}"

        try:
            sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))
            from auto_gate_trigger import trigger_gates_if_feature_complete  # noqa: PLC0415
            result = trigger_gates_if_feature_complete(feature_id, self.state_dir)
        except Exception as exc:
            logger.warning("auto_gate_trigger check failed: %s", exc)
            return

        if result.get("triggered"):
            _log_loop_event(self.data_dir, {
                "timestamp": _now_utc(),
                "event_type": "auto_gate_triggered",
                "feature_id": feature_id,
                "pr_number": result.get("pr_number"),
                "gates": result.get("gates", []),
                "gates_failed": result.get("gates_failed", []),
            })
            logger.info(
                "Auto-gate triggered for %s PR #%s: %s",
                feature_id, result.get("pr_number"), result.get("gates"),
            )

    def _check_all_gates_passed(self, event: LoopEvent) -> None:
        """When a gate event arrives, check if all required gates have passed."""
        ctx = _flatten_context(event.context)
        latest_event = ctx.get("latest_event", "")
        if "gate" not in latest_event.lower():
            return

        # Derive dispatch context
        dispatch_id = ctx.get("latest_dispatch_id", "")
        m = re.search(r"f(\d+)-pr", dispatch_id, re.IGNORECASE)
        if not m:
            return
        feature_id = f"F{m.group(1)}"

        # Check gate results directory for evidence that required gates passed
        gate_results_dir = self.state_dir / "review_gates" / "results"
        if not gate_results_dir.exists():
            return

        result_files = list(gate_results_dir.glob("pr-*.json"))
        if not result_files:
            return

        # Determine highest PR number from results
        pr_numbers: List[int] = []
        for f in result_files:
            m2 = re.match(r"pr-(\d+)-", f.name)
            if m2:
                pr_numbers.append(int(m2.group(1)))
        if not pr_numbers:
            return

        latest_pr = max(pr_numbers)
        pr_files = [f for f in result_files if f.name.startswith(f"pr-{latest_pr}-")]
        gate_names_present = {f.name.split(f"pr-{latest_pr}-")[1].replace(".json", "") for f in pr_files}

        required = {"codex_gate", "gemini_review"}
        if required.issubset(gate_names_present):
            _log_loop_event(self.data_dir, {
                "timestamp": _now_utc(),
                "event_type": "feature_gates_complete",
                "feature_id": feature_id,
                "pr_number": latest_pr,
                "gate_names": sorted(gate_names_present),
            })
            logger.info(
                "All required gates passed for %s PR #%d — next feature dispatch unblocked",
                feature_id, latest_pr,
            )

    def _decision_loop(self) -> None:
        """Read events from bus, route through DecisionRouter, log decisions."""
        while not self._shutdown.is_set():
            try:
                try:
                    event = self._event_bus.get(timeout=1.0)
                except queue.Empty:
                    continue

                if self._decision_router is None:
                    continue

                result = self._decision_router.decide(
                    context={"reason": event.reason, **_flatten_context(event.context)},
                    question="re_dispatch",
                )
                self._decisions_made += 1

                record: Dict[str, Any] = {
                    "timestamp": _now_utc(),
                    "event_type": "decision",
                    "reason": event.reason,
                    "action": result.action,
                    "reasoning": result.reasoning,
                    "confidence": result.confidence,
                    "backend_used": result.backend_used,
                    "latency_ms": result.latency_ms,
                    "dry_run": self.dry_run,
                }
                try:
                    _log_loop_event(self.data_dir, record)
                except Exception as exc:
                    logger.warning("Failed to log loop event: %s", exc)

                logger.info(
                    "Decision [%s] reason=%s action=%s confidence=%.2f",
                    result.backend_used, event.reason, result.action, result.confidence,
                )

                # Check feature completion after every receipt event
                if event.reason == "receipt" and not self.dry_run:
                    try:
                        self._check_feature_completion_and_trigger(event)
                        self._check_all_gates_passed(event)
                    except Exception as exc:
                        logger.warning("Feature completion check error: %s", exc)

                if result.action in ("re_dispatch", "analyze_failure") and not self.dry_run:
                    # Signal T0 via trigger mechanism
                    self._invoke_trigger(event)

            except Exception as exc:
                logger.error("Decision loop error: %s", exc)

    def _invoke_trigger(self, event: LoopEvent) -> None:
        """Delegate to trigger_headless_t0 when DecisionRouter says to act."""
        try:
            from headless_trigger import TriggerState, trigger_headless_t0  # noqa: PLC0415
            ts = TriggerState()
            trigger_headless_t0(
                reason=event.reason,
                context=event.context,
                state_dir=self.state_dir,
                dry_run=False,
                trigger_state=ts,
            )
        except Exception as exc:
            logger.error("trigger_headless_t0 error: %s", exc)

    def _health_loop(self) -> None:
        """Write health file every 30 seconds."""
        while not self._shutdown.is_set():
            try:
                self._write_health()
            except Exception as exc:
                logger.warning("Health write error: %s", exc)
            self._shutdown.wait(timeout=_HEALTH_INTERVAL)

    def _write_health(self) -> None:
        if self._started_at is None:
            return
        uptime = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        health = {
            "daemons": {
                "receipt_watcher": self._daemon_status("receipt-watcher"),
                "dispatch_daemon": self._daemon_status("dispatch-daemon"),
                "silence_watchdog": self._daemon_status("silence-watchdog"),
            },
            "started_at": self._started_at.isoformat(),
            "uptime_seconds": round(uptime, 1),
            "last_health_check": _now_utc(),
            "decisions_made": self._decisions_made,
        }
        health_path = self.data_dir / "headless_health.json"
        tmp_path = health_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(health, indent=2), encoding="utf-8")
        tmp_path.replace(health_path)
        logger.debug("Health file updated (uptime=%.0fs decisions=%d)", uptime, self._decisions_made)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _daemon_status(self, thread_name: str) -> str:
        t = self._find_thread(thread_name)
        if t is not None and t.is_alive():
            return "running"
        # receipt_watcher thread stored directly
        if thread_name == "receipt-watcher" and self._receipt_watcher is not None:
            t = self._receipt_watcher._thread
            return "running" if (t is not None and t.is_alive()) else "stopped"
        return "stopped"

    @staticmethod
    def _find_thread(name: str) -> Optional[threading.Thread]:
        for t in threading.enumerate():
            if t.name == name:
                return t
        return None


def _flatten_context(ctx: Any) -> Dict[str, Any]:
    if isinstance(ctx, dict):
        return ctx
    return {"raw": str(ctx)[:500]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="VNX Headless Orchestrator — autonomous loop")
    parser.add_argument("--data-dir", default=None, help="VNX_DATA_DIR override")
    parser.add_argument("--state-dir", default=None, help="VNX_STATE_DIR override")
    parser.add_argument("--dry-run", action="store_true", help="Run daemons but skip actual dispatch delivery")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir  = Path(args.data_dir)  if args.data_dir  else None
    state_dir = Path(args.state_dir) if args.state_dir else None

    orchestrator = HeadlessOrchestrator(
        data_dir=data_dir,
        state_dir=state_dir,
        dry_run=args.dry_run,
    )

    def _on_signal(signum: int, _frame: Any) -> None:
        logger.info("Signal %d received — initiating graceful shutdown", signum)
        orchestrator.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        orchestrator.start()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    # Wait until shutdown
    while not orchestrator._shutdown.is_set():
        orchestrator._shutdown.wait(timeout=1.0)

    return 0


if __name__ == "__main__":
    sys.exit(main())
