#!/usr/bin/env python3
"""
VNX Runtime Core Rollback Utility.

Enables or disables the PR-5 runtime core by writing feature flags to
.vnx-data/.env_override (sourced by bin/vnx on startup).

Usage:
  python scripts/rollback_runtime_core.py rollback  # disable runtime core
  python scripts/rollback_runtime_core.py enable    # re-enable runtime core
  python scripts/rollback_runtime_core.py status    # show current flag state

The rollback operation sets VNX_RUNTIME_PRIMARY=0 which causes:
  - dispatcher_v8_minimal.sh to skip all broker/lease calls
  - load_runtime_core() to return None (no-op)
  - legacy terminal_state_shadow path to remain the sole coordination path

See docs/runtime_core_rollback.md for the full rollback procedure.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent


def _get_env_override_path() -> Path:
    vnx_data = os.environ.get("VNX_DATA_DIR", "")
    if not vnx_data:
        print("ERROR: VNX_DATA_DIR is not set. Cannot locate .env_override.", file=sys.stderr)
        sys.exit(1)
    return Path(vnx_data) / ".env_override"


def _read_env_override(path: Path) -> dict[str, str]:
    """Parse key=value lines from .env_override (ignores comments, blank lines)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _write_env_override(path: Path, env: dict[str, str]) -> None:
    """Write env dict back to .env_override in export key=value format."""
    lines = ["# VNX runtime env overrides (managed by rollback_runtime_core.py)"]
    for key, value in sorted(env.items()):
        lines.append(f"export {key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# Flag sets for each mode
_ROLLBACK_FLAGS = {
    "VNX_RUNTIME_PRIMARY": "0",       # Disable runtime core
    "VNX_BROKER_SHADOW": "1",         # Broker back to shadow mode
    "VNX_CANONICAL_LEASE_ACTIVE": "0", # Legacy lock system active
}

_ENABLE_FLAGS = {
    "VNX_RUNTIME_PRIMARY": "1",        # Enable runtime core
    "VNX_BROKER_SHADOW": "0",          # Broker authoritative
    "VNX_CANONICAL_LEASE_ACTIVE": "1", # Canonical lease active
}

_WATCH_FLAGS = [
    "VNX_RUNTIME_PRIMARY",
    "VNX_BROKER_SHADOW",
    "VNX_BROKER_ENABLED",
    "VNX_CANONICAL_LEASE_ACTIVE",
    "VNX_TMUX_ADAPTER_ENABLED",
    "VNX_ADAPTER_PRIMARY",
]


def cmd_rollback(path: Path) -> None:
    env = _read_env_override(path)
    env.update(_ROLLBACK_FLAGS)
    _write_env_override(path, env)
    print("Runtime core DISABLED (rollback mode).")
    print(f"Written to: {path}")
    print()
    print("Active flags:")
    for k, v in _ROLLBACK_FLAGS.items():
        print(f"  {k}={v}")
    print()
    print("To restore: python scripts/rollback_runtime_core.py enable")
    print("Restart bin/vnx start for changes to take effect.")


def cmd_enable(path: Path) -> None:
    env = _read_env_override(path)
    env.update(_ENABLE_FLAGS)
    _write_env_override(path, env)
    print("Runtime core ENABLED (cutover mode).")
    print(f"Written to: {path}")
    print()
    print("Active flags:")
    for k, v in _ENABLE_FLAGS.items():
        print(f"  {k}={v}")
    print()
    print("Restart bin/vnx start for changes to take effect.")


def cmd_status(path: Path) -> None:
    env_file = _read_env_override(path) if path.exists() else {}
    print(f"env_override: {path} ({'exists' if path.exists() else 'not found'})")
    print()
    print("Runtime core flags (env > override > dispatcher default):")
    for flag in _WATCH_FLAGS:
        env_val = os.environ.get(flag)
        override_val = env_file.get(flag)
        # Dispatcher defaults (PR-5 cutover defaults)
        defaults = {
            "VNX_RUNTIME_PRIMARY": "1",
            "VNX_BROKER_SHADOW": "0",
            "VNX_BROKER_ENABLED": "1",
            "VNX_CANONICAL_LEASE_ACTIVE": "1",
            "VNX_TMUX_ADAPTER_ENABLED": "1",
            "VNX_ADAPTER_PRIMARY": "1",
        }
        effective = env_val or override_val or defaults.get(flag, "not set")
        source = "env" if env_val else ("override" if override_val else "default")
        print(f"  {flag}={effective}  ({source})")

    print()
    runtime_primary = os.environ.get("VNX_RUNTIME_PRIMARY") or env_file.get("VNX_RUNTIME_PRIMARY", "1")
    if runtime_primary == "1":
        print("Mode: RUNTIME CORE ACTIVE (broker + canonical lease)")
    else:
        print("Mode: LEGACY ONLY (rollback — terminal_state_shadow path)")


def main() -> None:
    parser = argparse.ArgumentParser(description="VNX Runtime Core Rollback Utility")
    parser.add_argument(
        "action",
        choices=["rollback", "enable", "status"],
        help="rollback=disable runtime core, enable=re-enable, status=show flags",
    )
    args = parser.parse_args()

    path = _get_env_override_path()

    if args.action == "rollback":
        cmd_rollback(path)
    elif args.action == "enable":
        cmd_enable(path)
    else:
        cmd_status(path)


if __name__ == "__main__":
    main()
