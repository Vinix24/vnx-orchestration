#!/usr/bin/env python3
"""
Generate Suggested Edits — Human-in-the-loop system tuning.

Reads session_analytics + improvement_suggestions from quality_intelligence.db
and generates pending_edits.json with suggested changes to:
  - MEMORY.md (model performance patterns)
  - .claude/rules/*.md (threshold adjustments)
  - Terminal CLAUDE.md (new sections)
  - Skill templates (intelligence references)

All edits are "pending" — nothing is applied automatically.
Use apply_suggested_edits.py to review, accept, reject, and apply.
"""

import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

_UTC = timezone.utc
from pathlib import Path
from typing import List

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
try:
    from vnx_paths import ensure_env
except Exception as exc:
    raise SystemExit(f"Failed to load vnx_paths: {exc}")

PATHS = ensure_env()
STATE_DIR = Path(PATHS["VNX_STATE_DIR"])
VNX_HOME = Path(PATHS["VNX_HOME"])
PROJECT_ROOT = Path(PATHS["PROJECT_ROOT"])
DB_PATH = STATE_DIR / "quality_intelligence.db"
PENDING_PATH = STATE_DIR / "pending_edits.json"
HISTORY_PATH = STATE_DIR / "edit_history.json"

LOOKBACK_DAYS = 7
MAX_SUGGESTIONS_PER_RUN = 5
MIN_SESSIONS_FOR_SUGGESTION = 5
MIN_CONFIDENCE = 0.7


def _load_existing_pending() -> dict:
    """Load existing pending edits (preserve unapplied ones)."""
    if PENDING_PATH.exists():
        try:
            data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
            return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"generated_at": "", "edits": []}


def _load_history() -> list:
    """Load edit history to avoid re-suggesting applied edits."""
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def _content_hash(category: str, target: str, content: str) -> str:
    """Simple fingerprint to detect duplicate suggestions."""
    import hashlib
    return hashlib.sha256(f"{category}|{target}|{content}".encode()).hexdigest()[:16]


def _is_already_suggested_or_applied(
    content_fp: str,
    existing_edits: list,
    history: list,
) -> bool:
    """Check if this content is already pending or was previously applied."""
    for edit in existing_edits:
        if edit.get("_fingerprint") == content_fp and edit.get("status") in ("pending", "accepted"):
            return True
    for entry in history:
        if entry.get("_fingerprint") == content_fp:
            return True
    return False


