"""Plan-first-gate enforcement (defense-in-depth, advisory-first).

The plan-first gate seeds a synthetic ``OI-PLAN-<track>`` blocker on a track so it
is "born plan-gated" (``planning_cli._seed_plan_blocker``). Until now that blocker
only gated CLOSURE bookkeeping — ``track_reconciler.close_track_if_done`` revalidates
it at close-time — but never the WORK: neither the dispatch door nor the merge gate
consulted it, so a track could be dispatched and its PR merged without the plan gate
ever passing (build-before-plan). Both enforcement points call the read-only check
here so the rule lives in exactly one place.

Flag ``VNX_PLAN_GATE_ENFORCE`` (off | advisory | required), default ``advisory``:
  - ``off``      : no check (an unknown value also fails safe to off).
  - ``advisory`` : check + surface a WARN, never block (default; mirrors the
                   evidence-bound-gate D3 rollout in ``evidence_bound_gate.py``).
  - ``required`` : block when the plan gate is unresolved.

Operator override ``VNX_OVERRIDE_PLAN_GATE=1`` forces a pass in ``required`` mode; the
caller records ``override_applied`` so the deviation stays in the audit trail (never
silent) — the same discipline as the ADR-027 signed gate-override.

This module is deliberately dependency-free (stdlib + sqlite only): the door
constructs a ``ConstraintVerdict`` from the state, the merge gate constructs its own
gate-shaped result, but both share this one truth.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Iterable

PLAN_OI_PREFIX = "OI-PLAN-"

# plan-gate state discriminators
PASSED = "passed"            # no unresolved OI-PLAN blocker (gate passed, or never seeded)
UNRESOLVED = "unresolved"    # an OI-PLAN-<track> 'blocks' row with resolved_at IS NULL
UNSUPPORTED = "unsupported"  # schema predates the resolvable-blocker columns; cannot enforce

_ENFORCE_MODES = {"off", "advisory", "required"}
_TRUTHY = {"1", "true", "yes", "on"}


def plan_blocker_oi(track_id: str) -> str:
    """The synthetic open-item id for a track's plan-first gate."""
    return f"{PLAN_OI_PREFIX}{track_id}"


def enforce_mode() -> str:
    """Resolve ``VNX_PLAN_GATE_ENFORCE`` to off/advisory/required (default advisory).

    Precedence: the process env var (an explicit per-session override) wins; then the
    persisted config-plane value (``project_config`` via ``config_runtime`` — the same
    surface ``/operator/config`` flips, audited + revertible); then the ``advisory``
    default. An unknown value fails safe to ``off`` — enforcement never turns on by
    accident. The config lookup is best-effort (fail-soft): a missing store leaves the
    env/default behaviour unchanged.
    """
    raw = os.environ.get("VNX_PLAN_GATE_ENFORCE")
    if not raw:
        try:
            import config_runtime  # noqa: PLC0415
            raw = config_runtime.get("VNX_PLAN_GATE_ENFORCE")
        except Exception as exc:  # vnx-silent-except: UNREADABLE config -> log + fail-soft to advisory
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning(
                "plan_gate_enforcement: VNX_PLAN_GATE_ENFORCE config read failed "
                "(falling back to advisory, fail-soft direction): %s", exc
            )
            raw = None
    raw = (raw or "advisory").strip().lower()
    return raw if raw in _ENFORCE_MODES else "off"


def override_active() -> bool:
    """True when the operator set ``VNX_OVERRIDE_PLAN_GATE`` to an affirmative value."""
    return (os.environ.get("VNX_OVERRIDE_PLAN_GATE") or "").strip().lower() in _TRUTHY


# --- Scope read-site (VNX_PLAN_GATE_COMPLEX_ONLY) ----------------------------
#
# The operator's dividing line for plan-gate scope (2026-08-08): a plan is HEAVY
# when it touches the dispatch-lane, store-resolution, a review-gate, or a
# central-DB schema (ADR-007); LIGHT in all other cases. Signals are derived
# from data that already exists on the plan — per-deliverable task_class and
# complexity tags, and the file paths it names — never a new manual scope field.
# Fail-closed: a plan we cannot judge resolves to HEAVY, never LIGHT.

HEAVY = "heavy"
LIGHT = "light"

# The reduced panel a LIGHT-scope plan runs when VNX_PLAN_GATE_COMPLEX_ONLY is
# on: two diverse families (Anthropic + Moonshot), the smallest panel that can
# still certify a pass (apply_panel_rule needs >= 2 readable verdicts). Matches
# the observed 2-seat convergences on main; an unknown label fails LOUD in
# filter_panel_seats, so a config drift can never silently shrink the panel.
LIGHT_PANEL_LABELS: tuple[str, ...] = ("opus", "kimi")

