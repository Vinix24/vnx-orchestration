#!/usr/bin/env python3
"""plan_gate_panel.py — the PM-skill plan-first gate (governed worker path).

A feature's PLAN (not its code) is reviewed BEFORE any implementation. This
module runs that review:

    plan doc + rubric  ->  the default seat (codex), via its governed lane
                       ->  BLOCK? the escalation seat (opus) reads the same plan
                       ->  parse each seat's structured verdict
                       ->  a seat that reviewed in PROSE gets one cheap second
                           extraction on the same lane before any re-dispatch
                       ->  apply the panel pass/fail rule (PM-SKILL)
                       ->  PASS | REVISE | INFRA_FAIL (0 readable verdicts —
                           an infrastructure outcome, never a plan judgment)

The caller (``planning_cli plan-gate run``) resolves the ``OI-PLAN-<track>``
blocker on PASS, which — via ``track_reconciler`` — flips the track's
``derived_status`` away from ``blocked`` and lets ``deliverable promote`` proceed.

Panel composition (operator decision 2026-10-17, P2): ONE seat reads the plan,
the configured ``default_seat`` (codex). A second seat, ``escalation_seat``
(opus), reads the same plan in the same run when the first returns BLOCK, or
from the start when the plan's dispatch paths fall in the schema or security
class (P1, ``second_seat_from_start``). Both labels live in
``configs/plan_gate_panel.yaml``; ``--panel-seats`` replaces the composition for
one run (``compose_panel``).

The operator's provider switch (``provider_switch``) then removes every seat whose
provider family is switched off for the track's project, before anything is
dispatched (``apply_provider_switch``). A removed seat is named in the run output
and the decision text, is never booked, and nothing takes its place. Below the
floor of the seats the run was entitled to (``panel_floor``) the run refuses.

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
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import dispatch_worktree_isolation

# R6 (OI-2039): a seat whose report carries no verdict fence may still have its
# verdict in its final answer, the result event of its archived event stream.
# A claude seat's stream is archived when its run ends (_archive_seat_stream).
import event_store
import final_answer_verdict
import provider_switch

HERE =Path(__file__).resolve().parent
PROVIDER_DISPATCH = HERE / "provider_dispatch.py"
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

# OI-2066: the lane's own statement that its deadline cut a seat off. The same
# reader the review gates use, so a seat and a gate can never read a cut-off
# report differently.
from gate_lane_contract import lane_deadline_detail

# The three keys every seat declares. Kept as a tuple so the loader and its
# validation stay in lock-step — a drift here would silently change what counts
# as a well-formed seat.
_SEAT_KEYS: tuple[str, ...] = ("label", "provider", "model_arg")

# Minimum meaningful characters (whitespace-stripped) a track's goal_state must
# carry when `plan-gate run` is invoked WITHOUT --doc. This is an emptiness
# floor on the goal field ALONE, not a plannability test: the goal is combined
# with the track's deliverables (see planning_cli.resolve_plan_source) to form
# the reviewed plan text, and a track with zero deliverables is refused
# separately for that reason regardless of how thick the goal is.
# The value is overridable in configs/plan_gate_panel.yaml (`goal_min_chars`),
# never hardcoded at the call site — a fixed literal here would let the config
# drift silently (see load_goal_min_chars).
DEFAULT_GOAL_MIN_CHARS = 200

# Per-seat verdict ledger (OI-888): one append-only, hash-chained record per
# panelist per run, under the repo's .vnx-attest/ dir next to the plan_gate_pass
# evidence ledger. Read by the plan-gate effectiveness probe.
SEAT_LEDGER_RELPATH = ".vnx-attest/plan-gate-seats.ndjson"
SEAT_RECORD_TYPE = "plan_gate_seat"

# Claude is NOT a provider-lane provider — provider_dispatch refuses it. A claude seat
# runs on the HEADLESS lane: `claude -p` through the door's own ClaudeSubprocessAdapter,
# in the shared checkout (a plan review reads; it never commits). Billing stays on the
# Max SUBSCRIPTION — measured 2026-08-11 from the auth state (no ANTHROPIC_API_KEY /
# ANTHROPIC_BASE_URL, keychain subscriptionType: max) — and the single-entry door has
# routed claude through this lane by default since A2 (2026-08-26).
#
# Until 2026-09-07 these seats ran on the TMUX-SPAWN lane instead, on the premise that
# headless `claude -p` "bills API credits post-cutover". That premise was never true:
# Anthropic never carried out that cutover. And the lane cost the gate its claude
# verdict — bracketed-paste delivery handed the worker only the LAST LINE of the
# instruction ("...as the very last step."), three times on 2026-09-06, so the opus seat
# asked what it was supposed to do and then sat there until its deadline (906s, 907s,
# 2400s). Every plan-gate with a claude seat that day ended without that seat's verdict
# and was decided by the tiebreaker.
_CLAUDE_PROVIDERS = {"claude"}

# The lane label stamped on a claude seat's receipt — the same string the door's
# headless lane uses (dispatch_envelope.run_envelope_headless_plan), so a seat receipt
# and a door receipt name the same lane instead of two.
_HEADLESS_LANE = "claude_headless"

# The terminal every panel seat books its lane work under. The provider branch below
# passes it as --terminal-id, so seat receipts from both lanes land under one name rather
# than splitting the audit trail by lane. event_store keeps its live stream per run
# (OI-2115), so seats running at the same time never share one stream.
_PANEL_TERMINAL_ID = event_store.HARNESS_SEAT_TERMINAL

# The seat roster a run picks from: (label, provider string, model_arg), one per provider
# family (Anthropic / Moonshot / Zhipu / DeepSeek / OpenAI). A plan-gate run uses the
# default seat and, on BLOCK, the escalation seat (compose_panel); an operator names any
# other seats with --panel-seats.
# NOTE: a panelist that flakes (a down proxy, an uninstalled CLI, or an unparseable verdict) is
# RETRIED once (VNX_PANEL_RETRY, see run_panel) and, if it still yields no readable verdict,
# ABSTAINS as a non-scoring lane instead of vetoing — so a single down lane no longer forces a
# REVISE (apply_panel_rule's liveness quorum). Keep every provider here runnable anyway; the
# retry only rescues transient flakes. glm-harness requires the local litellm proxy on :4141.
def _codex_registry_default() -> str:
    from providers import provider_registry
    return provider_registry.CODEX_REGISTRY_DEFAULT


_DEFAULT_PANEL_SEATS: List[Dict[str, str]] = [
    {"label": "opus", "provider": "claude", "model_arg": "opus"},
    {"label": "kimi", "provider": "kimi", "model_arg": "kimi-k3"},
    {"label": "glm-5.2-harness", "provider": "glm-harness", "model_arg": "glm-5.2"},
    {"label": "deepseek", "provider": "deepseek-harness", "model_arg": "deepseek-v4-pro"},
    {"label": "codex", "provider": "codex", "model_arg": _codex_registry_default()},
    # gemini is intentionally omitted until the `gemini` CLI is installed: an unrunnable
    # panelist emits no verdict, which the fail-safe rule turns into an unconditional REVISE.
]


def resolve_codex_seat_models(seats: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Copy of *seats* with every codex seat that asks for the registry default resolved.

    The codex model is read from ``openai.default_model`` when the panel is built,
    never at import time and never from a name written in this module (OI-2062). A
    seat that names its own model keeps it. When the registry cannot give a model the
    seat keeps the marker and carries the resolver's message in ``model_error``:
    ``_dispatch_one`` then abstains that one seat under ``model_unresolved`` instead
    of the whole panel failing to build.
    """
    from providers import provider_registry
    resolved: List[Dict[str, str]] = []
    for seat in seats:
        seat = dict(seat)
        if seat["provider"] == "codex" and seat["model_arg"] == provider_registry.CODEX_REGISTRY_DEFAULT:
            try:
                seat["model_arg"] = provider_registry.resolve_codex_model()
            except provider_registry.CodexModelResolutionError as exc:
                seat["model_error"] = str(exc)
        resolved.append(seat)
    return resolved


def default_panel() -> List[Dict[str, str]]:
    """The shipped five-seat roster, with the codex model resolved from the registry."""
    return resolve_codex_seat_models(_DEFAULT_PANEL_SEATS)

VERDICT_FENCE = "vnx-plan-verdict"

# ---------------------------------------------------------------------------
# Seat status (OI-1434) — WHY a seat did or did not score, by name.
#
# Before this, an EMPTY report and a report carrying a full prose review with no
# verdict fence both came back as ``parse_error=True``. Only the free-text
# rationale ("empty report" vs "no verdict block found") told them apart, and the
# decision rule read neither: both fell into the same non-scoring "abstained"
# bucket. A seat that had reviewed the entire plan and written "Verdict: revise"
# in prose counted exactly like a seat that returned nothing at all.
#
# Measured on track ``review-gate-kimi-codex-glm`` (2026-08-22, runs at 10:31 and
# 10:35): "only 1 readable verdict(s) of 3 - below quorum" on a panel where two
# seats had in fact delivered a complete review — see
# ``tests/fixtures/plan_gate_prose_report_20260822.md``.
#
# ``seat_status`` is carried on ``PanelistResult``, named in the decision
# rationale, and persisted in the seat ledger, so "empty" and "unreadable" can
# never again share one word.
SEAT_SCORED = "scored"                                    # the seat's own report carried a readable verdict
SEAT_SCORED_VIA_REEXTRACTION = "scored_via_reextraction"  # readable only after the second extraction
SEAT_SCORED_VIA_FINAL_ANSWER = "scored_via_final_answer"  # the report had no fence; the run's final answer had a readable one (R6)
SEAT_NO_REPORT = "no_report"                              # the lane returned an empty body
SEAT_PROSE_NO_FENCE = "prose_no_fence"                    # real content, zero verdict fences
SEAT_UNPARSEABLE_FENCE = "unparseable_fence"              # a fence exists, no body parses into a verdict
SEAT_LANE_NO_ANSWER = "lane_no_answer"                    # governance synthesized the body (timeout/error/no file), or the lane's own deadline cut it off
SEAT_DISPATCH_FAILED = "dispatch_failed"                  # the dispatch call itself raised
SEAT_MODEL_UNRESOLVED = "model_unresolved"                # codex seat: the registry gave no model, nothing was dispatched (OI-2062)

