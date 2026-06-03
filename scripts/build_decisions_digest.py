#!/usr/bin/env python3
"""VNX Nightly Beslissings-output Digest builder (pipeline phase 20).

Consumes existing nightly outputs (weekly_digest.json, t0_quality_digest.json,
dispatch_register.ndjson, quality_intelligence.db) and produces a 1-page
decisions-first markdown digest.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_UTC = timezone.utc

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

try:
    from vnx_paths import ensure_env

    PATHS = ensure_env()
    STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
    DATA_DIR = Path(PATHS["VNX_DATA_DIR"])
except Exception:
    DATA_DIR = Path(os.environ.get("VNX_DATA_DIR", ".vnx-data")).resolve()
    STATE_DIR = DATA_DIR / "state"


def _load_json(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ndjson_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return lines


def _parse_ts(ts_raw: str) -> datetime | None:
    if not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_UTC)
        return ts
    except ValueError:
        return None


def _select_top_3_decisions(
    suggestions: list[dict], antipatterns: list[dict]
) -> list[dict]:
    """Select top-3 decisions.

    Priority:
    1. Suggestion pending >24h not accepted/rejected
    2. Critical antipattern with concrete-action context
    3. PR review-gate stuck >24h
    """
    now = datetime.now(tz=_UTC)
    decisions: list[dict] = []
    seen_sources: set[str] = set()

    # P1: pending suggestions >24h
    for idx, s in enumerate(suggestions):
        if len(decisions) >= 3:
            break
        if s.get("status") != "pending":
            continue
        eid = str(s.get("id") or f"SUG-{idx + 1}")
        if eid in seen_sources:
            continue

        ts = _parse_ts(s.get("suggested_at") or s.get("created_at") or "")
        age_h = (now - ts).total_seconds() / 3600 if ts else 25.0  # undated = treat as stale

        if age_h < 24:
            continue

        seen_sources.add(eid)
        category = (s.get("category") or "").upper()
        target = Path(s.get("target") or "").name or "unknown"
        content_line = (s.get("content") or "").split("\n")[0][:80].rstrip()

        decisions.append(
            {
                "id": f"DEC-{len(decisions) + 1}",
                "source_id": eid,
                "title": f"[{category}] Suggestion for {target}",
                "context": content_line,
                "recommended": f"vnx suggest accept {eid}",
                "alternative": f"vnx suggest reject {eid}",
                "source_ref": f"suggestion#{eid}",
                "age_h": round(age_h, 1),
            }
        )

    # P2: critical antipatterns with concrete action
    if len(decisions) < 3:
        _concrete_keywords = ("gitignore", "arch", "integrat", "centrali", "communicat", "correct", "revise")
        for ap in antipatterns:
            if len(decisions) >= 3:
                break
            title = ap.get("title") or ap.get("description") or ""
            severity = (ap.get("severity") or "").lower()
            is_critical = severity == "critical" or "[CRITICAL]" in title
            if not is_critical:
                continue
            has_concrete = any(kw in title.lower() for kw in _concrete_keywords)
            if not has_concrete:
                continue
            source_id = str(ap.get("id") or f"AP-{len(decisions) + 1}")
            if source_id in seen_sources:
                continue
            seen_sources.add(source_id)
            decisions.append(
                {
                    "id": f"DEC-{len(decisions) + 1}",
                    "source_id": source_id,
                    "title": f"[ANTIPATTERN] {title[:80].rstrip()}",
                    "context": "Critical recurring issue flagged by intelligence pipeline",
                    "recommended": "Scope and create a fix dispatch",
                    "alternative": "Defer to 1.x backlog",
                    "source_ref": "antipattern:critical",
                    "age_h": 0,
                }
            )

    # P3: PR review gates stuck >24h
    if len(decisions) < 3:
        gates_dir = DATA_DIR / "state" / "review_gates" / "results"
        if gates_dir.is_dir():
            for gate_file in sorted(gates_dir.glob("*.json"))[-10:]:
                if len(decisions) >= 3:
                    break
                try:
                    gdata = json.loads(gate_file.read_text(encoding="utf-8"))
                    ts = _parse_ts(gdata.get("created_at") or gdata.get("timestamp") or "")
                    age_h = (now - ts).total_seconds() / 3600 if ts else 0.0
                    if age_h < 24:
                        continue
                    pr_ref = str(gdata.get("pr_ref") or gdata.get("pr") or gate_file.stem)
                    if pr_ref in seen_sources:
                        continue
                    seen_sources.add(pr_ref)
                    decisions.append(
                        {
                            "id": f"DEC-{len(decisions) + 1}",
                            "source_id": pr_ref,
                            "title": f"[PR GATE STUCK] {pr_ref}",
                            "context": f"Review gate open >{age_h:.0f}h with no merge decision",
                            "recommended": "Merge if gate is green",
                            "alternative": "Request re-review or close PR",
                            "source_ref": f"pr_gate:{gate_file.stem}",
                            "age_h": round(age_h, 1),
                        }
                    )
                except Exception:
                    pass

    return decisions[:3]


def _build_progress_table(yesterday: bool = True) -> dict:
    """Read dispatch_register + phase log for recent activity metrics."""
    now = datetime.now(tz=_UTC)
    cutoff = now - timedelta(hours=24 if yesterday else 168)

    register_lines = _ndjson_lines(DATA_DIR / "dispatch_register.ndjson")

    dispatches_total = 0
    dispatches_success = 0
    prs_merged = 0

    for line in register_lines:
        ts = _parse_ts(line.get("timestamp") or line.get("created_at") or "")
        if ts and ts < cutoff:
            continue

        event = (line.get("event") or line.get("event_type") or "").lower()
        if event in ("dispatch_closed", "closed", "completed"):
            dispatches_total += 1
            status = (line.get("status") or "").lower()
            if status in ("done", "success", "completed"):
                dispatches_success += 1
        elif "pr_merged" in event or event == "merged":
            prs_merged += 1

    # Dream cycles and failed phases from pipeline phase log
    dream_cycles = 0
    failed_phases = 0
    phase_lines = _ndjson_lines(STATE_DIR / "nightly_pipeline_phases.ndjson")
    for line in phase_lines:
        phase = (line.get("phase") or "").lower()
        status = (line.get("status") or "").lower()
        if "dream" in phase and status == "ok":
            dream_cycles += 1
        if status == "failed":
            failed_phases += 1

    success_pct = (
        f"{round(dispatches_success / dispatches_total * 100)}%"
        if dispatches_total
        else "n/a"
    )
    return {
        "prs_merged": prs_merged,
        "dispatches": dispatches_total,
        "dispatches_success_pct": success_pct,
        "ois_filed": 0,
        "ois_closed": 0,
        "dream_cycles": dream_cycles,
        "failed_ci": failed_phases,
    }


def _build_dream_insights(
    db_path: Path | None = None,
    days: int = 7,
) -> dict:
    """Read quality_intelligence.db for pattern candidates from last N days."""
    resolved_db = db_path or (STATE_DIR / "quality_intelligence.db")
    new_candidates: list[dict] = []
    auto_promoted: list[str] = []

    if not resolved_db.exists():
        return {"new_candidates": new_candidates, "auto_promoted": auto_promoted}

    cutoff_ts = (datetime.now(tz=_UTC) - timedelta(days=days)).isoformat()

    try:
        conn = sqlite3.connect(str(resolved_db), timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Dream proposals (preferred source)
        try:
            cur.execute(
                """SELECT title, confidence, occurrence_count
                   FROM dream_proposals
                   WHERE status = 'pending' AND created_at >= ?
                   ORDER BY confidence DESC LIMIT 5""",
                (cutoff_ts,),
            )
            for row in cur.fetchall():
                new_candidates.append(
                    {
                        "title": row["title"] or "",
                        "confidence": float(row["confidence"] or 0),
                        "occurrences": int(row["occurrence_count"] or 0),
                    }
                )
        except sqlite3.OperationalError:
            pass

        # Fallback: success_patterns as candidate source
        if not new_candidates:
            try:
                cur.execute(
                    """SELECT title, confidence, occurrence_count
                       FROM success_patterns
                       WHERE created_at >= ?
                       ORDER BY confidence DESC LIMIT 3""",
                    (cutoff_ts,),
                )
                for row in cur.fetchall():
                    new_candidates.append(
                        {
                            "title": row["title"] or "",
                            "confidence": float(row["confidence"] or 0),
                            "occurrences": int(row["occurrence_count"] or 0),
                        }
                    )
            except sqlite3.OperationalError:
                pass

        # Auto-promoted maintenance records
        try:
            cur.execute(
                """SELECT action, detail
                   FROM dream_maintenance_log
                   WHERE applied_at >= ? AND status = 'applied'
                   LIMIT 5""",
                (cutoff_ts,),
            )
            for row in cur.fetchall():
                detail = row["detail"] or ""
                auto_promoted.append(
                    row["action"] + (f": {detail}" if detail else "")
                )
        except sqlite3.OperationalError:
            pass

        conn.close()
    except Exception:
        pass

    return {"new_candidates": new_candidates, "auto_promoted": auto_promoted}


def _build_health() -> dict:
    """Aggregate pipeline health: phases, lane mix, receipt lag, DB sizes."""
    health_file = _load_json(STATE_DIR / "nightly_pipeline_health.json")
    register_lines = _ndjson_lines(DATA_DIR / "dispatch_register.ndjson")
    receipts_path = STATE_DIR / "t0_receipts.ndjson"

    # Lane mix from last 200 dispatch lines
    lane_counts: dict[str, int] = {}
    total_recent = 0
    for line in register_lines[-200:]:
        provider = (line.get("provider") or line.get("model") or "unknown").lower()
        for key in ("claude", "codex", "kimi", "gemini", "deepseek", "gemma", "local"):
            if key in provider:
                provider = key
                break
        lane_counts[provider] = lane_counts.get(provider, 0) + 1
        total_recent += 1

    if total_recent > 0 and lane_counts:
        parts = [
            f"{p} {round(c / total_recent * 100)}%"
            for p, c in sorted(lane_counts.items(), key=lambda x: -x[1])
        ]
        lane_mix_str = ", ".join(parts[:6])
    else:
        lane_mix_str = "n/a"

    # Receipt processor lag from last receipt timestamp
    receipt_lag_str = "unknown"
    if receipts_path.exists():
        try:
            last_line_raw = ""
            with open(receipts_path, encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        last_line_raw = stripped
            if last_line_raw:
                rec = json.loads(last_line_raw)
                ts = _parse_ts(rec.get("timestamp") or "")
                if ts:
                    lag_s = int((datetime.now(tz=_UTC) - ts).total_seconds())
                    receipt_lag_str = f"{lag_s}s since last receipt"
        except Exception:
            pass

    # DB sizes
    db_sizes: dict[str, str] = {}
    for db_name in ("quality_intelligence.db", "runtime_coordination.db"):
        db_p = STATE_DIR / db_name
        if db_p.exists():
            size_mb = round(db_p.stat().st_size / 1024 / 1024, 1)
            db_sizes[db_name] = f"{size_mb}MB"

    pipeline_status = "unknown"
    phases_ok = 0
    phases_run = 0
    if isinstance(health_file, dict):
        pipeline_status = health_file.get("overall_status", "unknown")
        phases_ok = int(health_file.get("phases_ok", 0))
        phases_run = int(health_file.get("phases_run", 0))

    return {
        "pipeline_status": pipeline_status,
        "phases_ok": phases_ok,
        "phases_run": phases_run,
        "lane_mix": lane_mix_str,
        "receipt_lag": receipt_lag_str,
        "db_sizes": db_sizes,
    }


def _build_tomorrow_queue(data_dir: Path | None = None) -> list[dict]:
    """Collect next-up items: pending dispatches and queued tracks."""
    resolved_data = data_dir or DATA_DIR
    items: list[dict] = []

    pending_dir = resolved_data / "dispatches" / "pending"
    if pending_dir.is_dir():
        for f in sorted(pending_dir.glob("*.md"))[:5]:
            items.append(
                {"type": "dispatch", "ref": f.stem, "title": f.stem, "status": "pending"}
            )

    db_path = resolved_data / "state" / "quality_intelligence.db"
    if db_path.exists() and len(items) < 5:
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            try:
                cur.execute(
                    """SELECT id, title, phase, horizon
                       FROM tracks
                       WHERE phase = 'queued'
                       ORDER BY updated_at DESC LIMIT 5"""
                )
                for row in cur.fetchall():
                    items.append(
                        {
                            "type": "track",
                            "ref": row["id"],
                            "title": row["title"] or row["id"],
                            "status": f"phase={row['phase']}, horizon={row.get('horizon') or '?'}",
                        }
                    )
            except sqlite3.OperationalError:
                pass
            conn.close()
        except Exception:
            pass

    return items[:5]


def _render_markdown(
    decisions: list[dict],
    progress: dict,
    dream: dict,
    health: dict,
    queue: list[dict],
    date_str: str,
) -> str:
    lines = [
        f"# VNX Nightly Digest -- {date_str}",
        "",
        "## Need YOUR decision (top 3, action required)",
        "",
    ]

    if decisions:
        for i, dec in enumerate(decisions, 1):
            age_note = f" (pending {dec['age_h']}h)" if dec.get("age_h") else ""
            lines += [
                f"{i}. **{dec['title']}**{age_note} -- {dec['context']}",
                f"   - Recommended: `{dec['recommended']}`",
                f"   - Alternative: `{dec['alternative']}`",
                f"   - Source: {dec['source_ref']}",
                f"   - Reply with: `vnx digest decide {dec['id']} accept|alt|defer`",
                "",
            ]
    else:
        lines += ["_No decisions pending -- all suggestions reviewed._", ""]

    lines += [
        "## Yesterday's progress",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| PRs merged | {progress.get('prs_merged', 0)} |",
        f"| Dispatches | {progress.get('dispatches', 0)} (success {progress.get('dispatches_success_pct', 'n/a')}) |",
        f"| OIs filed / closed | {progress.get('ois_filed', 0)} / {progress.get('ois_closed', 0)} |",
        f"| Auto-dream cycles | {progress.get('dream_cycles', 0)} |",
        f"| Failed pipeline phases | {progress.get('failed_ci', 0)} |",
        "",
        "## Auto-dream insights (last 7 days)",
        "",
    ]

    new_cands = dream.get("new_candidates", [])
    if new_cands:
        lines.append(f"**{len(new_cands)} new pattern candidates for promotion:**")
        for cand in new_cands:
            conf = cand.get("confidence", 0)
            occ = cand.get("occurrences", 0)
            lines.append(
                f"- \"{cand['title']}\": {occ} occurrences, confidence {conf:.2f}"
            )
    else:
        lines.append("_No new pattern candidates this week._")

    lines.append("")
    auto_prom = dream.get("auto_promoted", [])
    if auto_prom:
        lines.append("**Promoted last night (auto-apply):**")
        for item in auto_prom:
            lines.append(f"- {item}")
    else:
        lines.append("_No auto-promotions last night._")

    lines += [
        "",
        "## Health",
        "",
        f"- **Pipeline status:** {health.get('pipeline_status', 'unknown')} "
        f"({health.get('phases_ok', 0)}/{health.get('phases_run', 0)} phases OK)",
        f"- **Lane mix (recent):** {health.get('lane_mix', 'n/a')}",
        f"- **Receipt-processor:** {health.get('receipt_lag', 'unknown')}",
    ]
    db_sizes = health.get("db_sizes", {})
    if db_sizes:
        sizes_str = ", ".join(f"{k}: {v}" for k, v in db_sizes.items())
        lines.append(f"- **DB sizes:** {sizes_str}")

    lines += [
        "",
        "## Tomorrow's auto-queue",
        "",
    ]

    if queue:
        for i, item in enumerate(queue, 1):
            lines.append(
                f"{i}. **{item['type'].upper()} {item['ref']}**: "
                f"{item['title']} -- {item['status']}"
            )
    else:
        lines.append("_No items queued for tomorrow._")

    lines += [
        "",
        "---",
        f"Generated {datetime.now(tz=_UTC).isoformat().replace('+00:00', 'Z')} | "
        "Source: nightly_intelligence_pipeline phase-20",
    ]

    return "\n".join(lines)


def render_decisions_digest(
    state_dir: Path | None = None,
    data_dir: Path | None = None,
) -> str:
    """Load all inputs and return rendered markdown digest.

    Optional overrides allow tests to inject temp paths without
    monkeypatching module globals.
    """
    resolved_state = state_dir or STATE_DIR
    resolved_data = data_dir or DATA_DIR

    now = datetime.now(tz=_UTC)
    date_str = now.strftime("%Y-%m-%d")

    digest_raw = _load_json(resolved_state / "weekly_digest.json")
    metrics = digest_raw.get("metrics", {}) if isinstance(digest_raw, dict) else {}
    antipatterns = list(metrics.get("top_antipatterns", []))

    pending_edits = _load_json(resolved_state / "pending_edits.json")
    suggestions: list[dict] = []
    if isinstance(pending_edits, dict):
        suggestions = [
            e for e in pending_edits.get("edits", []) if e.get("status") == "pending"
        ]

    decisions = _select_top_3_decisions(suggestions, antipatterns)
    progress = _build_progress_table(yesterday=True)
    dream = _build_dream_insights()
    health = _build_health()
    queue = _build_tomorrow_queue(data_dir=resolved_data)

    return _render_markdown(decisions, progress, dream, health, queue, date_str)


def main() -> int:
    digest_md = render_decisions_digest()
    output_path = STATE_DIR / "decisions_digest.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(digest_md, encoding="utf-8")
    print(digest_md)
    print(f"\n[digest] Written to {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
