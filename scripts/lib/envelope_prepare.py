"""envelope_prepare.py — PREPARE-seam functions for the dispatch_envelope
module family.

Leaf module: no imports from sibling envelope_* modules or from
dispatch_envelope itself (the facade imports FROM here, never the reverse).
Moved unchanged from dispatch_envelope.py as PR-2 of the dispatch-monolith-split
(dispatch-monolith-split, PR-2 of 6) — see dispatch_envelope.py's module
docstring for the split's seam order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from envelope_types import EnvelopeSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PREPARE
# ---------------------------------------------------------------------------


def _prepare(spec: EnvelopeSpec) -> str:
    """Enrich instruction with role context + intelligence and repo map (best-effort).

    Mirrors _enrich_instruction in provider_dispatch.py but operates on EnvelopeSpec.
    Layers fall back silently to the original instruction on any failure:

    1. Repo-map layer on the RAW instruction (target-file extraction most
       accurate before any enrichment text is added).
    2. Role context + intelligence + full assembly via ``_inject_skill_context``
       — the shared lane-neutral injector used by every dispatch lane
       (dispatch-20260801-w10). This closes the envelope-lane gap where the
       role's CLAUDE.md never reached the worker. On injector failure the
       prompt falls back to a role label header.
    """
    instruction = spec.instruction

    try:
        from dispatch_enricher import apply_repo_map_layer  # noqa: PLC0415

        instruction = apply_repo_map_layer(instruction, {"role": spec.role})
    except Exception as exc:
        logger.warning(
            "envelope._prepare: repo map layer failed (%s) — skipping", exc
        )

    try:
        from skill_context import _inject_skill_context  # noqa: PLC0415

        dispatch_metadata: dict = {"dispatch_id": spec.dispatch_id}
        if spec.pr_id:
            dispatch_metadata["pr_id"] = spec.pr_id
        instruction = _inject_skill_context(
            spec.terminal_id,
            instruction,
            spec.role,
            dispatch_metadata,
        )
    except Exception as exc:  # noqa: BLE001 — fallback must never break a dispatch
        logger.warning(
            "envelope._prepare: skill injection failed (%s); falling back to role label",
            exc,
        )
        if spec.role:
            header = f"## Role\n\nYou are operating as a **{spec.role}** worker."
        else:
            header = (
                "## Worker Preamble\n\n"
                "You are a VNX headless worker executing a dispatch instruction."
            )
        instruction = f"{header}\n\n{instruction}"

    return instruction


# ---------------------------------------------------------------------------
# Final-prompt integrity (input-side audit closure)
# ---------------------------------------------------------------------------


def _record_integrity(
    spec: EnvelopeSpec,
    enriched_instruction: str,
    raw_instruction: str,
    *,
    bundle_dir: Optional[Path] = None,
):
    """Persist the enriched final prompt + verify raw+injections reconstruct it.

    Returns a FinalPromptIntegrity (stamped onto the receipt by _govern) or None
    when the integrity module is unavailable / persistence failed. The strict
    (fail-closed) reconstruction raise propagates; every other error is swallowed so
    the audit-closure step can never itself break a dispatch.
    """
    try:
        from final_prompt_integrity import record_final_prompt_integrity  # noqa: PLC0415
        from final_prompt_integrity import InjectionReconstructError  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return record_final_prompt_integrity(
            dispatch_id=spec.dispatch_id,
            final_prompt=enriched_instruction,
            raw_instruction=raw_instruction,
            data_dir=spec.data_dir,
            state_dir=spec.state_dir,
            bundle_dir=bundle_dir,
        )
    except InjectionReconstructError:
        raise
    except Exception as exc:  # noqa: BLE001 — audit closure must never break a dispatch
        logger.error(
            "envelope: final-prompt integrity failed (non-fatal) dispatch=%s: %s",
            spec.dispatch_id,
            exc,
        )
        return None


def _verify_role_application(
    final_prompt: str,
    terminal_id: str,
    role: Optional[str],
):
    """Best-effort: run the deterministic role-applied control for *final_prompt*.

    Returns a RoleApplicationVerdict (or None on any failure — the control must
    never break receipt emission; the fields then stay None/omitted).
    """
    try:
        from role_application import verify_role_applied  # noqa: PLC0415
        return verify_role_applied(final_prompt, terminal_id, role)
    except Exception as exc:  # noqa: BLE001 — verification must never break a dispatch
        logger.debug(
            "envelope._verify_role_application: failed (non-fatal) role=%s terminal=%s: %s",
            role,
            terminal_id,
            exc,
        )
        return None
