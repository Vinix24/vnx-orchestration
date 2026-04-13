#!/usr/bin/env python3
"""headless_dispatch_daemon.py — Watch dispatches/pending/ and auto-deliver to headless workers.

Closes the autonomous dispatch loop: polls pending/ every 5s, checks terminal availability
via t0_state.json, acquires lease, routes to subprocess_dispatch.py, and moves files through
their full lifecycle (pending → active → completed).

BILLING SAFETY: No Anthropic SDK. Only subprocess.Popen(["claude", ...]) via subprocess_dispatch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 5.0       # seconds between pending/ scans
_LEASE_SECONDS = 600       # default lease TTL


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_data_dir() -> Path:
    env = os.environ.get("VNX_DATA_DIR", "")
    if env:
        return Path(env)
    return _repo_root() / ".vnx-data"


def _default_state_dir() -> Path:
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    return _default_data_dir() / "state"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Dispatch metadata parser
# ---------------------------------------------------------------------------

@dataclass
class DispatchMeta:
    dispatch_id: str           # filename stem
    target_terminal: str       # "T1", "T2", "T3"
    track: Optional[str]       # "A", "B", "C"
    role: Optional[str]        # "backend-developer", etc.
    gate: Optional[str]        # "f48-pr1", etc.
    raw_instruction: str       # full .md body


_TARGET_RE = re.compile(r"\[\[TARGET:(T\d+)\]\]")
_TRACK_RE  = re.compile(r"^Track:\s*(\S+)", re.MULTILINE)
_ROLE_RE   = re.compile(r"^Role:\s*(\S+)", re.MULTILINE)
_GATE_RE   = re.compile(r"^Gate:\s*(\S+)", re.MULTILINE)


def parse_dispatch_metadata(path: Path) -> Optional[DispatchMeta]:
    """Extract TARGET, Track, Role, Gate from dispatch .md header."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read dispatch %s: %s", path, exc)
        return None

    target_m = _TARGET_RE.search(text)
    if not target_m:
        logger.debug("No [[TARGET:TX]] in %s — skipping", path.name)
        return None

    return DispatchMeta(
        dispatch_id=path.stem,
        target_terminal=target_m.group(1),
        track=(_m.group(1) if (_m := _TRACK_RE.search(text)) else None),
        role=(_m.group(1) if (_m := _ROLE_RE.search(text)) else None),
        gate=(_m.group(1) if (_m := _GATE_RE.search(text)) else None),
        raw_instruction=text,
    )


# ---------------------------------------------------------------------------
# Terminal availability check
# ---------------------------------------------------------------------------

def _is_terminal_headless(terminal_id: str) -> bool:
    """Return True when VNX_ADAPTER_TX=subprocess is configured."""
    env_key = f"VNX_ADAPTER_{terminal_id}"
    return os.environ.get(env_key, "").lower() == "subprocess"


def _load_t0_state(state_dir: Path) -> Dict[str, Any]:
    path = state_dir / "t0_state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Cannot parse t0_state.json: %s", exc)
        return {}


def _is_terminal_available(terminal_id: str, state_dir: Path) -> bool:
    """Return True when terminal is not leased per t0_state.json."""
    state = _load_t0_state(state_dir)
    terminals = state.get("terminals", {})
    info = terminals.get(terminal_id, {})
    lease_state = info.get("lease_state", "idle")
    return lease_state != "leased"


# ---------------------------------------------------------------------------
# Lease operations via runtime_core_cli
# ---------------------------------------------------------------------------

def _runtime_core_cli(*args: str) -> Optional[Dict[str, Any]]:
    """Call runtime_core_cli.py with args; return parsed JSON or None on error."""
    script = _repo_root() / "scripts" / "runtime_core_cli.py"
    cmd = [sys.executable, str(script)] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
            env={**os.environ},
        )
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("runtime_core_cli %s failed: %s", args[0] if args else "", exc)
    return None


