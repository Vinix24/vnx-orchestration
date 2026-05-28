"""intelligence_injection.py — Shared smart-context builder for all provider paths.

Extracts the Claude-path intelligence injection logic from
subprocess_dispatch_internals/skill_injection.py and makes it provider-agnostic.

Used by provider_dispatch.py for codex/gemini/litellm dispatches.
subprocess_dispatch.py delegates here via skill_injection._build_intelligence_section.

INT-2 (Wave-5 ADR injection): fetch_adr_context_section() queries the adrs table
and injects binding ADR context before the instruction.  Runs on all provider paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wave-5 ADR injection — constants and helpers (INT-2)
# ---------------------------------------------------------------------------

ADR_TRIGGER_ROLES = frozenset({
    "database-engineer",
    "architect",
    "intelligence-engineer",
    "security-engineer",
})

# Path prefixes that trigger ADR injection regardless of role
ADR_TRIGGER_PATH_PREFIXES = (
    "schemas/migrations/",
    "schemas/",
    "scripts/lib/coordination_db.py",
    "scripts/lib/quality_db.py",
)

ADR_MAX_RESULTS = 3
ADR_SUMMARY_MAX_CHARS = 200

# Env var checked by fetch_adr_context_section (set by --no-adr-inject in subprocess_dispatch)
_ENV_NO_ADR_INJECT = "VNX_NO_ADR_INJECT"


def _adr_injection_triggered(
    role: Optional[str],
    dispatch_paths: Optional[List[str]],
) -> bool:
    """Return True when the dispatch meets ADR injection trigger criteria."""
    if role in ADR_TRIGGER_ROLES:
        return True
    if not dispatch_paths:
        return False
    for path in dispatch_paths:
        for prefix in ADR_TRIGGER_PATH_PREFIXES:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return True
    return False


def _build_fts_terms(dispatch_paths: Optional[List[str]]) -> str:
    """Extract meaningful terms from dispatch paths for FTS5 MATCH query."""
    if not dispatch_paths:
        return ""
    terms: set = set()
    for path in dispatch_paths:
        parts = re.split(r"[/._\-]", path)
        for part in parts:
            if len(part) > 3 and part.isalpha():
                terms.add(part)
    if not terms:
        return ""
    return " OR ".join(sorted(terms))


def _query_adrs_from_db(
    db_path: Path,
    role: Optional[str],
    dispatch_paths: Optional[List[str]],
    project_id: str,
) -> List[dict]:
    """Query adrs table for relevant accepted ADRs.  Returns list of row dicts."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        params: list = [project_id]
        where_clauses = ["a.status = 'Accepted'", "a.project_id = ?"]
        sub_conditions: list = []

        if role:
            sub_conditions.append("a.applies_to_skills LIKE '%' || ? || '%'")
            params.append(role)

        fts_terms = _build_fts_terms(dispatch_paths)
        if fts_terms:
            sub_conditions.append(
                "a.rowid IN (SELECT rowid FROM adrs_fts WHERE adrs_fts MATCH ?)"
            )
            params.append(fts_terms)

        if sub_conditions:
            where_clauses.append(f"({' OR '.join(sub_conditions)})")

        sql = (
            "SELECT a.adr_id, a.title, a.decision_summary, a.binding_rules"
            " FROM adrs a"
            f" WHERE {' AND '.join(where_clauses)}"
            " ORDER BY a.adr_id"
            f" LIMIT {ADR_MAX_RESULTS}"
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        # adrs or adrs_fts table absent — old schema; retry without FTS subquery
        if "adrs_fts" in str(exc) and fts_terms:  # type: ignore[possibly-undefined]
            try:
                no_fts_sub = [c for c in sub_conditions if "adrs_fts" not in c]  # type: ignore[possibly-undefined]
                no_fts_params = [p for p in params if p != fts_terms]
                where_no_fts = ["a.status = 'Accepted'", "a.project_id = ?"]
                if no_fts_sub:
                    where_no_fts.append(f"({' OR '.join(no_fts_sub)})")
                sql_no_fts = (
                    "SELECT a.adr_id, a.title, a.decision_summary, a.binding_rules"
                    " FROM adrs a"
                    f" WHERE {' AND '.join(where_no_fts)}"
                    " ORDER BY a.adr_id"
                    f" LIMIT {ADR_MAX_RESULTS}"
                )
                rows = conn.execute(sql_no_fts, no_fts_params).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as e:
                logger.warning('ADR DB query failed (table missing or corrupt): %s', e)
                return []  # ADR injection is best-effort; missing/corrupt DB shouldn't block dispatch
        logger.debug("_query_adrs_from_db: OperationalError: %s", exc)
        return []
    finally:
        conn.close()


def _format_adr_context_block(adrs: List[dict]) -> str:
    """Format ADR rows as the Wave-5 binding context block."""
    lines = [
        "## ADR Context (auto-injected per Wave-5)",
        "",
        "The following Architectural Decision Records apply to your dispatch scope:",
        "",
    ]
    for row in adrs:
        adr_id = row.get("adr_id", "")
        title = row.get("title", "")
        summary = (row.get("decision_summary") or "")[:ADR_SUMMARY_MAX_CHARS]
        lines.append(f"### {adr_id} — {title}")
        lines.append(f"**Decision summary:** {summary}")
        try:
            rules: list = json.loads(row.get("binding_rules") or "[]")
        except (json.JSONDecodeError, TypeError):
            rules = []
        if rules:
            lines.append("**Binding rules:**")
            for rule in rules:
                lines.append(f"- {rule}")
        lines.append("")
    lines.append("These are BINDING — your implementation MUST comply.")
    return "\n".join(lines)


def _emit_adr_injection_event(
    dispatch_id: str,
    adr_ids: List[str],
    state_dir: Path,
) -> None:
    """Write ADR injection audit event to NDJSON ledger.

    Per ADR-005: ledger-first, raises OSError on write failure.
    """
    import datetime  # noqa: PLC0415

    event = {
        "event": "adr_context_injected",
        "dispatch_id": dispatch_id,
        "adr_ids": adr_ids,
        "injected_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "source": "intelligence_injection.fetch_adr_context_section",
    }
    register_path = state_dir / "dispatch_register.ndjson"
    register_path.parent.mkdir(parents=True, exist_ok=True)
    with open(register_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def fetch_adr_context_section(
    dispatch_id: str,
    role: Optional[str],
    dispatch_paths: Optional[List[str]],
    state_dir: Path,
    *,
    project_id: Optional[str] = None,
    no_inject: bool = False,
) -> str:
    """Fetch ADR context block for Wave-5 dispatch injection.

    Returns empty string when no ADRs match or injection is disabled.

    ADR-007: queries are always filtered by project_id.
    ADR-005: writes NDJSON audit event on injection; raises OSError on event-write failure.
    DO NOT wrap this function in a bare except — OSError from event-write must propagate.
    """
    if no_inject or os.environ.get(_ENV_NO_ADR_INJECT, "0") == "1":
        return ""
    if not _adr_injection_triggered(role, dispatch_paths):
        return ""

    _pid: str
    if project_id:
        _pid = project_id
    else:
        try:
            from project_scope import current_project_id  # noqa: PLC0415
            _pid = current_project_id()
        except Exception:
            _pid = "vnx-dev"

    quality_db_path = state_dir / "quality_intelligence.db"
    if not quality_db_path.exists():
        return ""

    try:
        adrs = _query_adrs_from_db(quality_db_path, role, dispatch_paths, _pid)
    except Exception as exc:
        logger.warning("ADR context query failed (%s); proceeding without", exc)
        return ""

    if not adrs:
        return ""

    adr_ids = [r["adr_id"] for r in adrs]
    # Per ADR-005: write NDJSON event FIRST, raise on failure (OSError propagates to caller)
    _emit_adr_injection_event(dispatch_id, adr_ids, state_dir)

    return _format_adr_context_block(adrs)


# ---------------------------------------------------------------------------
# Main injection API
# ---------------------------------------------------------------------------


def build_intelligence_section(
    instruction: str,
    dispatch_id: str,
    role: Optional[str],
    state_dir: Path,
    *,
    pr_id: Optional[str] = None,
    dispatch_paths: Optional[List[str]] = None,
) -> str:
    """Build enriched instruction with smart-context prefix.

    Calls IntelligenceSelector against the quality intelligence database.
    Records the injection event so the confidence feedback loop has a
    dispatch-scoped row to update on receipt arrival.

    Returns the original instruction unchanged when the database is absent,
    the selector returns no items, or any exception occurs (best-effort).
    OSError from ADR event-write is re-raised per ADR-005.
    """
    section = fetch_intelligence_section(
        dispatch_id=dispatch_id,
        role=role,
        state_dir=state_dir,
        pr_id=pr_id,
        dispatch_paths=dispatch_paths,
        instruction_text=instruction,
    )
    if not section:
        return instruction
    return (
        f"## Relevant Intelligence (from past dispatches)\n\n"
        f"{section}\n"
        f"---\n\n"
        f"{instruction}"
    )


def fetch_intelligence_section(
    dispatch_id: str,
    role: Optional[str],
    state_dir: Path,
    *,
    pr_id: Optional[str] = None,
    dispatch_paths: Optional[List[str]] = None,
    instruction_text: Optional[str] = None,
) -> str:
    """Fetch formatted intelligence markdown, or empty string (best-effort).

    Calls IntelligenceSelector.select(), emits the coordination event, and
    records the injection row (intelligence_injections + pattern_usage +
    dispatch_pattern_offered) so the post-dispatch confidence feedback loop
    has dispatch-scoped rows to update.  Stamps source_dispatch_ids so
    intelligence_persist.update_confidence_from_outcome can match injected
    patterns back to their source dispatch on receipt arrival.

    INT-2: Also prepends ADR context block from the adrs table when triggers match.
    OSError from ADR event-write propagates (ADR-005: raise on event-write failure).
    Other exceptions are caught and logged; callers receive empty string.
    """
    # ADR context injection (INT-2) — runs before existing intelligence
    # OSError propagates; other exceptions are handled below
    adr_section: str = ""
    try:
        adr_section = fetch_adr_context_section(
            dispatch_id=dispatch_id,
            role=role,
            dispatch_paths=dispatch_paths,
            state_dir=state_dir,
        )
    except OSError:
        raise  # per ADR-005: event-write failure must propagate
    except Exception as exc:
        logger.warning("ADR context fetch failed (%s); proceeding without", exc)

    try:
        from intelligence_selector import IntelligenceSelector  # noqa: PLC0415
        quality_db_path = state_dir / "quality_intelligence.db"
        selector = IntelligenceSelector(
            quality_db_path=quality_db_path,
            coord_db_state_dir=state_dir,
        )
        try:
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point="dispatch_create",
                skill_name=role or "",
                dispatch_paths=dispatch_paths or [],
                instruction_text=instruction_text or "",
                pr_id=pr_id,
            )
            try:
                selector.emit_event(result, coord_state_dir=state_dir)
            except Exception as exc:
                logger.debug("emit_event failed for %s: %s", dispatch_id, exc)
            try:
                selector.record_injection(result, coord_state_dir=state_dir)
            except Exception as exc:
                logger.debug("record_injection failed for %s: %s", dispatch_id, exc)
            try:
                selector.stamp_source_dispatch_ids(result)
            except Exception as exc:
                logger.debug(
                    "stamp_source_dispatch_ids failed for %s: %s", dispatch_id, exc
                )
        finally:
            selector.close()
        intel_section = format_intelligence_items(result.items) if result.items else ""
    except Exception as exc:
        logger.warning("intelligence injection failed (%s); proceeding without", exc)
        intel_section = ""

    # Combine: ADR context first (most binding), then intelligence items
    if adr_section and intel_section:
        return f"{adr_section}\n\n---\n\n{intel_section}"
    return adr_section or intel_section


def format_intelligence_items(items: list) -> str:
    """Group intelligence items by class and render as markdown sections."""
    by_class: dict = {}
    for item in items:
        by_class.setdefault(item.item_class, []).append(item)
    parts: list = []
    if "failure_prevention" in by_class:
        parts.append("### Antipatterns to avoid")
        for item in by_class["failure_prevention"]:
            parts.append(f"- **{item.title}**: {item.content}")
        parts.append("")
    if "proven_pattern" in by_class:
        parts.append("### Proven success patterns")
        for item in by_class["proven_pattern"]:
            parts.append(f"- **{item.title}**: {item.content}")
        parts.append("")
    if "recent_comparable" in by_class:
        parts.append("### Tag warnings")
        for item in by_class["recent_comparable"]:
            parts.append(f"- **{item.title}**: {item.content}")
        parts.append("")
    return "\n".join(parts)