# The statuses that COUNT toward the panel's quorum and its pass/fail arithmetic.
# Everything else is non-scoring. Membership is the single source of truth — the
# rule below asks this set, never a list of flags.
SCORING_SEAT_STATUSES: frozenset = frozenset({
    SEAT_SCORED, SEAT_SCORED_VIA_REEXTRACTION, SEAT_SCORED_VIA_FINAL_ANSWER,
})

SEAT_STATUSES: frozenset = frozenset({
    SEAT_SCORED, SEAT_SCORED_VIA_REEXTRACTION, SEAT_SCORED_VIA_FINAL_ANSWER,
    SEAT_NO_REPORT, SEAT_PROSE_NO_FENCE,
    SEAT_UNPARSEABLE_FENCE, SEAT_LANE_NO_ANSWER, SEAT_DISPATCH_FAILED,
    SEAT_MODEL_UNRESOLVED,
})

# The second-extraction deadline (OI-1434). Deliberately an order of magnitude
# below DEFAULT_SEAT_TIMEOUT_SECONDS: the re-extraction re-reads a report that
# already exists and emits one small JSON block — it does no review work, so a
# lane that cannot answer that in two minutes is not going to answer at all.
# Same env-knob style as VNX_PANEL_RETRY / VNX_PLAN_GATE_SEAT_TIMEOUT.
DEFAULT_REEXTRACTION_TIMEOUT_SECONDS = 120

# At most ONE re-extraction per seat per round. The budget is spent on the first
# prose-no-fence report and is NOT refilled by the retry: a lane that answers in
# prose twice has answered the question about its formatting.
REEXTRACTION_BUDGET_PER_SEAT = 1


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

    Falls back to ``default_panel()`` when the file is absent or unreadable (a
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
        return default_panel()
    try:
        import yaml  # noqa: PLC0415
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        warnings.warn(
            f"plan_gate_panel: failed to load {path}: {exc} — using the default panel",
            RuntimeWarning,
            stacklevel=2,
        )
        return default_panel()
    if not isinstance(loaded, dict) or "seats" not in loaded:
        warnings.warn(
            f"plan_gate_panel: {path} has no 'seats' list — using the default panel",
            RuntimeWarning,
            stacklevel=2,
        )
        return default_panel()
    raw_seats = loaded["seats"]
    if not isinstance(raw_seats, list) or not raw_seats:
        raise ValueError(
            f"plan_gate_panel: {path} 'seats' must be a non-empty list, got "
            f"{type(raw_seats).__name__}"
        )
    valid_providers = _valid_provider_strings()
    return resolve_codex_seat_models(
        [_validate_seat(s, i, valid_providers) for i, s in enumerate(raw_seats)]
    )


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
# Panel composition (operator decision 2026-10-17, P2): one seat, a second on BLOCK.
#
# Until P2 the governance variant sized the panel (0 to 3 seats) and every new
# feature got all five. The 17-10 measurement: codex alone landed on the panel's
# outcome in 35 of 36 runs, and codex does not draw on the Claude session limit.
# So a run has ONE default seat. The escalation seat reads the same plan only
# when the default seat returns BLOCK, the one verdict that says the plan is
# unsafe to build as written. The variant still lands in the trace and the
# round record; it no longer decides how many seats sit.
# ---------------------------------------------------------------------------
DEFAULT_SEAT_LABEL = "codex"
ESCALATION_SEAT_LABEL = "opus"


def panel_floor(entitled: int) -> int:
    """The readable verdicts a panel of ``entitled`` seats needs before it may certify.

    The one floor rule of the plan-gate: a multi-seat panel keeps two voices, a
    deliberate one-seat panel needs its one. ``apply_panel_rule`` applies it to the
    seats it judges, ``compose_panel`` to an operator's override, and
    ``apply_provider_switch`` to the seats a run was entitled to BEFORE a
    switched-off seat left, so a panel entitled to two or more seats never
    certifies on one voice.
    """
    return min(2, entitled)


@dataclass(frozen=True)
class PanelComposition:
    """The seats ONE plan-gate run uses, and the quorum its PASS needs.

    ``seats`` run first. ``escalation`` reads the same plan when a seat returns
    BLOCK; it is set only for the default composition, because an operator
    override runs exactly the seats it names. ``quorum`` is the number of
    readable verdicts a PASS needs: 1 for the default composition, so one
    readable PASS certifies even when a BLOCK brought a second seat in, and
    ``panel_floor(len(seats))`` for an override, the quorum the rule always had.

    ``second_seat`` (P1) names why the escalation seat sits from the start: a
    schema or security path (``second_seat_from_start``). Both seats are then
    in ``seats``, ``escalation`` is None, and the quorum is 2, because the
    second voice is the point.
    """
    seats: List[Dict[str, str]]
    escalation: Optional[Dict[str, str]]
    quorum: int
    chosen_by: str        # "default" | "operator"
    default_label: str    # the configured default seat, named in an override's trace
    second_seat: Optional[str] = None


def load_seat_roles(config_path: Optional[Path] = None) -> Dict[str, str]:
    """The ``default_seat`` and ``escalation_seat`` labels from ``configs/plan_gate_panel.yaml``.

    Same file and path resolution as ``load_panel_seats``. An absent file or key
    falls back to codex and opus. A present value that is not a non-empty
    string, or two roles naming the same seat, fails LOUD: a run that silently
    lost its escalation seat would let a BLOCK stand on one voice.
    """
    path = Path(config_path) if config_path is not None else _default_panel_config_path()
    loaded: Any = None
    if path.is_file():
        try:
            import yaml
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            loaded = None
    block = loaded if isinstance(loaded, dict) else {}
    roles: Dict[str, str] = {}
    for role, fallback in (("default", DEFAULT_SEAT_LABEL), ("escalation", ESCALATION_SEAT_LABEL)):
        raw = block.get(f"{role}_seat", fallback)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"plan_gate_panel: {path} {role}_seat must be a seat label, got {raw!r}"
            )
        roles[role] = raw.strip()
    if roles["default"] == roles["escalation"]:
        raise ValueError(
            f"plan_gate_panel: {path} default_seat and escalation_seat must be different "
            f"seats, both are {roles['default']!r}"
        )
    return roles


def compose_panel(
    requested_labels: Optional[List[str]] = None,
    *,
    config_path: Optional[Path] = None,
    dispatch_paths: Optional[List[str]] = None,
) -> PanelComposition:
    """The composition of one plan-gate run.

    Without ``requested_labels`` (no ``--panel-seats``): the default seat, the
    escalation seat for a BLOCK, quorum 1. When ``dispatch_paths`` fall in the
    schema or security class (``second_seat_from_start``), both seats sit from
    the start, quorum 2. With ``requested_labels``: exactly those seats in
    configured order, no escalation, quorum ``panel_floor(n)``. Both roles must name
    a configured seat whichever way the run goes, so a broken config surfaces on
    the next run, not on the first BLOCK.
    """
    seats = load_panel_seats(config_path)
    roles = load_seat_roles(config_path)
    configured = [s["label"] for s in seats]
    for role, label in roles.items():
        if label not in configured:
            raise ValueError(
                f"plan-gate panel: {role}_seat {label!r} is not a configured seat. "
                f"Configured labels: {configured}"
            )
    if requested_labels:
        chosen = filter_panel_seats(seats, requested_labels)
        return PanelComposition(
            seats=chosen, escalation=None, quorum=panel_floor(len(chosen)),
            chosen_by="operator", default_label=roles["default"],
        )
    default = filter_panel_seats(seats, [roles["default"]])[0]
    escalation = filter_panel_seats(seats, [roles["escalation"]])[0]
    reason = second_seat_from_start(dispatch_paths)
    if reason:
        return PanelComposition(
            seats=[default, escalation], escalation=None, quorum=2,
            chosen_by="default", default_label=roles["default"], second_seat=reason,
        )
    return PanelComposition(
        seats=[default], escalation=escalation,
        quorum=1, chosen_by="default", default_label=roles["default"],
    )


# ---------------------------------------------------------------------------
# The provider switch (provider-schakelaar D): a switched-off seat is left out.
#
# Nothing substitutes for a switched-off seat (no paid rerouting behind the
# operator's back). The seat is removed before anything is dispatched, named, and
# never booked, so it can never become a no-verdict seat that forbids PASS. The
# floor is ``panel_floor`` over the seats the run was entitled to, so leaving a
# seat out never lowers the bar a PASS has to clear.
# ---------------------------------------------------------------------------
SWITCH_CONSUMER = "plan_gate_panel"
SWITCHED_OFF_NOTE = "left out (provider switched off)"
ATTEST_HINT = (
    "clear the gate by operator attest "
    "(`vnx horizon plan-gate attest <track> --reason <text> --approval-id <id>`)"
)


