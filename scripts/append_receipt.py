#!/usr/bin/env python3
"""Canonical receipt append helper with lock, validation, and idempotency guard."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
    from project_root import resolve_state_dir
    from ghost_receipt_filter import should_route_to_gate_stream, gate_events_file
    from receipt_cache import (
        AppendReceiptError,
        EXIT_OK,
        EXIT_INVALID_INPUT,
        EXIT_VALIDATION_ERROR,
        EXIT_IO_ERROR,
        EXIT_LOCK_ERROR,
        EXIT_UNEXPECTED_ERROR,
        IDEMPOTENCY_FIELDS,
        _compute_idempotency_key,
        _load_cache,
        _write_cache,
        _is_completion_event,
        _is_subprocess_intermediate_completion,
    )
    from receipt_enrichment import _enrich_completion_receipt, _extract_changed_files_from_report
    from receipt_git_session import _build_session_metadata  # re-exported for backward compat
    from receipt_quality_oi import (
        _emit_dispatch_register,
        _count_quality_violations,
        _count_quality_violations_against_store,
        _register_quality_open_items,
    )
except Exception as exc:  # pragma: no cover - hard fail on bootstrap issue
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

DISPATCH_REQUIRED_EVENTS = {
    "task_started",
    "task_complete",
    "task_failed",
    "task_timeout",
    "task_blocked",
    "dispatch_sent",
    "dispatch_ack",
    "ack",
}

STATE_MUTATION_EVENTS = {"state_mutation"}

_REPO_ROOT = SCRIPT_DIR.parent
_REBUILD_THROTTLE_SECONDS = 30

_warned_review_gate_no_dispatch_id = False


def is_headless_t0() -> bool:
    """Return True when T0 is configured to run via subprocess adapter.

    Set VNX_ADAPTER_T0=subprocess to enable headless T0 mode.  When True,
    the receipt processor annotates the T0 terminal snapshot entry with
    adapter/headless metadata and skips tmux-dependent probes for T0.
    """
    return os.environ.get("VNX_ADAPTER_T0", "tmux").lower() == "subprocess"


@dataclass(frozen=True)
class AppendResult:
    status: str
    receipts_file: Path
    idempotency_key: str


def _emit(level: str, code: str, **fields: Any) -> None:
    payload = {
        "level": level,
        "code": code,
        "timestamp": int(time.time()),
    }
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def _resolve_receipts_file(receipts_file: Optional[str] = None) -> Path:
    if receipts_file:
        return Path(receipts_file).expanduser()

    paths = ensure_env()
    return Path(paths["VNX_STATE_DIR"]) / "t0_receipts.ndjson"


def _lock_file_for(receipts_path: Path) -> Path:
    return receipts_path.parent / "append_receipt.lock"


def _cache_file_for(receipts_path: Path) -> Path:
    return receipts_path.parent / "receipt_idempotency_recent.ndjson"


def _requires_dispatch_id(receipt: Dict[str, Any], event_name: str) -> bool:
    if event_name in DISPATCH_REQUIRED_EVENTS:
        return True
    if event_name.startswith("task_"):
        return True
    if receipt.get("task_id"):
        return True
    return False


def _warn_if_review_gate_missing_dispatch_id(event_name: str, receipt: Dict[str, Any]) -> None:
    global _warned_review_gate_no_dispatch_id
    if _warned_review_gate_no_dispatch_id:
        return
    if event_name == "review_gate_request":
        if not str(receipt.get("dispatch_id", "")).strip():
            _warned_review_gate_no_dispatch_id = True
            _emit(
                "WARN",
                "review_gate_request_missing_dispatch_id",
                message="review_gate_request receipt has no dispatch_id — receipt-to-gate audit linkage severed",
            )


def _validate_receipt(receipt: Dict[str, Any]) -> str:
    timestamp = str(receipt.get("timestamp", "")).strip()
    if not timestamp:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: timestamp",
        )

    event_name = str(receipt.get("event_type") or receipt.get("event") or "").strip()
    if not event_name:
        raise AppendReceiptError(
            "missing_required_key",
            EXIT_VALIDATION_ERROR,
            "Missing required key: event_type or event",
        )

    if _requires_dispatch_id(receipt, event_name):
        dispatch_id = str(receipt.get("dispatch_id", "")).strip()
        if not dispatch_id:
            raise AppendReceiptError(
                "missing_required_key",
                EXIT_VALIDATION_ERROR,
                "Missing required key: dispatch_id",
            )

    _warn_if_review_gate_missing_dispatch_id(event_name, receipt)

    return event_name


def append_receipt_payload(
    receipt: Dict[str, Any],
    *,
    receipts_file: Optional[str] = None,
    cache_window_seconds: int = 300,
    skip_enrichment: bool = False,
) -> AppendResult:
    if not isinstance(receipt, dict):
        raise AppendReceiptError("invalid_receipt_type", EXIT_INVALID_INPUT, "Receipt payload must be a JSON object")

    # Enrich completion receipts with quality advisory and terminal snapshot (best-effort).
    # Enrichment generates quality_advisory; count must run after it sees real items.
    if not skip_enrichment:
        receipt = _enrich_completion_receipt(receipt)

    # Count violations from real quality_advisory (generated by enrichment above).
    # _enrich_completion_receipt sets open_items_created internally; setdefault avoids
    # double-counting on that path and handles skip_enrichment=True (returns 0 when
    # quality_advisory is absent).
    receipt.setdefault("open_items_created", _count_quality_violations(receipt))

    # Route ghost gate receipts (dispatch_id="unknown" + gate event) to gate_events.ndjson.
    # review_gate_request with empty/missing dispatch_id is redirected here (pre-existing
    # behaviour from PR #255, see ghost_receipt_filter.py). With PR-2 fix, callers supply
    # a real dispatch_id so is_ghost_dispatch_id() returns False and receipts land normally
    # in t0_receipts.ndjson. This is documented intent, not a regression.
    if receipts_file is None and should_route_to_gate_stream(receipt):
        try:
            paths = ensure_env()
            state_dir = Path(paths["VNX_STATE_DIR"])
            receipts_file = str(gate_events_file(state_dir))
            _emit("INFO", "ghost_receipt_rerouted",
                  gate=str(receipt.get("gate") or ""),
                  pr_id=str(receipt.get("pr_id") or ""),
                  destination=receipts_file)
        except Exception as exc:
            _emit("WARN", "ghost_receipt_reroute_failed", error=str(exc))

    event_name = _validate_receipt(receipt)
    idempotency_key = _compute_idempotency_key(receipt, event_name)

    receipt_path = _resolve_receipts_file(receipts_file).expanduser().resolve()
    receipt_path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = _lock_file_for(receipt_path)
    cache_path = _cache_file_for(receipt_path)

    min_epoch = time.time() - max(1, int(cache_window_seconds))

    result: Optional[AppendResult] = None

    try:
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

            cache_entries = _load_cache(cache_path, min_epoch)
            recent_keys = {entry["key"] for entry in cache_entries}

            if idempotency_key in recent_keys:
                _write_cache(cache_path, cache_entries)
                result = AppendResult(
                    status="duplicate",
                    receipts_file=receipt_path,
                    idempotency_key=idempotency_key,
                )
            else:
                try:
                    with receipt_path.open("a", encoding="utf-8") as receipts_handle:
                        receipts_handle.write(json.dumps(receipt, separators=(",", ":"), sort_keys=False))
                        receipts_handle.write("\n")
                except OSError as exc:
                    raise AppendReceiptError("receipt_write_failed", EXIT_IO_ERROR, f"Failed to append receipt: {exc}") from exc

                cache_entries.append({"ts": time.time(), "key": idempotency_key})
                _write_cache(cache_path, cache_entries)

                result = AppendResult(
                    status="appended",
                    receipts_file=receipt_path,
                    idempotency_key=idempotency_key,
                )
    except AppendReceiptError:
        raise
    except OSError as exc:
        raise AppendReceiptError("lock_failed", EXIT_LOCK_ERROR, f"Failed to acquire append lock: {exc}") from exc

    # Best-effort post-append hooks (skipped for skip_enrichment=True lightweight events).
    # IMPORTANT: _register_quality_open_items must run only AFTER the idempotency check
    # has accepted this receipt as 'appended'. A receipt that hits the duplicate cache
    # must not mutate open_items.json or CQS state. Run it here (post-lock release is
    # fine — add_item_programmatic uses its own lock file).
    if result is not None and result.status == "appended" and not skip_enrichment:
        _register_quality_open_items(receipt)

        _update_confidence_from_receipt(receipt)

        _emit_dispatch_register(receipt)

        _maybe_trigger_state_rebuild(receipt)

        _trigger_receipt_classifier(receipt)

    return result


def _update_confidence_from_receipt(receipt: Dict[str, Any]) -> None:
    """Wire dispatch outcome into pattern confidence scores (best-effort)."""
    try:
        SUCCESS_STATUSES = {"success", "completed", "complete", "ok", ""}
        FAILURE_STATUSES = {"failed", "failure", "error", "blocked"}

        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()

        if event_type in ("task_complete", "task_completed"):
            if status in FAILURE_STATUSES:
                outcome = "failure"
            elif status in SUCCESS_STATUSES:
                outcome = "success"
            else:
                return  # unknown status — don't update confidence
        elif event_type == "task_failed":
            outcome = "failure"
        else:
            return

        dispatch_id = str(receipt.get("dispatch_id") or "")
        terminal = str(receipt.get("terminal") or "")
        if not dispatch_id:
            return

        state_dir = resolve_state_dir(__file__)

        db_path = state_dir / "quality_intelligence.db"
        if not db_path.exists():
            return

        from intelligence_persist import update_confidence_from_outcome
        update_confidence_from_outcome(db_path, dispatch_id, terminal, outcome)
    except Exception as exc:
        _emit("WARN", "confidence_update_failed", error=str(exc))


def _trigger_receipt_classifier(receipt: Dict[str, Any]) -> None:
    """Best-effort fire of the adaptive receipt classifier (ARC-3).

    Disabled by default; opt-in via VNX_RECEIPT_CLASSIFIER_ENABLED=1. Never
    raises — the receipt writer must remain on its happy path even if the
    classifier import or subprocess spawn fails.
    """
    if os.environ.get("VNX_RECEIPT_CLASSIFIER_ENABLED", "0") != "1":
        return
    try:
        from receipt_classifier import trigger_receipt_classifier_async
        action = trigger_receipt_classifier_async(receipt)
        if action:
            _emit("INFO", "receipt_classifier_action", action=action)
    except Exception as exc:
        _emit("WARN", "receipt_classifier_trigger_failed", error=str(exc))


def _maybe_trigger_state_rebuild(receipt: Dict[str, Any]) -> None:
    """Trigger state rebuild via shared throttled helper. Best-effort."""
    event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()

    TRIGGER_EVENTS = {
        "task_complete", "task_completed", "completion", "complete",
        "task_failed", "task_timeout",
        "dispatch_promoted", "dispatch_started",
    }
    if event_type not in TRIGGER_EVENTS:
        return

    try:
        from state_rebuild_trigger import maybe_trigger_state_rebuild
        maybe_trigger_state_rebuild(event_type=event_type)
    except Exception:
        pass  # best-effort


def _parse_input(receipt_json: Optional[str], receipt_file: Optional[str]) -> Dict[str, Any]:
    if receipt_json:
        raw = receipt_json.strip()
    elif receipt_file:
        try:
            raw = Path(receipt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AppendReceiptError("receipt_file_read_failed", EXIT_IO_ERROR, f"Failed to read receipt file: {exc}") from exc
    else:
        raw = sys.stdin.read().strip()

    if not raw or not raw.strip():
        raise AppendReceiptError("empty_input", EXIT_INVALID_INPUT, "No receipt JSON input provided")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AppendReceiptError("invalid_json", EXIT_INVALID_INPUT, f"Malformed receipt JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AppendReceiptError("invalid_receipt_type", EXIT_INVALID_INPUT, "Receipt payload must be a JSON object")

    return parsed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical receipt append helper")
    parser.add_argument("--receipt", help="Raw receipt JSON payload", default=None)
    parser.add_argument("--receipt-file", help="Path to file containing a single receipt JSON payload", default=None)
    parser.add_argument("--receipts-file", help="Override canonical receipts file path", default=None)
    parser.add_argument("--cache-window-seconds", type=int, default=300, help="Recent idempotency window in seconds")
    parser.add_argument("--skip-enrichment", action="store_true", default=False, help="Skip quality advisory and provenance enrichment (for state-mutation events)")
    args = parser.parse_args(argv)

    try:
        receipt = _parse_input(args.receipt, args.receipt_file)
        result = append_receipt_payload(
            receipt,
            receipts_file=args.receipts_file,
            cache_window_seconds=args.cache_window_seconds,
            skip_enrichment=args.skip_enrichment,
        )
    except AppendReceiptError as exc:
        _emit("ERROR", exc.code, message=exc.message)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - safety net
        _emit("ERROR", "unexpected_error", message=str(exc))
        return EXIT_UNEXPECTED_ERROR

    if result.status == "duplicate":
        _emit(
            "INFO",
            "duplicate_receipt_skipped",
            idempotency_key=result.idempotency_key,
            receipts_file=str(result.receipts_file),
        )
    else:
        _emit(
            "INFO",
            "receipt_appended",
            idempotency_key=result.idempotency_key,
            receipts_file=str(result.receipts_file),
        )

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
