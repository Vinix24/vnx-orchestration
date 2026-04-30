#!/usr/bin/env python3
"""T0 decision outcome reconciliation — resolve pending decisions post-hoc.

Reads `t0_decision_log.jsonl`, for each record with `outcome_pending=true`
checks the dispatch_register for a matching outcome event, and writes a
resolution to `t0_decision_outcomes.ndjson` if found.

Idempotent: a cursor file (`t0_decision_outcomes_cursor.json`) tracks
already-resolved decisions so repeated runs only process new ones.

Resolution rules:
  decision_type="dispatch_created" → outcome from dispatch_completed /
      dispatch_failed for the same dispatch_id.
  decision_type="gate_verdict"     → outcome is the verdict itself
      (already settled at write time, but kept pending so reconciliation
      can attach merge / re-run signals later — for now we resolve as
      `verdict_recorded`).

Terminal types (oi_closed, pr_merge) are not pending and are skipped.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    vnx_data = os.environ.get("VNX_DATA_DIR")
    if vnx_data:
        return Path(vnx_data).expanduser().resolve()
    return Path(__file__).resolve().parent.parent.parent / ".vnx-data"


def _state_dir() -> Path:
    state = os.environ.get("VNX_STATE_DIR")
    if state:
        return Path(state).expanduser().resolve()
    return _data_dir() / "state"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return out


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load_cursor(cursor_file: Path) -> set[str]:
    if not cursor_file.exists():
        return set()
    try:
        data = json.loads(cursor_file.read_text(encoding="utf-8"))
        return set(data.get("resolved_keys", []))
    except Exception:
        return set()


def _save_cursor(cursor_file: Path, resolved_keys: set[str]) -> None:
    cursor_file.parent.mkdir(parents=True, exist_ok=True)
    cursor_file.write_text(
        json.dumps({"resolved_keys": sorted(resolved_keys)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _decision_key(decision: dict[str, Any]) -> str:
    """Stable identity for a decision record.

    Uses timestamp + decision_type + main subject (dispatch_id / pr_number /
    oi_id / gate). Two writes of the same logical decision still produce
    distinct keys when timestamps differ (intentional — every decision is
    its own audit point), but a single record is reconciled exactly once.
    """
    parts = [
        str(decision.get("timestamp", "")),
        str(decision.get("decision_type", "")),
        str(decision.get("dispatch_id", "")),
        str(decision.get("pr_number", "")),
        str(decision.get("oi_id", "")),
        str(decision.get("gate", "")),
    ]
    return "|".join(parts)


def _index_register_outcomes(register_events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a dispatch_id → terminal-outcome index from register events."""
    index: dict[str, dict[str, Any]] = {}
    for ev in register_events:
        et = ev.get("event")
        if et not in ("dispatch_completed", "dispatch_failed"):
            continue
        did = ev.get("dispatch_id")
        if not did:
            continue
        # Last write wins — register is append-only chronological.
        index[str(did)] = {
            "outcome": "success" if et == "dispatch_completed" else "failure",
            "resolved_at": ev.get("timestamp", ""),
            "register_event": et,
        }
    return index


def reconcile(
    *,
    decision_log: Path | None = None,
    register_path: Path | None = None,
    outcomes_log: Path | None = None,
    cursor_file: Path | None = None,
) -> int:
    """Resolve pending decisions by reading the dispatch register.

    Returns the number of new resolutions written.
    """
    decision_log = decision_log or (_state_dir() / "t0_decision_log.jsonl")
    register_path = register_path or (_state_dir() / "dispatch_register.ndjson")
    outcomes_log = outcomes_log or (_state_dir() / "t0_decision_outcomes.ndjson")
    cursor_file = cursor_file or (_state_dir() / "t0_decision_outcomes_cursor.json")

    decisions = _read_jsonl(decision_log)
    if not decisions:
        return 0

    resolved_keys = _load_cursor(cursor_file)
    register_events = _read_jsonl(register_path)
    outcome_index = _index_register_outcomes(register_events)

    written = 0
    for decision in decisions:
        if not decision.get("outcome_pending"):
            continue
        key = _decision_key(decision)
        if key in resolved_keys:
            continue

        dtype = decision.get("decision_type")
        resolution: dict[str, Any] | None = None

        if dtype == "dispatch_created":
            did = str(decision.get("dispatch_id") or "")
            if did and did in outcome_index:
                hit = outcome_index[did]
                resolution = {
                    "decision_key": key,
                    "decision_type": dtype,
                    "dispatch_id": did,
                    "expected_outcome": decision.get("expected_outcome"),
                    "actual_outcome": hit["outcome"],
                    "register_event": hit["register_event"],
                    "resolved_at": _now_iso(),
                    "decision_resolved_at": hit["resolved_at"],
                }
        elif dtype == "gate_verdict":
            # Gate verdict is its own outcome — record the resolution so
            # downstream analytics can correlate verdicts with merges.
            resolution = {
                "decision_key": key,
                "decision_type": dtype,
                "dispatch_id": decision.get("dispatch_id"),
                "pr_number": decision.get("pr_number"),
                "gate": decision.get("gate"),
                "actual_outcome": decision.get("verdict"),
                "register_event": "verdict_recorded",
                "resolved_at": _now_iso(),
            }

        if resolution is None:
            continue

        _append_jsonl(outcomes_log, resolution)
        resolved_keys.add(key)
        written += 1

    if written:
        _save_cursor(cursor_file, resolved_keys)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve pending T0 decisions by reading the dispatch register."
    )
    parser.add_argument("--decision-log", type=Path, default=None)
    parser.add_argument("--register-path", type=Path, default=None)
    parser.add_argument("--outcomes-log", type=Path, default=None)
    parser.add_argument("--cursor-file", type=Path, default=None)
    args = parser.parse_args(argv)

    written = reconcile(
        decision_log=args.decision_log,
        register_path=args.register_path,
        outcomes_log=args.outcomes_log,
        cursor_file=args.cursor_file,
    )
    if written == 0:
        logger.info("t0_decision_reconcile: no new resolutions")
    else:
        logger.info("t0_decision_reconcile: wrote %d resolutions", written)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
