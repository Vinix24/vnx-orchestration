#!/usr/bin/env python3
"""shadow_mode_runner.py — F36 Wave C: Shadow mode decision parity harness.

Runs the headless T0 decision engine in shadow mode (dry-run, no execution)
against recent trigger events and compares the shadow decisions to the actual
decisions recorded in the decision log.

Shadow mode = run the decision engine in parallel with the real system, capture
its decisions, but never execute them. Parity = compare shadow decisions to the
actual decisions logged by the real system.

Parity report: JSONL comparison file + markdown summary written to
{VNX_DATA_DIR}/shadow_parity/.

Usage:
    python3 scripts/shadow_mode_runner.py [--limit N] [--output-dir DIR] [--dry-run]
    python3 scripts/shadow_mode_runner.py --json   # print JSON report to stdout
    python3 scripts/shadow_mode_runner.py --help

BILLING SAFETY: Uses dry-run (rule-based) backend by default. No Anthropic SDK.
No api.anthropic.com calls. CLI subprocess only if --backend claude-cli is set.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

logger = logging.getLogger("shadow_mode_runner")

_DEFAULT_LIMIT = 10
_DEFAULT_BACKEND = "dry-run"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    env = os.environ.get("VNX_DATA_DIR", "")
    return Path(env) if env else _REPO_ROOT / ".vnx-data"


def _state_dir() -> Path:
    env = os.environ.get("VNX_STATE_DIR", "")
    if env:
        return Path(env)
    return _data_dir() / "state"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_ndjson(path: Path, limit: int) -> list[dict[str, Any]]:
    """Load the N most recent NDJSON records from a file.

    Handles missing file, empty file, and malformed lines (skipped with warning).
    Returns an empty list when the file is missing or contains no valid records.
    """
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            records.append(json.loads(raw_line))
        except json.JSONDecodeError:
            logger.debug("Skipping malformed line in %s", path)
    return records[-limit:]


# ---------------------------------------------------------------------------
# Context builder for shadow decisions
# ---------------------------------------------------------------------------

def _build_shadow_context(event: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Build a decision-router context dict from a trigger event.

    Merges event metadata with lightweight signals read from t0_state.json.
    The state snapshot provides dispatch queue depth; the event provides the
    trigger reason and any dispatch identifiers.
    """
    t0_state: dict[str, Any] = {}
    state_file = state_dir / "t0_state.json"
    if state_file.exists():
        try:
            t0_state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    event_type = event.get("event_type", "")
    return {
        "event_type": event_type,
        "reason": event.get("trigger_reason") or event.get("reason", ""),
        "dispatch_id": event.get("dispatch_id"),
        "dispatch_target": event.get("dispatch_target"),
        "timestamp": event.get("timestamp", _now_iso()),
        "receipt": {
            "status": _infer_receipt_status(event_type),
        },
        "terminal_silence_seconds": 0,
        "state_summary": {
            "active_dispatches": len(t0_state.get("active_dispatches", [])),
            "pending_dispatches": len(t0_state.get("pending_dispatches", [])),
        },
    }


def _infer_receipt_status(event_type: str) -> str:
    """Map event type to a receipt status string expected by the decision router."""
    if event_type in ("t0_reject", "t0_escalate"):
        return "failed"
    if event_type == "t0_dispatch":
        return "success"
    return "unknown"


# ---------------------------------------------------------------------------
# Shadow decision execution
# ---------------------------------------------------------------------------

