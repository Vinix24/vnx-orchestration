#!/usr/bin/env python3
"""Canonical receipt append helper with lock, validation, and idempotency guard.

Facade + CLI entry point. Implementation lives in
``scripts/lib/append_receipt_internals/``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: F401  (mock.patch("append_receipt.subprocess.Popen") relies on this)
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env
    from project_root import resolve_state_dir
    from project_scope import current_project_id
    from quality_advisory import generate_quality_advisory, get_changed_files
    from terminal_snapshot import collect_terminal_snapshot
    from cqs_calculator import calculate_cqs
    from receipt_provenance import enrich_receipt_provenance, validate_receipt_provenance
    from ghost_receipt_filter import should_route_to_gate_stream, gate_events_file
except Exception as exc:  # pragma: no cover - hard fail on bootstrap issue
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

from append_receipt_internals.common import (
    AppendReceiptError,
    AppendResult,
    EXIT_INVALID_INPUT,
    EXIT_IO_ERROR,
    EXIT_OK,
    EXIT_UNEXPECTED_ERROR,
    REPO_ROOT as _REPO_ROOT,
    _emit,
    _get_open_items_manager,
    _safe_subprocess,
    _utc_now_iso,
    is_headless_t0,
    register_facade,
)
from append_receipt_internals.idempotency import (
    IDEMPOTENCY_FIELDS,
    _cache_file_for,
    _compute_idempotency_key,
    _load_cache,
    _lock_file_for,
    _resolve_receipts_file,
    _write_cache,
    _write_receipt_under_lock,
)
from append_receipt_internals.validation import (
    DISPATCH_REQUIRED_EVENTS,
    STATE_MUTATION_EVENTS,
    _is_completion_event,
    _is_subprocess_intermediate_completion,
    _requires_dispatch_id,
    _validate_receipt,
    _warn_if_review_gate_missing_dispatch_id,
)
from append_receipt_internals.report_extractor import _extract_changed_files_from_report
from append_receipt_internals.git_provenance import (
    _build_git_provenance,
    _extract_shortstat_value,
)
from append_receipt_internals.session_resolver import (
    _build_session_metadata,
    _extract_session_token_usage,
    _resolve_model_provider,
    _resolve_session_id,
    _rsi_check_env_session,
    _rsi_check_provider_files,
)
from append_receipt_internals.quality import (
    _SEVERITY_MAP,
    _count_quality_violations,
    _count_quality_violations_against_store,
    _register_quality_open_items,
)
from append_receipt_internals.register_emit import _emit_dispatch_register
from append_receipt_internals.enrichment import _enrich_completion_receipt
from append_receipt_internals.payload import (
    _maybe_trigger_state_rebuild,
    _run_post_append_hooks,
    _trigger_receipt_classifier,
    _update_confidence_from_receipt,
    append_receipt_payload,
)

# Register this module as the active facade so submodules can resolve
# patchable names back to whichever module the test loaded.
register_facade(sys.modules[__name__])


def _parse_input(receipt_json: Optional[str], receipt_file: Optional[str]) -> Dict[str, Any]:
    raw = ""

    if receipt_json is not None:
        raw = receipt_json
    elif receipt_file:
        try:
            raw = Path(receipt_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise AppendReceiptError("input_read_failed", EXIT_IO_ERROR, f"Failed to read receipt file: {exc}") from exc
    else:
        raw = sys.stdin.read()

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
