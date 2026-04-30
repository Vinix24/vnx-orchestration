#!/usr/bin/env python3
"""receipt_enrichment.py — Completion receipt enrichment with git/session/quality data.

Extracted from append_receipt.py to keep the main module under 500 lines.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from vnx_paths import ensure_env
from quality_advisory import generate_quality_advisory, get_changed_files
from terminal_snapshot import collect_terminal_snapshot
from cqs_calculator import calculate_cqs
from receipt_provenance import enrich_receipt_provenance, validate_receipt_provenance
from receipt_cache import _is_completion_event, _is_subprocess_intermediate_completion
from receipt_git_session import _build_git_provenance, _build_session_metadata, _utc_now_iso
from receipt_quality_oi import _count_quality_violations_against_store


def _emit(level: str, code: str, **fields: Any) -> None:
    payload = {"level": level, "code": code, "timestamp": int(time.time())}
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def _get_open_items_manager():
    """Lazy-load open_items_manager from scripts/ parent directory."""
    _scripts_dir = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_scripts_dir))
    import open_items_manager as _oim
    return _oim


def is_headless_t0() -> bool:
    """Return True when T0 is configured to run via subprocess adapter."""
    import os
    return os.environ.get("VNX_ADAPTER_T0", "tmux").lower() == "subprocess"


def _enrich_completion_receipt(receipt: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Enrich completion receipts with quality advisory and terminal snapshot.

    This is best-effort - failures will result in status="unavailable" markers
    rather than crashing the receipt append flow.

    Args:
        receipt: Receipt payload to enrich
        repo_root: Repository root path for git operations

    Returns:
        Enriched receipt with quality_advisory and terminal_snapshot fields
    """
    # Only enrich completion receipts
    if not _is_completion_event(receipt):
        return receipt

    enriched = receipt.copy()
    paths = ensure_env()
    state_dir = Path(paths.get("VNX_STATE_DIR", ".")).resolve()

    # Inject git provenance metadata (best-effort).
    if "provenance" not in enriched:
        try:
            resolved_repo_root = repo_root or Path(paths.get("PROJECT_ROOT", Path.cwd())).resolve()
            enriched["provenance"] = _build_git_provenance(resolved_repo_root)
        except Exception as exc:
            _emit("WARN", "provenance_capture_failed", error=str(exc))
            enriched["provenance"] = {
                "git_ref": "unknown",
                "branch": "unknown",
                "is_dirty": False,
                "dirty_files": 0,
                "diff_summary": None,
                "captured_at": _utc_now_iso(),
                "captured_by": "append_receipt",
                "status": "unavailable",
                "error": str(exc),
            }

    # Inject session metadata for usage correlation (best-effort).
    existing_session = enriched.get("session")
    if isinstance(existing_session, dict):
        merged_session = dict(existing_session)
        try:
            defaults = _build_session_metadata(enriched, state_dir)
            for key, value in defaults.items():
                merged_session.setdefault(key, value)
        except Exception as exc:
            _emit("WARN", "session_metadata_failed", error=str(exc))
        enriched["session"] = merged_session
    else:
        try:
            enriched["session"] = _build_session_metadata(enriched, state_dir)
        except Exception as exc:
            _emit("WARN", "session_metadata_failed", error=str(exc))
            enriched["session"] = {
                "session_id": "unknown",
                "terminal": str(enriched.get("terminal") or "unknown"),
                "model": "unknown",
                "provider": "unknown",
                "captured_at": _utc_now_iso(),
                "status": "unavailable",
                "error": str(exc),
            }

    # Enrich with provenance linkage fields (PR-2: dispatch_id, trace_token, etc.)
    try:
        enrich_receipt_provenance(enriched)
        prov_validation = validate_receipt_provenance(enriched)
        if prov_validation.gaps:
            gap_summaries = [g.to_dict() for g in prov_validation.gaps]
            enriched.setdefault("provenance_validation", {
                "chain_status": prov_validation.chain_status,
                "gaps": gap_summaries,
            })
            for gap in prov_validation.gaps:
                if gap.severity in ("warning", "error"):
                    _emit("WARN", "provenance_gap_detected",
                           gap_type=gap.gap_type, entity_id=gap.entity_id,
                           description=gap.description)
    except Exception as exc:
        _emit("WARN", "provenance_enrichment_failed", error=str(exc))

    # Collect terminal snapshot (best-effort)
    try:
        snapshot = collect_terminal_snapshot(state_dir)
        enriched["terminal_snapshot"] = snapshot.to_dict()
    except Exception as exc:
        _emit("WARN", "terminal_snapshot_failed", error=str(exc))
        enriched["terminal_snapshot"] = {
            "status": "unavailable",
            "error": str(exc),
        }

    # Annotate T0 terminal snapshot entry when T0 runs via subprocess adapter.
    if is_headless_t0():
        snapshot_data = enriched.get("terminal_snapshot") or {}
        terminals = snapshot_data.get("terminals") or {}
        t0_entry = dict(terminals.get("T0") or {})
        t0_entry["adapter"] = "subprocess"
        t0_entry["headless"] = True
        terminals["T0"] = t0_entry
        snapshot_data["terminals"] = terminals
        enriched["terminal_snapshot"] = snapshot_data

    # Subprocess intermediate completions skip quality-advisory generation and
    # CQS persistence entirely. The real report is not yet extracted at this point.
    if _is_subprocess_intermediate_completion(receipt):
        return enriched

    # Generate quality advisory (best-effort)
    try:
        if repo_root is None:
            repo_root = Path.cwd()

        changed_files = get_changed_files(repo_root)

        # Fallback: parse report for "Files Modified" when git diff is empty.
        if not changed_files:
            report_path = str(receipt.get("report_path") or "")
            if report_path:
                changed_files = _extract_changed_files_from_report(Path(report_path), repo_root)

        if changed_files:
            advisory = generate_quality_advisory(changed_files, repo_root)
            enriched["quality_advisory"] = advisory.to_dict()
        else:
            enriched["quality_advisory"] = {
                "version": "1.0",
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "scope": [],
                "checks": [],
                "summary": {
                    "warning_count": 0,
                    "blocking_count": 0,
                    "risk_score": 0,
                },
                "t0_recommendation": {
                    "decision": "approve",
                    "reason": "No changed files detected",
                    "suggested_dispatches": [],
                    "open_items": [],
                },
            }
    except Exception as exc:
        _emit("WARN", "quality_advisory_failed", error=str(exc))
        enriched["quality_advisory"] = {
            "status": "unavailable",
            "error": str(exc),
        }

    # Enrich with open items delta for CQS (best-effort)
    try:
        dispatch_id_for_oi = enriched.get("dispatch_id") or enriched.get("metadata", {}).get("dispatch_id")
        if dispatch_id_for_oi:
            oim = _get_open_items_manager()
            resolved_count = oim.count_items_closed_by_dispatch(dispatch_id_for_oi)
            enriched["open_items_resolved"] = resolved_count

            db_path_oi = state_dir / "quality_intelligence.db"
            if db_path_oi.exists():
                conn_oi = sqlite3.connect(str(db_path_oi))
                row = conn_oi.execute(
                    "SELECT target_open_items FROM dispatch_metadata WHERE dispatch_id=?",
                    (dispatch_id_for_oi,),
                ).fetchone()
                conn_oi.close()
                if row and row[0]:
                    try:
                        enriched["target_open_items"] = json.loads(row[0])
                    except (json.JSONDecodeError, TypeError):
                        pass
    except Exception as exc:
        _emit("WARN", "oi_delta_enrichment_failed", error=str(exc))

    # Compute the would-be creation count without mutating the open-items store.
    if "open_items_created" not in enriched:
        enriched["open_items_created"] = _count_quality_violations_against_store(enriched)

    # Compute CQS and persist to dispatch_metadata (best-effort).
    try:
        db_path = state_dir / "quality_intelligence.db"
        if db_path.exists():
            dispatch_id = enriched.get("dispatch_id") or enriched.get("metadata", {}).get("dispatch_id")
            if dispatch_id:
                cqs_result = calculate_cqs(enriched, None, db_path, dispatch_id)
                enriched["cqs"] = cqs_result
                conn = sqlite3.connect(str(db_path))
                conn.execute(
                    """UPDATE dispatch_metadata
                       SET cqs=?, normalized_status=?, cqs_components=?,
                           open_items_created=?, open_items_resolved=?,
                           quality_advisory_json=?
                       WHERE dispatch_id=?""",
                    (
                        cqs_result["cqs"],
                        cqs_result["normalized_status"],
                        json.dumps(cqs_result["components"]),
                        enriched.get("open_items_created", 0),
                        enriched.get("open_items_resolved", 0),
                        json.dumps(enriched.get("quality_advisory") or {}),
                        dispatch_id,
                    ),
                )
                conn.commit()
                conn.close()
    except Exception as exc:
        _emit("WARN", "cqs_calculation_failed", error=str(exc))

    return enriched


