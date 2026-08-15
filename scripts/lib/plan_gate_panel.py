#!/usr/bin/env python3
"""plan_gate_panel.py — the PM-skill plan-first gate (governed worker path).

A feature's PLAN (not its code) is reviewed by a diverse-family panel BEFORE any
implementation. This module runs that panel:

    plan doc + rubric  ->  N panelist lanes (opus / kimi / glm-5.2-harness)
                            each via provider_dispatch (governed: report -> receipt)
                       ->  parse each panelist's structured verdict
                       ->  apply the panel pass/fail rule (PM-SKILL)
                       ->  PASS | REVISE | BLOCK | INFRA_FAIL (0 readable verdicts —
                           an infrastructure outcome, never a plan judgment)

The caller (``planning_cli plan-gate run``) resolves the ``OI-PLAN-<track>``
blocker on PASS, which — via ``track_reconciler`` — flips the track's
``derived_status`` away from ``blocked`` and lets ``deliverable promote`` proceed.

Panel composition (PM-SKILL "always multi-model"): Opus + Kimi + GLM-5.2-harness,
three families so real disagreement surfaces. DeepSeek (own-key) is a legal third
but stays off the default panel; Codex is reserved for security/schema/governance
plans, never a default panelist.

Every lane routes through ``provider_dispatch.py``, so the provider constraints are
enforced by construction (kimi-via-cli-only, zai-via-openrouter-only,
no-anthropic-sdk) and each panelist emits a governed report -> receipt: the gate
that gates everything is itself in the audit trail.

Per-seat verdicts are appended to ``.vnx-attest/plan-gate-seats.ndjson`` (the same
append-only, hash-chained ledger pattern as the ``plan_gate_pass`` evidence record)
so the effectiveness probe can see whether every seat responded and what each one
said — previously only the final resolved record survived the run (OI-888).
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import tempfile
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROVIDER_DISPATCH = HERE / "provider_dispatch.py"
TMUX_INTERACTIVE_DISPATCH = HERE / "tmux_interactive_dispatch.py"
_SCRIPTS_DIR = HERE.parent
PANEL_CONFIG_RELATIVE_PATH = Path("configs") / "plan_gate_panel.yaml"

# OI-1066: the stable, greppable marker dispatch_govern stamps into every body it
# fabricates itself (a lane that timed out, errored, or never authored a report).
# Imported rather than re-declared so the panel's detection and the writer's stamp
# can never drift apart. A report carrying this marker never reached a worker, so
# it produced no verdict — the seat is "no verdict because the lane never
# delivered", NOT "the lane answered but its fence wouldn't parse" (parse_error).
# The import is defensive: in a stripped test context where dispatch_govern is not
# importable, fall back to the literal so detection still works (the writer and the
# detector stay in sync in production; the literal here is the same string).
try:
    from dispatch_govern import SYNTHESIZED_REPORT_MARKER  # noqa: PLC0415
except Exception:  # vnx-silent-except: keep the panel importable without govern
    SYNTHESIZED_REPORT_MARKER = "VNX_SYNTHESIZED_REPORT_BY_GOVERNANCE"

# The three keys every seat declares. Kept as a tuple so the loader and its
# validation stay in lock-step — a drift here would silently change what counts
# as a well-formed seat.
_SEAT_KEYS: tuple[str, ...] = ("label", "provider", "model_arg")

# Minimum meaningful characters (whitespace-stripped) a track's goal_state must
# carry to stand in for the plan when `plan-gate run` is invoked WITHOUT --doc.
# The value is overridable in configs/plan_gate_panel.yaml (`goal_min_chars`),
# never hardcoded at the call site — a fixed literal here would let the config
# drift silently (see load_goal_min_chars).
DEFAULT_GOAL_MIN_CHARS = 200

# Per-seat verdict ledger (OI-888): one append-only, hash-chained record per
# panelist per run, under the repo's .vnx-attest/ dir next to the plan_gate_pass
# evidence ledger. Read by the plan-gate effectiveness probe.
SEAT_LEDGER_RELPATH = ".vnx-attest/plan-gate-seats.ndjson"
SEAT_RECORD_TYPE = "plan_gate_seat"

# Claude is NOT a provider-lane provider — provider_dispatch refuses it. Claude lanes
# route via the TMUX-SPAWN lane (interactive `claude` in an ephemeral isolated worktree),
# which keeps billing on the SUBSCRIPTION (CLAUDE.md "June-15 escape"). They must NOT use
# headless `claude -p`: post-cutover that bills API credits.
_CLAUDE_PROVIDERS = {"claude"}

# Full diverse-family assurance panel: (label, provider string, model_arg).
# One panelist per provider family (Anthropic / Moonshot / Zhipu / DeepSeek / OpenAI) so a
# plan is reviewed from five independent vantage points before any code is written.
# NOTE: a panelist that flakes (a down proxy, an uninstalled CLI, or an unparseable verdict) is
# RETRIED once (VNX_PANEL_RETRY, see run_panel) and, if it still yields no readable verdict,
# ABSTAINS as a non-scoring lane instead of vetoing — so a single down lane no longer forces a
# REVISE (apply_panel_rule's liveness quorum). Keep every provider here runnable anyway; the
# retry only rescues transient flakes. glm-harness requires the local litellm proxy on :4141.
DEFAULT_PANEL: List[Dict[str, str]] = [
    {"label": "opus", "provider": "claude", "model_arg": "opus"},
    {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k3"},
    {"label": "glm-5.2-harness", "provider": "glm-harness", "model_arg": "glm-5.2"},
    {"label": "deepseek", "provider": "deepseek-harness", "model_arg": "deepseek-v4-pro"},
    {"label": "codex", "provider": "codex", "model_arg": "gpt-5.5"},
    # gemini is intentionally omitted until the `gemini` CLI is installed: an unrunnable
    # panelist emits no verdict, which the fail-safe rule turns into an unconditional REVISE.
]

VERDICT_FENCE = "vnx-plan-verdict"


def _default_panel_config_path() -> Path:
    """The on-disk seat-list config resolved the same way as the other fabric
    configs (``configs/plan_gate_panel.yaml`` at the repo root / central install
    root), never from this module's own location — a central install pins
    ``__file__`` inside a read-only version tree."""
    return _SCRIPTS_DIR.parent / PANEL_CONFIG_RELATIVE_PATH


def _valid_provider_strings() -> List[str]:
    """The closed set of legal ``provider`` strings (members of the ``Provider``
    enum in dispatch_spec). Resolved lazily so this module stays importable when
    dispatch_spec is not on the path yet, and so a future enum edit is picked up
    without touching this loader."""
    from dispatch_spec import Provider  # noqa: PLC0415
    return [p.value for p in Provider]


def _validate_seat(seat: Any, index: int, valid_providers: List[str]) -> Dict[str, str]:
    """Validate ONE seat from the config. Raises ``ValueError`` loud when the
    seat is malformed — never silently drops it, because a dropped seat reads as
    an abstention and turns into a REVISE via the fail-safe rule."""
    if not isinstance(seat, dict):
        raise ValueError(
            f"plan-gate panel seat #{index}: expected a mapping with keys "
            f"{list(_SEAT_KEYS)}, got {type(seat).__name__}"
        )
    missing = [k for k in _SEAT_KEYS if k not in seat]
    if missing:
        raise ValueError(
            f"plan-gate panel seat #{index} (label={seat.get('label', '<none>')!r}): "
            f"missing required key(s) {missing} — each seat needs {list(_SEAT_KEYS)}"
        )
    provider = str(seat["provider"]).strip()
    if provider not in valid_providers:
        raise ValueError(
            f"plan-gate panel seat #{index} (label={seat['label']!r}): "
            f"unknown provider {provider!r}. Valid providers: {valid_providers}"
        )
    return {
        "label": str(seat["label"]),
        "provider": provider,
        "model_arg": str(seat["model_arg"]),
    }


def load_panel_seats(config_path: Optional[Path] = None) -> List[Dict[str, str]]:
    """Load the ordered seat list from ``configs/plan_gate_panel.yaml``.

    Falls back to ``DEFAULT_PANEL`` when the file is absent or unreadable (a
    missing config never breaks the gate; the code constant is the safety net).
    A present-but-invalid config fails LOUD: each seat must carry the three keys
    ``label``/``provider``/``model_arg`` and ``provider`` must be a member of the
    closed ``Provider`` enum in ``scripts/lib/dispatch_spec.py``. An invalid seat
    is never silently dropped — a dropped seat reads as an abstention and turns
    into a REVISE via the fail-safe rule, so misconfiguration must surface at
    load time, not at verdict time.
    """
    path = Path(config_path) if config_path is not None else _default_panel_config_path()
    if not path.is_file():
        return list(DEFAULT_PANEL)
    try:
        import yaml  # noqa: PLC0415
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        warnings.warn(
            f"plan_gate_panel: failed to load {path}: {exc} — using DEFAULT_PANEL",
            RuntimeWarning,
            stacklevel=2,
        )
        return list(DEFAULT_PANEL)
    if not isinstance(loaded, dict) or "seats" not in loaded:
        warnings.warn(
            f"plan_gate_panel: {path} has no 'seats' list — using DEFAULT_PANEL",
            RuntimeWarning,
            stacklevel=2,
        )
        return list(DEFAULT_PANEL)
    raw_seats = loaded["seats"]
    if not isinstance(raw_seats, list) or not raw_seats:
        raise ValueError(
            f"plan_gate_panel: {path} 'seats' must be a non-empty list, got "
            f"{type(raw_seats).__name__}"
        )
    valid_providers = _valid_provider_strings()
    return [_validate_seat(s, i, valid_providers) for i, s in enumerate(raw_seats)]


def load_goal_min_chars(config_path: Optional[Path] = None) -> int:
    """The minimum goal length for ``plan-gate run`` invoked WITHOUT ``--doc``.

    Reads ``goal_min_chars`` from ``configs/plan_gate_panel.yaml`` (the same file
    and path resolution as ``load_panel_seats``). Falls back to
    ``DEFAULT_GOAL_MIN_CHARS`` when the file is absent/unreadable or the key is
    missing (a pre-existing config never breaks the gate). A present-but-invalid
    value (non-int, bool, or <= 0) fails LOUD: a silently-wrong threshold would
    either let a thin goal through to a panel it cannot inform or refuse every
    goal — both silent governance drifts, so misconfiguration must surface here.
    """
    path = Path(config_path) if config_path is not None else _default_panel_config_path()
    if not path.is_file():
        return DEFAULT_GOAL_MIN_CHARS
    try:
        import yaml  # noqa: PLC0415
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return DEFAULT_GOAL_MIN_CHARS
    if not isinstance(loaded, dict):
        return DEFAULT_GOAL_MIN_CHARS
    raw = loaded.get("goal_min_chars")
    if raw is None:
        return DEFAULT_GOAL_MIN_CHARS
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(
            f"plan_gate_panel: {path} 'goal_min_chars' must be a positive int, "
            f"got {raw!r}"
        )
    return raw


# Minimum number of DELIVERED seat reports the deliberation panel's synthesis
# stage requires before it will synthesize (OI-1154). Below this floor the
# synthesis refuses loudly rather than silently reporting over a handful of
# seats. The value is overridable in configs/plan_gate_panel.yaml
# (`synthesis_min_seats`), never hardcoded at the call site — a fixed literal in
# Python would let the config drift silently (see load_synthesis_min_seats). The
# consumer is the deliberation panel (`scripts/panel.py` wires it into
# run_deliberation), which is why this lives beside the other plan_gate_panel.yaml
# loaders even though the gate itself is in deliberation_panel.py.
DEFAULT_SYNTHESIS_MIN_SEATS = 3


def load_synthesis_min_seats(config_path: Optional[Path] = None) -> int:
    """The synthesis coverage floor, read from ``configs/plan_gate_panel.yaml``.

    Reads ``synthesis_min_seats`` from the same file and path resolution as
    ``load_panel_seats``/``load_goal_min_chars``. Falls back to
    ``DEFAULT_SYNTHESIS_MIN_SEATS`` when the file is absent/unreadable or the key
    is missing (a pre-existing config never breaks the panel). A present-but-
    invalid value (non-int, bool, or <= 0) fails LOUD: a silently-wrong floor
    would either refuse every synthesis (<= 0 never satisfies) or never refuse
    at all — both silent governance drifts, so misconfiguration must surface
    here, matching ``load_goal_min_chars``.
    """
    path = Path(config_path) if config_path is not None else _default_panel_config_path()
    if not path.is_file():
        return DEFAULT_SYNTHESIS_MIN_SEATS
    try:
        import yaml  # noqa: PLC0415
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return DEFAULT_SYNTHESIS_MIN_SEATS
    if not isinstance(loaded, dict):
        return DEFAULT_SYNTHESIS_MIN_SEATS
    raw = loaded.get("synthesis_min_seats")
    if raw is None:
        return DEFAULT_SYNTHESIS_MIN_SEATS
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(
            f"plan_gate_panel: {path} 'synthesis_min_seats' must be a positive int, "
            f"got {raw!r}"
        )
    return raw


def filter_panel_seats(
    seats: List[Dict[str, str]], labels: List[str]
) -> List[Dict[str, str]]:
    """Filter the configured seat list to ``labels`` for a single run.

    An unknown label fails loud listing the configured labels so the operator
    sees what is available instead of running a partial panel silently. Order
    follows the configured list (not the requested order) so a run's panel
    composition stays stable and auditable against the config.
    """
    configured = {s["label"]: s for s in seats}
    unknown = [lbl for lbl in labels if lbl not in configured]
    if unknown:
        raise ValueError(
            f"plan-gate panel: unknown --panel-seats label(s): {unknown}. "
            f"Configured labels: {list(configured.keys())}"
        )
    # Order follows the configured list, not the requested order, so a run's
    # panel composition stays stable and auditable against the config.
    wanted = set(labels)
    return [s for s in seats if s["label"] in wanted]


# ---------------------------------------------------------------------------
# Governance-variant seat ladder (operator ladder, 2026-08-15). The panel SIZE
# derives from the governance variant, not a flat "every plan gets the full
# panel". The ladder, from lightest to heaviest:
#   minimal (docs, reversible)           -> 0 seats (no panel runs at all)
#   business-light / light               -> 1 seat  (opus)
#   default (code)                       -> 2 seats (opus, kimi)
#   coding-strict (core / irreversible)  -> 3 seats (opus, kimi, glm-5.2-harness)
#   new feature (task_class 01_code_generation) -> full panel, regardless of variant
# Labels are ordered to match DEFAULT_PANEL so filter_panel_seats keeps the
# configured (auditable) order; the tuple length IS the seat count, so there is
# no second count dict to drift.
# ---------------------------------------------------------------------------
GOVERNANCE_VARIANT_SEAT_LABELS: Dict[str, tuple[str, ...]] = {
    "minimal": (),
    "business-light": ("opus",),
    "light": ("opus",),
    "default": ("opus", "kimi"),
    "coding-strict": ("opus", "kimi", "glm-5.2-harness"),
}


def seat_labels_for_governance_variant(
    seats: List[Dict[str, str]], variant: str, *, is_new_feature: bool = False
) -> List[str]:
    """The seat labels a governance variant entitles a plan to, for one run.

    A new feature (``is_new_feature``) always gets every configured seat — the
    full diverse-family panel — because a new feature is the highest
    blast-radius code change and must never be sized down by a coincidentally
    light path. Otherwise the variant selects an ordered prefix of the
    configured panel (``minimal`` selects none). Fail-loud on an unknown
    variant: a variant the ladder does not know must never silently shrink to
    zero seats.
    """
    if is_new_feature:
        return [s["label"] for s in seats]
    labels = GOVERNANCE_VARIANT_SEAT_LABELS.get(variant)
    if labels is None:
        raise ValueError(
            f"plan-gate panel: unknown governance variant {variant!r}; "
            f"known variants: {sorted(GOVERNANCE_VARIANT_SEAT_LABELS)}"
        )
    return list(labels)


def seat_override_direction(
    derived_variant: str, derived_count: int, chosen_count: int,
) -> str:
    """Direction of an operator seat override vs the variant-derived seat count.

    Returns "" when the counts match (no override), "upgrade" when the operator
    added seats, "downgrade" when they removed seats, or "strict-downgrade" when
    they removed seats from a coding-strict derivation — the heaviest variant
    class, where a lighter panel must be findable by a later sweep. Mirrors
    ``smart_router``'s gate override direction on the seat-count axis: the seat
    COUNT is the override axis, not the label set (a same-count relabel is not
    an override).
    """
    if chosen_count == derived_count:
        return ""
    if chosen_count > derived_count:
        return "upgrade"
    if derived_variant == "coding-strict":
        return "strict-downgrade"
    return "downgrade"


_OPEN_FENCE = "```" + VERDICT_FENCE
_VALID_VERDICTS = {"pass", "revise", "block"}

# A dispatcher takes (provider, model_arg, instruction, dispatch_id) and returns the
# panelist's report text. Injectable so the panel logic is testable without a live model.
DispatcherFn = Callable[[str, str, str, str], str]

_VERDICT_CONTRACT = (
    "When your review is done, append EXACTLY ONE fenced block and nothing after it:\n"
    f"```{VERDICT_FENCE}\n"
    "{\n"
    '  "verdict": "pass" | "revise" | "block",\n'
    '  "blocking_findings": ["short concrete issue", "..."],\n'
    '  "rationale": "one or two sentences"\n'
    "}\n"
    "```\n"
    "verdict=block: a fundamental flaw makes the plan unsafe to build as written.\n"
    "verdict=revise: real, fixable gaps remain but the approach is salvageable.\n"
    "verdict=pass: the plan is sound enough to implement.\n"
)


# The plan doc is untrusted input inlined into each panelist's instruction. Two guards:
#  - a doc must not be able to inject its own verdict fence (verdict spoofing);
#  - a doc must not blow argv past ARG_MAX when passed as --instruction.
#
# DEFAULT_MAX_DOC_CHARS derivation (OI-858-class visibility fix, 2026-07-31): the previous
# 60_000 was a 10x-too-conservative guess. Measured on this platform (macOS):
# `getconf ARG_MAX` = 1_048_576 bytes — the ceiling execve() enforces on the COMBINED argv +
# inherited-environment size for the provider_dispatch.py subprocess this doc is inlined
# into (the claude/tmux lane instead writes the doc to a temp file and never inlines it, so
# this cap only bites the kimi/glm/deepseek/codex provider lanes). Budget:
#   - this module's own wrapper text (rubric + verdict contract + track/report boilerplate)
#     measures ~1.2k chars around the doc body — negligible;
#   - the subprocess's inherited environment measured ~3.7KB on this dev machine; budgeted
#     here at a generous 150_000 bytes so the cap stays safe on much heavier CI/dev
#     environments too;
#   - the remaining ~898_000-byte margin is divided by 2 (not 1) bytes/char as a safety
#     factor for non-ASCII plan-doc content (typographic quotes, em-dashes, arrows), which
#     costs more than 1 byte/char in UTF-8, rather than assuming pure-ASCII.
# That yields ~449k chars of headroom; DEFAULT_MAX_DOC_CHARS is set to a round 400_000 —
# 6.7x the previous cap, comfortably inside the computed margin even under the pessimistic
# 2-bytes/char assumption (400_000 * 2 = 800_000 doc bytes + 150_000 overhead = 950_000,
# still ~98KB under ARG_MAX). Overridable via VNX_PLAN_GATE_MAX_DOC_CHARS (same pattern as
# VNX_PANEL_RETRY) for an exceptional oversized plan.
DEFAULT_MAX_DOC_CHARS = 400_000
MAX_DOC_CHARS = DEFAULT_MAX_DOC_CHARS  # the no-override default; effective cap is _max_doc_chars()


def _max_doc_chars() -> int:
    """Effective plan-doc truncation cap: ``VNX_PLAN_GATE_MAX_DOC_CHARS`` override, default
    ``DEFAULT_MAX_DOC_CHARS``. A malformed or non-positive value falls back to the default —
    same override pattern as ``_panel_retry_count``."""
    raw = os.environ.get("VNX_PLAN_GATE_MAX_DOC_CHARS", "").strip()
    if not raw:
        return DEFAULT_MAX_DOC_CHARS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_DOC_CHARS
    return value if value > 0 else DEFAULT_MAX_DOC_CHARS


def _sanitize_doc(doc_text: str) -> str:
    # Neutralize any embedded verdict fence so a plan doc cannot spoof a PASS: a space
    # after the backticks breaks the exact ```vnx-plan-verdict opener parse_verdict matches.
    safe = doc_text.replace("```" + VERDICT_FENCE, "``` " + VERDICT_FENCE + " (neutralized)")
    limit = _max_doc_chars()
    if len(safe) > limit:
        safe = safe[:limit] + f"\n\n[... plan doc truncated at {limit} chars for the gate ...]"
    return safe


def _doc_truncation_info(doc_text: str) -> Dict[str, Any]:
    """Whether ``_sanitize_doc`` will truncate this doc when inlined into a panelist
    instruction, and by how much.

    Mirrors ``_sanitize_doc``'s length check EXACTLY (same verdict-fence neutralization,
    same effective cap) so the info reported here can never drift from what actually gets
    truncated. This is the fix for the silent-partial-read defect: a gate that only reads
    part of its input must say so in the verdict it hands back, not just in the panelist's
    own prompt — see ``run_panel``, which attaches this to the top-level result and folds a
    note into the summary rationale whenever ``truncated`` is True.
    """
    safe = doc_text.replace("```" + VERDICT_FENCE, "``` " + VERDICT_FENCE + " (neutralized)")
    limit = _max_doc_chars()
    original_chars = len(safe)
    truncated = original_chars > limit
    return {
        "truncated": truncated,
        "original_chars": original_chars,
        "kept_chars": min(original_chars, limit),
        "limit_chars": limit,
    }


_RUBRIC = (
    "Judge the plan on:\n"
    "1. Problem: is the problem stated, and is it real?\n"
    "2. Approach: is it sound, or are there unaddressed failure modes?\n"
    "3. Deliverables: each scoped, independently shippable, task_class tagged?\n"
    "4. Risks: are the real risks named, each with a mitigation?\n"
    "5. Model-routing plan: a sane quality FLOOR per deliverable (not a hand-picked lane)?\n"
    "6. ADR-007: if it touches a central-DB table, does it carry a composite key over project_id?\n\n"
    "Be a skeptic. Surface concrete, fixable gaps. Do not rubber-stamp.\n"
)


def build_plan_review_instruction(doc_text: str, track_id: str) -> str:
    """Render the plan-review instruction handed to each panelist (inline-doc form).

    Used by provider lanes (kimi, glm) where the instruction is passed as a
    subprocess argument and the full inline doc is acceptable.  The claude/tmux
    lane uses ``build_plan_review_instruction_fileref`` instead so the ~50k-char
    doc body never inflates the instruction string.
    """
    doc_text = _sanitize_doc(doc_text)
    return (
        f"You are an independent plan reviewer for track {track_id}. Review the "
        "IMPLEMENTATION PLAN below. The plan only — no code exists yet.\n\n"
        + _RUBRIC
        + "\n----- PLAN UNDER REVIEW -----\n"
        f"{doc_text}\n"
        "----- END PLAN -----\n\n"
        + _VERDICT_CONTRACT
    )


def build_plan_review_instruction_fileref(
    doc_path: str, track_id: str, report_path: str
) -> str:
    """Render the plan-review instruction for the claude/tmux lane.

    The plan doc is passed by FILE REFERENCE (not inlined) so the instruction
    string stays short — avoiding the >120s bracketed-paste ingestion that trips
    the WORK_START_GATE timeout on a large doc.

    ``report_path``: the absolute path where the worker MUST write its report.
    This makes the expectation explicit so the worker does not have to guess the
    unified_reports location, and govern() can find the authored file.
    """
    return (
        f"You are an independent plan reviewer for track {track_id}.\n\n"
        f"Read the IMPLEMENTATION PLAN from this file:\n\n"
        f"  {doc_path}\n\n"
        "Review the plan only — no code exists yet.\n\n"
        + _RUBRIC
        + "\n"
        + _VERDICT_CONTRACT
        + f"\n\nREPORT FILE (MANDATORY): Write your complete review — including the "
        f"```{VERDICT_FENCE}``` block at the end — to this exact file path:\n\n"
        f"  {report_path}\n\n"
        "Do NOT write to any other path. The panel reads only that file. "
        "Your review is not recorded unless it lands there with the verdict fence intact."
    )


def build_generic_fileref_instruction(doc_path: str, report_path: str) -> str:
    """Render a generic (NON-plan-review) file-ref instruction for the claude/tmux lane.

    OI-811: ``_make_default_dispatcher`` is reused by callers whose instruction is NOT a
    plan review — e.g. the deliberation panel's diverge/contrarian/verify/synthesis
    stages. Wrapping their prompt in ``build_plan_review_instruction_fileref`` (which
    tells the worker "you are an independent plan reviewer... review the IMPLEMENTATION
    PLAN") caused a plan-reviewer-role worker to correctly reject the file as not a plan,
    corrupting the panel stage. This variant carries the file-ref benefit (short
    instruction string, avoids the bracketed-paste ingestion timeout on a large prompt)
    WITHOUT the plan-review framing — the worker is simply told to follow the referenced
    instruction and write its response to ``report_path``.
    """
    return (
        f"Read your complete instruction from this file:\n\n"
        f"  {doc_path}\n\n"
        "Follow it in full.\n\n"
        f"REPORT FILE (MANDATORY): Write your complete response to this exact file path:\n\n"
        f"  {report_path}\n\n"
        "Do NOT write to any other path. The caller reads only that file."
    )


def _strip_trailing_commas(text: str) -> str:
    """Remove a trailing comma immediately before a closing ``}``/``]`` (a recurring
    codex/glm flake: ``{"a": 1,}``)."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _extract_braced_object(text: str) -> Optional[str]:
    """Slice from the first ``{`` to the last ``}`` in ``text``.

    Strips prose bleed around the object (a panelist prefacing/trailing the JSON with
    commentary) and, as a side effect, strips a nested ` ```json ` code fence wrapped around
    the object too — the fence markers fall outside the first-``{``/last-``}`` window.
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start:end + 1]


def _parse_json_loose(body: str) -> Optional[Any]:
    """Best-effort repair of a near-miss verdict JSON body before giving up.

    Tries, in order: the body as-is; the body with a braced-object extraction (drops prose
    bleed and any nested ```json fence); each of those with trailing commas stripped. Returns
    the parsed value, or ``None`` if nothing repairs into valid JSON.
    """
    candidates = [body.strip()]
    braced = _extract_braced_object(body)
    if braced is not None:
        candidates.append(braced)
    candidates.extend([_strip_trailing_commas(c) for c in list(candidates)])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _iter_fence_bodies(report_text: str) -> List[str]:
    """Return the body text of every ``vnx-plan-verdict`` fence, in document order.

    Each fence's body is bounded by the START of the NEXT ``_OPEN_FENCE`` occurrence
    (or end-of-text for the last fence) before the closing-``` search runs — so when a
    report contains more than one fence, an earlier fence's body slice can never bleed
    into a later fence (a naive ``rfind`` bound at end-of-text would pick up the LAST
    fence's closing marker for every earlier fence too).
    """
    opens: List[int] = []
    search_from = 0
    while True:
        idx = report_text.find(_OPEN_FENCE, search_from)
        if idx == -1:
            break
        opens.append(idx)
        search_from = idx + len(_OPEN_FENCE)
    bodies = []
    for i, idx in enumerate(opens):
        bound = opens[i + 1] if i + 1 < len(opens) else len(report_text)
        chunk = report_text[idx + len(_OPEN_FENCE):bound]
        close_idx = chunk.rfind("```")
        if close_idx != -1:
            chunk = chunk[:close_idx]
        bodies.append(chunk)
    return bodies


def parse_verdict(report_text: str) -> Dict[str, Any]:
    """Extract a ``vnx-plan-verdict`` block from a panelist report.

    Scans every ``vnx-plan-verdict`` fence from LAST to FIRST and returns the first one
    that parses into a valid verdict object. This matters when a panelist's final fence
    is the ECHOED verdict-contract EXAMPLE (containing the non-JSON union
    ``"verdict": "pass" | "revise" | "block"``) rather than its actual verdict: that
    fence is unparseable by construction, so without scanning backward past it a real
    verdict emitted earlier in the report would be lost to a spurious abstain.

    Tolerant of common near-miss formatting via ``_parse_json_loose`` — a trailing comma, prose
    bleeding in around the JSON object, or a nested ` ```json ` code fence wrapping the object —
    so a slightly-malformed body no longer forces a spurious abstain (the codex/glm verdict-JSON
    flake). The exact ``vnx-plan-verdict`` fence label is still required verbatim for EVERY
    candidate fence: that is the same marker ``_sanitize_doc`` neutralizes in untrusted plan-doc
    input, so this deliberately never falls back to a bare/relabeled ```json fence — doing so
    would reopen the verdict-spoofing hole (kimi finding 4) via a doc that gets echoed into a
    panelist's own report.

    Fail-safe by design: if NO fence parses into a valid verdict, the result is ``revise`` with
    ``parse_error=True`` so a missing/garbled verdict can never silently PASS.
    """
    empty = {"verdict": "revise", "blocking_findings": [], "rationale": "", "parse_error": True}
    if not report_text:
        return {**empty, "rationale": "empty report"}
    bodies = _iter_fence_bodies(report_text)
    if not bodies:
        return {**empty, "rationale": "no verdict block found"}

    last_reason = "verdict block is not valid JSON"
    for body in reversed(bodies):
        data = _parse_json_loose(body)
        if data is None:
            last_reason = "verdict block is not valid JSON"
            continue
        if not isinstance(data, dict):
            last_reason = "verdict block is not a JSON object"
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in _VALID_VERDICTS:
            last_reason = f"unknown verdict {verdict!r}"
            continue
        findings = data.get("blocking_findings") or []
        if not isinstance(findings, list):
            findings = [str(findings)]
        return {
            "verdict": verdict,
            "blocking_findings": [str(x) for x in findings],
            "rationale": str(data.get("rationale", "")),
            "parse_error": False,
        }
    return {**empty, "rationale": last_reason}


@dataclass
class PanelistResult:
    label: str
    provider: str
    model: str = ""                  # the model_arg the panelist was dispatched with
    verdict: str = "revise"          # pass | revise | block
    blocking_findings: List[str] = field(default_factory=list)
    rationale: str = ""
    report_path: str = ""
    dispatched: bool = False         # did the dispatch + report read succeed
    parse_error: bool = False
    # OI-1066: the lane never produced a verdict at all because it never delivered
    # — a timeout, a governance-synthesized report, or no report file. This is a
    # THIRD category, distinct from ``parse_error`` (a REAL report whose verdict
    # fence wouldn't parse) and from ``not dispatched`` (the dispatch call itself
    # raised). A no-verdict seat MUST NOT be folded into "abstained" with a
    # parse_error seat: "abstained" reads as "the lane declined to weigh in",
    # but a timed-out lane never SAW the plan. The panel must not certify a PASS
    # on the remaining seats' strength when one seat has no verdict (see
    # apply_panel_rule).
    no_verdict: bool = False
    error: str = ""
    raw_text: str = ""               # OI-839: the lane's raw report, kept ONLY on
                                     # parse_error so the unparseable output survives
                                     # for diagnosis instead of vanishing with the tempfile


def _decision(decision: str, block: int, revise: int, passes: int, rationale: str) -> Dict[str, Any]:
    return {
        "decision": decision,
        "block_count": block,
        "revise_count": revise,
        "pass_count": passes,
        "rationale": rationale,
    }


def apply_panel_rule(results: List[PanelistResult]) -> Dict[str, Any]:
    """The PM-SKILL pass/fail rule, with NON-SCORING lanes + a liveness quorum.

    A lane with no readable verdict (undispatched, or a report whose verdict block did not
    parse) is NON-SCORING: it ABSTAINS rather than vetoing. Rationale (2026-06-24): a
    structurally-broken or input-degraded lane (e.g. a model that won't emit the requested
    fence on a large doc) must not veto a substantive PASS from the readable lanes forever —
    that is a liveness hole, not safety. The non-scoring lanes are named in the rationale so
    the abstention is transparent, never silent.

    OI-1066: a lane that never produced a verdict AT ALL because it never delivered
    (``PanelistResult.no_verdict`` — a timeout, a governance-synthesized report, or no
    report file) is a THIRD category, NOT folded into the abstaining parse_error lanes.
    "Abstained" reads as "the lane declined to weigh in"; a no-verdict lane never SAW the
    plan. For an operator judging whether a PASS means anything that is the whole
    difference, so the rule is:

        PASS requires that NO seat is in the no-verdict category.

    A panel with any no-verdict seat cannot certify a PASS on the remaining seats'
    strength alone — it returns REVISE (or INFRA_FAIL when every seat is no-verdict /
    there are zero readable verdicts, so an all-seats-down panel stays an infrastructure
    outcome and never a content verdict). The reasoning is asymmetric to the
    2026-06-24 abstain rule on purpose: a model that ANSWERED but malformed its verdict
    still SAW the plan, so it may legitimately abstain; a lane that timed out did not,
    so its silence is not an abstention. The no-verdict seats are named in the rationale
    under their own label (``no-verdict (timeout/no-report)``) so the one-line summary
    distinguishes them from the abstained seats.

    Over the SCORING (readable) lanes only:
    - infra floor: ZERO readable verdicts is NOT a plan judgment — the plan was never
      reviewed. That returns INFRA_FAIL (an infrastructure outcome, distinct from every
      content verdict) so a fleet-wide lane breakage can never read as an inhoudelijk
      REVISE ("revise the plan") for a plan no lane ever saw.
    - quorum: require >= 2 readable verdicts to certify (a single voice can't fold to PASS).
    - any BLOCK -> REVISE.
    - >= 2 REVISE -> REVISE.
    - <= 1 REVISE and no BLOCK, with passes OUTnumbering the dissent -> PASS (the lone dissent
      folds as a tracked note); a tie is safety-first REVISE.
    - any no-verdict seat -> PASS is forbidden (REVISE, or INFRA_FAIL at zero readable).
    """
    if not results:
        # An empty panel must never fall through to PASS (misconfigured panel=[]).
        return _decision("REVISE", 0, 0, 0, "no panelists ran — empty panel, cannot certify")
    # OI-1066: three categories, not two.
    #   scoring    — dispatched, no parse error, NOT no-verdict (the verdicts that count)
    #   no_verdict — the lane never delivered a verdict at all (timeout/synthesized/no file)
    #   abstained  — a real report whose fence would not parse (the 2026-06-24 non-scoring lane)
    scoring = [r for r in results if r.dispatched and not r.parse_error and not r.no_verdict]
    no_verdict = [r for r in results if r.no_verdict]
    abstained = [
        r for r in results
        if not (r.dispatched and not r.parse_error and not r.no_verdict) and not r.no_verdict
    ]
    nv_note = (
        f"; no-verdict (timeout/no-report): {', '.join(r.label for r in no_verdict)}"
        if no_verdict else ""
    )
    ns_note = (
        f"; non-scoring (abstained): {', '.join(r.label for r in abstained)}"
        if abstained else ""
    )
    block = sum(1 for r in scoring if r.verdict == "block")
    revise = sum(1 for r in scoring if r.verdict == "revise")
    passes = sum(1 for r in scoring if r.verdict == "pass")

    if not scoring:
        # 0 of N readable: every lane failed to dispatch, produced an unreadable verdict, or
        # never delivered at all. The plan was NOT reviewed — this is an infrastructure
        # failure, not a plan verdict, and must never read as "revise the plan and re-run".
        return _decision(
            "INFRA_FAIL", block, revise, passes,
            f"0 readable verdicts of {len(results)} — no lane produced a verdict, so "
            "the plan was NOT reviewed. This is an infrastructure failure, not a plan "
            "judgment: fix the lanes and re-run the gate"
            f"{ns_note}{nv_note}",
        )

    # OI-1066: a panel where any seat produced NO verdict at all cannot certify a PASS
    # on the strength of the remaining seats alone. This is REVISE (not INFRA_FAIL): at
    # least one lane DID review the plan, so there is a plan judgment to hand back — but
    # it cannot be a clean PASS while a seat that should have weighed in never saw the
    # plan. An operator must re-run so every seat gets the plan.
    if no_verdict:
        return _decision(
            "REVISE", block, revise, passes,
            f"{len(no_verdict)} seat(s) produced no verdict (timeout/no-report) of "
            f"{len(results)} — a lane that never saw the plan cannot be folded into a "
            f"PASS; re-run the gate so every seat delivers"
            f"{ns_note}{nv_note}",
        )

    # Liveness quorum: a multi-member panel must keep >= 2 readable voices to certify, so a
    # degraded 3-panel with only one readable lane can't pass on a single voice. A DELIBERATE
    # 1-member panel (a smoke) needs only its one voice. quorum = min(2, panel size).
    required = min(2, len(results))
    if len(scoring) < required:
        return _decision(
            "REVISE", block, revise, passes,
            f"only {len(scoring)} readable verdict(s) of {len(results)} — below quorum "
            f"({required}); cannot certify{ns_note}{nv_note}",
        )
    if block >= 1:
        return _decision(
            "REVISE", block, revise, passes,
            f"{block} BLOCK verdict(s) — revise the blocking sections, re-run the delta only{ns_note}{nv_note}",
        )
    if revise >= 2:
        return _decision(
            "REVISE", block, revise, passes,
            f"{revise} REVISE verdicts — one revise round{ns_note}{nv_note}",
        )
    if passes > revise:
        dissent = [r.label for r in scoring if r.verdict != "pass"]
        note = f"folded dissent (tracked): {', '.join(dissent)}" if dissent else "unanimous pass (scoring)"
        return _decision("PASS", block, revise, passes, note + ns_note + nv_note)
    return _decision(
        "REVISE", block, revise, passes,
        f"no passing majority — the dissent is not outnumbered{ns_note}{nv_note}",
    )


def _read_report(base: Optional[Path], dispatch_id: str, stderr: str) -> Optional[str]:
    """Locate a panelist's unified report. Authoritative source: the ``Report: <path>``
    line provider_dispatch prints to stderr; falls back to the deterministic path.

    Only a path whose filename is exactly ``{dispatch_id}.md`` is accepted — a foreign
    or stale ``Report:`` line must never feed this panelist a different dispatch's
    verdict (the gate's verdict-source integrity)."""
    expected = f"{dispatch_id}.md"
    for line in (stderr or "").splitlines():
        if line.startswith("Report: "):
            p = Path(line[len("Report: "):].strip())
            if p.name == expected and p.is_file():
                return p.read_text(encoding="utf-8")
    if base is not None:
        p = base / "unified_reports" / expected
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return None


def _resolve_data_dir(data_dir: Optional[str]) -> Path:
    """Resolve the data_dir the panel reports are written under and read back from.

    Resolution order (matches the rest of the fabric):
      1. A caller-supplied ``data_dir`` wins.
      2. ``VNX_DATA_DIR`` — honored on the NORMAL path, and ONLY when
         ``VNX_DATA_DIR_EXPLICIT=1`` is also set (the same two-key contract as
         ``project_root.resolve_data_dir`` / ``vnx_paths.resolve_paths``). A bare
         inherited ``VNX_DATA_DIR`` is pollution, not config: it is ignored with a
         warning, never silently honored.
      3. The central store for the ACTIVE project: ``~/.vnx-data/<project_id>`` via
         ``project_root.resolve_project_id()`` (env > ``.vnx-project-id`` marker >
         git remote) + ``project_root.resolve_central_data_dir()`` — the same store
         ``provider_dispatch._resolve_data_dir`` writes the provider-lane reports to,
         so the write path and ``_read_report``'s read-back base always agree.

    The data dir must NEVER be derived from this module's own location: in a central
    install ``__file__`` sits inside the read-only ``~/.vnx-system/versions/<v>/``
    tree, so ``resolve_data_dir(caller_file=__file__)`` resolves the keystone and
    every report write dies with EACCES (fleet-wide plan-gate block, 2026-07-31 —
    latent since the lane existed; surfaced when pinned version dirs went read-only).

    There is deliberately NO tempfile fallback: a gate report that must be read back
    from a throwaway dir is a silent verdict loss, not a degradation. When no
    project_id can be resolved this raises — loudly — instead of writing state the
    gate will never find.

    A ``None`` base means ``_read_report``'s base-fallback path can never resolve the
    claude/tmux-lane report: unlike ``provider_dispatch``, that lane prints no ``Report:``
    stderr line, so the opus seat's authored report is never found -> NO-VERDICT rc=1
    "staging_validator: unstaged dispatch override" (#1102 class bug).
    """
    if data_dir:
        return Path(data_dir)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))

    explicit_val = os.environ.get("VNX_DATA_DIR", "").strip()
    if explicit_val:
        if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1":
            return Path(explicit_val).expanduser().resolve()
        warnings.warn(
            f"VNX_DATA_DIR env-var set ({explicit_val}) but VNX_DATA_DIR_EXPLICIT=1 "
            "is required for it to be honored. Ignoring and resolving the central "
            "store for the active project_id instead. "
            "See https://github.com/Vinix24/vnx-orchestration/issues/225",
            DeprecationWarning,
            stacklevel=2,
        )

    from project_root import resolve_central_data_dir, resolve_project_id  # noqa: PLC0415
    try:
        project_id = resolve_project_id()
    except Exception as exc:
        raise RuntimeError(
            "plan-gate panel: cannot resolve the active project_id, so there is no "
            "central store to write panel reports to. Set VNX_PROJECT_ID, run from a "
            "project with a .vnx-project-id marker, or pass data_dir explicitly. "
            "(No tempfile fallback: a gate report written to a throwaway dir is a "
            "silently lost verdict.)"
        ) from exc
    return resolve_central_data_dir(project_id)


def _make_default_dispatcher(
    data_dir: Optional[str], timeout_seconds: int,
    *, role: str = "plan-reviewer",
) -> DispatcherFn:
    """Real dispatcher: run a panelist through its governed lane, return the report text.

    INTERIM — PR-12 consolidation target. This calls the lane scripts DIRECTLY, which is a
    side door. Once the single-entry dispatch door (`vnx dispatch` / dispatch_bridge) is
    wired and flipped, this MUST route through that one door instead. The split below is
    exactly what the door decides internally:
      claude            -> tmux-spawn lane (interactive claude, ephemeral worktree;
                           billing stays on the SUBSCRIPTION per the June-15 escape).
                           NOT provider_dispatch (refuses claude), NOT headless `claude -p`
                           (bills API credits post-cutover).
      kimi/glm/deepseek -> provider_dispatch (constraint-safe per provider).

    ``role``: OI-811 — this factory is reused by callers whose instruction is NOT a plan
    review (e.g. the deliberation panel's diverge/contrarian/verify/synthesis stages).
    Defaults to "plan-reviewer" for backward compatibility with ``run_panel``. A caller
    with a different role gets the generic (non-plan-framed) file-ref instruction on the
    claude/tmux lane, and that role is stamped on both lanes' ``--role`` so govern() and
    the phantom-guard evaluate it correctly instead of being told it is a plan review.
    """
    base = _resolve_data_dir(data_dir)

    def _dispatch(provider: str, model_arg: str, instruction: str, dispatch_id: str) -> str:
        env = dict(os.environ)
        # OI-1153: pin the seat subprocess to the SAME data dir this dispatcher
        # resolved (`base`), or the lane re-resolves its own. provider_dispatch /
        # tmux_interactive_dispatch both honor the two-key VNX_DATA_DIR +
        # VNX_DATA_DIR_EXPLICIT=1 contract (see their _resolve_data_dir/
        # _resolve_state_dir); without it the seat's report write path can land
        # outside the dir _read_report reads back from (e.g. the legacy
        # ~/.vnx-data/unified_reports root), and the seat report is silently
        # never found. This applies to BOTH lanes: the claude/tmux branch below
        # and the provider branch.
        env["VNX_DATA_DIR"] = str(base)
        env["VNX_DATA_DIR_EXPLICIT"] = "1"
        _tmp_doc_path: Optional[str] = None
        try:
            if provider in _CLAUDE_PROVIDERS:
                # Scoped-spawn fix (2026-07-14): --working-tree-only's commit/push deny
                # only binds in the scoped detached spawn; tmux_interactive_dispatch.dispatch()
                # fails CLOSED otherwise (its D2.2 scoping precondition), refusing the dispatch
                # before any report is written -> silent NO-VERDICT for this seat. Opt this
                # subprocess's env into the scoped posture so the --working-tree-only flag this
                # lane already passes is actually honored. Provider (kimi/glm/deepseek) lanes
                # below are untouched — this only applies to the claude/tmux branch.
                env["VNX_WORKER_SCOPED"] = "1"

                # BUG-2 FIX (file-ref): the instruction already has the full plan doc inlined
                # by run_panel's build_plan_review_instruction call. For the claude/tmux lane
                # we replace it with a compact file-ref instruction so the ~50k-char body never
                # inflates the bracketed-paste and does not trip the WORK_START_GATE timeout.
                #
                # We write the original inline instruction to a temp file so the worker can
                # read the plan + rubric + verdict contract from a stable on-disk path.
                # The expected report path is derived from data_dir (resolved above, never
                # None) so the file-ref instruction's write path and _read_report's read path
                # agree.
                report_path_str = str(base / "unified_reports" / f"{dispatch_id}.md")

                # Write the full inline instruction (plan + rubric + contract) to a temp file.
                # The worker reads this file; the short file-ref instruction points to it.
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    suffix=".vnx_plan_review.md",
                    delete=False,
                    encoding="utf-8",
                    prefix=f"plan_gate_{dispatch_id}_",
                ) as fh:
                    fh.write(instruction)
                    _tmp_doc_path = fh.name

                # Short instruction: rubric + verdict contract + explicit report-path directive.
                # No 50k doc body — the worker reads it from _tmp_doc_path. A non-plan-review
                # caller (OI-811) gets the generic file-ref framing instead — never told it is
                # reviewing an IMPLEMENTATION PLAN.
                if role == "plan-reviewer":
                    claude_instruction = build_plan_review_instruction_fileref(
                        doc_path=_tmp_doc_path,
                        track_id="<see file>",
                        report_path=report_path_str,
                    )
                else:
                    claude_instruction = build_generic_fileref_instruction(
                        doc_path=_tmp_doc_path,
                        report_path=report_path_str,
                    )

                cmd = [
                    sys.executable, str(TMUX_INTERACTIVE_DISPATCH),
                    "--dispatch-id", dispatch_id,
                    "--model", model_arg,
                    "--role", role,
                    "--instruction", claude_instruction,
                    "--deadline-seconds", str(timeout_seconds),
                    # A plan review is READ-ONLY (reads the doc file, writes a verdict report) —
                    # it needs no isolated worktree. --shared-worktree skips the expensive
                    # `git worktree add`, which on a large repo (e.g. SEOcrawler) blows the
                    # deadline and times opus out; it also grounds the review against the REAL
                    # checkout.
                    "--shared-worktree",
                    "--allow-unstaged",
                    # D2.2: a plan-review is working-tree-only — it reads the doc and
                    # writes a verdict report; it must NOT commit/push (OI-097). The
                    # flag denies git commit/push at the tool-permission layer.
                    "--working-tree-only",
                    "--reason", f"plan-gate panel {dispatch_id}",
                ]
                run_timeout = timeout_seconds + 180  # tmux warmup + teardown headroom
            else:
                claude_instruction = instruction  # provider lane: inline doc OK
                cmd = [
                    sys.executable, str(PROVIDER_DISPATCH),
                    "--provider", provider,
                    "--terminal-id", "plan-gate",
                    "--dispatch-id", dispatch_id,
                    "--model", model_arg,
                    "--role", role,
                    "--instruction", instruction,
                    "--no-auto-commit",
                ]
                run_timeout = timeout_seconds
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=run_timeout, check=False, env=env,
            )
            report = _read_report(base, dispatch_id, proc.stderr)
            if report is None:
                # tmux_interactive_dispatch's CLI prints the InteractiveDispatchResult (incl.
                # the actionable `failure_reason`) as JSON to STDOUT, not stderr -- surfacing
                # only stderr here previously masked the real cause (e.g. the D2.2 scoping
                # refusal) behind an unrelated last-stderr-line red herring.
                raise RuntimeError(
                    f"no report for {dispatch_id} (rc={proc.returncode}): "
                    f"stdout: {(proc.stdout or '')[-800:]} | stderr: {(proc.stderr or '')[-400:]}"
                )
            return report
        finally:
            # Always clean up the temp doc file regardless of success or failure.
            if _tmp_doc_path is not None:
                try:
                    os.unlink(_tmp_doc_path)
                except OSError:
                    pass

    return _dispatch