# Substrings for the four heavy domains, matched case-insensitively against the
# plan text + the paths it names. Deliberately module/area-specific: a generic
# plan (add an endpoint, tweak a view) hits none of them and reads LIGHT.
_HEAVY_MARKERS: tuple[str, ...] = (
    # dispatch-lane
    "dispatch_cli",
    "provider_dispatch",
    "tmux_interactive_dispatch",
    "subprocess_dispatch",
    "dispatch_bridge",
    "dispatch-lane",
    "dispatch lane",
    "routing_policy",
    "smart_router",
    "dispatch_spec",
    # store-resolution
    "resolve_state_dir",
    "resolve_data_dir",
    "resolve_central_data_dir",
    "project_root",
    "vnx_paths",
    "central store",
    "vnx_data_dir",
    # review-gate
    "review-gate",
    "review_gate",
    "review_floor",
    "review-floor",
    "evidence_bound_gate",
    "codex_gate",
    "gemini_review",
    "verify_pr",
    "merge gate",
    "merge-gate",
    "plan_gate_evidence",
    "phantom_guard",
    # central-DB schema (ADR-007)
    "adr-007",
    "central-db",
    "central_db",
    "track_open_items",
    "runtime_coordination.db",
    "schema migration",
    "schema change",
    "composite key",
    "unique constraint",
)

# task_class values that are HEAVY by construction: a code-review plan IS a
# review-gate touchpoint, the first of the four domains. The other classes are
# judged by what the plan actually names, not by their label.
_HEAVY_TASK_CLASSES: frozenset[str] = frozenset({"02_code_review"})

# Per-deliverable complexity levels that force HEAVY even with no domain
# marker — a plan explicitly rated complex keeps the full panel.
_HEAVY_COMPLEXITY: frozenset[str] = frozenset({"high", "critical", "complex"})

# The `[^\n:=]*` between the label and the separator tolerates markdown bold
# (``**Task class**: 02_code_review``) while never crossing a line break — a tag
# belongs to one deliverable line. ``[:=]`` is required so a bare mention of the
# word "complexity" in prose does not capture a value.
_TASK_CLASS_RE = re.compile(r"(?i)\btask[\s_-]?class\b[^\n:=]*[:=]\s*(\d{2}_[a-z_]+)")
_COMPLEXITY_RE = re.compile(r"(?i)\bcomplexity\b[^\n:=]*[:=]\s*([a-z][a-z0-9]*)")


def plan_gate_scope(
    plan_text: "str | None" = None,
    *,
    task_class: "str | None" = None,
    complexity: "str | None" = None,
    paths: "Iterable[str] | None" = None,
) -> str:
    """Classify a plan as HEAVY or LIGHT for the plan-first gate.

    Heavy when the plan touches the dispatch-lane, store-resolution, a
    review-gate, or a central-DB schema (ADR-007); light in all other cases.
    Derivation order: an explicit heavy task_class/complexity, then domain
    markers in the plan text and the paths it names, then inline per-deliverable
    tags in the doc.

    Fail-closed: a plan we cannot judge (no text, no task_class, no complexity,
    no paths) resolves to HEAVY — a silent fallback to the cheap panel would
    downgrade exactly the plans we failed to size.
    """
    if (task_class or "").strip().lower() in _HEAVY_TASK_CLASSES:
        return HEAVY
    if (complexity or "").strip().lower() in _HEAVY_COMPLEXITY:
        return HEAVY

    text = (plan_text or "").strip()
    path_text = "\n".join(p for p in (paths or []) if p)
    haystack = "\n".join(t for t in (text, path_text) if t).lower()

    # Fail-closed: with NO signal at all (no plan text, no touched paths, no
    # explicit complexity rating) we cannot judge -> HEAVY. task_class alone does
    # not count: 01_code_generation is the classifier's catch-all default and is
    # no evidence of lightness. An explicit non-heavy complexity rating IS a
    # signal — the caller sized it, so fall through to LIGHT absent any marker.
    if not haystack and not (complexity or "").strip():
        return HEAVY

    for marker in _HEAVY_MARKERS:
        if marker in haystack:
            return HEAVY

    for tag in _TASK_CLASS_RE.findall(text):
        if tag.lower() in _HEAVY_TASK_CLASSES:
            return HEAVY
    for tag in _COMPLEXITY_RE.findall(text):
        if tag.lower() in _HEAVY_COMPLEXITY:
            return HEAVY

    return LIGHT


