"""
scout_sketch source — cheap-model scout pre-pass grounding.

Direct-injection builder that reads a per-dispatch ``scout_context.json`` sidecar
(written by the scout pre-pass in the door) and renders it as a ranked
INCLUDE/MAYBE pointer block + plan sketch. Fail-open: no sidecar → None, and the
worker falls back to the deterministic code_anchor injection.
"""
from __future__ import annotations

from typing import Optional

from ._common import PATTERN_CATEGORY_CODE, IntelligenceItem

try:
    import scout_prepass as _scout_prepass
except ImportError:  # pragma: no cover - lib path bootstrap
    _scout_prepass = None  # type: ignore[assignment]


def build_scout_sketch_item(
    state_dir: object,
    dispatch_id: str,
    now_ts: str,
) -> Optional[IntelligenceItem]:
    """Return a scout_sketch IntelligenceItem, or None when no sidecar exists.

    Reads the scout sidecar for ``dispatch_id`` under ``state_dir`` and renders
    the bounded, pointer-only sketch. Confidence is 1.0 (model-curated recon over
    deterministic candidates); evidence_count is the number of ranked pointers.
    """
    if _scout_prepass is None or state_dir is None or not dispatch_id:
        return None
    sidecar = _scout_prepass.read_scout_sidecar(state_dir, dispatch_id)
    if not sidecar:
        return None
    content = _scout_prepass.format_scout_sketch(sidecar)
    if not content:
        return None
    last_seen = str(sidecar.get("generated_at") or now_ts)
    return IntelligenceItem(
        item_id=f"intel_scout_{dispatch_id}",
        item_class="scout_sketch",
        title="Scout pre-pass — ranked context for this dispatch",
        content=content,
        confidence=1.0,
        evidence_count=_scout_prepass.sidecar_evidence_count(sidecar),
        last_seen=last_seen,
        scope_tags=[],
        source_refs=[
            str(it.get("ref", ""))
            for it in (sidecar.get("include") or [])
            if isinstance(it, dict) and it.get("ref")
        ],
        task_class_filter=[],
        pattern_category=PATTERN_CATEGORY_CODE,
        content_hash="",
    )