def _panel_retry_count() -> int:
    """Extra attempts for a flaked panelist (``VNX_PANEL_RETRY``, default 1, clamped >= 0).

    A panelist whose verdict is unparseable (parse_error) or that failed to dispatch is a
    transient-flake suspect (the codex verdict-JSON flake, the glm parse flake). It is retried
    up to this many times before it falls through to the abstain/non-scoring path — recovering
    the flake without letting one down lane force a REVISE. A malformed value falls back to 1.
    """
    raw = os.environ.get("VNX_PANEL_RETRY", "").strip()
    if not raw:
        return 1
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


# The per-seat deadline default. Before OI-1068 this was a bare literal (900) baked into
# run_panel's signature with no override knob, so a seat that could not meet a 900s deadline
# booked a fabricated abstention with no way to widen it — on mission-control the opus lane
# hit three consecutive timeouts in one afternoon. VNX_PLAN_GATE_SEAT_TIMEOUT is the knob, in
# the same env-var style as VNX_PANEL_RETRY; run_panel resolves to this when the caller does
# not pass an explicit timeout (CLI flag / direct kwarg).
DEFAULT_SEAT_TIMEOUT_SECONDS = 900


def _seat_timeout(explicit: "int | None" = None) -> int:
    """Resolve a panelist's per-seat deadline (``VNX_PLAN_GATE_SEAT_TIMEOUT``, default 900).

    Same env-var style as ``_panel_retry_count`` (``VNX_PANEL_RETRY``): a caller that already
    resolved a value — the CLI flag ``--seat-timeout`` — passes it as ``explicit`` and it wins
    outright (None means "no explicit value, read the env"). The env var, when set and a valid
    positive int, overrides the default; a malformed or non-positive value falls back to the
    default rather than silently widening the deadline to infinity or clamping a real value
    away. Returned value is always a positive int (>= 1).
    """
    if explicit is not None:
        return max(1, int(explicit))
    raw = os.environ.get("VNX_PLAN_GATE_SEAT_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_SEAT_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_SEAT_TIMEOUT_SECONDS
    return val if val >= 1 else DEFAULT_SEAT_TIMEOUT_SECONDS


def _dispatch_one(
    dispatcher: DispatcherFn, member: Dict[str, str], instruction: str, dispatch_id: str,
) -> PanelistResult:
    """Dispatch ONE panelist once and parse its verdict into a ``PanelistResult``.

    Best-effort and non-raising: a dispatch / report-read failure returns a non-scoring result
    (``dispatched=False``, ``error`` set); a returned report whose verdict block does not parse
    returns ``parse_error=True``. Both outcomes are RETRYABLE by ``run_panel`` before they fall
    through to the abstain path — a retry that itself errors just degrades to the same abstain.

    OI-1066: a returned report that carries the governance-synthesized marker
    (``SYNTHESIZED_REPORT_MARKER``) is a NO-VERDICT result — the lane RAN but the
    governance layer had to fabricate the report itself because the worker never
    authored one (a timeout, or no report file at all). This is a THIRD category,
    distinct from ``parse_error`` (a REAL report whose fence would not parse) and
    from a raised dispatch (``dispatched=False``): a synthesized seat saw the plan
    dispatched but never got a verdict back, so the seat must not be folded into
    "abstained" with a parse-flaked seat. A raised dispatch stays ``dispatched=False``
    (the 2026-06-24 undispatched-lane non-scoring behaviour), and ``parse_error``
    is left untouched for a real report whose fence would not parse (the 2026-06-24
    fail-safe stays exactly as-is). All three non-scoring flavors are retryable.
    """
    try:
        report_text = dispatcher(member["provider"], member["model_arg"], instruction, dispatch_id)
    except Exception as exc:  # vnx-silent-except: dispatch/report-read failure -> no verdict, recorded as abstain
        return PanelistResult(
            label=member["label"], provider=member["provider"],
            model=member.get("model_arg", ""),
            dispatched=False, error=str(exc), report_path=dispatch_id,
        )
    # OI-1066: detect the synthesized marker BEFORE parse_verdict. A fabricated
    # body has no verdict fence, so parse_verdict would otherwise return its
    # parse_error fail-safe and the seat would read as "answered but malformed" —
    # the very conflation this fix separates. The marker is the writer's own
    # machine-readable stamp, so an exact substring match is both sufficient and
    # stable (prose wording drifts; the marker does not).
    if SYNTHESIZED_REPORT_MARKER in (report_text or ""):
        return PanelistResult(
            label=member["label"], provider=member["provider"],
            model=member.get("model_arg", ""),
            verdict="revise", rationale="lane produced no verdict (synthesized report)",
            dispatched=True, parse_error=False, no_verdict=True,
            report_path=dispatch_id,
        )
    parsed = parse_verdict(report_text)
    # OI-839: on parse_error the raw lane output is the ONLY diagnostic that tells
    # us WHAT failed to parse (trailing comma, prose-bleed, nested fence). Keep it
    # on the result so _emit_seat_records can persist it into the hash-chained
    # seat ledger — previously it vanished with the temporary report file.
    return PanelistResult(
        label=member["label"], provider=member["provider"],
        model=member.get("model_arg", ""),
        verdict=parsed["verdict"], blocking_findings=parsed["blocking_findings"],
        rationale=parsed["rationale"], parse_error=parsed["parse_error"],
        dispatched=True, report_path=dispatch_id,
        raw_text=report_text if parsed["parse_error"] else "",
    )


def _find_repo_root(start: Path) -> Optional[Path]:
    """Walk ``start`` and its parents for a ``.git`` marker (dir or file).

    Subprocess-free mirror of ``project_root.resolve_project_root``'s git
    resolution: a worktree's ``.git`` is a FILE, a normal checkout's is a
    directory — ``exists()`` covers both. The canonical helper is not used here
    because it shells out through ``subprocess.run``, which the dispatcher-env
    tests mock and count (they assert exactly one subprocess call per panel).
    """
    for d in [start, *start.parents]:
        if (d / ".git").exists():
            return d
    return None


def _resolve_seat_ledger_path(data_dir: Optional[str]) -> Optional[Path]:
    """Resolve the per-seat verdict ledger path, or ``None`` with no repo anchor.

    The governed path (``planning_cli plan-gate run`` -> ``run_panel``) always
    passes ``data_dir``; the repo root is resolved by the ``.git`` marker so the
    seat ledger lands next to the ``plan-gates.ndjson`` evidence ledger under
    ``.vnx-attest/``. A caller with no ``data_dir`` (e.g. a test injecting a
    dispatcher) gets ``None`` and skips persistence unless it passes
    ``seat_ledger_path`` explicitly (OI-888).

    Resolution order is CWD-first (OI-1145): the panel runs with the governed
    project as its working directory, so ``Path.cwd()`` is the project root. A
    ``__file__``-first walk anchors on this file's own checkout instead — the
    fabric keystone in a central install — and lands the seat ledger in the
    read-only version tree, never the project. ``__file__`` remains only as the
    fallback for callers not running inside a project checkout.
    """
    if not data_dir:
        return None
    root = _find_repo_root(Path.cwd()) or _find_repo_root(Path(__file__).resolve().parent)
    if root is None:
        return None
    return root / SEAT_LEDGER_RELPATH


def _emit_seat_records(
    results: List[PanelistResult],
    *,
    track_id: str,
    project_id: str,
    seat_ledger_path: Optional[Path],
) -> None:
    """Append one append-only, hash-chained ``plan_gate_seat`` record per panelist.

    Best-effort and non-raising: seat persistence must never break the gate it
    hangs off (same contract as ``plan_gate_evidence.emit_plan_gate_pass``). Each
    record carries the panelist id, model, the effective verdict (``abstain`` for
    a non-scoring lane, ``no-verdict`` for a lane that never delivered — OI-1066),
    and whether a report was returned at all — the durable per-seat signal the
    effectiveness probe reads. Previously nothing survived beyond the single
    resolved ``plan_gate_pass`` record (OI-888).
    """
    if not seat_ledger_path or not results:
        return
    try:
        from ndjson_hash_chain import append_chained_entry  # noqa: PLC0415
        now = datetime.now(timezone.utc).isoformat()
        for result in results:
            # OI-1066: a no-verdict seat (lane timed out / synthesized / no file)
            # records its OWN effective verdict, distinct from ``abstain`` — so
            # historical analysis can separate "the model abstained" (it answered
            # but its fence would not parse) from "the lane was down" (it never
            # delivered). A scoring seat records its real verdict; a parse_error
            # seat records ``abstain`` (unchanged 2026-06-24 behaviour).
            if result.no_verdict:
                effective_verdict = "no-verdict"
            elif result.dispatched and not result.parse_error:
                effective_verdict = result.verdict
            else:
                effective_verdict = "abstain"
            record = {
                "type": SEAT_RECORD_TYPE,
                "track_id": track_id,
                "project_id": project_id,
                "panelist_id": result.label,
                "model": result.model,
                "verdict": effective_verdict,
                "responded": result.dispatched,
                "parse_error": result.parse_error,
                "no_verdict": result.no_verdict,
                "run_at": now,
            }
            # OI-839: carry the raw lane output on parse-error records so the
            # unparseable text is preserved in the durable, hash-chained ledger
            # for diagnosis — a later parser hardening can be built against the
            # REAL failure mode instead of a guessed one.
            if result.parse_error and result.raw_text:
                record["raw_output"] = result.raw_text
            append_chained_entry(seat_ledger_path, record)
    except Exception:  # vnx-silent-except: seat persistence must never break the gate
        return


def run_panel(
    doc_path: str | Path | None = None,
    *,
    doc_text: Optional[str] = None,
    track_id: str,
    project_id: str = "vnx-dev",
    panel: Optional[List[Dict[str, str]]] = None,
    dispatcher: Optional[DispatcherFn] = None,
    data_dir: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    seat_ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the plan-first panel over ``doc_path`` (or ``doc_text``) and return the verdict.

    Exactly one plan source is required: ``doc_text`` when the caller already
    holds the plan text (e.g. a track's ``goal_state`` standing in for the doc),
    otherwise the text is read from ``doc_path``. ``doc_text`` wins when both are
    given. ``dispatcher`` is injectable; when omitted the governed
    provider_dispatch dispatcher is used. Returns a dict with the overall
    ``decision`` (PASS|REVISE|BLOCK, or INFRA_FAIL when no lane produced a
    readable verdict — an infrastructure failure, not a plan judgment), the rule
    ``summary``, and per-panelist detail.

    ``timeout_seconds`` (OI-1068): the per-seat deadline a panelist has before its
    lane times out and the seat books a fabricated abstention. ``None`` (the
    default) resolves to ``VNX_PLAN_GATE_SEAT_TIMEOUT`` (default 900) via
    ``_seat_timeout`` — the same env-var-knob style as ``VNX_PANEL_RETRY``. An
    explicit kwarg (the ``--seat-timeout`` CLI flag) wins outright. Only the
    default dispatcher consumes it; an injected ``dispatcher`` ignores it.
    """
    panel = panel or DEFAULT_PANEL
    resolved_timeout = _seat_timeout(timeout_seconds)
    dispatcher = dispatcher or _make_default_dispatcher(data_dir, resolved_timeout)
    if doc_text is None:
        if doc_path is None:
            raise ValueError("run_panel: a plan source is required — pass doc_path or doc_text")
        doc_text = Path(doc_path).read_text(encoding="utf-8")
    doc_truncation = _doc_truncation_info(doc_text)
    instruction = build_plan_review_instruction(doc_text, track_id)

    retries = _panel_retry_count()

    results: List[PanelistResult] = []
    for member in panel:
        # One dispatch, plus up to `retries` retries when the lane flakes (a dispatch failure
        # or an unparseable verdict). The first SCORING verdict wins and short-circuits; if a
        # retry also flakes we keep its (still non-scoring) result, which then abstains via
        # apply_panel_rule. Never more than `retries` extra attempts — each is a fresh governed
        # dispatch id so the retry lands its own report -> receipt.
        result: PanelistResult
        for _ in range(retries + 1):
            did = f"plan-gate-{track_id}-{member['label']}-{uuid.uuid4().hex[:8]}"
            result = _dispatch_one(dispatcher, member, instruction, did)
            # A SCORING verdict: dispatched, no parse error, and NOT a no-verdict
            # synthesized report. OI-1066: a no-verdict seat (lane timed out /
            # governance-synthesized) is retryable too — the lane may deliver on
            # a fresh dispatch id — so the retry continues past it just as it
            # continues past a parse_error or a raised dispatch.
            if result.dispatched and not result.parse_error and not result.no_verdict:
                break  # readable verdict — no retry needed
        results.append(result)

    summary = apply_panel_rule(results)
    if seat_ledger_path is None:
        seat_ledger_path = _resolve_seat_ledger_path(data_dir)
    _emit_seat_records(
        results, track_id=track_id, project_id=project_id, seat_ledger_path=seat_ledger_path,
    )
    if doc_truncation["truncated"]:
        # The gate must never certify a verdict while silently hiding that it only read
        # PART of the plan — fold an explicit, quantified note into the rationale (the one
        # field every consumer — CLI text, --json, callers reading result["summary"]) already
        # surfaces, alongside the structured detail below for a caller that wants the exact
        # counts.
        pct = 100.0 * doc_truncation["kept_chars"] / doc_truncation["original_chars"]
        summary = {
            **summary,
            "rationale": (
                summary["rationale"]
                + f"; PLAN DOC TRUNCATED: gate read {doc_truncation['kept_chars']} of "
                f"{doc_truncation['original_chars']} chars ({pct:.1f}%) — verdict may be "
                "based on an incomplete plan"
            ),
        }
    return {
        "track_id": track_id,
        "project_id": project_id,
        "decision": summary["decision"],
        "summary": summary,
        "panelists": [r.__dict__ for r in results],
        "doc_truncation": doc_truncation,
    }


def build_decision_ref(
    decision: str,
    panelists: List[Dict[str, Any]],
    *,
    reports_base: str = "unified_reports",
    source: str = "plan-gate",
    set_at: Optional[str] = None,
) -> str:
    """Render the JSON ``decision_ref`` payload for a plan-gate round (OI-1190).

    ``panelists`` is ``run_panel``'s ``panelists`` list (the ``PanelistResult.__dict__``
    form). ``reports`` collects every panelist that actually delivered a report
    (``dispatched`` is True — a report file exists on disk); ``rejected_alternatives``
    collects the scoring block/revise panelists with their findings + rationale, so the
    reasons an approach was rejected survive on the track rather than only in the
    name-pattern-matchable report files. Returns a compact JSON string (the
    ``tracks.decision_ref`` payload written via ``tracks.set_decision_ref``).
    """
    reports: List[str] = []
    rejected: List[Dict[str, Any]] = []
    for p in panelists:
        report_path = (p.get("report_path") or "").strip()
        if p.get("dispatched") and report_path:
            path = report_path if report_path.endswith(".md") else f"{report_path}.md"
            reports.append(f"{reports_base}/{path}")
        # A rejected alternative is a SCORING block/revise verdict: the lane actually
        # reviewed the plan and gave concrete reasons. parse_error (fail-safe revise)
        # and no_verdict (synthesized) lanes saw no real verdict — no reason to record.
        if (
            p.get("dispatched")
            and not p.get("parse_error")
            and not p.get("no_verdict")
            and str(p.get("verdict", "")).lower() in ("block", "revise")
        ):
            rejected.append({
                "panelist": p.get("label", ""),
                "verdict": str(p.get("verdict", "")).lower(),
                "findings": list(p.get("blocking_findings") or []),
                "rationale": str(p.get("rationale", "")),
            })
    payload = {
        "reports": reports,
        "decision": decision,
        "rejected_alternatives": rejected,
        "set_at": set_at or datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    return json.dumps(payload, sort_keys=True)
