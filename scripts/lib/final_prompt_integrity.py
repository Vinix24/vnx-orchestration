"""final_prompt_integrity.py — persist the assembled FINAL dispatch prompt and
verify that the raw instruction + intelligence injections reconstruct it.

This closes the input-side of the audit chain (authored → injected → delivered →
response → report → receipt). Before this module, neither dispatch lane persisted
the exact enriched body the model actually saw:

  * tmux-interactive: ``_assemble_context`` builds the enriched body and pastes it
    into the pane — nothing persisted.
  * provider/envelope: ``_prepare`` builds the enriched instruction — the receipt
    records only the RAW ``instruction``, not the enriched body.

What is written:

  * ``dispatches/pending/<id>/final_prompt.md`` — the assembled enriched body.
  * ``final_prompt_sha256`` + ``final_prompt_path`` added to ``dispatch-spec.json``
    (alongside the existing ``instruction_sha256``). ``load_spec`` reads known keys
    by name, so the extra keys are inert to the door.
  * ``final_prompt_path`` / ``final_prompt_sha256`` / ``injection_reconstructs`` on
    the dispatch receipt.

The integrity check is CONTAINMENT and tolerant by construction: the raw instruction
bytes must appear in the final body, and every item recorded in
``intelligence_injections.items_json`` must appear in it too (whitespace-normalized
substring). Deterministic lane wrappers (permission preamble, report-contract
directive, completion protocol, scope guard, trailer sentinel) are ADDITIONS layered
on top of the enriched body — they never remove information, so containment is
insensitive to them. They are enumerated in ``DEFAULT_WRAPPER_MARKERS`` so a future
exact-reconstruction mode can account for them.

Fail-loud contract: when containment fails, ``injection_reconstructs`` lands False on
the receipt AND an ERROR is logged (never silent). ``VNX_INJECTION_RECONSTRUCT_STRICT=1``
escalates to a raised ``InjectionReconstructError`` (fail-closed), per the track's
advisory-first-then-fail-closed rollout.

No Anthropic SDK. Pure stdlib + the coordination-DB reader.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from atomic_io import atomic_write_json, atomic_write_text  # noqa: E402

logger = logging.getLogger(__name__)

# Deterministic lane wrappers layered ON TOP of the enriched body. Registered (not
# gating) so a future exact-reconstruction mode can subtract them before diffing.
DEFAULT_WRAPPER_MARKERS: tuple[str, ...] = (
    "## Completion Protocol",            # tmux completion-protocol footer
    "## Scope Guard",                    # tmux scope-note
    "## Role",                           # legacy role header (fallback path)
    "## Worker Preamble",                # legacy no-role preamble
    "## Report Contract",                # report-body-contract directive heading
    "<!-- VNX-END-OF-INSTRUCTION -->",   # trailer sentinel
)

_ENV_STRICT = "VNX_INJECTION_RECONSTRUCT_STRICT"

_WS_RE = re.compile(r"\s+")


class InjectionReconstructError(RuntimeError):
    """Raised when reconstruction fails AND strict/fail-closed mode is enabled."""


@dataclass(frozen=True)
class ReconstructResult:
    """Outcome of the containment check."""

    reconstructs: bool
    items_checked: int
    missing: tuple[str, ...]


@dataclass(frozen=True)
class FinalPromptIntegrity:
    """Everything a lane needs to stamp the spec + receipt after assembly."""

    final_prompt_path: Optional[str]
    final_prompt_sha256: str
    injection_reconstructs: bool
    items_checked: int
    missing: tuple[str, ...]


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def compute_sha256(text: str) -> str:
    """sha256 hexdigest of *text* encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_ws(text: str) -> str:
    """Collapse every whitespace run to a single space and strip the ends.

    Injection rendering interleaves newlines/indentation that the raw item content
    does not carry (and vice versa); normalizing both sides makes the substring
    containment check robust to that formatting without loosening what it proves.
    """
    return _WS_RE.sub(" ", text).strip()


