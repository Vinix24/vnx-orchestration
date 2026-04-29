#!/usr/bin/env python3
"""CLI: run RuntimeSupervisor.supervise_all() and persist detected anomalies.

Wires the existing supervisor into a periodic tick. Emits one structured
JSON line per anomaly to stderr, appends a `runtime_anomaly_detected` event
to dispatch_register.ndjson per anomaly, and (unless --no-oi) writes
durable open items for blocking-severity anomalies.

Designed to be invoked from dispatcher_v8_minimal.sh on a 60s throttle when
VNX_SUPERVISOR_MODE=unified.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from project_root import resolve_state_dir  # noqa: E402
from runtime_supervisor import (  # noqa: E402
    AnomalyRecord,
    RuntimeSupervisor,
    create_open_items_for_anomalies,
)
from dispatch_register import append_event  # noqa: E402

BLOCKING_SEVERITY = "blocking"


def _emit_stderr(record: AnomalyRecord) -> None:
    payload = {
        "log": "runtime_supervise",
        "anomaly": record.anomaly_type,
        "severity": record.severity,
        "terminal_id": record.terminal_id,
        "dispatch_id": record.dispatch_id,
        "worker_state": record.worker_state,
        "lease_state": record.lease_state,
        "detected_at": record.detected_at,
    }
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _emit_register(record: AnomalyRecord) -> bool:
    dispatch_id = record.dispatch_id or f"anomaly:{record.terminal_id}:{record.anomaly_type}"
    extra = {
        "anomaly": record.anomaly_type,
        "severity": record.severity,
        "terminal_id": record.terminal_id,
        "worker_state": record.worker_state,
        "lease_state": record.lease_state,
        "detected_at": record.detected_at,
    }
    if record.evidence:
        extra["evidence"] = record.evidence
    return append_event(
        "runtime_anomaly_detected",
        dispatch_id=dispatch_id,
        terminal=record.terminal_id,
        extra=extra,
    )


def _serialize_for_json(record: AnomalyRecord) -> dict:
    return {
        "anomaly_type": record.anomaly_type,
        "severity": record.severity,
        "terminal_id": record.terminal_id,
        "dispatch_id": record.dispatch_id,
        "worker_state": record.worker_state,
        "lease_state": record.lease_state,
        "detected_at": record.detected_at,
        "evidence": record.evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runtime_supervise",
        description="Run RuntimeSupervisor.supervise_all() and persist anomalies.",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="Path to VNX state dir (defaults to resolve_state_dir()).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Alias of --state-dir for compatibility with adjacent CLIs.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit summary JSON on stdout instead of a human-readable line.",
    )
    parser.add_argument(
        "--no-oi",
        action="store_true",
        help="Skip writing open items for blocking-severity anomalies.",
    )
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir or args.db or resolve_state_dir())

    supervisor = RuntimeSupervisor(state_dir)
    anomalies = supervisor.supervise_all()

    register_failures = 0
    for record in anomalies:
        _emit_stderr(record)
        if not _emit_register(record):
            register_failures += 1

    oi_results: list = []
    if anomalies and not args.no_oi:
        blockers = [a for a in anomalies if a.severity == BLOCKING_SEVERITY]
        if blockers:
            oi_path = state_dir / "open_items.json"
            oi_results = create_open_items_for_anomalies(blockers, oi_path)

    if args.json:
        summary = {
            "count": len(anomalies),
            "blocking_count": sum(1 for a in anomalies if a.severity == BLOCKING_SEVERITY),
            "open_items_written": len(oi_results),
            "register_failures": register_failures,
            "anomalies": [_serialize_for_json(a) for a in anomalies],
        }
        sys.stdout.write(json.dumps(summary, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(
            f"runtime_supervise: {len(anomalies)} anomalies "
            f"({sum(1 for a in anomalies if a.severity == BLOCKING_SEVERITY)} blocking, "
            f"{len(oi_results)} open items written)\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