def generate_memory_suggestions(conn: sqlite3.Connection, since: str) -> List[dict]:
    """Generate MEMORY.md suggestions from model performance patterns."""
    cur = conn.cursor()

    # Model comparison: which model performs better for which task type
    cur.execute("""
        SELECT
            session_model,
            primary_activity,
            COUNT(*) as total,
            SUM(CASE WHEN has_error_recovery = 0 THEN 1 ELSE 0 END) as success_count,
            AVG(total_output_tokens) as avg_tokens,
            SUM(cache_read_tokens) as cache_read,
            SUM(cache_creation_tokens) as cache_create
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
        GROUP BY session_model, primary_activity
        HAVING COUNT(*) >= ?
        ORDER BY session_model
    """, (since, MIN_SESSIONS_FOR_SUGGESTION))

    model_activity = {}
    for row in cur.fetchall():
        model, activity, total, success, avg_tok, cache_r, cache_c = row
        key = (model, activity)
        model_activity[key] = {
            "total": total, "success": success,
            "rate": round(success / total, 2) if total > 0 else 0,
            "avg_tokens": round(avg_tok),
            "cache_read": cache_r, "cache_create": cache_c,
        }

    suggestions = []

    # Find activities where models differ significantly
    activities = set(a for _, a in model_activity.keys())
    for activity in activities:
        models_for_activity = {
            m: model_activity[(m, activity)]
            for m, a in model_activity.keys()
            if a == activity
        }
        if len(models_for_activity) < 2:
            continue

        sorted_models = sorted(models_for_activity.items(),
                               key=lambda x: x[1]["rate"], reverse=True)
        best, best_stats = sorted_models[0]
        worst, worst_stats = sorted_models[-1]

        rate_diff = best_stats["rate"] - worst_stats["rate"]
        if rate_diff < 0.20:
            continue

        confidence = round(min(0.95, MIN_CONFIDENCE + rate_diff * 0.5), 2)
        best_pct = round(best_stats["rate"] * 100)
        worst_pct = round(worst_stats["rate"] * 100)

        suggestions.append({
            "category": "memory",
            "target": "MEMORY.md",
            "section": "## Geleerde Patronen",
            "action": "append",
            "content": (
                f"- {best}: {activity} taken {best_pct}% first-try success "
                f"vs {worst} {worst_pct}%. Prefer {best} voor {activity}."
            ),
            "evidence": (
                f"{best_stats['success']}/{best_stats['total']} {best} vs "
                f"{worst_stats['success']}/{worst_stats['total']} {worst} "
                f"(afgelopen {LOOKBACK_DAYS}d)"
            ),
            "confidence": confidence,
        })

    # Token profile per model
    cur.execute("""
        SELECT
            session_model,
            COUNT(*) as total,
            AVG(total_output_tokens) as avg_tokens,
            SUM(cache_read_tokens) as cache_read,
            SUM(cache_creation_tokens) as cache_create
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
        GROUP BY session_model
        HAVING COUNT(*) >= ?
    """, (since, MIN_SESSIONS_FOR_SUGGESTION))

    token_parts = []
    total_sessions = 0
    for row in cur.fetchall():
        model, count, avg_tok, cache_r, cache_c = row
        total_cache = (cache_r or 0) + (cache_c or 0)
        cache_pct = round((cache_r or 0) / total_cache * 100) if total_cache > 0 else 0
        avg_k = round((avg_tok or 0) / 1000)
        token_parts.append(f"{model} avg={avg_k}K/sess cache={cache_pct}%")
        total_sessions += count

    if token_parts and total_sessions >= MIN_SESSIONS_FOR_SUGGESTION:
        suggestions.append({
            "category": "memory",
            "target": "MEMORY.md",
            "section": "## Geleerde Patronen",
            "action": "append",
            "content": f"- Model token profiel ({LOOKBACK_DAYS}d): {' | '.join(token_parts)}",
            "evidence": f"Aggregatie van {total_sessions} sessies over {LOOKBACK_DAYS} dagen",
            "confidence": 0.95,
        })

    return suggestions


def generate_rule_suggestions(conn: sqlite3.Connection, since: str) -> List[dict]:
    """Generate rules suggestions by comparing thresholds with actual performance."""
    suggestions = []

    # Look for performance metrics that significantly beat current thresholds
    # This is a framework — specific rules files are scanned for numeric thresholds
    rules_dir = PROJECT_ROOT / ".claude" / "rules"
    if not rules_dir.exists():
        return suggestions

    # Gather actual performance data
    cur = conn.cursor()
    cur.execute("""
        SELECT
            AVG(duration_minutes) as avg_duration,
            COUNT(*) as total
        FROM session_analytics
        WHERE session_date >= ?
    """, (since,))
    row = cur.fetchone()
    if not row or (row[1] or 0) < 10:
        return suggestions

    return suggestions


def generate_onetime_suggestions() -> List[dict]:
    """Generate one-time suggestions (session brief references, etc.)."""
    suggestions = []

    # Check if t0_session_brief.json exists (generated by generate_t0_session_brief.py)
    brief_path = STATE_DIR / "t0_session_brief.json"
    if not brief_path.exists():
        return suggestions

    # Suggest adding session intelligence section to T0 CLAUDE.md
    t0_claude_md = PROJECT_ROOT / ".claude" / "terminals" / "T0" / "CLAUDE.md"
    if t0_claude_md.exists():
        content = t0_claude_md.read_text(encoding="utf-8")
        if "Session Intelligence" not in content:
            suggestions.append({
                "category": "claude_md",
                "target": str(t0_claude_md.relative_to(PROJECT_ROOT)),
                "section": "## Session Intelligence",
                "action": "append_section",
                "content": (
                    "## Session Intelligence\n\n"
                    "Lees `t0_session_brief.json` voor model routing data.\n"
                    "- Check model_performance voor model sterktes/zwaktes\n"
                    "- Check model_routing_hints voor taak-type aanbevelingen\n"
                    "- Check active_concerns voor model-specifieke problemen"
                ),
                "evidence": "T0 heeft geen toegang tot sessie-intelligence, brief is nu beschikbaar",
                "confidence": 1.0,
            })

    # Suggest adding session brief reference to T0 orchestrator skill
    skills_dir = Path(PATHS.get("VNX_SKILLS_DIR", ""))
    t0_skill = skills_dir / "t0-orchestrator" / "template.md" if skills_dir.exists() else None
    if t0_skill and t0_skill.exists():
        content = t0_skill.read_text(encoding="utf-8")
        if "Session Intelligence" not in content:
            suggestions.append({
                "category": "skill",
                "target": str(t0_skill.relative_to(PROJECT_ROOT)) if str(t0_skill).startswith(str(PROJECT_ROOT)) else str(t0_skill),
                "section": "Session Intelligence",
                "action": "append_section",
                "content": (
                    "## Session Intelligence (lees voor dispatching)\n\n"
                    "Beschikbaar: `t0_session_brief.json`\n"
                    "Gebruik bij dispatch: model_routing_hints voor model selectie, "
                    "active_concerns voor risico's"
                ),
                "evidence": "Session brief wordt nightly gegenereerd maar T0 skill verwijst er niet naar",
                "confidence": 1.0,
            })

    return suggestions


