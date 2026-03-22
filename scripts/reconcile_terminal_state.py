#!/usr/bin/env python3
"""CLI for terminal_state.json fallback reconciliation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from terminal_state_reconciler import ReconcilerConfig, reconcile_terminal_state  # noqa: E402
from vnx_paths import ensure_env  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile terminal_state.json from fallback sources")
    parser.add_argument("--state-dir", default=None, help="Override state directory (defaults to VNX_STATE_DIR)")
    parser.add_argument("--repair", action="store_true", help="Write reconciled terminal_state.json (default read-only)")
    parser.add_argument("--stale-after-seconds", type=int, default=180, help="Primary stale threshold in seconds")
    parser.add_argument("--receipt-scan-lines", type=int, default=600, help="How many receipt lines to scan")
    parser.add_argument("--active-window-seconds", type=int, default=240, help="Recent activity window for tmux signals")
    parser.add_argument("--no-tmux-probe", action="store_true", help="Disable tmux runtime probes")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    paths = ensure_env()
    state_dir = args.state_dir or paths["VNX_STATE_DIR"]

    cfg = ReconcilerConfig(
        stale_after_seconds=args.stale_after_seconds,
        receipt_scan_lines=args.receipt_scan_lines,
        active_window_seconds=args.active_window_seconds,
        allow_tmux_probe=not args.no_tmux_probe,
    )

    result = reconcile_terminal_state(state_dir=state_dir, config=cfg, repair=args.repair)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