def _acquire_lease(terminal_id: str, dispatch_id: str) -> Optional[int]:
    """Acquire lease; return generation on success, None on failure."""
    data = _runtime_core_cli(
        "acquire-lease",
        "--terminal", terminal_id,
        "--dispatch-id", dispatch_id,
        "--lease-seconds", str(_LEASE_SECONDS),
    )
    if data and data.get("acquired"):
        return data.get("generation")
    logger.warning("Lease acquire failed for %s/%s: %s", terminal_id, dispatch_id, data)
    return None


def _release_lease(terminal_id: str, generation: int) -> bool:
    """Release lease; return True on success."""
    data = _runtime_core_cli(
        "release-lease",
        "--terminal", terminal_id,
        "--generation", str(generation),
    )
    return bool(data and data.get("released"))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _write_audit(data_dir: Path, record: Dict[str, Any]) -> None:
    audit_path = data_dir / "dispatch_audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Lifecycle file moves
# ---------------------------------------------------------------------------

def _move_dispatch(src: Path, dest_dir: Path) -> Path:
    """Move dispatch file to dest_dir, return new path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.move(str(src), str(dest))
    return dest


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _deliver(meta: DispatchMeta, active_path: Path, state_dir: Path) -> bool:
    """Invoke deliver_with_recovery from subprocess_dispatch for this dispatch.

    Returns True on success, False on failure.
    """
    scripts_lib = _repo_root() / "scripts" / "lib"
    sys.path.insert(0, str(scripts_lib))
    try:
        from subprocess_dispatch import deliver_with_recovery  # noqa: PLC0415
    except ImportError as exc:
        logger.error("Cannot import subprocess_dispatch: %s", exc)
        return False

    model = os.environ.get("VNX_DISPATCH_MODEL", "sonnet")

    try:
        return deliver_with_recovery(
            terminal_id=meta.target_terminal,
            instruction=meta.raw_instruction,
            model=model,
            dispatch_id=meta.dispatch_id,
            role=meta.role,
            max_retries=1,
        )
    except Exception as exc:
        logger.error("Delivery exception for %s: %s", meta.dispatch_id, exc)
        return False


# ---------------------------------------------------------------------------
# Core daemon
# ---------------------------------------------------------------------------

class DispatchDaemon:
    """Watch dispatches/pending/ and auto-deliver to headless workers.

    Lifecycle per dispatch:
      1. Detect .md in pending/
      2. Parse metadata (TARGET, Track, Role, Gate)
      3. Check: is terminal headless AND available?
      4. Acquire lease
      5. Move pending/ → active/
      6. Deliver via subprocess_dispatch
      7. Move active/ → completed/  (or dead_letter/ on failure)
      8. Release lease
      9. Write audit record
    """

    def __init__(
        self,
        data_dir: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self.data_dir = data_dir or _default_data_dir()
        self.state_dir = state_dir or _default_state_dir()
        self.poll_interval = poll_interval
        self.pending_dir = self.data_dir / "dispatches" / "pending"
        self.active_dir  = self.data_dir / "dispatches" / "active"
        self.completed_dir = self.data_dir / "dispatches" / "completed"
        self.dead_letter_dir = self.data_dir / "dispatches" / "dead_letter"

        self._shutdown = threading.Event()
        self._processed: set[str] = set()   # dispatch_id stems already handled

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start daemon poll loop in background thread."""
        t = threading.Thread(target=self._run, daemon=True, name="dispatch-daemon")
        t.start()
        logger.info(
            "DispatchDaemon started (pending=%s poll=%.1fs)", self.pending_dir, self.poll_interval
        )

    def stop(self) -> None:
        self._shutdown.set()

    def run_once(self) -> int:
        """Single scan pass — returns count of dispatches processed."""
        return self._scan()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._scan()
            except Exception as exc:
                logger.error("Daemon scan error: %s", exc)
            self._shutdown.wait(timeout=self.poll_interval)

    def _scan(self) -> int:
        if not self.pending_dir.exists():
            return 0

        dispatches = sorted(
            p for p in self.pending_dir.iterdir()
            if p.suffix == ".md" and p.stem not in self._processed
        )
        processed = 0
        for path in dispatches:
            self._handle(path)
            processed += 1
        return processed

    def _handle(self, path: Path) -> None:
        dispatch_id = path.stem
        self._processed.add(dispatch_id)

        meta = parse_dispatch_metadata(path)
        if meta is None:
            logger.info("Skipping non-parseable dispatch: %s", path.name)
            return

        terminal = meta.target_terminal

        # Skip non-headless terminals
        if not _is_terminal_headless(terminal):
            logger.info(
                "Terminal %s is not headless (VNX_ADAPTER_%s != subprocess) — skipping %s",
                terminal, terminal, dispatch_id,
            )
            return

        # Check availability
        if not _is_terminal_available(terminal, self.state_dir):
            logger.info("Terminal %s is leased — deferring %s", terminal, dispatch_id)
            self._processed.discard(dispatch_id)   # retry next cycle
            return

        # Acquire lease
        generation = _acquire_lease(terminal, dispatch_id)
        if generation is None:
            logger.warning("Could not acquire lease for %s — deferring", dispatch_id)
            self._processed.discard(dispatch_id)
            return

        # Move pending → active
        try:
            active_path = _move_dispatch(path, self.active_dir)
        except OSError as exc:
            logger.error("Cannot move %s to active/: %s", path.name, exc)
            _release_lease(terminal, generation)
            self._processed.discard(dispatch_id)
            return

        logger.info("Delivering %s → %s (role=%s gate=%s)", dispatch_id, terminal, meta.role, meta.gate)

        start_ts = time.monotonic()
        outcome = "failed"
        try:
            success = _deliver(meta, active_path, self.state_dir)
            outcome = "done" if success else "failed"
        except Exception as exc:
            logger.error("Delivery error for %s: %s", dispatch_id, exc)
            outcome = "failed"
        finally:
            elapsed = time.monotonic() - start_ts

        # Move active → completed or dead_letter
        dest_dir = self.completed_dir if outcome == "done" else self.dead_letter_dir
        try:
            _move_dispatch(active_path, dest_dir)
        except OSError as exc:
            logger.warning("Cannot move %s to %s: %s", active_path.name, dest_dir.name, exc)

        # Release lease
        released = _release_lease(terminal, generation)
        if not released:
            logger.warning("Lease release failed for %s gen=%d", terminal, generation)

        # Audit record
        _write_audit(self.data_dir, {
            "timestamp": _now_utc(),
            "dispatch_id": dispatch_id,
            "terminal": terminal,
            "track": meta.track,
            "role": meta.role,
            "gate": meta.gate,
            "outcome": outcome,
            "elapsed_seconds": round(elapsed, 2),
            "lease_generation": generation,
            "lease_released": released,
        })

        logger.info("Dispatch %s finished: %s (%.1fs)", dispatch_id, outcome, elapsed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="VNX Headless Dispatch Daemon")
    parser.add_argument("--data-dir", default=None, help="VNX_DATA_DIR override")
    parser.add_argument("--state-dir", default=None, help="VNX_STATE_DIR override")
    parser.add_argument("--poll-interval", type=float, default=_POLL_INTERVAL)
    parser.add_argument("--once", action="store_true", help="Single scan then exit")
    args = parser.parse_args()

    data_dir  = Path(args.data_dir)  if args.data_dir  else None
    state_dir = Path(args.state_dir) if args.state_dir else None

    daemon = DispatchDaemon(
        data_dir=data_dir,
        state_dir=state_dir,
        poll_interval=args.poll_interval,
    )

    if args.once:
        n = daemon.run_once()
        logger.info("Single scan: %d dispatch(es) processed", n)
        return 0

    def _on_signal(signum: int, _frame: Any) -> None:
        logger.info("Signal %d — stopping daemon", signum)
        daemon.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    daemon.start()

    shutdown_ev = daemon._shutdown
    while not shutdown_ev.is_set():
        shutdown_ev.wait(timeout=1.0)

    logger.info("DispatchDaemon stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
