#!/usr/bin/env python3
"""CLI wrapper for terminal_state.json shadow writes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

# Import the lib module by file path to avoid self-shadowing: this CLI wrapper
# is also named terminal_state_shadow.py, so a bare `from terminal_state_shadow`
# would resolve back to this file when scripts/ is on sys.path.
import importlib.util as _ilu
_name = "terminal_state_shadow_lib"
_spec = _ilu.spec_from_file_location(
    _name,
    str(SCRIPT_DIR / "lib" / "terminal_state_shadow.py"),
)
_mod = _ilu.module_from_spec(_spec)
sys.modules[_name] = _mod  # required: @dataclass resolves annotations via sys.modules
_spec.loader.exec_module(_mod)
TerminalUpdate = _mod.TerminalUpdate
default_lease_expires = _mod.default_lease_expires
get_worktree_path = _mod.get_worktree_path
set_worktree_path = _mod.set_worktree_path
update_terminal_state = _mod.update_terminal_state

from vnx_paths import ensure_env  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write terminal_state.json in shadow mode")
    parser.add_argument("--terminal-id", required=True, help="Terminal id, e.g. T1")
    parser.add_argument("--status", required=True, help="Terminal status")
    parser.add_argument("--claimed-by", default=None, help="Claim owner (e.g. dispatch id)")
    parser.add_argument("--claimed-at", default=None, help="Claim timestamp (ISO8601)")
    parser.add_argument("--lease-expires-at", default=None, help="Lease expiration timestamp (ISO8601)")
    parser.add_argument("--last-activity", default=None, help="Last activity timestamp (ISO8601)")
    parser.add_argument("--lease-seconds", type=int, default=None, help="Auto-generate lease_expires_at from now")
    parser.add_argument("--clear-claim", action="store_true", help="Clear claim fields")
    parser.add_argument("--worktree-path", default=None, help="Worktree directory path for this terminal")
    parser.add_argument("--state-dir", default=None, help="Override state directory (defaults to VNX_STATE_DIR)")
    return parser


def main() -> int:
    # Support subcommand mode: get-worktree <terminal_id>
    if len(sys.argv) >= 2 and sys.argv[1] == "get-worktree":
        terminal_id = sys.argv[2] if len(sys.argv) > 2 else None
        if not terminal_id:
            print("Usage: terminal_state_shadow.py get-worktree <terminal_id>", file=sys.stderr)
            return 1
        paths = ensure_env()
        state_dir_flag = None
        if "--state-dir" in sys.argv:
            idx = sys.argv.index("--state-dir")
            state_dir_flag = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        state_dir = state_dir_flag or paths["VNX_STATE_DIR"]
        wt_path = get_worktree_path(state_dir, terminal_id)
        if wt_path:
            print(wt_path)
        return 0

    if len(sys.argv) >= 2 and sys.argv[1] == "set-worktree":
        terminal_id = sys.argv[2] if len(sys.argv) > 2 else None
        wt_path = sys.argv[3] if len(sys.argv) > 3 else None
        if not terminal_id or not wt_path:
            print("Usage: terminal_state_shadow.py set-worktree <terminal_id> <path>", file=sys.stderr)
            return 1
        paths = ensure_env()
        state_dir_flag = None
        if "--state-dir" in sys.argv:
            idx = sys.argv.index("--state-dir")
            state_dir_flag = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        state_dir = state_dir_flag or paths["VNX_STATE_DIR"]
        set_worktree_path(state_dir, terminal_id, wt_path)
        print(json.dumps({"terminal_id": terminal_id, "worktree_path": wt_path}))
        return 0

    args = _build_parser().parse_args()
    paths = ensure_env()
    state_dir = args.state_dir or paths["VNX_STATE_DIR"]

    lease_expires_at = args.lease_expires_at
    if args.lease_seconds is not None and args.lease_seconds > 0:
        lease_expires_at = default_lease_expires(args.lease_seconds)

    update = TerminalUpdate(
        terminal_id=args.terminal_id,
        status=args.status,
        claimed_by=args.claimed_by,
        claimed_at=args.claimed_at,
        lease_expires_at=lease_expires_at,
        last_activity=args.last_activity,
        clear_claim=args.clear_claim,
        worktree_path=args.worktree_path,
    )

    record = update_terminal_state(state_dir=state_dir, update=update)
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
