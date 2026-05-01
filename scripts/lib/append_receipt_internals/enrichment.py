"""Completion-receipt enrichment orchestrator + per-concern helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .common import _emit, _utc_now_iso, facade
from .validation import _is_completion_event, _is_subprocess_intermediate_completion


def _enrich_project_id_and_git(enriched: Dict[str, Any], paths: Dict[str, str], repo_root: Optional[Path]) -> None:
    enriched.setdefault("project_id", facade.current_project_id())

    if "provenance" in enriched:
        return
    try:
        resolved_repo_root = repo_root or Path(paths.get("PROJECT_ROOT", Path.cwd())).resolve()
        enriched["provenance"] = facade._build_git_provenance(resolved_repo_root)
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


def _enrich_session_metadata(enriched: Dict[str, Any], state_dir: Path) -> None:
    existing_session = enriched.get("session")
    if isinstance(existing_session, dict):
        merged_session = dict(existing_session)
        try:
            defaults = facade._build_session_metadata(enriched, state_dir)
            for key, value in defaults.items():
                merged_session.setdefault(key, value)
        except Exception as exc:
            _emit("WARN", "session_metadata_failed", error=str(exc))
        enriched["session"] = merged_session
        return
    try:
        enriched["session"] = facade._build_session_metadata(enriched, state_dir)
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


def _enrich_provenance_linkage(enriched: Dict[str, Any]) -> None:
    try:
        facade.enrich_receipt_provenance(enriched)
        prov_validation = facade.validate_receipt_provenance(enriched)
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


def _enrich_terminal_snapshot(enriched: Dict[str, Any], state_dir: Path) -> None:
    try:
        snapshot = facade.collect_terminal_snapshot(state_dir)
        enriched["terminal_snapshot"] = snapshot.to_dict()
    except Exception as exc:
        _emit("WARN", "terminal_snapshot_failed", error=str(exc))
        enriched["terminal_snapshot"] = {
            "status": "unavailable",
            "error": str(exc),
        }

    if facade.is_headless_t0():
        snapshot_data = enriched.get("terminal_snapshot") or {}
        terminals = snapshot_data.get("terminals") or {}
        t0_entry = dict(terminals.get("T0") or {})
        t0_entry["adapter"] = "subprocess"
        t0_entry["headless"] = True
        terminals["T0"] = t0_entry
        snapshot_data["terminals"] = terminals
        enriched["terminal_snapshot"] = snapshot_data


def _enrich_quality_advisory(enriched: Dict[str, Any], receipt: Dict[str, Any], repo_root: Optional[Path]) -> None:
    try:
        if repo_root is None:
            repo_root = Path.cwd()

        changed_files = facade.get_changed_files(repo_root)

        if not changed_files:
            report_path = str(receipt.get("report_path") or "")
            if report_path:
                changed_files = facade._extract_changed_files_from_report(Path(report_path), repo_root)

        if changed_files:
            advisory = facade.generate_quality_advisory(changed_files, repo_root)
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


def _enrich_oi_delta(enriched: Dict[str, Any], state_dir: Path) -> None:
    try:
        dispatch_id_for_oi = enriched.get("dispatch_id") or enriched.get("metadata", {}).get("dispatch_id")
        if not dispatch_id_for_oi:
            return
        oim = facade._get_open_items_manager()
        resolved_count = oim.count_items_closed_by_dispatch(dispatch_id_for_oi)
        enriched["open_items_resolved"] = resolved_count

        db_path_oi = state_dir / "quality_intelligence.db"
        if not db_path_oi.exists():
            return
        import sqlite3 as _sqlite3_oi
        conn_oi = _sqlite3_oi.connect(str(db_path_oi))
        try:
            cols = conn_oi.execute("PRAGMA table_info(dispatch_metadata)").fetchall()
            has_project_oi = any(c[1] == "project_id" for c in cols)
        except _sqlite3_oi.Error:
            has_project_oi = False
        if has_project_oi:
            row = conn_oi.execute(
                "SELECT target_open_items FROM dispatch_metadata "
                "WHERE dispatch_id=? AND project_id=?",
                (dispatch_id_for_oi, facade.current_project_id()),
            ).fetchone()
        else:
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


def _enrich_cqs(enriched: Dict[str, Any], state_dir: Path) -> None:
    try:
        db_path = state_dir / "quality_intelligence.db"
        if not db_path.exists():
            return
        dispatch_id = enriched.get("dispatch_id") or enriched.get("metadata", {}).get("dispatch_id")
        if not dispatch_id:
            return
        cqs_result = facade.calculate_cqs(enriched, None, db_path, dispatch_id)
        enriched["cqs"] = cqs_result
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        has_project = False
        try:
            rows = conn.execute("PRAGMA table_info(dispatch_metadata)").fetchall()
            has_project = any(r[1] == "project_id" for r in rows)
        except sqlite3.Error:
            has_project = False
        if has_project:
            conn.execute(
                """UPDATE dispatch_metadata
                   SET cqs=?, normalized_status=?, cqs_components=?,
                       open_items_created=?, open_items_resolved=?,
                       quality_advisory_json=?
                   WHERE dispatch_id=? AND project_id=?""",
                (
                    cqs_result["cqs"],
                    cqs_result["normalized_status"],
                    json.dumps(cqs_result["components"]),
                    enriched.get("open_items_created", 0),
                    enriched.get("open_items_resolved", 0),
                    json.dumps(enriched.get("quality_advisory") or {}),
                    dispatch_id,
                    facade.current_project_id(),
                ),
            )
        else:
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


def _enrich_completion_receipt(receipt: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Enrich completion receipts with quality advisory and terminal snapshot.

    This is best-effort - failures will result in status="unavailable" markers
    rather than crashing the receipt append flow.
    """
    if not _is_completion_event(receipt):
        return receipt

    enriched = receipt.copy()
    paths = facade.ensure_env()
    state_dir = Path(paths.get("VNX_STATE_DIR", ".")).resolve()

    _enrich_project_id_and_git(enriched, paths, repo_root)
    _enrich_session_metadata(enriched, state_dir)
    _enrich_provenance_linkage(enriched)
    _enrich_terminal_snapshot(enriched, state_dir)

    if _is_subprocess_intermediate_completion(receipt):
        return enriched

    _enrich_quality_advisory(enriched, receipt, repo_root)
    _enrich_oi_delta(enriched, state_dir)

    if "open_items_created" not in enriched:
        enriched["open_items_created"] = facade._count_quality_violations_against_store(enriched)

    _enrich_cqs(enriched, state_dir)

    return enriched
