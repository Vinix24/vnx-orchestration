"""Completion-receipt enrichment orchestrator + per-concern helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .common import _emit, facade
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
            "status": "unavailable",
            "error": str(exc),
        }


def _enrich_session_metadata(enriched: Dict[str, Any], state_dir: Path) -> None:
    """ADR-035 §4: session{} collapses to a single session_id pointer.

    model/provider/token_usage/instruction_sha256 are already top-level
    fields in the v2 canonical shape (§3) — not session state — so they are
    promoted directly onto the receipt here instead of nested under a
    (now-removed) session{} object. Caller-supplied values are never
    overwritten. The rest of the session's state (liveness, terminal
    heartbeat) is resolved by session_id against runtime_coordination.db at
    read time, not carried on the immutable receipt line.
    """
    try:
        session_meta = facade._build_session_metadata(enriched, state_dir)
    except Exception as exc:
        _emit("WARN", "session_metadata_failed", error=str(exc))
        session_meta = {
            "session_id": "unknown",
            "terminal": str(enriched.get("terminal") or "unknown"),
            "model": "unknown",
            "provider": "unknown",
        }

    enriched.setdefault("session_id", session_meta.get("session_id", "unknown"))
    if not enriched.get("terminal"):
        enriched["terminal"] = session_meta.get("terminal", "unknown")
    enriched.setdefault("model", session_meta.get("model", "unknown"))
    enriched.setdefault("provider", session_meta.get("provider", "unknown"))
    if "token_usage" in session_meta:
        enriched.setdefault("token_usage", session_meta["token_usage"])
    if "instruction_sha256" in session_meta:
        enriched.setdefault("instruction_sha256", session_meta["instruction_sha256"])


def _enrich_provenance_linkage(enriched: Dict[str, Any], state_dir: Path) -> None:
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
        # Light up the provenance chain: write the registry row so dispatch -> receipt -> commit -> PR
        # is queryable (and its gaps visible). At append time only dispatch_id + receipt_id + trace_token
        # are known (the commit happens later), so chain_status stays 'incomplete' until merge fills
        # commit_sha. Best-effort; never blocks the append.
        _register_provenance_link(enriched, state_dir)
    except Exception as exc:
        _emit("WARN", "provenance_enrichment_failed", error=str(exc))


def _register_provenance_link(enriched: Dict[str, Any], state_dir: Path) -> None:
    dispatch_id = enriched.get("dispatch_id")
    if not dispatch_id or str(dispatch_id) in ("", "unknown", "none", "null"):
        return
    db_path = state_dir / "runtime_coordination.db"
    if not db_path.exists():
        return
    try:
        import sqlite3 as _sqlite3
        from receipt_provenance import register_provenance_link  # noqa: PLC0415
        receipt_id = str(enriched.get("run_id") or enriched.get("task_id") or "") or None
        pr_number = enriched.get("pr_number")
        try:
            pr_number = int(pr_number) if pr_number is not None else None
        except (TypeError, ValueError):
            pr_number = None
        conn = _sqlite3.connect(str(db_path))
        try:
            register_provenance_link(
                conn,
                dispatch_id=str(dispatch_id),
                receipt_id=receipt_id,
                trace_token=enriched.get("trace_token"),
                pr_number=pr_number,
                feature_plan_pr=enriched.get("feature_plan_pr"),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _emit("WARN", "provenance_register_failed", error=str(exc))


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
    """Enrich completion receipts with provenance, session, and terminal snapshot.

    This is best-effort - failures will result in status="unavailable" markers
    rather than crashing the receipt append flow.

    ADR-035 §3.3/§9 PR-5: quality_advisory{} is no longer generated here —
    superseded by verdict{}/warnings[] (§6), which the shared append
    primitive's finalize step populates. cqs_calculator.py (migrated PR-4b)
    reads verdict{}/warnings[] first, falling back to quality_advisory{}
    only when replaying a pre-cutover (v1) receipt.
    """
    if not _is_completion_event(receipt):
        return receipt

    enriched = receipt.copy()
    paths = facade.ensure_env()
    state_dir = Path(paths.get("VNX_STATE_DIR", ".")).resolve()

    _enrich_project_id_and_git(enriched, paths, repo_root)
    _enrich_session_metadata(enriched, state_dir)
    _enrich_provenance_linkage(enriched, state_dir)
    _enrich_terminal_snapshot(enriched, state_dir)

    if _is_subprocess_intermediate_completion(receipt):
        return enriched

    _enrich_oi_delta(enriched, state_dir)

    if "open_items_created" not in enriched:
        enriched["open_items_created"] = facade._count_quality_violations_against_store(enriched)

    _enrich_cqs(enriched, state_dir)

    return enriched