def generate_digest_section(edits: list) -> str:
    """Generate the digest markdown section for pending edits."""
    if not edits:
        return ""

    pending = [e for e in edits if e.get("status") == "pending"]
    if not pending:
        return ""

    lines = [
        f"## Voorgestelde Wijzigingen ({len(pending)} pending)",
        "",
        "Review met: `vnx suggest review`",
        "Accepteer: `vnx suggest accept 1,3,5`",
        "Afwijzen:  `vnx suggest reject 2,4`",
        "Toepassen: `vnx suggest apply`",
        "",
    ]

    cat_labels = {
        "memory": "MEMORY",
        "rule": "RULE",
        "claude_md": "CLAUDE.MD",
        "skill": "SKILL",
        "hook": "HOOK",
    }

    for edit in pending:
        eid = edit.get("id", "?")
        cat = cat_labels.get(edit.get("category", ""), edit.get("category", "").upper())
        target = Path(edit.get("target", "")).name
        action = edit.get("action", "append")
        content = edit.get("content", "")
        confidence = edit.get("confidence", 0)
        evidence = edit.get("evidence", "")

        action_label = "Toevoegen" if action in ("append", "append_section") else "Wijzigen"
        content_preview = content.split("\n")[0][:80]

        lines.append(f"### #{eid} [{cat}] {target}")
        lines.append(f"**{action_label}**: {content_preview}")
        lines.append(f"**Confidence**: {confidence:.2f} | **Bewijs**: {evidence}")
        lines.append("")

    return "\n".join(lines)


def main():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    # Load existing state
    existing = _load_existing_pending()
    existing_edits = existing.get("edits", [])
    history = _load_history()

    # Generate new suggestions
    all_suggestions = []
    all_suggestions.extend(generate_memory_suggestions(conn, since))
    all_suggestions.extend(generate_rule_suggestions(conn, since))
    all_suggestions.extend(generate_onetime_suggestions())
    conn.close()

    # Filter out duplicates and already-applied
    new_edits = []
    next_id = max((e.get("id", 0) for e in existing_edits), default=0) + 1

    for sg in all_suggestions:
        fp = _content_hash(sg["category"], sg["target"], sg["content"])
        if _is_already_suggested_or_applied(fp, existing_edits, history):
            continue

        if sg.get("confidence", 0) < MIN_CONFIDENCE:
            continue

        sg["id"] = next_id
        sg["status"] = "pending"
        sg["_fingerprint"] = fp
        sg["suggested_at"] = datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z")
        new_edits.append(sg)
        next_id += 1

        if len(new_edits) >= MAX_SUGGESTIONS_PER_RUN:
            break

    # Merge: keep existing non-applied pending edits + add new ones
    kept_edits = [e for e in existing_edits if e.get("status") in ("pending", "accepted")]
    final_edits = kept_edits + new_edits

    output = {
        "generated_at": datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z"),
        "edits": final_edits,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    # Generate digest section
    digest_section = generate_digest_section(final_edits)

    print(f"Suggested edits written to: {PENDING_PATH}")
    print(f"  New: {len(new_edits)} | Carried over: {len(kept_edits)} | Total pending: {len(final_edits)}")

    if digest_section:
        print(f"\n{digest_section}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
