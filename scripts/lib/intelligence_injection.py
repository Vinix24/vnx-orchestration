"""intelligence_injection.py — Shared smart-context builder for all provider paths.

Extracts the Claude-path intelligence injection logic from
subprocess_dispatch_internals/skill_injection.py and makes it provider-agnostic.

Used by provider_dispatch.py for codex/gemini/litellm dispatches.
subprocess_dispatch.py delegates here via skill_injection._build_intelligence_section.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


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

    Any import or DB failure is caught and logged; callers receive an empty
    string and proceed without intelligence.
    """
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
        if not result.items:
            return ""
        return format_intelligence_items(result.items)
    except Exception as exc:
        logger.warning("intelligence injection failed (%s); proceeding without", exc)
        return ""


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
