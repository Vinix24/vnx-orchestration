#!/usr/bin/env python3
"""
Quality Digest Builder — 3-Section Format (PR-4)
Purpose: Generate actionable quality digest with 3 structured sections.
Sections:
  1. Operational Defects — code hotspots and critical issues
  2. Prompt/Config Tuning — prevention rules, low-confidence patterns, pending edits
  3. Governance Health — SPC alerts, metric anomalies, failed gates
Output:
  - quality_digest.ndjson  (append-only per G-L6) in runtime state directory
  - t0_quality_digest.json (backward compat, latest only)
Lookback: 24h for receipt-based evidence
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
QUALITY_DB = STATE_DIR / "quality_intelligence.db"
NDJSON_OUTPUT = STATE_DIR / "quality_digest.ndjson"       # append-only (G-L6)
JSON_COMPAT_OUTPUT = STATE_DIR / "t0_quality_digest.json"  # backward compat

MAX_PER_SECTION = 5
LOOKBACK_HOURS = 24
SCHEMA_VERSION = "2.0"


# ── Evidence helpers ──────────────────────────────────────────────────────────

def _load_recent_receipts(state_dir: Path, hours: int = LOOKBACK_HOURS) -> List[Dict]:
    """Load receipts from the last N hours from t0_receipts.ndjson."""
    receipts_path = state_dir / "t0_receipts.ndjson"
    if not receipts_path.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    receipts: List[Dict] = []

    try:
        with open(receipts_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    ts_raw = r.get("timestamp", "")
                    if ts_raw:
                        # Normalise: strip sub-seconds, ensure UTC
                        ts_clean = ts_raw.replace("Z", "+00:00").split(".")[0] + "+00:00"
                        ts = datetime.fromisoformat(ts_clean)
                        if ts > cutoff:
                            receipts.append(r)
                except (json.JSONDecodeError, ValueError):
                    continue
    except OSError:
        pass

    return receipts


def _build_evidence_map(receipts: List[Dict]) -> Dict[str, Any]:
    """Build dispatch-level evidence maps from recent receipts."""
    dispatch_ids: List[str] = []
    failed_ids: List[str] = []

    for r in receipts:
        did = r.get("dispatch_id") or r.get("task_id") or ""
        status = str(r.get("status", "")).lower()
        if did and did not in dispatch_ids:
            dispatch_ids.append(did)
        if did and status in ("failed", "fail", "error", "blocked"):
            if did not in failed_ids:
                failed_ids.append(did)

    return {
        "dispatch_ids": dispatch_ids[:10],
        "failed_dispatch_ids": failed_ids[:5],
    }


def _load_pending_items(state_dir: Path) -> List[Dict]:
    """Load pending edits/recommendations from pending_edits.json."""
    path = state_dir / "pending_edits.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        # Handle both old ("edits") and new ("recommendations") key
        items = data.get("recommendations") or data.get("edits") or []
        # Filter to pending status only
        return [i for i in items if i.get("status", "pending") == "pending"]
    except (json.JSONDecodeError, OSError):
        return []


# ── Section 1: Operational Defects ───────────────────────────────────────────

def build_operational_defects(
    db: Optional[sqlite3.Connection],
    evidence: Dict[str, Any],
) -> List[Dict]:
    """Code hotspots and critical issues (top 5)."""
    if not db:
        return []
    recs: List[Dict] = []
    try:
        cursor = db.execute(
            """
            SELECT q.file_path, q.complexity_score, q.critical_issues,
                   q.warning_issues, q.cyclomatic_complexity,
                   q.max_function_length, q.last_scan, q.suggested_track
            FROM vnx_code_quality q
            WHERE q.file_path IN (SELECT file_path FROM files_needing_attention)
            ORDER BY q.critical_issues DESC, q.complexity_score DESC
            LIMIT ?
            """,
            (MAX_PER_SECTION,),
        )
    except sqlite3.OperationalError as exc:
        logger.debug(f"operational_defects query failed: {exc}")
        return []

    for row in cursor.fetchall():
        (file_path, complexity, critical, warning,
         cyclomatic, max_fn_len, last_scan, track) = row
        complexity = round(float(complexity or 0), 2)
        critical = int(critical or 0)
        warning = int(warning or 0)
        severity = (
            "critical" if complexity >= 90 or critical > 0
            else "high" if complexity >= 75 or warning > 2
            else "medium"
        )
        recs.append({
            "rank": len(recs) + 1,
            "type": "code_hotspot",
            "title": f"Quality hotspot: {Path(file_path).name}",
            "severity": severity,
            "detail": (
                f"complexity={complexity}, cyclomatic={cyclomatic or '?'}, "
                f"critical={critical}, warnings={warning}, "
                f"max_fn_len={max_fn_len or '?'}"
            ),
            "action": f"Refactor to reduce complexity — assign to Track {track or 'C'}",
            "evidence": {
                "file_paths": [file_path],
                "receipt_ids": [],
                "dispatch_ids": evidence["dispatch_ids"][:3],
            },
        })
    return recs


# ── Section 2: Prompt/Config Tuning ──────────────────────────────────────────

def build_prompt_config_tuning(
    db: Optional[sqlite3.Connection],
    state_dir: Path,
    evidence: Dict[str, Any],
) -> List[Dict]:
    """Prevention rules, low-confidence patterns, and pending config edits (top 5)."""
    recs: List[Dict] = []

    # 2a: pending config edits awaiting review
    pending = _load_pending_items(state_dir)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for item in pending[: min(2, MAX_PER_SECTION)]:
        created_raw = item.get("created_at", "")
        is_stale = False
        if created_raw:
            try:
                ca = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                is_stale = ca < stale_cutoff
            except ValueError:
                pass

        ev_ids = item.get("evidence_ids", [])
        recs.append({
            "rank": len(recs) + 1,
            "type": "pending_edit",
            "title": f"Pending config edit: {item.get('target', 'unknown')}",
            "severity": "high" if is_stale else "medium",
            "detail": (
                f"{'[STALE >7d] ' if is_stale else ''}#{item.get('id', '?')}: "
                f"{str(item.get('description', item.get('symptom', 'no description')))[:120]}"
            ),
            "action": "Review and accept/reject via `vnx suggested-edits review`",
            "evidence": {
                "file_paths": [item["target"]] if item.get("target") else [],
                "receipt_ids": [str(i) for i in (ev_ids or [])[:3]],
                "dispatch_ids": [],
            },
        })

    # 2b: prevention rules from DB
    if db:
        remaining = MAX_PER_SECTION - len(recs)
        try:
            cursor = db.execute(
                """
                SELECT id, tag_combination, rule_type, recommendation, confidence
                FROM prevention_rules
                ORDER BY confidence DESC, triggered_count DESC
                LIMIT ?
                """,
                (remaining,),
            )
            for row in cursor.fetchall():
                rule_id, tags_raw, rule_type, recommendation, confidence = row
                try:
                    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else tags_raw
                    tag_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                except (json.JSONDecodeError, TypeError):
                    tag_str = str(tags_raw or "")
                recs.append({
                    "rank": len(recs) + 1,
                    "type": "prevention_rule",
                    "title": f"Prevention rule: [{tag_str}]",
                    "severity": "high" if float(confidence or 0) > 0.7 else "medium",
                    "detail": (
                        f"{rule_type}: {str(recommendation or '')[:150]}"
                    ),
                    "action": "Review rule and promote to active via operator confirmation",
                    "evidence": {
                        "file_paths": [],
                        "receipt_ids": [str(rule_id)],
                        "dispatch_ids": evidence["dispatch_ids"][:2],
                    },
                })
        except sqlite3.OperationalError as exc:
            logger.debug(f"prevention_rules query failed: {exc}")

    # 2c: low-confidence patterns (fill remaining slots)
    if db:
        remaining = MAX_PER_SECTION - len(recs)
        if remaining > 0:
            try:
                cursor = db.execute(
                    """
                    SELECT pattern_id, pattern_title, confidence, ignored_count, used_count
                    FROM pattern_usage
                    WHERE confidence < 0.5
                    ORDER BY confidence ASC, ignored_count DESC
                    LIMIT ?
                    """,
                    (remaining,),
                )
                for row in cursor.fetchall():
                    pid, title, conf, ignored, used = row
                    recs.append({
                        "rank": len(recs) + 1,
                        "type": "low_confidence_pattern",
                        "title": f"Low-confidence pattern: {title or pid}",
                        "severity": "low",
                        "detail": (
                            f"confidence={round(float(conf or 0), 3)}, "
                            f"ignored={ignored or 0}, used={used or 0}"
                        ),
                        "action": "Retire pattern or update content to improve adoption",
                        "evidence": {
                            "file_paths": [],
                            "receipt_ids": [str(pid)],
                            "dispatch_ids": [],
                        },
                    })
            except sqlite3.OperationalError as exc:
                logger.debug(f"pattern_usage query failed: {exc}")

    return recs[:MAX_PER_SECTION]


# ── Section 3: Governance Health ─────────────────────────────────────────────

def build_governance_health(
    db: Optional[sqlite3.Connection],
    evidence: Dict[str, Any],
) -> List[Dict]:
    """SPC alerts, governance metric anomalies, and failed gates (top 5)."""
    recs: List[Dict] = []

    # 3a: unacknowledged SPC alerts
    if db:
        try:
            cursor = db.execute(
                """
                SELECT id, alert_type, metric_name, scope_type, scope_value,
                       observed_value, control_limit, description, severity, detected_at
                FROM spc_alerts
                WHERE acknowledged_at IS NULL
                ORDER BY
                    CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                    detected_at DESC
                LIMIT ?
                """,
                (MAX_PER_SECTION,),
            )
            for row in cursor.fetchall():
                (alert_id, alert_type, metric, scope_type, scope_val,
                 observed, limit_val, description, severity, detected_at) = row
                recs.append({
                    "rank": len(recs) + 1,
                    "type": "spc_alert",
                    "title": f"SPC alert: {metric} ({alert_type})",
                    "severity": severity or "warning",
                    "detail": (
                        f"{description or alert_type}: {scope_type}={scope_val}, "
                        f"observed={round(float(observed or 0), 3)}, "
                        f"limit={round(float(limit_val), 3) if limit_val is not None else 'N/A'}"
                    ),
                    "action": "Investigate metric trend and acknowledge or escalate",
                    "evidence": {
                        "file_paths": [],
                        "receipt_ids": [str(alert_id)],
                        "dispatch_ids": evidence["dispatch_ids"][:2],
                    },
                })
        except sqlite3.OperationalError as exc:
            logger.debug(f"spc_alerts query failed: {exc}")

    # 3b: failed gates from recent receipts
    remaining = MAX_PER_SECTION - len(recs)
    for did in evidence["failed_dispatch_ids"][:remaining]:
        recs.append({
            "rank": len(recs) + 1,
            "type": "failed_gate",
            "title": f"Failed gate: {did}",
            "severity": "high",
            "detail": f"Dispatch {did} reported failure in last {LOOKBACK_HOURS}h",
            "action": "Review failure report and create investigation dispatch",
            "evidence": {
                "file_paths": [],
                "receipt_ids": [],
                "dispatch_ids": [did],
            },
        })

    # 3c: governance metrics below threshold
    if db:
        remaining = MAX_PER_SECTION - len(recs)
        if remaining > 0:
            try:
                cursor = db.execute(
                    """
                    SELECT metric_name, scope_type, scope_value, metric_value, computed_at
                    FROM governance_metrics
                    WHERE datetime(computed_at) > datetime('now', '-1 day')
                      AND metric_value < 0.5
                    ORDER BY metric_value ASC
                    LIMIT ?
                    """,
                    (remaining,),
                )
                for row in cursor.fetchall():
                    metric_name, scope_type, scope_val, value, computed_at = row
                    recs.append({
                        "rank": len(recs) + 1,
                        "type": "governance_metric",
                        "title": f"Low governance metric: {metric_name}",
                        "severity": "medium",
                        "detail": (
                            f"{scope_type}={scope_val}, "
                            f"{metric_name}={round(float(value or 0), 3)} (<0.5)"
                        ),
                        "action": "Review governance metrics and address process gaps",
                        "evidence": {
                            "file_paths": [],
                            "receipt_ids": [],
                            "dispatch_ids": evidence["dispatch_ids"][:2],
                        },
                    })
            except sqlite3.OperationalError as exc:
                logger.debug(f"governance_metrics query failed: {exc}")

    # Fallback: healthy status when nothing to report
    if not recs:
        recs.append({
            "rank": 1,
            "type": "governance_status",
            "title": "No active governance alerts",
            "severity": "info",
            "detail": (
                f"No SPC alerts, failed gates, or metric anomalies "
                f"in last {LOOKBACK_HOURS}h"
            ),
            "action": "System operating within normal parameters",
            "evidence": {"file_paths": [], "receipt_ids": [], "dispatch_ids": []},
        })

    return recs[:MAX_PER_SECTION]


# ── Digest assembly & output ──────────────────────────────────────────────────

def _assemble_digest(sections: Dict[str, List[Dict]], run_id: str) -> Dict:
    total = sum(len(v) for v in sections.values())
    critical_high = sum(
        1 for s in sections.values()
        for r in s if r.get("severity") in ("critical", "high")
    )
    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "pipeline_run_id": run_id,
        "lookback_hours": LOOKBACK_HOURS,
        "sections": {
            "operational_defects": {
                "title": "Operational Defects",
                "description": (
                    "Code complexity hotspots and critical issues "
                    "requiring immediate attention"
                ),
                "recommendations": sections["operational_defects"],
            },
            "prompt_config_tuning": {
                "title": "Prompt/Config Tuning",
                "description": (
                    "Prevention rules, low-confidence patterns, "
                    "and pending configuration edits"
                ),
                "recommendations": sections["prompt_config_tuning"],
            },
            "governance_health": {
                "title": "Governance Health",
                "description": (
                    "SPC alerts, governance metric anomalies, "
                    "and failed quality gates"
                ),
                "recommendations": sections["governance_health"],
            },
        },
        "summary": {
            "total_recommendations": total,
            "critical_or_high_count": critical_high,
            "sections": {k: len(v) for k, v in sections.items()},
        },
    }


def _append_ndjson(digest: Dict, output_path: Path) -> None:
    """Append one digest record as a single NDJSON line (G-L6 — append-only)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(digest, ensure_ascii=False, separators=(",", ":")) + "\n")
    logger.info(f"Appended digest run {digest['pipeline_run_id']} to {output_path}")