def run_shadow_decision(
    event: dict[str, Any],
    state_dir: Path,
    backend: str,
) -> dict[str, Any]:
    """Run the shadow decision engine for a single event.

    Imports DecisionRouter from llm_decision_router and evaluates the event
    context using the requested backend (default: dry-run, no LLM required).

    Returns a dict with shadow_decision, shadow_reasoning, shadow_confidence,
    shadow_backend, shadow_latency_ms.
    """
    from llm_decision_router import DecisionRouter  # noqa: PLC0415

    context = _build_shadow_context(event, state_dir)
    router = DecisionRouter(backend=backend)

    t_start = time.monotonic()
    try:
        result = router.decide(context, "re_dispatch")
    except Exception as exc:
        logger.warning("Shadow decision failed for %s: %s", event.get("event_type"), exc)
        return {
            "shadow_decision": "UNKNOWN",
            "shadow_reasoning": f"Error: {exc}",
            "shadow_confidence": 0.0,
            "shadow_backend": backend,
            "shadow_latency_ms": int((time.monotonic() - t_start) * 1000),
        }

    return {
        "shadow_decision": result.action.upper(),
        "shadow_reasoning": result.reasoning,
        "shadow_confidence": result.confidence,
        "shadow_backend": result.backend_used,
        "shadow_latency_ms": result.latency_ms,
    }


# ---------------------------------------------------------------------------
# Decision vocabulary mapping
# ---------------------------------------------------------------------------

# DecisionRouter actions → decision log action vocabulary
_ROUTER_TO_LOG_ACTION: dict[str, str] = {
    "re_dispatch":      "dispatch",
    "escalate":         "escalate",
    "skip":             "wait",
    "analyze_failure":  "dispatch",
}


def shadow_action_to_log_action(shadow_action: str) -> str:
    """Translate a DecisionRouter action to the decision log action vocabulary."""
    return _ROUTER_TO_LOG_ACTION.get(shadow_action.lower(), shadow_action.lower())


# ---------------------------------------------------------------------------
# Parity comparison
# ---------------------------------------------------------------------------

def compare_decisions(
    event: dict[str, Any],
    actual_decision: dict[str, Any] | None,
    shadow: dict[str, Any],
) -> dict[str, Any]:
    """Compare a shadow decision to the corresponding actual decision.

    Returns a flat comparison record suitable for JSONL output.
    When actual_decision is None (no logged actual for this event), the
    actual_action is recorded as "UNKNOWN" and match is False.
    """
    event_type = event.get("event_type", "")
    actual_action = (actual_decision.get("action", "UNKNOWN") if actual_decision else "UNKNOWN")
    shadow_action = shadow.get("shadow_decision", "UNKNOWN")
    shadow_log_action = shadow_action_to_log_action(shadow_action)
    match = shadow_log_action == actual_action and actual_action != "UNKNOWN"

    return {
        "timestamp": _now_iso(),
        "event_type": event_type,
        "event_timestamp": event.get("timestamp", ""),
        "actual_action": actual_action,
        "shadow_action": shadow_action,
        "shadow_log_action": shadow_log_action,
        "match": match,
        "shadow_reasoning": shadow.get("shadow_reasoning", ""),
        "shadow_confidence": shadow.get("shadow_confidence", 0.0),
        "shadow_backend": shadow.get("shadow_backend", ""),
        "shadow_latency_ms": shadow.get("shadow_latency_ms", 0),
        "dispatch_id": event.get("dispatch_id"),
    }


# ---------------------------------------------------------------------------
# Parity report aggregator
# ---------------------------------------------------------------------------

