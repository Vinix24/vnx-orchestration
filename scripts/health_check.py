#!/usr/bin/env python3
"""CLI entry point for VNX component health beacons.

Reads all beacons under ``$VNX_DATA_DIR/health/`` (or the path given via
``--state-dir``) and prints a compact status table. Exit codes:

    0  all beacons OK
    1  one or more beacons stale / fail / corrupt / missing
    2  framework broken (state_dir unresolvable, internal error)

Use ``--json`` for machine-readable output and ``--components`` to
restrict to specific components.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent
_LIB_DIR = _SCRIPT_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from health_beacon import all_beacons  # noqa: E402


def _resolve_state_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    try:
        from project_root import resolve_data_dir
        return resolve_data_dir(__file__)
    except Exception:
        # Fall through — the caller decides whether to error out.
        raise


def _filter_components(
    beacons: dict, components: Iterable[str] | None
) -> dict:
    if not components:
        return beacons
    wanted = {c.strip() for c in components if c.strip()}
    if not wanted:
        return beacons
    return {k: v for k, v in beacons.items() if k in wanted}


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _format_iso(beacon: dict) -> str:
    iso = beacon.get("last_run_iso")
    if isinstance(iso, str) and iso:
        return iso
    ts = beacon.get("last_run_ts")
    try:
        ts_f = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts_f = None
    if ts_f is None:
        return "-"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_f))


def _print_table(beacons: dict, requested: list[str]) -> None:
    rows: list[tuple[str, str, str, str]] = []

    seen = set(beacons.keys())
    ordered = list(beacons.keys())
    for r in requested:
        if r and r not in seen:
            ordered.append(r)

    for name in ordered:
        if name in beacons:
            b = beacons[name]
            health = b.get("health", "ok").upper()
            iso = _format_iso(b)
            age = _format_age(b.get("age_seconds"))
        else:
            health = "MISSING"
            iso = "-"
            age = "-"
        rows.append((name, health, iso, age))

    if not rows:
        print("(no beacons found)")
        return

    name_w = max(len("Component"), max(len(r[0]) for r in rows))
    health_w = max(len("Health"), max(len(r[1]) for r in rows))
    iso_w = max(len("Last run"), max(len(r[2]) for r in rows))

    header = (
        f"{'Component'.ljust(name_w)}  "
        f"{'Health'.ljust(health_w)}  "
        f"{'Last run'.ljust(iso_w)}  "
        f"Age"
    )
    print(header)
    print("-" * len(header))
    for name, health, iso, age in rows:
        print(
            f"{name.ljust(name_w)}  "
            f"{health.ljust(health_w)}  "
            f"{iso.ljust(iso_w)}  "
            f"{age}"
        )


def _exit_code(beacons: dict, requested: list[str]) -> int:
    requested_set = {c for c in requested if c}
    if requested_set:
        for name in requested_set:
            if name not in beacons:
                return 1
            if beacons[name].get("health") != "ok":
                return 1
        return 0

    if not beacons:
        # No requested filter and no beacons present -> degraded.
        return 1
    for b in beacons.values():
        if b.get("health") != "ok":
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Show VNX component health beacons."
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Override beacon root (defaults to resolved $VNX_DATA_DIR).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated component names to filter on.",
    )
    args = parser.parse_args(argv)

    requested = [
        c.strip() for c in (args.components or "").split(",") if c.strip()
    ]

    try:
        state_dir = _resolve_state_dir(args.state_dir)
    except Exception as exc:
        msg = {
            "error": "state_dir_unresolvable",
            "message": str(exc),
        }
        if args.json:
            print(json.dumps(msg))
        else:
            print(f"ERROR: cannot resolve state dir: {exc}", file=sys.stderr)
        return 2

    try:
        beacons = all_beacons(state_dir)
    except Exception as exc:
        msg = {"error": "beacon_read_failed", "message": str(exc)}
        if args.json:
            print(json.dumps(msg))
        else:
            print(f"ERROR: failed to read beacons: {exc}", file=sys.stderr)
        return 2

    filtered = _filter_components(beacons, requested)

    if args.json:
        # Include MISSING entries so callers can detect absent components.
        result = {
            "state_dir": str(state_dir),
            "beacons": filtered,
        }
        if requested:
            for name in requested:
                if name not in filtered:
                    result["beacons"][name] = {
                        "component": name,
                        "health": "missing",
                    }
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_table(filtered, requested)

    return _exit_code(filtered, requested)


if __name__ == "__main__":
    sys.exit(main())
