#!/usr/bin/env python3
"""subagents_allow.py — grant/revoke/inspect the OI-1643 subagent override marker.

Claude Code subagents (the Task tool) bypass the governance receipt trail, so
``scripts/hooks/pretooluse_block_subagent.sh`` denies every Task call by
default. This CLI is the operator's deliberate, time-boxed override: it
writes/removes ``<state_dir>/subagents_allowed.json``, the marker the hook
reads. Every subagent call made while a marker is active is still appended to
``<state_dir>/subagent_use.ndjson`` by the hook — this CLI only controls
whether calls are allowed, not whether they are logged.

Usage:
    python3 scripts/subagents_allow.py --reason "..." --hours 24
    python3 scripts/subagents_allow.py --status
    python3 scripts/subagents_allow.py --revoke

Stdlib-only. State dir resolution matches every other hook in this repo
(vnx_paths.resolve_paths()['VNX_STATE_DIR']) — never a hardcoded
``.vnx-data/`` literal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from vnx_paths import resolve_paths  # noqa: E402
from atomic_io import atomic_write_json  # noqa: E402

MARKER_FILENAME = "subagents_allowed.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _state_dir() -> Path:
    return Path(resolve_paths()["VNX_STATE_DIR"])


def _marker_path() -> Path:
    return _state_dir() / MARKER_FILENAME


def _granted_by() -> str:
    return (
        os.environ.get("VNX_ACTOR")
        or os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or "unknown"
    )


def cmd_grant(reason: str, hours: float) -> int:
    reason = reason.strip()
    if not reason:
        print("Weiger: --reason mag niet leeg zijn.", file=sys.stderr)
        return 2
    if hours is None or hours <= 0:
        print("Weiger: --hours is verplicht en moet > 0 zijn.", file=sys.stderr)
        return 2

    granted_at = _utc_now()
    expires_at = granted_at + timedelta(hours=hours)
    marker = {
        "reason": reason,
        "granted_at": _iso(granted_at),
        "expires_at": _iso(expires_at),
        "granted_by": _granted_by(),
    }
    atomic_write_json(_marker_path(), marker)
    print(
        f"Subagents toegestaan tot {marker['expires_at']} UTC "
        f"(reden: {reason}, granted_by: {marker['granted_by']})."
    )
    return 0


def cmd_status() -> int:
    marker_path = _marker_path()
    if not marker_path.is_file():
        print("Subagents niet toegestaan (geen marker aanwezig).")
        return 1

    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Subagents niet toegestaan (marker onleesbaar: {exc}).")
        return 1

    reason = str(marker.get("reason") or "").strip()
    if not reason:
        print("Subagents niet toegestaan (marker ongeldig: reason ontbreekt).")
        return 1

    expiry_dt = _parse_iso(marker.get("expires_at", ""))
    if expiry_dt is None:
        print("Subagents niet toegestaan (marker ongeldig: expires_at is geen geldige datum).")
        return 1

    if _utc_now() >= expiry_dt:
        print(f"Subagents niet toegestaan (marker verlopen op {marker['expires_at']}).")
        return 1

    print(
        f"Subagents toegestaan tot {marker['expires_at']} UTC "
        f"(reden: {reason}, granted_by: {marker.get('granted_by', 'onbekend')})."
    )
    return 0


def cmd_revoke() -> int:
    marker_path = _marker_path()
    if marker_path.is_file():
        marker_path.unlink()
        print("Subagent-marker ingetrokken.")
    else:
        print("Geen actieve subagent-marker om in te trekken.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grant, revoke, or inspect the OI-1643 subagent (Task tool) override marker."
    )
    parser.add_argument("--reason", type=str, default=None, help="Reden voor het toestaan (verplicht bij grant).")
    parser.add_argument("--hours", type=float, default=None, help="Aantal uren tot de marker verloopt.")
    parser.add_argument("--status", action="store_true", help="Toon de huidige marker-status.")
    parser.add_argument("--revoke", action="store_true", help="Trek een actieve marker in.")
    args = parser.parse_args(argv)

    if args.status:
        return cmd_status()
    if args.revoke:
        return cmd_revoke()
    if args.reason is not None or args.hours is not None:
        if args.reason is None:
            print("Weiger: --reason is verplicht (samen met --hours) om subagents toe te staan.", file=sys.stderr)
            return 2
        return cmd_grant(args.reason, args.hours)

    parser.error("Geef --reason (met --hours), --status, of --revoke.")
    return 2  # pragma: no cover — parser.error() exits before this


if __name__ == "__main__":
    sys.exit(main())
