"""skill_injection — layered prompt assembly + permission profile preamble."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _inject_permission_profile(terminal_id: str, role: str | None, instruction: str) -> str:
    """Prepend permission preamble to instruction if a profile exists for role.

    Resolves the terminal's expected role from terminal_assignments when role
    is None.  Logs a warning on role/terminal mismatch.  Returns instruction
    unchanged when no profile is found or worker_permissions cannot be loaded.
    """
    try:
        from worker_permissions import (
            load_permissions,
            generate_permission_preamble,
            validate_dispatch_permissions,
        )
    except ImportError:
        logger.debug("_inject_permission_profile: worker_permissions not available, skipping")
        return instruction

    effective_role = _resolve_effective_role(terminal_id, role)
    if not effective_role:
        return instruction

    warnings = validate_dispatch_permissions(
        {"terminal": terminal_id, "role": effective_role}
    )
    for w in warnings:
        logger.warning(w)

    profile = load_permissions(effective_role)
    if not profile.allowed_tools and not profile.denied_tools and not profile.bash_deny_patterns:
        logger.debug("_inject_permission_profile: empty profile for role '%s', skipping preamble", effective_role)
        return instruction

    preamble = generate_permission_preamble(profile)
    logger.info(
        "Permission profile applied: terminal=%s role=%s allowed=%s denied=%s",
        terminal_id, effective_role,
        profile.allowed_tools,
        profile.denied_tools,
    )
    return f"{preamble}\n---\n\n{instruction}"


def _resolve_effective_role(terminal_id: str, role: str | None) -> str | None:
    """Resolve effective role from terminal_assignments when role is None."""
    if role:
        return role
    try:
        import yaml
        yaml_path = Path(__file__).resolve().parents[3] / ".vnx" / "worker_permissions.yaml"
        data = yaml.safe_load(yaml_path.read_text()) or {}
        return data.get("terminal_assignments", {}).get(terminal_id)
    except Exception as exc:
        logger.debug("_inject_permission_profile: could not resolve role for %s: %s", terminal_id, exc)
        return None


def _build_intelligence_section(dispatch_id: str, role: str | None) -> str:
    """Return formatted intelligence items as markdown, or empty string (best-effort).

    Calls IntelligenceSelector to gather antipatterns, success patterns, and
    recent comparable dispatches from quality_intelligence.db.  After selecting,
    emits the coordination event and records the injection (intelligence_injections
    + pattern_usage + dispatch_pattern_offered) so the post-dispatch confidence
    feedback loop has dispatch-scoped rows to update.  Any import or DB failure
    is caught and logged — dispatch proceeds without intelligence.
    """
    try:
        from intelligence_selector import IntelligenceSelector  # noqa: PLC0415
        # Look up _default_state_dir via subprocess_dispatch namespace so test
        # monkeypatches at the facade ("subprocess_dispatch._default_state_dir")
        # are honoured when this helper is called directly by tests.
        import subprocess_dispatch as _sd
        state_dir = _sd._default_state_dir()
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
            )
            try:
                selector.emit_event(result, coord_state_dir=state_dir)
            except Exception as exc:
                logger.debug("emit_event failed for %s: %s", dispatch_id, exc)
            try:
                selector.record_injection(result, coord_state_dir=state_dir)
            except Exception as exc:
                logger.debug("record_injection failed for %s: %s", dispatch_id, exc)
        finally:
            selector.close()
        if not result.items:
            return ""
        return _format_intelligence_items(result.items)
    except Exception as exc:
        logger.warning("intelligence injection failed (%s); proceeding without", exc)
        return ""


def _format_intelligence_items(items: list) -> str:
    """Group items by class and render as markdown sections."""
    by_class: dict[str, list] = {}
    for item in items:
        by_class.setdefault(item.item_class, []).append(item)
    parts: list[str] = []
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


def _inject_skill_context(
    terminal_id: str,
    instruction: str,
    role: str | None = None,
    dispatch_metadata: "dict | None" = None,
) -> str:
    """Compose layered user message context for headless dispatch.

    Uses PromptAssembler (3-layer architecture) when available, with fallback
    to the legacy 3-tier CLAUDE.md resolution for backward compatibility.

    Layer architecture (PromptAssembler path):
      Layer 1 — Base worker context (universal rules, report format)
      Layer 2 — Role context (capabilities, permissions for the role)
      Layer 3 — Dispatch payload (passed through as instruction)

    Legacy fallback (3-tier CLAUDE.md resolution):
      1. agents/{role}/CLAUDE.md        — project-level agent override
      2. .claude/skills/{role}/CLAUDE.md — skill definition
      3. .claude/terminals/{terminal}/CLAUDE.md — terminal fallback

    Args:
        terminal_id:       Terminal identifier (e.g. "T1").
        instruction:       Raw dispatch instruction text.
        role:              Agent role (e.g. "backend-developer").
        dispatch_metadata: Optional metadata dict forwarded to PromptAssembler
                           for L3 enrichments (dispatch_id, gate, pr, track, model,
                           intelligence, historical).  Merged with terminal+role.

    Returns the full pipe_input string ready for `claude -p`.
    """
    # Gather intelligence before assembling prompt (best-effort).  Looked up via
    # subprocess_dispatch namespace so test patches at the facade are honoured.
    import subprocess_dispatch as _sd
    _dispatch_id = (dispatch_metadata or {}).get("dispatch_id") or ""
    intelligence_section = _sd._build_intelligence_section(_dispatch_id, role)

    assembled = _try_prompt_assembler(
        terminal_id, instruction, role, dispatch_metadata, intelligence_section,
    )
    if assembled is not None:
        return assembled

    return _legacy_claude_md_resolution(
        terminal_id, instruction, role, intelligence_section,
    )


def _try_prompt_assembler(
    terminal_id: str,
    instruction: str,
    role: str | None,
    dispatch_metadata: "dict | None",
    intelligence_section: str,
) -> str | None:
    """Attempt PromptAssembler path; return assembled pipe_input or None on failure."""
    try:
        from prompt_assembler import PromptAssembler  # noqa: PLC0415
        assembler = PromptAssembler()
        meta = dict(dispatch_metadata or {})
        meta.setdefault("role", role or "")
        meta.setdefault("terminal", terminal_id)
        if intelligence_section:
            meta.setdefault("intelligence", intelligence_section)
        assembled = assembler.assemble(
            dispatch_metadata=meta,
            instruction=instruction,
        )
        logger.info(
            "_inject_skill_context: assembler path — role=%s L1=%d L2=%d L3=%d chars",
            assembled.metadata.get("role"),
            assembled.metadata.get("layer1_chars", 0),
            assembled.metadata.get("layer2_chars", 0),
            assembled.metadata.get("layer3_chars", 0),
        )
        return assembled.to_pipe_input()
    except Exception as exc:
        logger.warning(
            "_inject_skill_context: PromptAssembler failed (%s) — falling back to legacy CLAUDE.md resolution",
            exc,
        )
        return None


def _legacy_claude_md_resolution(
    terminal_id: str,
    instruction: str,
    role: str | None,
    intelligence_section: str,
) -> str:
    """3-tier CLAUDE.md resolution fallback."""
    project_root = Path(__file__).resolve().parents[3]

    candidates: list[Path] = []
    if role:
        candidates.append(project_root / "agents" / role / "CLAUDE.md")
        candidates.append(project_root / ".claude" / "skills" / role / "CLAUDE.md")
    candidates.append(project_root / ".claude" / "terminals" / terminal_id / "CLAUDE.md")

    for path in candidates:
        if path.exists():
            context = path.read_text()
            if intelligence_section:
                return (
                    f"{context}\n\n---\n\n"
                    f"## Relevant Intelligence (from past dispatches)\n\n"
                    f"{intelligence_section}\n"
                    f"---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"
                )
            return f"{context}\n\n---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"

    if intelligence_section:
        return (
            f"## Relevant Intelligence (from past dispatches)\n\n"
            f"{intelligence_section}\n"
            f"---\n\nDISPATCH INSTRUCTION:\n\n{instruction}"
        )
    return instruction


def _resolve_agent_cwd(role: str | None) -> Path | None:
    """Return agents/{role}/ as Path if the directory exists, else None.

    Resolves project root via the facade's ``__file__`` and ``Path`` so tests
    that patch ``subprocess_dispatch.Path`` can intercept the resolution
    (test_subprocess_dispatch_f34.TestDeliverCwdPropagation).
    """
    if not role:
        return None
    import subprocess_dispatch as _sd
    candidate = _sd.Path(_sd.__file__).resolve().parents[2] / "agents" / role
    return candidate if candidate.is_dir() else None


def _load_agent_profile(config_path: Path) -> str:
    """Load governance_profile from agent config.yaml.

    Uses a simple line-scan so no yaml dependency is required.
    Returns 'default' when the key is absent or the file cannot be read.
    """
    try:
        for line in config_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("governance_profile:"):
                return line.split(":", 1)[1].strip()
    except Exception as _exc:
        logger.warning("Failed to read agent config %s: %s", config_path, _exc)
    return "default"