def _extract_changed_files_from_report(report_path: Path, repo_root: Path) -> List[Path]:
    """Best-effort: parse 'Files Modified' section from report markdown.

    Supports two formats:
    1. Bullet list:  - `path/to/file.py` — description
    2. Markdown table:  | `path/to/file.py` | Type | Description |
    """
    if not report_path.exists():
        return []

    try:
        content = report_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    pattern = re.compile(
        r"^#{2,3}\s+Files\s+Modified(?:/Created)?\s*$", re.MULTILINE
    )
    match = pattern.search(content)
    if not match:
        return []

    section = content[match.end():]
    next_heading = re.search(r"^##+\s+", section, re.MULTILINE)
    if next_heading:
        section = section[:next_heading.start()]

    files: List[Path] = []
    for line in section.splitlines():
        line = line.strip()

        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue

        backtick = re.search(r"`([^`]+\.\w+)`", line)
        if backtick:
            raw_path = backtick.group(1).strip()
        elif line.startswith("-"):
            raw_path = line.lstrip("-").strip().split(":", 1)[0].strip()
        elif line.startswith("|"):
            cells = [c.strip() for c in line.split("|") if c.strip()]
            raw_path = cells[0] if cells else ""
        else:
            continue

        if not raw_path or not re.search(r"\.\w+$", raw_path):
            continue

        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if candidate.exists():
            files.append(candidate)

    return files