def generate_parity_report(
    comparisons: list[dict[str, Any]],
    run_metadata: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate comparison records into a structured parity report.

    Args:
        comparisons:  List of comparison dicts from compare_decisions().
        run_metadata: Flat dict of run-level metadata (run_id, backend, etc.).

    Returns:
        Report dict with total, matched, mismatched, parity_rate, by_event_type,
        comparisons list, and all run_metadata fields merged at the top level.
    """
    total = len(comparisons)

    if total == 0:
        return {
            "total": 0,
            "matched": 0,
            "mismatched": 0,
            "parity_rate": None,
            "by_event_type": {},
            "comparisons": [],
            **run_metadata,
        }

    matched = sum(1 for c in comparisons if c["match"])
    mismatched = total - matched
    parity_rate = round(matched / total * 100, 1)

    by_type: dict[str, dict[str, int]] = {}
    for c in comparisons:
        et = c["event_type"]
        entry = by_type.setdefault(et, {"total": 0, "matched": 0})
        entry["total"] += 1
        if c["match"]:
            entry["matched"] += 1

    return {
        "total": total,
        "matched": matched,
        "mismatched": mismatched,
        "parity_rate": parity_rate,
        "by_event_type": by_type,
        "comparisons": comparisons,
        **run_metadata,
    }


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def write_parity_jsonl(comparisons: list[dict[str, Any]], output_file: Path) -> None:
    """Write comparison records as NDJSON (one JSON object per line)."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as fh:
        for c in comparisons:
            fh.write(json.dumps(c, separators=(",", ":")) + "\n")


def write_parity_report_md(report: dict[str, Any], output_file: Path) -> None:
    """Write a human-readable markdown parity report."""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    total = report["total"]
    matched = report["matched"]
    parity_rate = report.get("parity_rate")
    run_id = report.get("run_id", "unknown")
    backend = report.get("backend", "dry-run")
    limit = report.get("limit", total)
    generated_at = report.get("generated_at", _now_iso())

    lines = [
        "# Shadow Mode Parity Report",
        "",
        f"**Run ID**: `{run_id}`  ",
        f"**Generated**: {generated_at}  ",
        f"**Backend**: `{backend}`  ",
        f"**Events sampled**: {limit}  ",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total comparisons | {total} |",
        f"| Matched | {matched} |",
        f"| Mismatched | {total - matched} |",
    ]
    if parity_rate is not None:
        lines.append(f"| Parity rate | {parity_rate}% |")
    else:
        lines.append("| Parity rate | N/A (no events) |")
    lines.append("")

    by_type = report.get("by_event_type", {})
    if by_type:
        lines += [
            "## By Event Type",
            "",
            "| Event Type | Total | Matched | Parity% |",
            "|-----------|-------|---------|---------|",
        ]
        for et, stats in sorted(by_type.items()):
            t_cnt = stats["total"]
            m_cnt = stats["matched"]
            pct = round(m_cnt / t_cnt * 100, 1) if t_cnt > 0 else 0.0
            lines.append(f"| `{et}` | {t_cnt} | {m_cnt} | {pct}% |")
        lines.append("")

    comparisons = report.get("comparisons", [])
    if comparisons:
        lines += [
            "## Decision-level Detail",
            "",
            "| Event Type | Actual | Shadow | Match |",
            "|-----------|--------|--------|-------|",
        ]
        for c in comparisons:
            match_str = "ok" if c["match"] else "MISMATCH"
            lines.append(
                f"| `{c['event_type']}` "
                f"| `{c['actual_action']}` "
                f"| `{c['shadow_log_action']}` "
                f"| {match_str} |"
            )
        lines.append("")

    output_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_shadow_mode(
    data_dir: Path,
    state_dir: Path,
    limit: int,
    backend: str,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the shadow mode parity harness.

    Loads recent trigger events and actual decisions, evaluates shadow decisions
    via the configured backend (default: rule-based dry-run), and compares them
    to produce a parity report.

    Args:
        data_dir:   VNX data directory (contains events/t0_decisions.ndjson).
        state_dir:  VNX state directory (contains t0_decision_log.jsonl, t0_state.json).
        limit:      Maximum number of recent events to sample.
        backend:    Decision backend ('dry-run', 'claude-cli', 'ollama').
        output_dir: Directory for output files.
        dry_run:    When True, skip writing output files.

    Returns:
        Parity report dict (same shape as generate_parity_report output).
    """
    run_id = f"shadow-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    generated_at = _now_iso()

    events_file = data_dir / "events" / "t0_decisions.ndjson"
    decision_log = state_dir / "t0_decision_log.jsonl"

    logger.info(
        "Shadow run %s: limit=%d backend=%s events=%s log=%s",
        run_id, limit, backend, events_file, decision_log,
    )

    events = _load_ndjson(events_file, limit)
    actual_decisions = _load_ndjson(decision_log, limit)

    logger.info("Loaded %d events, %d actual decisions", len(events), len(actual_decisions))

    if not events:
        logger.warning("No events found at %s — parity check skipped", events_file)

    # Pair events to decisions by position; pad shorter list with None
    comparisons: list[dict[str, Any]] = []
    for i, event in enumerate(events):
        actual = actual_decisions[i] if i < len(actual_decisions) else None
        shadow = run_shadow_decision(event, state_dir, backend)
        comparison = compare_decisions(event, actual, shadow)
        comparisons.append(comparison)
        logger.debug(
            "Event[%d] %s: actual=%s shadow=%s match=%s",
            i, event.get("event_type", "?"),
            comparison["actual_action"],
            comparison["shadow_log_action"],
            comparison["match"],
        )

    run_meta: dict[str, Any] = {
        "run_id": run_id,
        "generated_at": generated_at,
        "backend": backend,
        "limit": limit,
        "events_file": str(events_file),
        "decision_log": str(decision_log),
    }
    report = generate_parity_report(comparisons, run_meta)

    logger.info(
        "Parity run %s: total=%d matched=%d parity=%s%%",
        run_id,
        report["total"],
        report["matched"],
        report.get("parity_rate", "N/A"),
    )

    if not dry_run and comparisons:
        ts_short = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        jsonl_path = output_dir / f"shadow_parity_{ts_short}.jsonl"
        md_path    = output_dir / f"shadow_parity_{ts_short}.md"
        write_parity_jsonl(comparisons, jsonl_path)
        write_parity_report_md(report, md_path)
        logger.info("Parity JSONL written: %s", jsonl_path)
        logger.info("Parity report written: %s", md_path)

    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the shadow mode parity runner.

    Returns 0 on success (including the case of no events to process).
    Returns 1 on unrecoverable startup errors.
    """
    parser = argparse.ArgumentParser(
        description="F36 Wave C: Shadow mode decision parity harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                         # dry-run backend, last 10 events\n"
            "  %(prog)s --limit 20 --json       # 20 events, JSON to stdout\n"
            "  %(prog)s --backend claude-cli    # shadow via CLI (requires claude)\n"
            "  %(prog)s --dry-run               # compare but don't write files\n"
        ),
    )
    parser.add_argument(
        "--limit", type=int, default=_DEFAULT_LIMIT,
        help=f"Maximum recent events to sample (default: {_DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--backend", default=_DEFAULT_BACKEND,
        choices=["dry-run", "claude-cli", "ollama"],
        help="Shadow decision backend (default: dry-run)",
    )
    parser.add_argument("--data-dir",   default=None, help="VNX_DATA_DIR override")
    parser.add_argument("--state-dir",  default=None, help="VNX_STATE_DIR override")
    parser.add_argument("--output-dir", default=None, help="Directory for parity report files")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compare decisions but skip writing output files",
    )
    parser.add_argument(
        "--json", action="store_true", dest="print_json",
        help="Print the parity report as JSON to stdout",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir   = Path(args.data_dir)   if args.data_dir   else _data_dir()
    state_dir  = Path(args.state_dir)  if args.state_dir  else _state_dir()
    output_dir = Path(args.output_dir) if args.output_dir else data_dir / "shadow_parity"

    report = run_shadow_mode(
        data_dir=data_dir,
        state_dir=state_dir,
        limit=args.limit,
        backend=args.backend,
        output_dir=output_dir,
        dry_run=args.dry_run,
    )

    if args.print_json:
        print(json.dumps(report, indent=2, default=str))

    return 0


if __name__ == "__main__":
    sys.exit(main())