def complex_only_active() -> bool:
    """True when VNX_PLAN_GATE_COMPLEX_ONLY is set to an affirmative value.

    Precedence mirrors enforce_mode: the process env var (a per-session
    override) wins; then the persisted config-plane value via config_runtime
    (the ``/operator/config`` surface, audited + revertible); then the registry
    default (off). The config lookup is best-effort, but best-effort has TWO
    cases that must not be conflated: a NOT-SET flag and a UNREADABLE one.

    - NOT-SET (the flag is simply absent, or the store is missing): the
      config-plane returns False on its own and the lookup stays silent —
      this is the legitimate default, the behaviour an un-set flag always had.
      A missing store leaves the env/default behaviour unchanged.
    - UNREADABLE (the import or the read itself raised — an import fault, a
      broken row the façade did not catch): the fallback is still False, so
      the fail-safe direction is preserved (False = the FULL panel = more
      review, never less), but it is LOGGED. An operator who turns the flag
      on via /operator/config and hits a config-plane fault would otherwise
      see everything keep running heavy with no signal that the read failed —
      hunting the scope classifier while the fault is in the config read.

    The same loud-on-failure discipline the plan-gate-pass record adopted
    (commit 9e7ad79d: a control that cannot fail reported every dropped record
    as success); this is the second read-site of that class in this file pair.
    """
    raw = os.environ.get("VNX_PLAN_GATE_COMPLEX_ONLY")
    if raw:
        return raw.strip().lower() in _TRUTHY
    try:
        import config_runtime  # noqa: PLC0415
        return bool(config_runtime.get_bool("VNX_PLAN_GATE_COMPLEX_ONLY"))
    except Exception as exc:  # vnx-silent-except: UNREADABLE config -> log + fail-closed to False (full panel)
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "plan_gate_enforcement: VNX_PLAN_GATE_COMPLEX_ONLY config read failed "
            "(falling back to False = full panel, fail-safe direction): %s", exc
        )
        return False


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        is not None
    )


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def plan_gate_state(db_path: "str | Path", track_id: str, project_id: str) -> str:
    """Return ``PASSED`` / ``UNRESOLVED`` / ``UNSUPPORTED`` for a track's plan-first gate.

    Records every evaluation to the observe-only guard-fired counter
    (guard_stats; fired == UNRESOLVED) so a plan gate that never blocks for
    weeks is visible as a statistic instead of vanishing into logs. The
    counter wraps the return value and can never alter it.

    Read-only URI connection: a missing DB file raises immediately rather than
    silently creating an empty one (callers degrade any exception to a WARN — never
    crash the door). ``UNSUPPORTED`` when the schema lacks ``track_open_items`` or its
    ``resolved_at`` column — the same predicate ``planning_cli._plan_gate_supported``
    guards SEED with, so a DB that could never CLEAR a blocker is never enforced against.

    Only the ``OI-PLAN-<track>`` blocker counts here; other unresolved ``blocks``
    open-items are the closure gate's concern, not the plan-first gate's.
    """
    state = _plan_gate_state_decision(db_path, track_id, project_id)
    try:
        import logging  # noqa: PLC0415

        import guard_stats  # noqa: PLC0415

        guard_stats.record_guard_evaluation(
            "plan_gate_state",
            state == UNRESOLVED,
            detail={"track_id": track_id, "state": state},
        )
    except Exception as exc:  # noqa: BLE001 — observe-only: never break the gate on the counter
        logging.getLogger(__name__).warning(
            "plan_gate_enforcement: guard-fired counter failed (state unchanged): %s", exc
        )
    return state


