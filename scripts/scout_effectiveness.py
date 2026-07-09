#!/usr/bin/env python3
"""CLI for the scout effectiveness measurement harness.

Usage:
    python3 scripts/scout_effectiveness.py [--state-dir PATH] [--output-json PATH]

Resolves the state directory through VNX path helpers (``vnx_paths``), which
honor ``VNX_STATE_DIR`` and fall back to the resolved project state directory
(typically ``~/.vnx-data/<project_id>/state`` for existing central installs).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
LIB_DIR = SCRIPT_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from scout_effectiveness import (  # noqa: E402
    compute_effectiveness,
    correlate_records,
    format_report,
    list_scout_sidecar_dispatch_ids,
    load_receipts,
    load_scout_receipts,
    write_artifact,
)

try:
    from vnx_paths import ensure_env  # noqa: E402
except Exception as exc:  # pragma: no cover - bootstrap failure
    raise SystemExit(f"Failed to load vnx_paths: {exc}") from exc

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_NAME = "scout_effectiveness.json"


def _resolve_state_dir(cli_override: Optional[Path] = None) -> Path:
    if cli_override is not None:
        return cli_override.expanduser().resolve()
    env_state = os.environ.get("VNX_STATE_DIR")
    if env_state:
        return Path(env_state).expanduser().resolve()
    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"]).expanduser().resolve()


def _default_output_path(state_dir: Path) -> Path:
    return state_dir / DEFAULT_ARTIFACT_NAME


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure scout effectiveness from existing VNX receipts and scout sidecars."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Override VNX state directory (default: resolved VNX_STATE_DIR).",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write JSON artifact to this path (default: <state-dir>/scout_effectiveness.json).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the CLI table; still writes the JSON artifact if requested.",
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Do not write the JSON artifact.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: WARNING).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")

    state_dir = _resolve_state_dir(args.state_dir)
    if not state_dir.is_dir():
        print(f"[scout-effectiveness] State directory does not exist: {state_dir}", file=sys.stderr)
        return 1

    receipts, invalid_receipts = load_receipts(state_dir)
    scout_receipts, invalid_scout = load_scout_receipts(state_dir)
    sidecar_ids = list_scout_sidecar_dispatch_ids(state_dir)

    records = correlate_records(receipts, scout_receipts, sidecar_ids)
    report = compute_effectiveness(records)

    if invalid_receipts:
        report.warnings.append(f"Skipped {invalid_receipts} malformed t0_receipts lines.")
    if invalid_scout:
        report.warnings.append(f"Skipped {invalid_scout} malformed scout_receipts lines.")

    if not receipts:
        report.warnings.append(f"No receipts found at {state_dir / 't0_receipts.ndjson'}.")
    if not scout_receipts and not sidecar_ids:
        report.warnings.append(
            "No scout receipts or sidecars found. "
            "Either the scout has not run yet or it writes enrichment through a different path."
        )

    if not args.quiet:
        print(format_report(report))

    if not args.no_json:
        output_path = args.output_json or _default_output_path(state_dir)
        write_artifact(output_path, report)
        if not args.quiet:
            print(f"JSON artifact written: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
