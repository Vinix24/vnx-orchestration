#!/usr/bin/env python3
"""receipt_quality_oi.py — Quality violation counting and open-item registration.

Extracted from append_receipt.py to keep the main module under 500 lines.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_open_items_manager = None

_SEVERITY_MAP = {
    "blocking": "blocker",
    "warning": "warn",
    "info": "info",
}


def _emit(level: str, code: str, **fields: Any) -> None:
    payload = {"level": level, "code": code, "timestamp": int(time.time())}
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def _get_open_items_manager():
    global _open_items_manager
    if _open_items_manager is None:
        _scripts_dir = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(_scripts_dir))
        import open_items_manager as _oim
        _open_items_manager = _oim
    return _open_items_manager


def _emit_dispatch_register(receipt: dict) -> bool:
    """Emit dispatch_register event for codex_gate-relevant receipts.

    SCOPE: codex_gate only. gemini_review and claude_github_optional are
    deferred until proper findings parsers exist (separate PR).

    Returns True on success, False on any failure (best-effort, never raises).
    """
    try:
        from dispatch_register import append_event

        event_type = str(receipt.get("event_type") or receipt.get("event") or "").lower()
        status = str(receipt.get("status", "")).lower()
        gate = str(receipt.get("gate", "")).lower()
        dispatch_id = str(receipt.get("dispatch_id", ""))
        terminal = str(receipt.get("terminal", ""))
        feature_id = str(receipt.get("feature_id", ""))
        pr_number = receipt.get("pr_number")
        if pr_number is None:
            pr_number = receipt.get("metadata", {}).get("pr_number") if isinstance(receipt.get("metadata"), dict) else None
        try:
            pr_number = int(pr_number) if pr_number is not None else None
        except (ValueError, TypeError):
            pr_number = None

        SUCCESS_STATUSES = {"success", "completed", "complete", "ok", ""}
        FAILURE_STATUSES = {"failed", "failure", "error", "blocked"}

        register_event = None
        if event_type in ("task_complete", "task_completed"):
            if status in FAILURE_STATUSES:
                register_event = "dispatch_failed"
            elif status in SUCCESS_STATUSES:
                register_event = "dispatch_completed"
            else:
                return False
        elif event_type == "task_failed":
            register_event = "dispatch_failed"
        elif event_type == "task_timeout":
            # task_timeout → dispatch_failed: terminal failure semantic.
            register_event = "dispatch_failed"
        elif event_type in ("task_started", "task_start", "dispatch_start"):
            register_event = "dispatch_started"
        elif event_type == "review_gate_request":
            if gate != "codex_gate":
                return False
            register_event = "gate_requested"
        else:
            return False

        return append_event(
            register_event,
            dispatch_id=dispatch_id,
            pr_number=pr_number,
            feature_id=feature_id,
            terminal=terminal,
            gate=gate,
        )
    except Exception:
        return False


def _count_quality_violations(receipt: dict) -> int:
    """Count violations that _register_quality_open_items WILL create (after dedup).

    Uses the same dedup_key logic as _register_quality_open_items so the
    pre-computed count matches the actual creation count.
    Returns 0 when quality_advisory or t0_recommendation is absent.
    """
    advisory = receipt.get("quality_advisory") or {}
    rec = advisory.get("t0_recommendation") or {}
    open_items = rec.get("open_items") or []
    if not open_items:
        return 0
    seen_keys: set = set()
    for item in open_items:
        check_id = str(item.get("check_id", "unknown"))
        file_path = str(item.get("file", "")) or "unknown"
        symbol = str(item.get("symbol") or "")
        seen_keys.add(f"qa:{check_id}:{file_path}:{symbol}")
    return len(seen_keys)


def _count_quality_violations_against_store(receipt: Dict[str, Any]) -> int:
    """Dry-run dedup count: how many open items _register_quality_open_items WOULD create.

    Mirrors the dedup_key construction of _register_quality_open_items and
    consults the on-disk open-items store via OpenItemsManager.load_items()
    so the count reflects items that are NOT already tracked (any status).

    This is read-only: it never writes to open_items.json.

    Returns:
        Count of unique dedup keys that would result in new open items.
        0 when quality_advisory or t0_recommendation is absent or on any error.
    """
    try:
        advisory = receipt.get("quality_advisory")
        if not isinstance(advisory, dict):
            return 0
        rec = advisory.get("t0_recommendation")
        if not isinstance(rec, dict):
            return 0
        open_items = rec.get("open_items") or []
        if not open_items:
            return 0

        existing_keys: set = set()
        try:
            oim = _get_open_items_manager()
            data = oim.load_items()
            for item in data.get("items", []):
                key = item.get("dedup_key")
                if key:
                    existing_keys.add(key)
        except Exception as exc:
            _emit("WARN", "oi_dryrun_load_failed", error=str(exc))
            existing_keys = set()

        seen_keys: set = set()
        for item in open_items:
            check_id = str(item.get("check_id", "unknown"))
            file_path = str(item.get("file", "")) or "unknown"
            symbol = str(item.get("symbol") or "")
            dedup_key = f"qa:{check_id}:{file_path}:{symbol}"
            if dedup_key in existing_keys:
                continue
            seen_keys.add(dedup_key)
        return len(seen_keys)
    except Exception as exc:
        _emit("WARN", "oi_dryrun_count_failed", error=str(exc))
        return 0


def _register_quality_open_items(receipt: Dict[str, Any]) -> int:
    """Best-effort: register quality advisory violations as tracked open items.

    Reads t0_recommendation.open_items[] from the enriched receipt, creates
    open items with dedup keys, and ALWAYS writes a sidecar summary for the
    receipt processor to include in T0 notifications (even when clean).

    Returns:
        Count of newly created open items.
    """
    try:
        advisory = receipt.get("quality_advisory")
        if not isinstance(advisory, dict):
            return 0

        rec = advisory.get("t0_recommendation")
        if not isinstance(rec, dict):
            return 0

        dispatch_id = str(receipt.get("dispatch_id") or "unknown")
        report_path = str(receipt.get("report_path") or "")
        pr_id = str(receipt.get("pr_id") or "")

        new_ids: List[str] = []
        counts = {"blocker": 0, "warn": 0, "info": 0}

        open_items = rec.get("open_items") or []

        if open_items:
            oim = _get_open_items_manager()

            for item in open_items:
                try:
                    check_id = str(item.get("check_id", "unknown"))
                    file_path = str(item.get("file", ""))
                    symbol = str(item.get("symbol") or "")
                    raw_severity = str(item.get("severity", "info"))
                    mapped_severity = _SEVERITY_MAP.get(raw_severity, "info")
                    title = str(item.get("item", ""))
                    dedup_key = f"qa:{check_id}:{file_path or 'unknown'}:{symbol}"

                    item_id, created = oim.add_item_programmatic(
                        title=title,
                        severity=mapped_severity,
                        dispatch_id=dispatch_id,
                        report_path=report_path,
                        pr_id=pr_id,
                        details=f"file={file_path}, symbol={symbol}" if symbol else f"file={file_path}",
                        dedup_key=dedup_key,
                        source="quality_advisory",
                    )

                    counts[mapped_severity] = counts.get(mapped_severity, 0) + 1
                    if created:
                        new_ids.append(item_id)
                except Exception as exc:
                    _emit("WARN", "quality_oi_item_failed", error=str(exc))

        return len(new_ids)

    except Exception as exc:
        _emit("WARN", "quality_oi_registration_failed", error=str(exc))
        return 0
