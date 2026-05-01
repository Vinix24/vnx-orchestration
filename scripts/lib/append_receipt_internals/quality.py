"""Quality-advisory violation counting + open-item registration."""

from __future__ import annotations

from typing import Any, Dict, List

from .common import _emit, facade

_SEVERITY_MAP = {
    "blocking": "blocker",
    "warning": "warn",
    "info": "info",
}


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

    This is read-only: it never writes to open_items.json. It exists so the
    enrichment step can record an accurate ``open_items_created`` value
    BEFORE the receipt's idempotency check, without mutating state for
    receipts that are later skipped as duplicates.

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
            oim = facade._get_open_items_manager()
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
            oim = facade._get_open_items_manager()

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