def strip_known_wrappers(text: str, markers: "tuple[str, ...]" = DEFAULT_WRAPPER_MARKERS) -> str:
    """Return *text* with the deterministic wrapper sections elided.

    Best-effort helper for callers that want the enriched-body-only view (the
    containment check itself does not need it — it tolerates the wrappers). Only the
    trailer sentinel and everything after the FIRST wrapper heading are removed, in
    marker order of appearance, so the enriched prefix is preserved.
    """
    cut = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip()


# ---------------------------------------------------------------------------
# Injection items
# ---------------------------------------------------------------------------

def load_injection_items(state_dir: "str | Path", dispatch_id: str) -> List[dict]:
    """Load the ``items_json`` recorded for *dispatch_id* at ``dispatch_create``.

    Reads the coordination DB (``runtime_coordination.db``). Returns the parsed list
    of item dicts, or ``[]`` when the DB/table/row is absent or unparseable (the
    check then trivially holds with ``items_checked=0`` — an honest "nothing to
    verify", never a false failure). Best-effort: never raises.
    """
    try:
        from coordination_db import db_path_from_state_dir  # noqa: PLC0415
    except ImportError:
        return []
    db_path = db_path_from_state_dir(state_dir)
    if not Path(db_path).exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT items_json, injection_point FROM intelligence_injections "
                "WHERE dispatch_id = ? ORDER BY injected_at DESC",
                (dispatch_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("final_prompt_integrity: injection read failed dispatch=%s: %s", dispatch_id, exc)
        return []

    if not rows:
        return []
    # Prefer the dispatch_create injection (the one that enriched the prompt);
    # fall back to the most-recent row of any point.
    chosen = None
    for row in rows:
        if (row["injection_point"] or "") == "dispatch_create":
            chosen = row
            break
    if chosen is None:
        chosen = rows[0]
    try:
        items = json.loads(chosen["items_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [it for it in items if isinstance(it, dict)]


# ---------------------------------------------------------------------------
# Reconstruction check
# ---------------------------------------------------------------------------

def _item_label(item: dict) -> str:
    return str(item.get("item_id") or item.get("item_class") or "item")


def verify_injection_reconstructs(
    final_prompt: str,
    raw_instruction: str,
    injection_items: List[dict],
    *,
    dispatch_id: str = "",
    strict: Optional[bool] = None,
) -> ReconstructResult:
    """Assert raw instruction + each injection item survive into *final_prompt*.

    Containment (tolerant): the whitespace-normalized raw instruction, and the
    whitespace-normalized ``content`` of every injection item, must each be a
    substring of the normalized final body. ``items_json`` stores a PREFIX of the
    item content (``to_dict`` caps it), and a prefix of verbatim-rendered content is
    always a substring — so a passing item genuinely reached the model, and a
    corrupted/dropped item genuinely fails.

    On failure: logs an ERROR with the missing pieces (fail-loud) and returns
    ``reconstructs=False``. When *strict* (or ``VNX_INJECTION_RECONSTRUCT_STRICT=1``)
    is set, raises ``InjectionReconstructError`` (fail-closed).
    """
    norm_final = _normalize_ws(final_prompt)
    missing: List[str] = []

    raw_norm = _normalize_ws(raw_instruction)
    if raw_norm and raw_norm not in norm_final:
        missing.append("raw-instruction")

    checked = 0
    for item in injection_items:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        checked += 1
        if _normalize_ws(content) not in norm_final:
            missing.append(_item_label(item))

    reconstructs = not missing
    if not reconstructs:
        logger.error(
            "final_prompt_integrity: injection reconstruction FAILED dispatch=%s "
            "missing=%s (raw+%d items checked against %d-char final body) — the "
            "delivered prompt does not contain the governed raw instruction and/or "
            "recorded injection items; audit chain broken at the input side",
            dispatch_id or "(unknown)",
            missing,
            checked,
            len(final_prompt),
        )

    _strict = strict
    if _strict is None:
        _strict = os.environ.get(_ENV_STRICT, "0").strip().lower() in ("1", "true", "yes", "on")
    if not reconstructs and _strict:
        raise InjectionReconstructError(
            f"final_prompt_integrity: reconstruction failed dispatch={dispatch_id or '(unknown)'} "
            f"missing={missing} (fail-closed via {_ENV_STRICT})"
        )
    return ReconstructResult(reconstructs=reconstructs, items_checked=checked, missing=tuple(missing))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist_final_prompt(
    bundle_dir: "str | Path",
    final_prompt: str,
    *,
    update_spec: bool = True,
) -> "tuple[Optional[Path], str]":
    """Write ``final_prompt.md`` into *bundle_dir* and pin its sha in the spec.

    Returns ``(final_prompt_path, final_prompt_sha256)``. The sha is ALWAYS computed
    and returned (it is the receipt-carried fact); ``final_prompt_path`` is None only
    when the file could not be written (logged, non-fatal). When *update_spec* and a
    sibling ``dispatch-spec.json`` exists, ``final_prompt_sha256`` +
    ``final_prompt_path`` are merged into it atomically.
    """
    sha = compute_sha256(final_prompt)
    bundle = Path(bundle_dir)
    final_path: Optional[Path] = None
    try:
        bundle.mkdir(parents=True, exist_ok=True)
        final_path = bundle / "final_prompt.md"
        atomic_write_text(final_path, final_prompt)
    except OSError as exc:
        logger.error(
            "final_prompt_integrity: could not persist final_prompt.md in %s: %s "
            "(sha still recorded on the receipt)",
            bundle,
            exc,
        )
        return None, sha

    if update_spec:
        spec_file = bundle / "dispatch-spec.json"
        if spec_file.exists():
            try:
                data = json.loads(spec_file.read_text(encoding="utf-8"))
                data["final_prompt_sha256"] = sha
                data["final_prompt_path"] = str(final_path.resolve())
                atomic_write_json(spec_file, data)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "final_prompt_integrity: could not stamp final_prompt_sha256 into %s: %s",
                    spec_file,
                    exc,
                )
    return final_path, sha


# ---------------------------------------------------------------------------
# Lane entry point
# ---------------------------------------------------------------------------

def _default_bundle_dir(data_dir: "str | Path", dispatch_id: str) -> Path:
    return Path(data_dir) / "dispatches" / "pending" / dispatch_id


def record_final_prompt_integrity(
    *,
    dispatch_id: str,
    final_prompt: str,
    raw_instruction: str,
    data_dir: "str | Path",
    state_dir: "str | Path",
    bundle_dir: "Optional[str | Path]" = None,
    strict: Optional[bool] = None,
) -> FinalPromptIntegrity:
    """Persist the final prompt, then verify raw + injections reconstruct it.

    One call for both lanes:
      1. write ``final_prompt.md`` + stamp ``final_prompt_sha256`` in the spec,
      2. load the recorded injection items,
      3. run the containment check (fail-loud; fail-closed under strict).

    Returns a ``FinalPromptIntegrity`` the lane stamps onto its receipt. Persistence
    is best-effort (a missing bundle never blocks a dispatch); the reconstruction
    check's strict-mode raise propagates so an opted-in operator gets fail-closed.
    """
    if bundle_dir is None:
        bundle_dir = _default_bundle_dir(data_dir, dispatch_id)

    final_path, sha = persist_final_prompt(bundle_dir, final_prompt)
    items = load_injection_items(state_dir, dispatch_id)
    recon = verify_injection_reconstructs(
        final_prompt,
        raw_instruction,
        items,
        dispatch_id=dispatch_id,
        strict=strict,
    )
    return FinalPromptIntegrity(
        final_prompt_path=str(final_path) if final_path else None,
        final_prompt_sha256=sha,
        injection_reconstructs=recon.reconstructs,
        items_checked=recon.items_checked,
        missing=recon.missing,
    )