def _write_compat_json(digest: Dict, output_path: Path) -> None:
    """Write latest digest as pretty JSON for backward-compat consumers."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    compat = {
        "updated_at": digest["run_at"],
        "schema_version": digest["schema_version"],
        "pipeline_run_id": digest["pipeline_run_id"],
        "lookback_hours": digest["lookback_hours"],
        # Legacy key kept for older consumers
        "top_hotspots": digest["sections"]["operational_defects"]["recommendations"],
        "sections": digest["sections"],
        "summary": digest["summary"],
        # Flat recommendation list for scripts that iterate this key
        "recommendations": [
            r["title"]
            for sec in digest["sections"].values()
            for r in sec["recommendations"]
            if r.get("severity") in ("critical", "high")
        ][:5],
    }
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(compat, fh, indent=2, ensure_ascii=False)
    logger.info(f"Written compat digest to {output_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== T0 Quality Digest Builder v2.0 (3-section format) ===")
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Load evidence from receipts
    receipts = _load_recent_receipts(STATE_DIR, LOOKBACK_HOURS)
    evidence = _build_evidence_map(receipts)
    logger.info(
        f"Evidence: {len(receipts)} receipts, "
        f"{len(evidence['dispatch_ids'])} dispatch IDs, "
        f"{len(evidence['failed_dispatch_ids'])} failures"
    )

    # Open DB (read-only queries)
    db: Optional[sqlite3.Connection] = None
    if QUALITY_DB.exists():
        db = sqlite3.connect(str(QUALITY_DB))
    else:
        logger.warning(f"quality_intelligence.db not found at {QUALITY_DB}")

    try:
        sections = {
            "operational_defects": build_operational_defects(db, evidence),
            "prompt_config_tuning": build_prompt_config_tuning(db, STATE_DIR, evidence),
            "governance_health": build_governance_health(db, evidence),
        }
    finally:
        if db:
            db.close()

    digest = _assemble_digest(sections, run_id)

    # Output 1: append-only NDJSON (G-L6)
    _append_ndjson(digest, NDJSON_OUTPUT)

    # Output 2: latest JSON for backward compat
    _write_compat_json(digest, JSON_COMPAT_OUTPUT)

    # Summary
    s = digest["summary"]
    print(f"\n📊 Quality Digest ({run_id}):")
    print(f"  Operational Defects:   {s['sections']['operational_defects']} items")
    print(f"  Prompt/Config Tuning:  {s['sections']['prompt_config_tuning']} items")
    print(f"  Governance Health:     {s['sections']['governance_health']} items")
    print(
        f"  Total: {s['total_recommendations']} "
        f"({s['critical_or_high_count']} critical/high)"
    )
    print(f"  NDJSON: {NDJSON_OUTPUT}")
    print(f"  JSON:   {JSON_COMPAT_OUTPUT}")


if __name__ == "__main__":
    main()