def _plan_gate_state_decision(db_path: "str | Path", track_id: str, project_id: str) -> str:
    """The read-only plan-gate decision (see ``plan_gate_state`` for the contract)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        if not (_has_table(conn, "track_open_items") and _has_col(conn, "track_open_items", "resolved_at")):
            return UNSUPPORTED
        oi = plan_blocker_oi(track_id)
        if _has_col(conn, "track_open_items", "project_id"):
            row = conn.execute(
                "SELECT 1 FROM track_open_items "
                "WHERE track_id=? AND project_id=? AND oi_id=? "
                "AND link_type='blocks' AND resolved_at IS NULL LIMIT 1",
                (track_id, project_id, oi),
            ).fetchone()
        else:  # pre-0024 DB: no tenant column
            row = conn.execute(
                "SELECT 1 FROM track_open_items "
                "WHERE track_id=? AND oi_id=? AND link_type='blocks' "
                "AND resolved_at IS NULL LIMIT 1",
                (track_id, oi),
            ).fetchone()
        return UNRESOLVED if row is not None else PASSED
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plan-gate review disposition — the "blocked" refinement
# ---------------------------------------------------------------------------
#
# A single ``blocked`` state hides two facts under one name: a track the
# plan-first gate has NEVER reviewed (queue depth) and a track the gate reviewed
# and REFUSED (a finding with a reason). Both have an open ``OI-PLAN-<track>``
# blocker; the difference is whether a plan-gate decision is on record. That
# difference is DERIVED from the durable decision record — ``tracks.decision_ref``
# (OI-1190), written by ``plan_gate_panel.build_decision_ref`` for every panel
# outcome — never stored as a second flag, so the disposition can never drift
# out of sync with the gate records.

UNREAD = "unread"        # OI-PLAN blocker open; no refusal on record (never reviewed)
REFUSED = "refused"      # OI-PLAN blocker open; the recorded decision was REVISE/BLOCK
CLEARED = "cleared"      # no open OI-PLAN blocker (passed/attested/derived, or never seeded)

# Panel decisions that are a refusal WITH a reason on record. INFRA_FAIL is
# deliberately NOT a refusal: no lane produced a readable verdict, so the plan
# was never actually reviewed and still reads UNREAD. Pre-panel refusals
# (too-thin goal, zero deliverables) write no decision_ref at all and therefore
# also read UNREAD — correct, because no panel looked at the plan.
_REFUSAL_DECISIONS = frozenset({"revise", "block"})


def _decision_ref_is_refusal(decision_ref: "str | None") -> bool:
    """True when a track's ``decision_ref`` records a refused (REVISE/BLOCK) decision.

    ``decision_ref`` is the JSON payload ``plan_gate_panel.build_decision_ref``
    writes onto the track for every panel outcome. An empty or unparseable value
    is not a refusal — fail-safe to "not refused" so a corrupt record can never
    mint a finding the panel did not record.
    """
    if not decision_ref:
        return False
    try:
        payload = json.loads(decision_ref)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return str(payload.get("decision", "")).strip().lower() in _REFUSAL_DECISIONS


def classify_review_state(
    *,
    open_plan_blocker: bool,
    decision_ref: "str | None",
) -> str:
    """Map the two plan-gate facts to a review disposition (pure, no DB).

    ``open_plan_blocker`` is whether the track has an OPEN OI-PLAN blocker;
    ``decision_ref`` is the track's durable plan-gate decision record (``None``
    on a store predating migration 0033, or when the gate never ran). This
    mapping is the whole derivation — nothing is stored, so the disposition can
    never diverge from the records it reads.
    """
    if not open_plan_blocker:
        return CLEARED
    if _decision_ref_is_refusal(decision_ref):
        return REFUSED
    return UNREAD


def plan_gate_review_state(db_path: "str | Path", track_id: str, project_id: str) -> str:
    """Return ``UNREAD`` / ``REFUSED`` / ``CLEARED`` / ``UNSUPPORTED`` for a track.

    Read-only (``mode=ro``) over the same DB ``plan_gate_state`` reads: the open
    OI-PLAN blocker comes from ``track_open_items`` and the recorded decision
    from ``tracks.decision_ref`` (the OI-1190 durable pointer). ``UNSUPPORTED``
    when the schema cannot determine whether the blocker is open — the same
    predicate ``_plan_gate_state_decision`` uses. A store with the blocker
    columns but no ``decision_ref`` column (pre-0033) reads every open blocker
    as ``UNREAD``: that store has no decision record to derive a refusal from.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    try:
        if not (_has_table(conn, "track_open_items") and _has_col(conn, "track_open_items", "resolved_at")):
            return UNSUPPORTED
        oi = plan_blocker_oi(track_id)
        if _has_col(conn, "track_open_items", "project_id"):
            open_blocker = conn.execute(
                "SELECT 1 FROM track_open_items "
                "WHERE track_id=? AND project_id=? AND oi_id=? "
                "AND link_type='blocks' AND resolved_at IS NULL LIMIT 1",
                (track_id, project_id, oi),
            ).fetchone() is not None
        else:  # pre-0024 DB: no tenant column
            open_blocker = conn.execute(
                "SELECT 1 FROM track_open_items "
                "WHERE track_id=? AND oi_id=? AND link_type='blocks' "
                "AND resolved_at IS NULL LIMIT 1",
                (track_id, oi),
            ).fetchone() is not None
        decision_ref = None
        if _has_col(conn, "tracks", "decision_ref"):
            row = conn.execute(
                "SELECT decision_ref FROM tracks WHERE track_id=? AND project_id=?",
                (track_id, project_id),
            ).fetchone()
            decision_ref = row[0] if row else None
        return classify_review_state(
            open_plan_blocker=open_blocker, decision_ref=decision_ref,
        )
    finally:
        conn.close()
