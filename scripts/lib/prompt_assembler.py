#!/usr/bin/env python3
"""prompt_assembler.py — Layered user message assembler for headless workers.

Composes a structured user message for `claude -p` from three independent layers:

  Layer 1 — Base worker context (universal rules, report format, billing safety)
  Layer 2 — Role context      (capabilities, permissions, workflow for the role)
  Layer 3 — Dispatch payload  (instruction + enrichments: repo map, intelligence,
                                historical context, dispatch metadata)

NOTE: Neither "Layer 1+2" nor "Layer 3" is a Claude system prompt. Claude Code's
system prompt is Anthropic's hidden, fixed layer — identical for all dispatches
and not controlled by VNX. Everything this assembler produces is the USER MESSAGE
passed to `claude -p "..."`.

BILLING SAFETY: No Anthropic SDK imports. No api.anthropic.com calls. CLI-only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Absolute path to the prompts directory so this module works from any cwd
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Regex to strip [[TARGET:TX]] dispatch header line
_TARGET_HEADER_RE = re.compile(r"^\[\[TARGET:T\d+\]\]\s*\n?", re.MULTILINE)


@dataclass
class AssembledPrompt:
    """Composed user message for headless dispatch.

    NOTE: 'context' and 'instruction' are both parts of the USER MESSAGE
    sent to `claude -p`. Neither is a system prompt — Claude Code's system
    prompt is Anthropic's hidden layer that we don't control.

    Attributes:
        context:     Layer 1 + Layer 2 (base rules + role context).
        instruction: Layer 3 (dispatch payload + enrichments).
        metadata:    Assembly metadata: role, terminal, model, enrichments applied.
    """
    context: str
    instruction: str
    metadata: dict = field(default_factory=dict)

    def to_pipe_input(self) -> str:
        """Compose the full user message string for `claude -p`.

        Produces: <context>\\n\\n---\\n\\nDISPATCH INSTRUCTION:\\n\\n<instruction>
        This is the identical format previously produced by _inject_skill_context().
        """
        return f"{self.context}\n\n---\n\nDISPATCH INSTRUCTION:\n\n{self.instruction}"


class PromptAssembler:
    """Compose layered user messages for headless workers.

    This assembler does NOT create system prompts. Claude Code's system prompt
    is fixed by Anthropic. We compose the user message that gets passed to
    `claude -p` as the sole input.

    Usage::

        assembler = PromptAssembler()
        prompt = assembler.assemble(
            dispatch_metadata={"role": "backend-developer", "terminal": "T1", ...},
            instruction="<raw dispatch instruction text>",
        )
        pipe_input = prompt.to_pipe_input()

    Optional enrichment keys in dispatch_metadata:
        repo_map        (str)  — pre-formatted repo map section
        intelligence    (str)  — intelligence context block
        historical      (str)  — similar dispatch outcomes block
        dispatch_id     (str)  — for receipt footer
        gate            (str)  — gate tag (e.g. "f58-pr3")
        pr              (str)  — PR label (e.g. "F58-PR3")
        track           (str)  — track letter (e.g. "A")
        model           (str)  — model name
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(self, dispatch_metadata: dict, instruction: str) -> AssembledPrompt:
        """Assemble 3-layer user message from dispatch metadata and instruction.

        Args:
            dispatch_metadata: Dict with at least ``role`` and optionally
                               ``terminal``, ``model``, ``repo_map``,
                               ``intelligence``, ``historical``, ``dispatch_id``,
                               ``gate``, ``pr``, ``track``.
            instruction:       Raw dispatch instruction text (may contain
                               ``[[TARGET:TX]]`` header — stripped automatically).

        Returns:
            AssembledPrompt with context (L1+L2), instruction (L3), and metadata.
        """
        role = (dispatch_metadata.get("role") or "").strip()
        terminal = dispatch_metadata.get("terminal", "")
        model = dispatch_metadata.get("model", "")

        # ---- Layer 1: Base worker context --------------------------------
        layer1 = self._load_base()
        logger.debug("L1 loaded: %d chars", len(layer1))

        # ---- Layer 2: Role context ----------------------------------------
        layer2, role_resolved = self._load_role(role)
        logger.debug("L2 loaded: role=%s resolved=%s chars=%d", role, role_resolved, len(layer2))

        context = f"{layer1}\n\n---\n\n{layer2}"

        # ---- Layer 3: Dispatch payload ------------------------------------
        cleaned_instruction = _TARGET_HEADER_RE.sub("", instruction).strip()
        layer3 = self._build_layer3(cleaned_instruction, dispatch_metadata)
        logger.info(
            "Prompt assembled: role=%s terminal=%s L1=%d L2=%d L3=%d chars",
            role_resolved, terminal, len(layer1), len(layer2), len(layer3),
        )

        enrichments_applied: list[str] = []
        if dispatch_metadata.get("repo_map"):
            enrichments_applied.append("repo_map")
        if dispatch_metadata.get("intelligence"):
            enrichments_applied.append("intelligence")
        if dispatch_metadata.get("historical"):
            enrichments_applied.append("historical")

        metadata = {
            "role": role_resolved,
            "terminal": terminal,
            "model": model,
            "enrichments_applied": enrichments_applied,
            "layer1_chars": len(layer1),
            "layer2_chars": len(layer2),
            "layer3_chars": len(layer3),
        }

        return AssembledPrompt(context=context, instruction=layer3, metadata=metadata)

    # ------------------------------------------------------------------
    # Layer loaders
    # ------------------------------------------------------------------

    def _load_base(self) -> str:
        """Load Layer 1 from scripts/lib/prompts/base_worker.md."""
        path = _PROMPTS_DIR / "base_worker.md"
        if path.exists():
            return path.read_text().strip()
        logger.warning("base_worker.md not found at %s; using empty L1", path)
        return ""

    def _load_role(self, role: str) -> tuple[str, str]:
        """Load Layer 2 from scripts/lib/prompts/roles/<role>.md.

        Returns (content, resolved_role_name). Falls back to base_worker.md
        content when the role file is missing, logging a warning.
        """
        if role:
            role_path = _PROMPTS_DIR / "roles" / f"{role}.md"
            if role_path.exists():
                return role_path.read_text().strip(), role
            logger.warning(
                "Role prompt not found for '%s' at %s; falling back to base worker context",
                role, role_path,
            )

        # Graceful degradation: return base content as role layer
        base_content = self._load_base()
        return base_content, role or "unknown"

    def _build_layer3(self, instruction: str, metadata: dict) -> str:
        """Build Layer 3: dispatch payload + enrichments.

        Appends optional sections in order:
          ### Repo Map          — from metadata["repo_map"]
          ### Intelligence Context — from metadata["intelligence"]
          ### Historical Context   — from metadata["historical"]
          ### Dispatch Metadata    — ID, gate, PR, track, model
        """
        parts: list[str] = [instruction]

        repo_map = metadata.get("repo_map", "")
        if repo_map:
            parts.append(f"### Repo Map\n\n{repo_map.strip()}")

        intelligence = metadata.get("intelligence", "")
        if intelligence:
            parts.append(f"### Intelligence Context\n\n{intelligence.strip()}")

        historical = metadata.get("historical", "")
        if historical:
            parts.append(f"### Historical Context\n\n{historical.strip()}")

        # Dispatch metadata footer — always appended when dispatch_id is present
        dispatch_id = metadata.get("dispatch_id", "")
        if dispatch_id:
            lines = [
                "### Dispatch Metadata",
                "",
                f"- Dispatch-ID: {dispatch_id}",
            ]
            for key in ("gate", "pr", "track", "model", "terminal"):
                val = metadata.get(key, "")
                if val:
                    lines.append(f"- {key.capitalize()}: {val}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Per-provider formatting
# ---------------------------------------------------------------------------

def format_for_provider(assembled: "AssembledPrompt", provider: str) -> dict:
    """Format an AssembledPrompt for a specific provider's invocation.

    Returns a dict with provider-specific fields:

    claude:
        {"pipe_input": "<full user message string>"}
        Everything is a single user message; Claude's system prompt is fixed.

    gemini:
        {"system_instruction": "<L1+L2>", "prompt": "<L3>"}
        Layer 1+2 CAN go into --system-instruction if the CLI supports it.
        Layer 3 is sent as stdin/prompt.

    codex:
        {"pipe_input": "<full concatenated string>"}
        Codex has no separate system instruction; everything is concatenated.

    ollama:
        {"system": "<L1+L2>", "prompt": "<L3>"}
        Ollama HTTP API has a native "system" field separate from "prompt".

    Args:
        assembled: AssembledPrompt from PromptAssembler.assemble().
        provider:  One of "claude", "gemini", "codex", "ollama".

    Returns:
        Dict with provider-specific payload keys.

    Raises:
        ValueError: When provider is not one of the supported values.
    """
    provider = provider.lower().strip()

    if provider == "claude":
        return {"pipe_input": assembled.to_pipe_input()}

    if provider == "gemini":
        return {
            "system_instruction": assembled.context,
            "prompt": assembled.instruction,
        }

    if provider == "codex":
        return {"pipe_input": assembled.to_pipe_input()}

    if provider == "ollama":
        return {
            "system": assembled.context,
            "prompt": assembled.instruction,
        }

    raise ValueError(
        f"Unknown provider '{provider}'. "
        "Supported: claude, gemini, codex, ollama"
    )