def switched_off_entry(decision: provider_switch.SwitchDecision, **identity: str) -> Dict[str, str]:
    """One left-out seat as plain strings: it lands in the run's --json output."""
    return {
        **identity,
        "provider": decision.identifier,
        "family": decision.family,
        "source": decision.source,
        "scope": decision.scope,
        "detail": decision.detail(),
    }


def switched_off_note(left_out: List[Dict[str, str]]) -> str:
    """The one line that names every left-out seat, or "" when none was left out."""
    if not left_out:
        return ""
    names = ", ".join(
        f"{seat.get('label') or seat['provider']} (family={seat['family']} source={seat['source']})"
        for seat in left_out
    )
    return f"{SWITCHED_OFF_NOTE}: {names}"


def switch_remedy(left_out: List[Dict[str, str]]) -> str:
    """What the operator can do about the left-out seats: one step per distinct cause."""
    steps: List[str] = []
    for seat in left_out:
        family, source, scope = seat["family"], seat["source"], seat["scope"]
        if source in (provider_switch.SOURCE_OVERRIDE, provider_switch.SOURCE_OVERRIDE_INVALID):
            step = f"unset {provider_switch.OVERRIDE_ENV_PREFIX}{family.upper()}"
        elif source == provider_switch.SOURCE_UNREADABLE:
            step = "`vnx providers repair`"
        elif source == provider_switch.SOURCE_UNKNOWN_IDENTIFIER:
            step = f"map {seat['provider']!r} to its family in scripts/lib/provider_switch.py"
        elif source == provider_switch.SOURCE_PROJECT:
            step = f"`vnx providers on {family} --project {scope}`"
        else:
            step = f"`vnx providers on {family}`"
            if scope != provider_switch.SCOPE_GLOBAL:
                step += f" (or with `--project {scope}` for this project only)"
        if step not in steps:
            steps.append(step)
    return "; ".join(steps)


@dataclass(frozen=True)
class SwitchedPanel:
    """A composition after the provider switch.

    ``composition`` holds the seats that remain (an escalation seat that is switched
    off is gone too). ``left_out`` names every removed seat (``switched_off_entry``
    plus ``label`` and ``role``). ``entitled`` is the seat count before removal and
    ``floor`` is ``panel_floor(entitled)``. ``refusal`` is set when the run may not
    go: an operator's ``--panel-seats`` names a switched-off seat, or fewer seats
    remain than the floor.
    """
    composition: PanelComposition
    left_out: List[Dict[str, str]]
    entitled: int
    floor: int
    refusal: str = ""

    def note(self) -> str:
        return switched_off_note(self.left_out)


def _switch_refusal(composition: PanelComposition, kept: int, left_out: List[Dict[str, str]],
                    floor: int) -> str:
    """The refusal for a composition the switch shrank, or "" when the run may go."""
    seats = [seat["label"] for seat in left_out if seat["role"] == "seat"]
    if composition.chosen_by == "operator" and seats:
        head = f"--panel-seats names switched-off seat(s): {', '.join(seats)}"
    elif kept < floor:
        head = (f"the panel is entitled to {len(composition.seats)} seat(s) and needs {floor} "
                f"readable verdict(s), but {kept} remain after the provider switch")
    else:
        return ""
    details = "; ".join(f"{seat['label']}: {seat['detail']}" for seat in left_out)
    return (f"plan-gate refused before any dispatch: {head}. Left out: {details}. No seat was "
            f"dispatched and no round was booked; the track stays blocked. To go on: "
            f"{switch_remedy(left_out)}; or {ATTEST_HINT}.")


def apply_provider_switch(composition: PanelComposition, *,
                          project_id: Optional[str]) -> SwitchedPanel:
    """Remove every seat whose provider family is switched off for ``project_id``.

    ``project_id`` is the track's project. The escalation seat is checked too: when it
    is off, the run goes without one. The floor is ``panel_floor`` over the seats the
    run was entitled to (``composition.seats`` before removal), so a panel entitled to
    two or more seats never certifies on one voice; the remaining composition keeps
    its quorum for the same reason. Nothing is substituted. With no seat switched off
    the composition comes back unchanged.
    """
    entitled = len(composition.seats)
    floor = panel_floor(entitled)
    remaining: List[Dict[str, str]] = []
    left_out: List[Dict[str, str]] = []
    escalation = composition.escalation
    roles = [(seat, "seat") for seat in composition.seats]
    if escalation is not None:
        roles.append((escalation, "escalation"))
    for seat, role in roles:
        decision = provider_switch.decide(seat["provider"], consumer=SWITCH_CONSUMER,
                                          project_id=project_id)
        if not decision.allowed:
            left_out.append(switched_off_entry(decision, label=seat["label"], role=role))
            escalation = None if role == "escalation" else escalation
        elif role == "seat":
            remaining.append(seat)
    if not left_out:
        return SwitchedPanel(composition=composition, left_out=[], entitled=entitled, floor=floor)
    return SwitchedPanel(
        composition=replace(composition, seats=remaining, escalation=escalation),
        left_out=left_out, entitled=entitled, floor=floor,
        refusal=_switch_refusal(composition, len(remaining), left_out, floor),
    )


_OPEN_FENCE ="```" + VERDICT_FENCE
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
# into (the claude seat instead writes the doc to a temp file and never inlines it, so
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


# P2 (operator decision 2026-10-17): the plan-gate judges the architecture of the
# plan. Line-level and test-level review belongs to the builder and to codex in
# the PR review, where the code exists to check it against.
_RUBRIC = (
    "Judge the plan on four things:\n"
    "1. Scope: is the problem real and stated, and does the plan solve that problem "
    "and no more?\n"
    "2. PR split: is each deliverable a small, independently shippable PR, task_class "
    "tagged, in an order that works?\n"
    "3. Decisions and ADRs: are the design decisions named, does each decision that "
    "needs an ADR get one, and does a change to a central-DB table carry a composite "
    "key over project_id (ADR-007)?\n"
    "4. Feasibility: can it be built as written, are the real risks named with a "
    "mitigation, and is the quality floor per deliverable sane?\n\n"
    "Do not review line numbers or test details: the builder and codex check those in "
    "the PR review.\n"
    "Be a skeptic. Surface concrete, fixable gaps. Do not rubber-stamp.\n"
)


def build_plan_review_instruction(doc_text: str, track_id: str) -> str:
    """Render the plan-review instruction handed to each panelist (inline-doc form).

    Used by provider lanes (kimi, glm) where the instruction is passed as a
    subprocess argument and the full inline doc is acceptable.  The claude seat
    uses ``build_plan_review_instruction_fileref`` instead so the ~50k-char
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
    """Render the plan-review instruction for the claude seat.

    The plan doc is passed by FILE REFERENCE (not inlined) so the instruction
    string stays short. It was written for the tmux lane, where a ~50k-char body
    took >120s to ingest and tripped the WORK_START_GATE timeout; it survives the
    move to the headless lane because a short prompt argument is worth having
    there too — the seat reads the plan from disk with the tool it already has.

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
    """Render a generic (NON-plan-review) file-ref instruction for the claude seat.

    OI-811: ``_make_default_dispatcher`` is reused by callers whose instruction is NOT a
    plan review — e.g. the deliberation panel's diverge/contrarian/verify/synthesis
    stages. Wrapping their prompt in ``build_plan_review_instruction_fileref`` (which
    tells the worker "you are an independent plan reviewer... review the IMPLEMENTATION
    PLAN") caused a plan-reviewer-role worker to correctly reject the file as not a plan,
    corrupting the panel stage. This variant carries the file-ref benefit (a short
    instruction string, whatever the size of the referenced prompt) WITHOUT the
    plan-review framing — the worker is simply told to follow the referenced instruction
    and write its response to ``report_path``.
    """
    return (
        f"Read your complete instruction from this file:\n\n"
        f"  {doc_path}\n\n"
        "Follow it in full.\n\n"
        f"REPORT FILE (MANDATORY): Write your complete response to this exact file path:\n\n"
        f"  {report_path}\n\n"
        "Do NOT write to any other path. The caller reads only that file."
    )


def build_verdict_reextraction_instruction(report_text: str) -> str:
    """Render the SECOND-EXTRACTION instruction for a seat that reviewed in prose (OI-1434).

    The seat already did the expensive work: it read the plan and formed a judgment. What
    is missing is only the machine-readable block. So this instruction asks for exactly
    that and nothing else — no re-review, no new findings, no change of opinion. The seat's
    OWN report is the sole input.

    The report is passed through ``_sanitize_doc``, the same neutralization the untrusted
    plan doc gets. Two reasons, both load-bearing:

      - a fence in the input is disarmed, so the block ``parse_verdict`` reads back can only
        have come from the re-extraction itself and never be an echo of the input. On the
        live path the input provably carries no fence (that is what ``prose_no_fence``
        means), but the guard must not depend on the caller having checked — the moment this
        is reused for a seat whose report DOES carry a fence, the spoofing hole would be
        wide open;
      - the length cap keeps the instruction inside ARG_MAX on the provider lanes, which
        pass it as a subprocess argument.
    """
    safe = _sanitize_doc(report_text)
    return (
        "You already reviewed an implementation plan and wrote the review below. Your "
        "review is complete and CORRECT — this is not a request to review anything "
        "again.\n\n"
        "The only thing missing is the machine-readable verdict block. Read your own "
        "review and emit the block that matches the judgment you ALREADY reached.\n\n"
        "Rules:\n"
        "- Do NOT change your judgment, your findings, or your reasoning.\n"
        "- Do NOT re-read or re-review the plan.\n"
        "- Do NOT add anything outside the block: no preamble, no explanation, no closing "
        "line.\n"
        "- If your review concluded the plan is sound, the verdict is pass; if it named "
        "fixable gaps, revise; if it named a fundamental flaw, block.\n\n"
        "----- YOUR REVIEW -----\n"
        f"{safe}\n"
        "----- END REVIEW -----\n\n"
        "Output EXACTLY this fenced block and nothing else:\n"
        f"```{VERDICT_FENCE}\n"
        "{\n"
        '  "verdict": "pass" | "revise" | "block",\n'
        '  "blocking_findings": ["the concrete issues your review named", "..."],\n'
        '  "rationale": "one or two sentences, taken from your own review"\n'
        "}\n"
        "```\n"
    )


