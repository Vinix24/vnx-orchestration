#!/usr/bin/env python3
"""subprocess_dispatch.py — Thin helper for routing dispatch delivery via SubprocessAdapter.

Called from dispatch_deliver.sh when VNX_ADAPTER_T{n}=subprocess is set.

BILLING SAFETY: Only calls subprocess.Popen(["claude", ...]). No Anthropic SDK.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from subprocess_adapter import SubprocessAdapter

logger = logging.getLogger(__name__)


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


def deliver_via_subprocess(
    terminal_id: str,
    instruction: str,
    model: str,
    dispatch_id: str,
    *,
    lease_generation: int | None = None,
    heartbeat_interval: float = 300.0,
    chunk_timeout: float = 120.0,
    total_deadline: float = 600.0,
) -> bool:
    """Deliver a dispatch instruction to terminal_id via SubprocessAdapter.

    Blocks until the subprocess exits, consuming all stream events.
    Events are persisted to EventStore via read_events_with_timeout() internally.

    If lease_generation is provided, a background heartbeat thread renews the
    lease every heartbeat_interval seconds to prevent TTL expiry during long tasks.

    Returns True on success, False on failure.
    """
    adapter = SubprocessAdapter()
    result = adapter.deliver(
        terminal_id,
        dispatch_id,
        instruction=instruction,
        model=model,
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

    try:
        for _event in adapter.read_events_with_timeout(
            terminal_id,
            chunk_timeout=chunk_timeout,
            total_deadline=total_deadline,
        ):
            pass
        return True
    except Exception:
        logger.exception("deliver_via_subprocess failed for %s", terminal_id)
        return False
    finally:
        if heartbeat_stop is not None:
            heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=5)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deliver dispatch via SubprocessAdapter")
    parser.add_argument("--terminal-id", required=True)
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--dispatch-id", required=True)
    args = parser.parse_args()

    ok = deliver_via_subprocess(
        terminal_id=args.terminal_id,
        instruction=args.instruction,
        model=args.model,
        dispatch_id=args.dispatch_id,
    )
    sys.exit(0 if ok else 1)
