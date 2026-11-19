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

from model_inference_guard import evaluate_activity_routing, HINT
from digest_text import smart_truncate, decision_grade, dedup_suggestions
from suggested_edit_targets import guard_target

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
DEFAULT_ANTIPATTERN_MAX_AGE_DAYS = 30
ROLE_SECTION = "## Wat review het vaakst vindt"
# Every category a generator in this file can emit; apply_suggested_edits needs a handler for each.
EMITTED_CATEGORIES = frozenset({"memory", "claude_md", "skill", "role"})


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
    """Generate MEMORY.md suggestions from difficulty-controlled model performance.

    Model-vs-model comparisons run through model_inference_guard so that a "prefer
    model X for task Y" suggestion is only emitted when the two models share a
    task-difficulty bucket with a sufficient sample AND an infra-excluded reasoning
    signal shows a meaningful gap. Confounded comparisons (the orchestrator model gets
    the hard tasks; error-recovery is resilience, not failure) are dropped.

    The bare per-model token-profile dump is intentionally NOT emitted: it is a
    non-actionable raw internal, not a decision the operator can accept or reject.
    """
    cur = conn.cursor()

    cur.execute("""
        SELECT
            session_model,
            primary_activity,
            total_output_tokens,
            duration_minutes,
            has_error_recovery
        FROM session_analytics
        WHERE session_date >= ?
          AND session_model != 'unknown'
          AND primary_activity IS NOT NULL
    """, (since,))

    from collections import defaultdict as _dd
    activity_sessions = _dd(lambda: _dd(list))
    for row in cur.fetchall():
        model, activity, tokens, duration, err = row
        activity_sessions[activity][model].append({
            "total_output_tokens": tokens or 0,
            "duration_minutes": duration,
            "has_error_recovery": bool(err),
            "reasoning_error": None,  # no infra-excluded signal in session_analytics
        })

    suggestions = []
    for activity, sessions_by_model in activity_sessions.items():
        result = evaluate_activity_routing(activity, sessions_by_model)
        if result.get("status") != HINT:
            continue
        best = result["recommended_model"]
        worst = result["avoid_model"]
        suggestions.append({
            "category": "memory",
            "target": "MEMORY.md",
            "section": "## Geleerde Patronen",
            "action": "append",
            "content": (
                f"- Prefer {best} boven {worst} voor {activity} taken "
                f"(vergelijkbare moeilijkheid, {result['bucket']} bucket)."
            ),
            "evidence": result["evidence"],
            "confidence": result["confidence"],
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


def _row_evidence(source_dispatch_ids: object) -> str:
    """Distinct dispatches behind a row, or "aantal onbekend". Never occurrence_count."""
    try:
        ids = json.loads(source_dispatch_ids) if isinstance(source_dispatch_ids, str) else None
    except json.JSONDecodeError:
        ids = None
    if not isinstance(ids, list):
        return "aantal onbekend"
    distinct = {i for i in ids if isinstance(i, str) and i}
    if not distinct:
        return "aantal onbekend"
    return f"{len(distinct)} dispatches"


_SEVERITY_CONFIDENCE = {"critical": 0.9, "high": 0.8, "medium": 0.7}


def _count_refusal(refusals: dict | None, reason: str) -> None:
    if refusals is not None:
        refusals[reason] = refusals.get(reason, 0) + 1


def generate_prevention_rule_suggestions(
    conn: sqlite3.Connection,
    project_id: str | None = None,
    root: Path | None = None,
    max_age_days: int = DEFAULT_ANTIPATTERN_MAX_AGE_DAYS,
    refusals: dict | None = None,
) -> List[dict]:
    """``role`` suggestions from antipatterns of ``project_id`` seen within ``max_age_days``.

    The row's ``category`` names the role; no role file means a refusal counted
    in ``refusals``. Evidence is the distinct dispatches behind the row, never
    ``occurrence_count``. A missing table/column raises ``sqlite3.OperationalError``.
    """
    from project_scope import current_project_id
    from suggested_edit_targets import guard_target, role_target, REASON_NOT_ON_LIST

    pid = project_id if project_id is not None else current_project_id()
    base = Path(root) if root is not None else PROJECT_ROOT
    cutoff = (datetime.now(tz=_UTC) - timedelta(days=max_age_days)).strftime("%Y-%m-%d")

    rows = conn.execute(
        "SELECT title, description, severity, category, source_dispatch_ids "
        "FROM antipatterns "
        "WHERE project_id = ? AND last_seen IS NOT NULL AND substr(last_seen, 1, 10) >= ? "
        "ORDER BY last_seen DESC LIMIT 20",
        (pid, cutoff),
    ).fetchall()

    suggestions = []
    for row in rows:
        title, description, severity, category, src_ids = tuple(row)
        target = role_target(category)
        if target is None:
            _count_refusal(refusals, REASON_NOT_ON_LIST)
            continue
        resolved, reason = guard_target(target, base)
        if resolved is None:
            _count_refusal(refusals, reason)
            continue
        text = (description or "").strip() or (title or "").strip()
        if not text:
            continue
        suggestions.append({
            "category": "role",
            "target": target,
            "section": ROLE_SECTION,
            "action": "append",
            "proposed_change": f"Add rule: {text}",
            "content": f"- {text}",
            "evidence": _row_evidence(src_ids),
            "confidence": _SEVERITY_CONFIDENCE.get((severity or "").lower(), 0.0),
        })

    return suggestions


def generate_digest_section(edits: list) -> str:
    """Generate the digest markdown section for pending edits."""
    if not edits:
        return ""

    pending = [e for e in edits if e.get("status") == "pending"]
    if not pending:
        return ""

    # Decision-grade: collapse near-duplicate suggestions and drop non-actionable raw
    # internal dumps so the digest surfaces decisions, not raw rows.
    pending = decision_grade(pending)
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
        "role": "ROLE",
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
        content_preview = smart_truncate(content.split("\n")[0], 80)

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
    refusals: dict = {}
    try:
        all_suggestions.extend(generate_prevention_rule_suggestions(conn, refusals=refusals))
    except sqlite3.OperationalError as exc:
        print(f"prevention rules not generated: {exc}", file=sys.stderr)
    all_suggestions.extend(generate_onetime_suggestions())
    conn.close()

    # Filter out duplicates and already-applied
    new_edits = []
    next_id = max((e.get("id", 0) for e in existing_edits), default=0) + 1

    for sg in all_suggestions:
        # Closed reading list: a refused proposal is counted, never written.
        _, refusal = guard_target(sg.get("target"), PROJECT_ROOT)
        if refusal:
            _count_refusal(refusals, refusal)
            continue
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

    # Merge: keep existing non-applied pending edits + add new ones, then collapse
    # near-duplicate suggestions (same target+intent) so the persisted queue and every
    # downstream digest carry one row per decision, not three at different sample sizes.
    kept_edits = [e for e in existing_edits if e.get("status") in ("pending", "accepted")]
    final_edits = dedup_suggestions(kept_edits + new_edits)

    output = {
        "generated_at": datetime.now(tz=_UTC).isoformat().replace("+00:00", "Z"),
        "edits": final_edits,
        "refusals": refusals,
    }

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    # Generate digest section
    digest_section = generate_digest_section(final_edits)

    print(f"Suggested edits written to: {PENDING_PATH}")
    print(f"  New: {len(new_edits)} | Carried over: {len(kept_edits)} | Total pending: {len(final_edits)}")

    for reason, count in sorted(refusals.items()):
        print(f"  Refused ({reason}): {count}")

    if digest_section:
        print(f"\n{digest_section}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