def _reextraction_timeout(explicit: "int | None" = None) -> int:
    """The second extraction's deadline (``VNX_PLAN_GATE_REEXTRACT_TIMEOUT``, default 120).

    Same resolution contract as ``_seat_timeout``: an explicit caller value wins outright,
    then the env var when it is a valid positive int, then the default. A malformed or
    non-positive value falls back to the default rather than widening the deadline to
    infinity or clamping a real value away.
    """
    if explicit is not None:
        return max(1, int(explicit))
    raw = os.environ.get("VNX_PLAN_GATE_REEXTRACT_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_REEXTRACTION_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_REEXTRACTION_TIMEOUT_SECONDS
    return val if val >= 1 else DEFAULT_REEXTRACTION_TIMEOUT_SECONDS


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

    OI-1434 — the result also carries a ``seat_status`` naming WHICH failure this was, because
    ``parse_error`` alone folded three unlike outcomes into one word:

      - ``no_report``        — the lane returned an empty body. Nothing was reviewed.
      - ``prose_no_fence``   — real content, zero verdict fences. The plan WAS reviewed; only
                               the machine-readable block is missing. This is the status
                               ``run_panel`` re-extracts from (``_reextract_verdict``) before
                               it pays for a full re-dispatch.
      - ``unparseable_fence``— a fence exists but no body parses into a valid verdict. The
                               tolerant repair pass above already ran and still failed, so a
                               re-extraction of the same text buys nothing; the seat falls
                               through to the retry.
      - ``scored``           — a readable verdict.

    The three failure rationales are unchanged verbatim; the status is the machine-readable
    half of the same statement.
    """
    empty = {"verdict": "revise", "blocking_findings": [], "rationale": "", "parse_error": True}
    if not report_text:
        return {**empty, "rationale": "empty report", "seat_status": SEAT_NO_REPORT}
    bodies = _iter_fence_bodies(report_text)
    if not bodies:
        return {
            **empty, "rationale": "no verdict block found",
            "seat_status": SEAT_PROSE_NO_FENCE,
        }

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
            "seat_status": SEAT_SCORED,
        }
    return {**empty, "rationale": last_reason, "seat_status": SEAT_UNPARSEABLE_FENCE}


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
    # OI-1434: the named reason this seat did or did not score — one of
    # SEAT_STATUSES. Every production path sets it (``_dispatch_one`` /
    # ``_reextract_verdict``). It stays "" for a result built by hand without one
    # (the effectiveness probe, backfill_track_decision_ref, older tests): an
    # empty status means "not classified", never a status of its own, and the
    # rationale then names the seat without a status suffix rather than guessing
    # which flavor of unreadable it was.
    seat_status: str = ""
    # The dispatch id of the second extraction, when one ran. Non-empty ONLY when
    # a re-extraction was attempted — its presence is the seat's provenance trail
    # from the resolved verdict back to the governed dispatch that produced it.
    reextraction_dispatch_id: str = ""
    # R6 (OI-2039): why the run's final answer was refused — the run did not end
    # normally, or its archive is not bound to this dispatch. Non-empty ONLY then.
    # A seat R6 refused keeps its booking and is never re-extracted: the second
    # extraction would hand a failed run a verdict R6 refused it.
    final_answer_refused: str = ""


def is_scoring(result: "PanelistResult") -> bool:
    """True when this seat's verdict counts toward the panel arithmetic.

    One definition, used by the rule, the retry loop, and the seat ledger, so
    "scoring" can never mean three slightly different things in three places.
    A seat with an explicit ``seat_status`` is judged by that status alone
    (``SCORING_SEAT_STATUSES``); a result built without one falls back to the
    original three flags.
    """
    if result.seat_status:
        return result.seat_status in SCORING_SEAT_STATUSES
    return result.dispatched and not result.parse_error and not result.no_verdict


def _labelled(results: List["PanelistResult"]) -> str:
    """``label (seat_status)`` per seat, comma-joined — the naming the rationale uses.

    A seat with no recorded status renders as the bare label, so the rationale
    never invents a status it does not have (and the pre-OI-1434 rationale text
    stays a prefix of the new one).
    """
    return ", ".join(
        f"{r.label} ({r.seat_status})" if r.seat_status else r.label for r in results
    )


def count_scoring_seats(panelists: List[Dict[str, Any]]) -> int:
    """How many seats in a ``run_panel`` result actually scored.

    Takes the ``panelists`` list in its ``PanelistResult.__dict__`` form (what
    ``run_panel`` returns and ``planning_cli`` holds) and counts the seats whose
    ``seat_status`` is in ``SCORING_SEAT_STATUSES``. This is the number
    ``plan_gate_tiebreaker.record_round`` persists as ``scored_seats``, so the
    stop-rule can tell a round that was READ from a round that merely RAN
    (OI-1280). A seat dict without a status falls back to the three flags, the
    same way ``is_scoring`` does.
    """
    return sum(1 for p in panelists if _panelist_scores(p))


def _panelist_scores(panelist: Dict[str, Any]) -> bool:
    """``is_scoring`` for a seat in its ``PanelistResult.__dict__`` form."""
    status = str(panelist.get("seat_status") or "")
    if status:
        return status in SCORING_SEAT_STATUSES
    return bool(
        panelist.get("dispatched")
        and not panelist.get("parse_error")
        and not panelist.get("no_verdict")
    )


def seat_verdicts_of(panelists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each seat's verdict on the plan it read, in the form a round record keeps (P2).

    Takes ``run_panel``'s ``panelists`` list. ``verdict`` is the seat's own verdict
    when it scored, ``no-verdict`` when its lane never delivered and ``abstain``
    otherwise: the words the seat ledger uses. The round record carries this so a
    later run on the same plan version can hand the tiebreaker the seat's verdict
    without running the seat again.
    """
    verdicts: List[Dict[str, Any]] = []
    for p in panelists:
        if _panelist_scores(p):
            verdict = str(p.get("verdict") or "")
        else:
            verdict = "no-verdict" if p.get("no_verdict") else "abstain"
        verdicts.append({
            "seat": str(p.get("label") or ""),
            "verdict": verdict,
            "findings": [str(f) for f in p.get("blocking_findings") or []],
            "rationale": str(p.get("rationale") or ""),
        })
    return verdicts


def _decision(decision: str, block: int, revise: int, passes: int, rationale: str) -> Dict[str, Any]:
    return {
        "decision": decision,
        "block_count": block,
        "revise_count": revise,
        "pass_count": passes,
        "rationale": rationale,
    }


def apply_panel_rule(
    results: List[PanelistResult], *, quorum: Optional[int] = None,
) -> Dict[str, Any]:
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

    OI-1434: every named lane in the rationale carries its ``seat_status`` in parentheses
    — ``glm-5.2-harness (prose_no_fence)`` reads differently from
    ``glm-5.2-harness (no_report)``, and an operator reading "below quorum" can now see
    whether the missing voices reviewed the plan and mis-formatted, or never delivered at
    all. The category a seat lands in is unchanged; only its silence now has a name.

    Over the SCORING (readable) lanes only:
    - infra floor: ZERO readable verdicts is NOT a plan judgment — the plan was never
      reviewed. That returns INFRA_FAIL (an infrastructure outcome, distinct from every
      content verdict) so a fleet-wide lane breakage can never read as an inhoudelijk
      REVISE ("revise the plan") for a plan no lane ever saw.
    - quorum: the readable verdicts a PASS needs. ``quorum`` when the caller passes one
      (P2: the default composition passes 1, so one readable PASS certifies even when a
      BLOCK brought the escalation seat in), else ``panel_floor(len(results))``: a
      multi-seat panel keeps two voices, a deliberate one-seat panel needs its one.
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
    scoring = [r for r in results if is_scoring(r)]
    no_verdict = [r for r in results if r.no_verdict]
    abstained = [r for r in results if not is_scoring(r) and not r.no_verdict]
    nv_note = (
        f"; no-verdict (timeout/no-report): {_labelled(no_verdict)}"
        if no_verdict else ""
    )
    ns_note = (
        f"; non-scoring (abstained): {_labelled(abstained)}"
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
    # 1-member panel needs only its one voice. The caller's composition decides when it
    # knows better than the result count (P2's default seat plus its escalation seat).
    required = quorum if quorum is not None else panel_floor(len(results))
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
    claude seat's report: unlike ``provider_dispatch``, the headless lane prints no
    ``Report:`` stderr line, so the opus seat's authored report is never found ->
    NO-VERDICT rc=1 "staging_validator: unstaged dispatch override" (#1102 class bug).
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


def _seat_checkout() -> Path:
    """The checkout a claude seat runs ``claude -p`` in.

    A plan review READS: the plan from the temp file the instruction names, and the
    repository it is reviewing against (git log/diff/show, grep — the whole bash
    allow-list of the plan-reviewer profile). It WRITES exactly one file, the verdict
    report, at an absolute path the instruction names. So the seat runs in the SHARED
    checkout: no ``git worktree add`` per seat (on a large repo that cost more than the
    review itself and timed the seat out), and the review is grounded against the real
    tree instead of a copy of it.

    Resolved from the INVOCATION context, never from this module's own location: in a
    central install the panel code sits under ``~/.vnx-system/versions/<v>/``, and a seat
    spawned there would review the keystone instead of the operator's project — the same
    trap ``_resolve_data_dir`` documents for the data dir.
    """
    env_root = os.environ.get("VNX_PROJECT_ROOT", "").strip()
    if env_root and Path(env_root).is_dir():
        return Path(env_root).resolve()
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    from project_root import resolve_project_root  # noqa: PLC0415
    return resolve_project_root()


def _seat_lane_outcome(result: Any) -> str:
    """One line naming what the lane did, stamped on the seat's synthesized receipt.

    ``dispatch_govern.ensure_receipt`` writes this into ``failure_reason``. The tmux
    lane's default there was a fixed literal ("tmux_receipt_deadline_exceeded") that
    claimed a deadline for every outcome including success; the headless lane holds the
    adapter result in-process, so the receipt can carry what actually happened.
    """
    if result.status == "success":
        return "claude_headless_completed"
    if result.status == "timeout":
        return f"claude_headless_deadline_exceeded (rc={result.returncode})"
    return (
        f"claude_headless_{result.status} (rc={result.returncode}): "
        f"{result.error or 'no error text from the lane'}"
    )


@contextmanager
def _seat_env_pins(base: Path) -> Iterator[None]:
    """The claude seat's env pins on ``os.environ`` for one run, restored exactly afterwards.

    Why on ``os.environ`` and not a child-only mapping: see
    ``_run_claude_headless_seat``. A key that did not exist before the run does
    not exist after it.
    """
    pins = {
        "VNX_DATA_DIR": str(base),
        "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_WORKER_SCOPED": "1",
    }
    restore = {key: os.environ.get(key) for key in pins}
    try:
        os.environ.update(pins)
        yield
    finally:
        for key, previous in restore.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _archive_seat_stream(base: Path, dispatch_id: str) -> Optional[Path]:
    """Archive a claude seat's completed event stream when its run ends (R6, OI-2039).

    The seat's adapter writes ``<base>/events/plan-gate/<dispatch_id>.ndjson``
    (its EventStore resolves the data dir while the pins are on; the stream is
    the run's own, OI-2115), and neither the adapter nor ``dispatch_govern``
    archives it. Before R6 the archive came into being only when the next
    dispatch on the terminal rotated the boundary, seconds after
    ``_seat_final_answer`` had looked for it, so a claude seat's final answer
    was never read. This is the teardown provider_dispatch and the
    door's headless envelope already run, done for the seat's own events dir
    (an explicit path, not the ambient env the pins were just taken off).

    A stream that holds another dispatch's event is left alone
    (``EventStore.archive_completed_stream``); the seat then reads as it did
    before: no archive, so the re-extraction runs.
    """
    return event_store.EventStore(events_dir=Path(base) / "events").archive_completed_stream(
        _PANEL_TERMINAL_ID, dispatch_id,
    )


def _run_claude_headless_seat(
    *,
    dispatch_id: str,
    model: str,
    instruction: str,
    role: str,
    base: Path,
    timeout_seconds: int,
) -> str:
    """Run ONE claude seat on the headless lane and return its report text.

    Reuses the door's building blocks rather than growing a second ``claude -p``
    wrapper: ``ClaudeSubprocessAdapter`` is the same adapter
    ``dispatch_envelope.run_envelope_headless_plan`` runs the door's headless dispatches
    through, and ``dispatch_govern.govern`` is the same GOVERN step the lanes share.
    What this does NOT reuse is the envelope around them — a permit-carrying
    ExecutionPlan, an isolated worktree, and push+PR enforcement are the shape of a
    dispatch that DELIVERS code, and a plan review delivers a verdict file.

    The env pins are set on ``os.environ`` for the duration of the run, not passed as a
    child-only mapping, because they have to bind in BOTH directions:
      - the child inherits them (``SubprocessAdapter.deliver`` builds the worker env
        from ``os.environ.copy()``), which is what OI-1153 asks for — the seat's report
        write path and ``_read_report``'s read-back base stay one directory;
      - ``VNX_WORKER_SCOPED=1`` is read HERE, in this process, while
        ``subprocess_adapter._build_worker_scope_args`` builds the argv. Without it the
        seat spawns with blanket ``--dangerously-skip-permissions`` instead of the
        plan-reviewer profile's read-only tool scope, which in a shared checkout is the
        difference between a reviewer and a writer.
    They are restored afterwards, so a seat never leaves a process-wide side effect
    behind for the next seat or for the caller.

    GOVERN always runs, exactly as the door's lanes do: the seat's report and receipt do
    not become optional because the worker went quiet. That is also what keeps the panel's
    three-way seat classification honest — when the worker authors nothing, govern writes
    the body carrying ``SYNTHESIZED_REPORT_MARKER`` and the seat reads back as
    ``no_verdict`` ("the lane never delivered") instead of as an abstention.

    Raises RuntimeError when not even govern left a readable report — the seat then books
    ``dispatched=False`` and the message carries the lane's own failure reason.
    """
    from envelope_adapters_claude import ClaudeSubprocessAdapter  # noqa: PLC0415
    from envelope_types import EnvelopeSpec  # noqa: PLC0415

    checkout = _seat_checkout()
    spec = EnvelopeSpec(
        dispatch_id=dispatch_id,
        terminal_id=_PANEL_TERMINAL_ID,
        provider="claude",
        model=model,
        instruction=instruction,
        role=role,
        pr_id=None,
        state_dir=base / "state",
        data_dir=base,
        deadline_seconds=timeout_seconds,
        # OI-1422: a seat is a read-only verdict worker, never a delivery worker.
        # Same task_class the provider branch stamps, for the same reason — it is the
        # fabric's canonical class for reviewer work and a member of
        # phantom_guard.REVIEW_TASK_CLASSES.
        task_class="research_structured",
    )

    start = datetime.now(timezone.utc)
    with _seat_env_pins(base):
        result = ClaudeSubprocessAdapter().run(spec, cwd=checkout)
    duration = (datetime.now(timezone.utc) - start).total_seconds()
    _archive_seat_stream(base, dispatch_id)

    from dispatch_govern import GovernRaw, GovernSpec, govern  # noqa: PLC0415
    govern(
        GovernSpec(
            dispatch_id=dispatch_id,
            terminal_id=_PANEL_TERMINAL_ID,
            instruction=instruction,
            data_dir=base,
            state_dir=base / "state",
            worktree_path=checkout,
            model=model,
            role=role,
            task_class=spec.task_class,
        ),
        # receipt=None is what arms ensure_receipt's fallback: a seat worker writes a
        # report, never its own receipt, so without this the seat's run would leave no
        # ledger row at all.
        GovernRaw(
            receipt=None,
            duration_seconds=duration,
            failure_reason=_seat_lane_outcome(result),
        ),
        lane=_HEADLESS_LANE,
    )

    report = _read_report(base, dispatch_id, "")
    if report is None:
        raise RuntimeError(
            f"no report for {dispatch_id} (rc={result.returncode} "
            f"status={result.status}): {result.error or 'no error text from the lane'} "
            f"| completion: {(result.completion_text or '')[-400:]}"
        )
    return report


def _seat_child_env(base: Path, total_deadline_seconds: Optional[int]) -> Dict[str, str]:
    """The environment ``_make_default_dispatcher`` hands a provider_dispatch child.

    A copy of this process's environment — never ``os.environ`` itself — with the
    data dir pinned (OI-1153, see the call site) and, when a caller passes one, the
    lane's own deadline as ``VNX_TOTAL_DEADLINE`` (OI-2066). Without a deadline the
    result is exactly the env the dispatcher built before OI-2066.
    """
    env = dict(os.environ)
    env["VNX_DATA_DIR"] = str(base)
    env["VNX_DATA_DIR_EXPLICIT"] = "1"
    if total_deadline_seconds is not None:
        env["VNX_TOTAL_DEADLINE"] = str(total_deadline_seconds)
    return env


def _run_provider_lane_child(
    cmd: List[str],
    *,
    env: Dict[str, str],
    timeout_seconds: int,
    dispatch_id: str,
) -> subprocess.CompletedProcess:
    """Run the ``provider_dispatch.py`` child and reap its worktree if it dies.

    The child creates its own isolated worktree and removes it in its own
    ``finally``.  A child killed for exceeding *timeout_seconds* (SIGKILL from
    ``subprocess.run``) never reaches that ``finally``, so the parent reaps the
    tree here — otherwise every timed-out gate seat leaks a
    ``dispatch-*-gate-*`` worktree under ``.vnx-data/worktrees/`` (measured
    2026-10-24: 11 such trees).  The reap runs whatever the child does and
    never alters the outcome: a ``TimeoutExpired`` still propagates to the
    caller after it.

    ``review_only=True`` is correct for every seat this dispatcher builds: they
    run with ``--no-auto-commit`` and materialize only a disposable PR
    reproduction, which ``remove_dispatch_worktree`` discards before
    classification — and only while HEAD still equals the recorded base, so a
    real local commit's branch is never discarded.  The claude branch never
    reaches this helper.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds,
            check=False, env=env,
        )
    finally:
        dispatch_worktree_isolation.remove_dispatch_worktree_if_present(
            dispatch_id, review_only=True,
        )


def _make_default_dispatcher(
    data_dir: Optional[str], timeout_seconds: int,
    *, role: str = "plan-reviewer", base_ref: Optional[str] = None,
    total_deadline_seconds: Optional[int] = None,
) -> DispatcherFn:
    """Real dispatcher: run a panelist through its governed lane, return the report text.

    INTERIM — PR-12 consolidation target. This dispatches WITHOUT going through the
    single-entry door (`vnx dispatch` / dispatch_bridge), which is a side door. Once the
    door is wired and flipped, this MUST route through that one door instead. The split
    below is exactly what the door decides internally:
      claude            -> headless lane, `claude -p` via ClaudeSubprocessAdapter, on
                           the SUBSCRIPTION (measured 2026-08-11; the door's default for
                           claude since A2, 2026-08-26). NOT provider_dispatch — it
                           refuses claude.
      kimi/glm/deepseek -> provider_dispatch (constraint-safe per provider).

    ``role``: OI-811 — this factory is reused by callers whose instruction is NOT a plan
    review (e.g. the deliberation panel's diverge/contrarian/verify/synthesis stages).
    Defaults to "plan-reviewer" for backward compatibility with ``run_panel``. A caller
    with a different role gets the generic (non-plan-framed) file-ref instruction on the
    claude seat, and that role is stamped on both lanes so govern() and the phantom-guard
    evaluate it correctly instead of being told it is a plan review.

    ``base_ref``: OI-1887 — threaded straight into ``provider_dispatch.py --base-ref``
    (which already threads it into ``create_dispatch_worktree``, see that module) so a
    review-gate caller reviewing a PR can isolate the provider's worktree on
    ``origin/<pr-branch>`` instead of the default ``origin/main``. None (the default)
    leaves the plan-gate and deliberation-panel callers unchanged: neither reviews a PR
    branch, so both keep isolating on main. Never applies to the claude branch below —
    that lane isolates via ``dispatch_worktree_isolation`` on its own dispatch_id and has
    no PR branch to isolate on either.

    ``total_deadline_seconds``: OI-2066 — the lane's OWN deadline, set as
    ``VNX_TOTAL_DEADLINE`` in the provider_dispatch child's environment (the drainer and
    the claude-harness adapter read it there). ``timeout_seconds`` stays the outer
    subprocess timeout; a review gate passes it with a margin on top so the lane's own
    deadline fires first and the lane writes its own report. Only the child env carries
    it — this process's ``os.environ`` is never touched. None (the default) leaves the
    plan-gate and deliberation-panel callers exactly as they were. The claude branch
    keeps running its seat with ``timeout_seconds`` as its deadline; no review gate
    routes through it.
    """
    base = _resolve_data_dir(data_dir)

    def _dispatch(provider: str, model_arg: str, instruction: str, dispatch_id: str) -> str:
        # OI-1153: pin the seat subprocess to the SAME data dir this dispatcher
        # resolved (`base`), or the lane re-resolves its own. provider_dispatch
        # honors the two-key VNX_DATA_DIR + VNX_DATA_DIR_EXPLICIT=1 contract (see
        # its _resolve_data_dir); without it the seat's report write path can land
        # outside the dir _read_report reads back from (e.g. the legacy
        # ~/.vnx-data/unified_reports root), and the seat report is silently
        # never found. The claude branch pins the same two keys — see
        # _run_claude_headless_seat, which sets them on os.environ because on that
        # lane they must bind in this process as well as in the child.
        env = _seat_child_env(base, total_deadline_seconds)
        _tmp_doc_path: Optional[str] = None
        try:
            if provider in _CLAUDE_PROVIDERS:
                # BUG-2 FIX (file-ref): the instruction already has the full plan doc inlined
                # by run_panel's build_plan_review_instruction call. For the claude seat we
                # replace it with a compact file-ref instruction so the ~50k-char body never
                # has to travel as one prompt argument.
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

                return _run_claude_headless_seat(
                    dispatch_id=dispatch_id,
                    model=model_arg,
                    instruction=claude_instruction,
                    role=role,
                    base=base,
                    timeout_seconds=timeout_seconds,
                )

            cmd = [
                sys.executable, str(PROVIDER_DISPATCH),
                "--provider", provider,
                "--terminal-id", _PANEL_TERMINAL_ID,
                "--dispatch-id", dispatch_id,
                "--model", model_arg,
                "--role", role,
                # OI-1422: every seat this dispatcher builds (plan-reviewer,
                # deliberation-panelist) is a read-only verdict/analysis worker —
                # it reads a doc and writes a report, it never touches repo files.
                # Without an explicit --task-class, provider_dispatch.py's
                # argparse default ("") falls through to VNX_TASK_CLASS, which is
                # unset here, so _build_frontmatter's own fallback stamps
                # task_class="implementation" on the receipt (provider_dispatch.py
                # _build_frontmatter). kimi_spawn.py's completion-vs-execution
                # fabrication guard (_finalize_kimi_result) keys off task_class
                # ALONE — it has no role parameter — so "implementation" leaves a
                # clean-worktree kimi seat classified as a delivery worker that
                # produced nothing, and the guard fires a false "completion
                # without execution" failure on real panel/review output.
                # "research_structured" is the fabric's own canonical task_class
                # for reviewer/architect/planner/security-engineer/data-analyst
                # work (dispatch_router.py SKILL_TO_TASK_CLASS) and is already a
                # member of phantom_guard.REVIEW_TASK_CLASSES, so this exempts the
                # seat through both guards without widening either SSOT.
                "--task-class", "research_structured",
                "--instruction", instruction,
                "--no-auto-commit",
            ]
            if base_ref:
                cmd += ["--base-ref", base_ref]
            proc = _run_provider_lane_child(
                cmd,
                env=env,
                timeout_seconds=timeout_seconds,
                dispatch_id=dispatch_id,
            )
            report = _read_report(base, dispatch_id, proc.stderr)
            if report is None:
                # provider_dispatch prints its actionable detail across both streams —
                # surfacing only stderr once masked the real cause behind an unrelated
                # last-stderr-line red herring for a full day.
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


# The two parse outcomes in which a report carries no verdict fence at all (R6). A
# report whose fence exists but will not parse is the model's own written block, and
# its booking (unparseable_fence) stands: the final answer never overrides it.
_NO_FENCE_STATUSES = frozenset({SEAT_NO_REPORT, SEAT_PROSE_NO_FENCE})


def _readable_plan_verdict(text: str) -> Optional[Dict[str, Any]]:
    """``parse_verdict`` of *text* when it reads a verdict, else None (R6's parser for a seat)."""
    parsed = parse_verdict(text)
    return None if parsed["parse_error"] else parsed


def _seat_final_answer(
    archive_base: Optional[Path], dispatch_id: str, report_text: str, parsed: Dict[str, Any],
) -> Optional[final_answer_verdict.FinalAnswerVerdict]:
    """The verdict of a seat's final answer when its report carries no fence (R6, OI-2039).

    Read with ``parse_verdict``, the parser a seat's report goes through, so a
    plan seat never reads a PR-review verdict. ``archive_base`` is the data dir
    the panel's lanes write under; without one there is nothing to read. A
    synthesized or deadline-cut report never gets here: ``_dispatch_one`` books
    those as ``lane_no_answer`` first, and ``recover_verdict`` refuses them as
    well.
    """
    if archive_base is None or parsed["seat_status"] not in _NO_FENCE_STATUSES:
        return None
    return final_answer_verdict.recover_verdict(
        archive_base, dispatch_id, report_text=report_text, parse=_readable_plan_verdict,
    )


def _dispatch_one(
    dispatcher: DispatcherFn, member: Dict[str, str], instruction: str, dispatch_id: str,
    archive_base: Optional[Path] = None,
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

    R6 (OI-2039): a report with no verdict fence is not the end of it when
    ``archive_base`` is given. The run's final answer is read
    (``_seat_final_answer``); a readable verdict there books the seat
    ``scored_via_final_answer``. A run R6 refuses keeps its booking and carries
    the reason in ``final_answer_refused``.
    """
    if member.get("model_error"):
        return PanelistResult(
            label=member["label"], provider=member["provider"],
            model=member.get("model_arg", ""),
            dispatched=False, error=member["model_error"], report_path=dispatch_id,
            seat_status=SEAT_MODEL_UNRESOLVED,
        )
    try:
        report_text = dispatcher(member["provider"], member["model_arg"], instruction, dispatch_id)
    except Exception as exc:  # vnx-silent-except: dispatch/report-read failure -> no verdict, recorded as abstain
        return PanelistResult(
            label=member["label"], provider=member["provider"],
            model=member.get("model_arg", ""),
            dispatched=False, error=str(exc), report_path=dispatch_id,
            seat_status=SEAT_DISPATCH_FAILED,
        )
    # OI-1066: detect the synthesized marker BEFORE parse_verdict. A fabricated
    # body has no verdict fence, so parse_verdict would otherwise return its
    # parse_error fail-safe and the seat would read as "answered but malformed" —
    # the very conflation this fix separates. The marker is the writer's own
    # machine-readable stamp, so an exact substring match is both sufficient and
    # stable (prose wording drifts; the marker does not).
    # OI-2066: a report whose frontmatter says the lane's own deadline stopped
    # the run is the same no-verdict seat, whatever fence it happens to hold —
    # the review never finished, so a partial verdict block in it never scores.
    no_answer = (
        "synthesized report" if SYNTHESIZED_REPORT_MARKER in (report_text or "")
        else lane_deadline_detail(report_text)
    )
    if no_answer is not None:
        return PanelistResult(
            label=member["label"], provider=member["provider"],
            model=member.get("model_arg", ""),
            verdict="revise", rationale=f"lane produced no verdict ({no_answer})",
            dispatched=True, parse_error=False, no_verdict=True,
            report_path=dispatch_id,
            seat_status=SEAT_LANE_NO_ANSWER,
        )
    parsed = parse_verdict(report_text)
    final_answer = _seat_final_answer(archive_base, dispatch_id, report_text, parsed)
    if final_answer is not None and final_answer.recovered:
        parsed = {**final_answer.verdict, "seat_status": SEAT_SCORED_VIA_FINAL_ANSWER}
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
        seat_status=parsed["seat_status"],
        final_answer_refused=(
            final_answer.detail if final_answer is not None and final_answer.refused else ""
        ),
    )


def _reextract_verdict(
    dispatcher: DispatcherFn,
    member: Dict[str, str],
    result: PanelistResult,
    dispatch_id: str,
) -> PanelistResult:
    """Second, cheap extraction of a verdict from a seat's OWN prose report (OI-1434).

    A ``prose_no_fence`` seat reviewed the whole plan and then wrote its judgment in
    sentences. The review exists; only the fence is missing. Rather than throw the review
    away — which is what counting the seat as non-scoring does — this hands the seat's own
    report back to the SAME lane with one job: emit the fenced block that belongs to this
    review, change nothing else.

    ORDER (deliberate, and pinned by test): this runs BEFORE ``run_panel``'s retry. The
    retry re-dispatches the entire seat — the plan, the rubric, a fresh full review at the
    full seat deadline. The re-extraction re-reads a report that already exists and asks
    for one small JSON block at a 120s deadline. Paying the expensive one first while the
    cheap one would have sufficed is pure waste, so the cheap one goes first and a success
    means the retry never fires at all.

    FAILURE CONTRACT — four ways this can fail, one outcome for all four: the seat is
    returned EXACTLY as it came in (``parse_error=True``, ``seat_status=prose_no_fence``,
    non-scoring). A second extraction never upgrades a seat except by producing a real,
    parseable verdict block:

      1. the extra dispatch raises (lane down, timeout, no report file);
      2. the answer is empty;
      3. the answer's frontmatter says the lane's own deadline cut it off (OI-2066);
      4. the answer parses no verdict — no fence, or a fence that will not read.

    Never a pass by default, never a fabricated verdict. The returned result carries
    ``reextraction_dispatch_id`` either way, so a failed attempt is as visible in the seat
    ledger as a successful one.

    ORDER AGAINST R6 (OI-2039, pinned by test): the final-answer fallback runs first,
    inside ``_dispatch_one``. A seat it scored is never re-extracted into another verdict,
    and a seat whose run it refused (``final_answer_refused``) is never handed a verdict
    here. Both come back untouched, with no extraction dispatched and none recorded.
    """
    if result.final_answer_refused or is_scoring(result):
        return result

    def _unchanged(note: str) -> PanelistResult:
        """Hand the seat back exactly as it came in, with the reason appended."""
        result.reextraction_dispatch_id = dispatch_id
        result.error = f"{result.error}; {note}" if result.error else note
        return result

    try:
        answer = dispatcher(
            member["provider"], member["model_arg"],
            build_verdict_reextraction_instruction(result.raw_text),
            dispatch_id,
        )
    except Exception as exc:  # vnx-silent-except: a failed re-extraction leaves the seat exactly as it was
        return _unchanged(f"verdict re-extraction failed: {exc}")
    if not (answer or "").strip():
        return _unchanged("verdict re-extraction returned an empty answer")
    # OI-2066: an answer the lane's own deadline cut off is no answer, even
    # when a fence in it would parse.
    deadline = lane_deadline_detail(answer)
    if deadline is not None:
        return _unchanged(f"verdict re-extraction was cut off: {deadline}")
    parsed = parse_verdict(answer)
    if parsed["parse_error"]:
        return _unchanged(
            f"verdict re-extraction produced no readable block ({parsed['rationale']})"
        )
    return PanelistResult(
        label=result.label, provider=result.provider, model=result.model,
        verdict=parsed["verdict"], blocking_findings=parsed["blocking_findings"],
        rationale=parsed["rationale"], parse_error=False,
        dispatched=True, no_verdict=False,
        # The seat's report path stays the ORIGINAL report: that is where the review
        # lives. The re-extraction id is carried separately so the provenance of the
        # VERDICT (a second governed dispatch) is never confused with the provenance of
        # the REVIEW.
        report_path=result.report_path,
        seat_status=SEAT_SCORED_VIA_REEXTRACTION,
        reextraction_dispatch_id=dispatch_id,
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
                # OI-1434: the NAMED reason, alongside the three booleans it
                # subsumes. Written on every record (empty string when the result
                # was built without one) so a later sweep can tell a same-schema
                # "not classified" from an older-schema record that never had the
                # field at all — the same missing-vs-empty contract record_round
                # holds for its governance fields.
                "seat_status": result.seat_status,
                "run_at": now,
            }
            # Provenance: this seat's verdict did not come from its own report but
            # from a second, governed extraction dispatch over the same lane. The
            # id is written whenever an extraction was ATTEMPTED — a failed attempt
            # is as much a fact about this seat as a successful one.
            if result.reextraction_dispatch_id:
                record["reextraction_dispatch_id"] = result.reextraction_dispatch_id
            # R6 (OI-2039): why the final-answer fallback refused this seat's run.
            if result.final_answer_refused:
                record["final_answer_refused"] = result.final_answer_refused
            # OI-839: carry the raw lane output on parse-error records so the
            # unparseable text is preserved in the durable, hash-chained ledger
            # for diagnosis — a later parser hardening can be built against the
            # REAL failure mode instead of a guessed one.
            if result.parse_error and result.raw_text:
                record["raw_output"] = result.raw_text
            append_chained_entry(seat_ledger_path, record)
    except Exception:  # vnx-silent-except: seat persistence must never break the gate
        return


def _with_truncation_note(summary: Dict[str, Any], doc_truncation: Dict[str, Any]) -> Dict[str, Any]:
    """*summary* with a quantified note in its rationale when the plan doc was truncated.

    The gate must never certify a verdict while silently hiding that it only read
    PART of the plan — fold an explicit, quantified note into the rationale (the one
    field every consumer — CLI text, --json, callers reading result["summary"]) already
    surfaces, alongside the structured detail ``run_panel`` returns for a caller that
    wants the exact counts.
    """
    if not doc_truncation["truncated"]:
        return summary
    pct = 100.0 * doc_truncation["kept_chars"] / doc_truncation["original_chars"]
    return {
        **summary,
        "rationale": (
            summary["rationale"]
            + f"; PLAN DOC TRUNCATED: gate read {doc_truncation['kept_chars']} of "
            f"{doc_truncation['original_chars']} chars ({pct:.1f}%) — verdict may be "
            "based on an incomplete plan"
        ),
    }


def _with_switched_off_note(summary: Dict[str, Any], left_out: List[Dict[str, str]]) -> Dict[str, Any]:
    """*summary* with the switched-off seats named in its rationale, the decision text
    every consumer surfaces. A verdict reached without a seat the operator switched off
    says so; it never reads as the full panel's verdict."""
    note = switched_off_note(left_out)
    if not note:
        return summary
    return {**summary, "rationale": f"{summary['rationale']}; {note}"}


def _run_seat(
    member: Dict[str, str],
    *,
    dispatcher: DispatcherFn,
    extraction_lane: Callable[[], DispatcherFn],
    instruction: str,
    track_id: str,
    archive_base: Optional[Path],
    retries: int,
) -> PanelistResult:
    """One seat's verdict: dispatch, re-extract a prose review, retry a flake.

    One dispatch, plus up to ``retries`` retries when the lane flakes (a dispatch
    failure or an unparseable verdict). The first SCORING verdict wins and
    short-circuits; if a retry also flakes its (still non-scoring) result is kept,
    which then abstains via apply_panel_rule. Each attempt is a fresh governed
    dispatch id so it lands its own report -> receipt.

    OI-1434: in FRONT of that retry sits the cheap step. A seat whose report is
    prose_no_fence has already done the review; only the block is missing.
    Re-extracting it costs one small call at a 120s deadline, where the retry
    costs a whole second review at the full seat deadline. So: dispatch ->
    (prose? re-extract) -> retry. The re-extraction budget is per SEAT per ROUND,
    not per attempt — the retry does not refill it (REEXTRACTION_BUDGET_PER_SEAT).
    """
    result: PanelistResult
    reextractions_left = REEXTRACTION_BUDGET_PER_SEAT
    for _ in range(retries + 1):
        did = f"plan-gate-{track_id}-{member['label']}-{uuid.uuid4().hex[:8]}"
        result = _dispatch_one(dispatcher, member, instruction, did, archive_base)
        # OI-1066: a no-verdict seat (lane timed out / governance-synthesized) is
        # retryable too — the lane may deliver on a fresh dispatch id.
        if is_scoring(result):
            break
        # R6: a seat whose run the final-answer fallback refused is not re-extracted.
        if (result.seat_status == SEAT_PROSE_NO_FENCE and reextractions_left > 0
                and not result.final_answer_refused):
            reextractions_left -= 1
            result = _reextract_verdict(extraction_lane(), member, result, f"{did}-reextract")
            if is_scoring(result):
                break  # the review was readable after all — the retry never fires
    return result


def _blocked(results: List[PanelistResult]) -> bool:
    """True when a seat that scored returned BLOCK: the one verdict that escalates (P2)."""
    return any(is_scoring(r) and r.verdict == "block" for r in results)


def second_seat_from_start(dispatch_paths: Optional[List[str]]) -> Optional[str]:
    """Why the escalation seat reads a plan from the start, or None.

    The second case for the second seat, next to a BLOCK (``_blocked``): a plan
    whose dispatch paths fall in the schema or security class
    (``smart_router.plan_scope_path_category``) gets both seats in its first
    round, and so does one with a path that leaves the repository, which no
    class can clear. Returns the class and the first path that put it there,
    normalised as it was classified.
    """
    import smart_router

    classes = {
        smart_router.PLAN_SCOPE_SCHEMA, smart_router.PLAN_SCOPE_SECURITY,
        smart_router.PLAN_SCOPE_OUTSIDE_REPO,
    }
    for path in dispatch_paths or []:
        category = smart_router.plan_scope_path_category(path)
        if category in classes:
            return f"{category} path {smart_router.normalize_dispatch_path(path)}"
    return None


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
    reextract_dispatcher: Optional[DispatcherFn] = None,
    escalation_seat: Optional[Dict[str, str]] = None,
    quorum: Optional[int] = None,
    switched_off_seats: Optional[List[Dict[str, str]]] = None,
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

    ``reextract_dispatcher`` (OI-1434): the lane the SECOND extraction runs on for a
    seat that reviewed in prose. When ``run_panel`` builds its own dispatcher it
    builds a second one at the much shorter re-extraction deadline
    (``VNX_PLAN_GATE_REEXTRACT_TIMEOUT``, default 120) — the extraction re-reads an
    existing report and emits one JSON block, so it has no business holding a
    900-second seat deadline. That second lane is built LAZILY, on the first seat
    that needs it, so a round in which every seat emits its fence builds exactly
    one lane as before. An INJECTED ``dispatcher`` is reused for both calls unless
    a separate one is passed: a test double has no deadline, and the two calls are
    told apart by their instruction and their dispatch id (``<seat-id>-reextract``),
    never by which lane object served them.

    ``escalation_seat`` and ``quorum`` (P2): ``planning_cli`` passes them from
    ``compose_panel``. When a seat in ``panel`` returns BLOCK, the escalation seat
    reads the same plan in the same run, before anything is decided, and the rule
    then decides over every verdict with ``quorum``. A REVISE does not escalate.
    Without them the run is exactly the seats in ``panel`` under the rule's own
    quorum, as before.

    ``switched_off_seats`` (provider switch): the seats ``apply_provider_switch``
    left out of this run. They are never dispatched and never booked; they are
    named in the rationale (the round's decision text) and returned under
    ``switched_off_seats``.
    """
    panel = panel or default_panel()
    resolved_timeout = _seat_timeout(timeout_seconds)
    caller_supplied_lane = dispatcher is not None
    dispatcher = dispatcher or _make_default_dispatcher(data_dir, resolved_timeout)

    # The extraction lane. An injected dispatcher serves both calls (a test double
    # has no deadline, and the two calls are told apart by their instruction and
    # dispatch id, never by which object served them). Otherwise it is built
    # LAZILY on the first seat that actually needs it: most rounds see no
    # prose_no_fence seat at all, and a lane nobody calls should not be built.
    reextract_lane: Optional[DispatcherFn] = reextract_dispatcher
    if reextract_lane is None and caller_supplied_lane:
        reextract_lane = dispatcher

    def _extraction_lane() -> DispatcherFn:
        nonlocal reextract_lane
        if reextract_lane is None:
            reextract_lane = _make_default_dispatcher(data_dir, _reextraction_timeout())
        return reextract_lane

    if doc_text is None:
        if doc_path is None:
            raise ValueError("run_panel: a plan source is required — pass doc_path or doc_text")
        doc_text = Path(doc_path).read_text(encoding="utf-8")
    doc_truncation = _doc_truncation_info(doc_text)
    instruction = build_plan_review_instruction(doc_text, track_id)

    retries = _panel_retry_count()
    # R6 (OI-2039): the data dir the seats' lanes write their event archives under.
    # Only an explicit one: the governed caller (planning_cli) always passes it.
    archive_base = Path(data_dir) if data_dir else None

    def _seat(member: Dict[str, str]) -> PanelistResult:
        return _run_seat(
            member, dispatcher=dispatcher, extraction_lane=_extraction_lane,
            instruction=instruction, track_id=track_id,
            archive_base=archive_base, retries=retries,
        )

    results = [_seat(member) for member in panel]
    if escalation_seat is not None and _blocked(results):
        results.append(_seat(escalation_seat))

    summary = apply_panel_rule(results, quorum=quorum)
    if seat_ledger_path is None:
        seat_ledger_path = _resolve_seat_ledger_path(data_dir)
    _emit_seat_records(
        results, track_id=track_id, project_id=project_id, seat_ledger_path=seat_ledger_path,
    )
    summary = _with_truncation_note(summary, doc_truncation)
    summary = _with_switched_off_note(summary, switched_off_seats or [])
    outcome = {
        "track_id": track_id,
        "project_id": project_id,
        "decision": summary["decision"],
        "summary": summary,
        "panelists": [r.__dict__ for r in results],
        "doc_truncation": doc_truncation,
    }
    if switched_off_seats:
        outcome["switched_off_seats"] = list(switched_off_seats)
    return outcome


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


def build_tiebreak_decision_ref(
    result: Dict[str, Any],
    *,
    previous_decision_ref: Optional[str],
    reports_base: str = "unified_reports",
    set_at: Optional[str] = None,
) -> str:
    """Render the JSON ``decision_ref`` payload for a tiebreaker outcome.

    A tiebreaker round resolves a plan blocker that a full panel round left
    stuck (REVISE/BLOCK with no PASS after ``max_rounds``). Without this
    builder, ``tracks.decision_ref`` keeps showing the last PANEL round's
    verdict forever — a reader who only checks ``decision_ref`` never sees
    that a tiebreaker actually cleared (or upheld) the gate.

    ``result`` carries the tiebreaker's own facts: ``outcome`` ("START" or
    "STOP"), ``report_path`` (the tiebreak report's own filename, with or
    without ``.md`` — omitted from ``reports`` when falsy, e.g. the report
    could not be located), and best-effort ``model`` / ``round`` /
    ``required_change`` / ``rationale`` (a historical reconstruction, such as
    the backfill, may not know all of them — an absent value renders as
    empty/``None`` rather than being guessed at).

    ``previous_decision_ref`` is the track's ``decision_ref`` BEFORE the
    tiebreaker ran (typically the superseded panel round's
    ``build_decision_ref`` payload). When present and parseable, its
    ``reports`` and ``rejected_alternatives`` are carried forward (appended
    after the tiebreak's own report) and its ``decision`` is recorded as
    ``superseded_decision`` — so the full history (which panel reports were
    read, which alternatives were rejected, and what round finally decided)
    survives on the track. An absent or unparseable ``previous_decision_ref``
    is never guessed at: the payload then carries only the tiebreak's own
    report and ``superseded_decision: None``.

    Returns a compact JSON string (the ``tracks.decision_ref`` payload written
    via ``tracks.set_decision_ref``).
    """
    reports: List[str] = []
    report_path = str(result.get("report_path") or "").strip()
    if report_path:
        path = report_path if report_path.endswith(".md") else f"{report_path}.md"
        reports.append(f"{reports_base}/{path}")

    rejected: List[Dict[str, Any]] = []
    superseded_decision: Optional[str] = None
    if previous_decision_ref:
        try:
            prev = json.loads(previous_decision_ref)
        except (json.JSONDecodeError, ValueError, TypeError):
            prev = None
        if isinstance(prev, dict):
            superseded_decision = prev.get("decision")
            prev_reports = prev.get("reports")
            if isinstance(prev_reports, list):
                reports.extend(str(r) for r in prev_reports)
            prev_rejected = prev.get("rejected_alternatives")
            if isinstance(prev_rejected, list):
                rejected.extend(prev_rejected)

    outcome = str(result.get("outcome") or "").strip()
    payload = {
        "decision": f"tiebreak:{outcome}",
        "reports": reports,
        "rejected_alternatives": rejected,
        "superseded_decision": superseded_decision,
        "tiebreaker_model": result.get("model") or "",
        "round": result.get("round"),
        "required_change": result.get("required_change") or "",
        "rationale": result.get("rationale") or "",
        "set_at": set_at or datetime.now(timezone.utc).isoformat(),
        "source": "plan-gate-tiebreak",
    }
    return json.dumps(payload, sort_keys=True)
