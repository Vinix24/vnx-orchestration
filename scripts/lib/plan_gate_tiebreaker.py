"""plan_gate_tiebreaker.py — the plan-gate stop-rule and tiebreaker (punten 7-9).

The plan-first gate used to run the full panel every round with no stop-rule: a
track that got REVISE twice ran a third full panel. The ijkmeting over 820
reports showed a five-seat panel converges with the first seat in 89.4% of
cases; seats 4 and 5 each moved the outcome 1.7%. A third full round buys
~nothing and keeps the track blocked. This module implements the stop-rule:

  1. Rondeteller (punt 7) — the gate counts how many panel rounds a track has
     had. The counter lives in the EXISTING plan-gate state (the same append-only
     seat ledger as the per-seat verdicts, ``.vnx-attest/plan-gate-seats.ndjson``)
     — a ``plan_gate_round`` record is appended per round, and the counter is the
     highest ``round`` for the track read back from that ledger. No second store.
     Above ``max_rounds`` (default 2, configurable in
     ``configs/plan_gate_panel.yaml``) the gate runs NO full panel.

  2. Tiebreaker (punt 8) — at the threshold ONE tiebreaker decides instead of the
     seats. It has a deliberately different brief than a panel seat:
       - Binary outcome: START or STOP. No third option.
       - At most ONE required change. Not zero, not three.
       - NEVER a findings list. A seat returns findings; the tiebreaker returns a
         decision. If the model returns a list anyway, parsing fails LOUD — we do
         not silently fill in a decision.
       - The model comes from the registry (``wave7_models.yaml``), default
         ``fable-5``. No model literal in Python (ADR-036): the router reads
         models from the registry, an unknown provider fails loud.

  3. Sporen (punt 9) — on STOP the remaining findings from the last round become
     open items via ``open_items_manager.add_item_programmatic`` (not a loose
     note). The blocker clears with a ``resolution_reason`` that names the
     tiebreaker (which model, which round, which outcome). A blocker may not
     vanish without a reason since #1511; this path respects that.

The module is dependency-light (stdlib + the registry loader) and takes an
injectable ``DispatcherFn`` so the stop-rule is testable without a live model —
the SAME injection contract ``plan_gate_panel.run_panel`` uses.
"""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Reuse the panel's dispatcher contract + ledger constants so the stop-rule and
# the panel never drift on what a "dispatcher" is or where the seat ledger lives.
import plan_gate_panel as pgp  # noqa: PLC0415  (sibling module, on sys.path)

# The round-counter record type, appended to the SAME seat ledger as the
# per-seat verdicts (SEAT_LEDGER_RELPATH). One ``plan_gate_round`` record per
# panel round per track; the counter is the highest ``round`` read back. This is
# the "existing plan-gate state" the dispatch names — no second store.
ROUND_RECORD_TYPE = "plan_gate_round"

# The tiebreaker verdict fence. Distinct from VERDICT_FENCE so a panel seat's
# report and a tiebreaker's report can never be confused for each other (and so
# _sanitize_doc's neutralization of VERDICT_FENCE in untrusted plan input does
# not touch the tiebreaker fence).
TIEBREAKER_FENCE = "vnx-plan-tiebreak"

START = "START"
STOP = "STOP"
_VALID_OUTCOMES = {"START", "STOP"}

# Default max-rounds threshold. A malformed config value falls back to this.
# 2 means: rounds 1 and 2 run the full panel; round 3 (and any further) is the
# tiebreaker. Configurable via configs/plan_gate_panel.yaml -> tiebreaker.max_rounds.
DEFAULT_MAX_ROUNDS = 2

# Config keys read from configs/plan_gate_panel.yaml -> tiebreaker.
_TIEBREAKER_CONFIG_KEYS = ("max_rounds", "provider", "model")


# A dispatcher takes (provider, model_arg, instruction, dispatch_id) and returns
# the tiebreaker's report text — the SAME contract plan_gate_panel uses, so the
# default dispatcher and an injected test dispatcher are interchangeable.
DispatcherFn = Callable[[str, str, str, str], str]


@dataclass
class TiebreakerResult:
    """The outcome of one tiebreaker run.

    ``outcome`` is START or STOP (never anything else — a parse failure raises
    ``TiebreakerParseError`` rather than populating this with a default).
    ``required_change`` is the single change the tiebreaker demanded (at most
    one; empty string when the model offered none, which is legal on START and
    on a STOP that finds no remaining gap). ``model`` is the registry model key
    the tiebreaker was dispatched with, carried onto the ``resolution_reason``
    so the audit trail names WHO decided. ``round`` is the round number this
    tiebreaker ran at (>= 1). ``raw_text`` is kept ONLY on a parse failure so
    the unparseable output survives for diagnosis instead of vanishing.
    """
    outcome: str
    model: str
    round: int
    required_change: str = ""
    rationale: str = ""
    raw_text: str = ""


class TiebreakerParseError(ValueError):
    """The tiebreaker's answer did not satisfy the strict contract.

    Raised LOUD (never silently filled-in) when the model returned:
      - no parseable fence, or a fence that is not valid JSON / not an object;
      - an ``outcome`` that is not START or STOP;
      - a ``findings``/``blocking_findings`` list (a seat returns findings; the
        tiebreaker returns a decision — a list means the model answered as a
        panel seat, not a tiebreaker);
      - more than one ``required_change`` (the brief allows at most one).

    The raw text is attached as ``raw`` so a caller can surface what failed to
    parse, not just that it failed.
    """

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _tiebreaker_config_path() -> Path:
    """The on-disk config resolved the same way as the seat list
    (``configs/plan_gate_panel.yaml`` at the repo root / central install root)."""
    return pgp._default_panel_config_path()


def load_tiebreaker_config(
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load the ``tiebreaker`` block from ``configs/plan_gate_panel.yaml``.

    Returns a dict with ``max_rounds`` (int >= 1), ``provider`` (str), and
    ``model`` (str). Falls back to the code defaults when the file is absent or
    has no ``tiebreaker`` block (a missing config never breaks the gate; the
    code constant is the safety net). A present-but-malformed ``max_rounds``
    falls back to ``DEFAULT_MAX_ROUNDS`` rather than silently widening the
    threshold to infinity. A present ``tiebreaker`` block with a non-mapping
    shape fails LOUD (same discipline as ``load_panel_seats``: misconfiguration
    must surface, not silently degrade).
    """
    path = Path(config_path) if config_path is not None else _tiebreaker_config_path()
    fallback: Dict[str, Any] = {
        "max_rounds": DEFAULT_MAX_ROUNDS,
        # The default provider/model are resolved from the registry by
        # resolve_tiebreaker_model, NOT carried as Python literals on the
        # dispatch path (ADR-036). These defaults are only the config fallback
        # when the YAML block is absent; resolve_tiebreaker_model still
        # validates them against the registry and fails loud on drift.
        "provider": "anthropic",
        "model": "fable-5",
    }
    if not path.is_file():
        return dict(fallback)
    try:
        import yaml  # noqa: PLC0415
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        warnings.warn(
            f"plan_gate_tiebreaker: failed to load {path}: {exc} — using defaults",
            RuntimeWarning,
            stacklevel=2,
        )
        return dict(fallback)
    if not isinstance(loaded, dict) or "tiebreaker" not in loaded:
        return dict(fallback)
    block = loaded["tiebreaker"]
    if not isinstance(block, dict):
        raise ValueError(
            f"plan_gate_tiebreaker: {path} 'tiebreaker' must be a mapping, got "
            f"{type(block).__name__}"
        )
    raw_rounds = block.get("max_rounds", DEFAULT_MAX_ROUNDS)
    try:
        max_rounds = int(raw_rounds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"plan_gate_tiebreaker: {path} tiebreaker.max_rounds must be an "
            f"integer, got {raw_rounds!r}"
        ) from exc
    if max_rounds < 1:
        raise ValueError(
            f"plan_gate_tiebreaker: {path} tiebreaker.max_rounds must be >= 1, "
            f"got {max_rounds}"
        )
    provider = str(block.get("provider", fallback["provider"])).strip()
    model = str(block.get("model", fallback["model"])).strip()
    if not provider or not model:
        raise ValueError(
            f"plan_gate_tiebreaker: {path} tiebreaker.provider and tiebreaker.model "
            "must both be non-empty strings"
        )
    return {"max_rounds": max_rounds, "provider": provider, "model": model}


# ---------------------------------------------------------------------------
# Registry-sourced model identity (ADR-036)
# ---------------------------------------------------------------------------

def resolve_tiebreaker_model(
    config: Optional[Dict[str, Any]] = None,
    *,
    registry_path: Optional[Path] = None,
) -> Tuple[str, str, str]:
    """Resolve the tiebreaker's (provider_key, model_key, dispatch_model_arg).

    Model identity comes from the registry (``wave7_models.yaml``), never a
    Python literal on the dispatch path (ADR-036). The provider+model pair from
    the config is validated against the registry: the provider section must
    exist and be enabled, and the model key must exist under it. An unknown
    provider or model raises ``RegistryLookupError`` naming what was missing and
    where it was looked for — the same fail-loud contract the static router
    uses (``provider_registry._resolve_step``). Never a silent None.

    Returns ``(provider_key, model_key, cli_model_arg)``:
      - ``provider_key`` is the registry section key (e.g. ``anthropic``).
      - ``model_key`` is the registry model key (e.g. ``fable-5``).
      - ``cli_model_arg`` is the ``cli_model_arg`` the registry carries for that
        model (the string handed to the dispatch lane), falling back to the
        model key when the registry entry carries no ``cli_model_arg`` (the
        anthropic section's models do not, so the model key IS the cli arg).
    """
    from providers.provider_registry import (  # noqa: PLC0415
        RegistryLookupError,
        load,
    )

    cfg = config if config is not None else load_tiebreaker_config()
    provider_key = cfg["provider"]
    model_key = cfg["model"]

    registry = load(registry_path)
    pcfg = registry.get(provider_key)
    if pcfg is None:
        raise RegistryLookupError(
            f"tiebreaker provider {provider_key!r} is not in the registry "
            f"(checked wave7_models.yaml) — add it or fix "
            f"configs/plan_gate_panel.yaml tiebreaker.provider"
        )
    if not pcfg.enabled:
        raise RegistryLookupError(
            f"tiebreaker provider {provider_key!r} is disabled in the registry"
        )
    model = pcfg.models.get(model_key)
    if model is None:
        raise RegistryLookupError(
            f"tiebreaker model {model_key!r} is not under provider "
            f"{provider_key!r} in the registry — add it or fix "
            f"configs/plan_gate_panel.yaml tiebreaker.model"
        )
    # The anthropic registry models carry no cli_model_arg (the model key is the
    # arg the claude/tmux lane takes, e.g. "fable-5"). Fall back to the model
    # key so a registry section without cli_model_arg still resolves.
    cli_arg = model.cli_model_arg or model_key
    return provider_key, model_key, cli_arg


# ---------------------------------------------------------------------------
# Round counter (punt 7) — persisted in the seat ledger
# ---------------------------------------------------------------------------

def _round_ledger_path(seat_ledger_path: Optional[Path]) -> Optional[Path]:
    """The ledger the round counter lives in.

    The round counter shares the seat ledger (``SEAT_LEDGER_RELPATH``): a
    ``plan_gate_round`` record is appended per round, and the counter is the
    highest ``round`` read back from that same file. There is deliberately no
    second store — ``None`` when no seat ledger path is resolvable (a test that
    injects a dispatcher and passes no path skips persistence).
    """
    return seat_ledger_path


def read_round_count(
    seat_ledger_path: Optional[Path], track_id: str, project_id: str,
) -> int:
    """The highest ``round`` number recorded for ``(track_id, project_id)``.

    Reads the seat ledger (the existing plan-gate state) and returns the max
    ``round`` across ``plan_gate_round`` records for this track. ``0`` when the
    ledger is absent or has no round records for the track yet — the gate has
    not run a round. Read-only; never raises (a corrupt ledger line is skipped,
    mirroring ``walk_chain``'s tolerance, so a damaged tail never blocks the
    gate). This is the value the dispatch's persistence test reads back: write,
    discard the writer, read again, believe the file — not the return value.
    """
    if seat_ledger_path is None or not seat_ledger_path.exists():
        return 0
    highest = 0
    try:
        with seat_ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") != ROUND_RECORD_TYPE:
                    continue
                if rec.get("track_id") != track_id:
                    continue
                if rec.get("project_id") not in (None, project_id):
                    continue
                try:
                    rnd = int(rec.get("round", 0))
                except (TypeError, ValueError):
                    continue
                if rnd > highest:
                    highest = rnd
    except OSError:
        return 0
    return highest


def record_round(
    seat_ledger_path: Optional[Path],
    *,
    track_id: str,
    project_id: str,
    round_number: int,
    outcome: str,
    model: str = "",
    timestamp: Optional[str] = None,
) -> bool:
    """Append a ``plan_gate_round`` record to the seat ledger.

    The record carries the round number, the outcome of that round
    (``panel`` for a full-panel round, ``tiebreak:START``/``tiebreak:STOP`` for
    a tiebreaker round), and the model that decided it. Append-only and
    hash-chained via ``append_chained_entry`` (the same primitive the seat
    records use), so the round history is tamper-evident alongside the seat
    verdicts. Best-effort and non-raising: round persistence must never break
    the gate it hangs off (same contract as ``_emit_seat_records``).

    Returns True when a record was appended, False when no ledger path was
    given or the append failed (the caller treats False as "not recorded" and
    surfaces it, never as a silent success).
    """
    ledger = _round_ledger_path(seat_ledger_path)
    if ledger is None:
        return False
    try:
        from ndjson_hash_chain import append_chained_entry  # noqa: PLC0415
        record = {
            "type": ROUND_RECORD_TYPE,
            "track_id": track_id,
            "project_id": project_id,
            "round": int(round_number),
            "outcome": outcome,
            "model": model,
            "recorded_at": timestamp or datetime.now(timezone.utc).isoformat(),
        }
        append_chained_entry(ledger, record)
        return True
    except Exception:  # vnx-silent-except: round persistence must never break the gate
        return False


def should_run_tiebreaker(
    seat_ledger_path: Optional[Path],
    track_id: str,
    project_id: str,
    *,
    max_rounds: Optional[int] = None,
) -> bool:
    """True when the track has reached the round threshold.

    The threshold is ``max_rounds`` (resolved from the config when ``None``):
    at/above that count the gate runs the tiebreaker instead of the full panel.
    The decision is read from the persisted round count (the seat ledger), NOT
    from an in-memory counter — so a restart after two rounds still sees the
    threshold reached. ``max_rounds < 1`` is treated as the default (a guard
    against a malformed runtime override; load_tiebreaker_config already
    rejects this at load, but this keeps the runtime path safe too).
    """
    threshold = max_rounds if max_rounds is not None and max_rounds >= 1 else DEFAULT_MAX_ROUNDS
    return read_round_count(seat_ledger_path, track_id, project_id) >= threshold


# ---------------------------------------------------------------------------
# Tiebreaker verdict parsing (punt 8)
# ---------------------------------------------------------------------------

_TIEBREAKER_CONTRACT = (
    "You are the tiebreaker for a plan-gate that has already run its full panel "
    "rounds without a PASS. The seats have had their say. Your job is NOT to "
    "review the plan again as a seat — it is to DECIDE whether to keep going.\n\n"
    "Return EXACTLY ONE decision. Binary. No third option:\n"
    "  START  — the plan is good enough to build; clear the gate.\n"
    "  STOP   — stop here; the plan is not converging, unblock the track and "
    "record the remaining gaps as open items.\n\n"
    "You may demand AT MOST ONE required change. Not zero changes on a plan you "
    "think is wrong, not three. One concrete change, or none.\n\n"
    "Do NOT return a findings list. A seat returns findings; you return a "
    "decision. If you return a list of findings, your answer is rejected.\n\n"
    "When your decision is done, append EXACTLY ONE fenced block and nothing "
    "after it:\n"
    f"```{TIEBREAKER_FENCE}\n"
    "{\n"
    '  "outcome": "START" | "STOP",\n'
    '  "required_change": "one concrete change, or empty string",\n'
    '  "rationale": "one or two sentences"\n'
    "}\n"
    "```\n"
)


def build_tiebreaker_instruction(
    doc_text: str, track_id: str, *, rounds_done: int, last_round_findings: List[str],
) -> str:
    """Render the tiebreaker instruction (inline-doc form).

    Deliberately a DIFFERENT brief than a panel seat (see ``_TIEBREAKER_CONTRACT``):
    binary START/STOP, at most one change, no findings list. The plan doc is
    sanitized the same way a seat's is (embedded fence neutralized, length
    capped) so a plan cannot spoof the tiebreaker fence or blow ARG_MAX. The
    last round's findings are passed as CONTEXT (what the seats raised), not as
    a list the tiebreaker is asked to return — the tiebreaker decides in light
    of them.
    """
    safe = pgp._sanitize_doc(doc_text)
    findings_block = ""
    if last_round_findings:
        bullets = "\n".join(f"  - {f}" for f in last_round_findings)
        findings_block = (
            f"\nThe seats raised these findings in the last round (context, not "
            f"your output):\n{bullets}\n"
        )
    return (
        f"You are the tiebreaker for track {track_id}. The plan-gate has run "
        f"{rounds_done} full panel round(s) without a PASS.\n\n"
        + _TIEBREAKER_CONTRACT
        + findings_block
        + "\n----- PLAN UNDER REVIEW -----\n"
        f"{safe}\n"
        "----- END PLAN -----\n"
    )


def _iter_tiebreak_fence_bodies(report_text: str) -> List[str]:
    """Return the body of every ``vnx-plan-tiebreak`` fence, in document order.

    Mirrors ``plan_gate_panel._iter_fence_bodies`` but for ``TIEBREAKER_FENCE``,
    so a multi-fence report is sliced correctly (an earlier fence's body never
    bleeds into a later one).
    """
    open_marker = "```" + TIEBREAKER_FENCE
    opens: List[int] = []
    search_from = 0
    while True:
        idx = report_text.find(open_marker, search_from)
        if idx == -1:
            break
        opens.append(idx)
        search_from = idx + len(open_marker)
    bodies: List[str] = []
    for i, idx in enumerate(opens):
        bound = opens[i + 1] if i + 1 < len(opens) else len(report_text)
        chunk = report_text[idx + len(open_marker):bound]
        close_idx = chunk.rfind("```")
        if close_idx != -1:
            chunk = chunk[:close_idx]
        bodies.append(chunk)
    return bodies


def parse_tiebreaker(report_text: str) -> TiebreakerResult:
    """Parse a tiebreaker report into a ``TiebreakerResult`` (strict).

    Scans every ``vnx-plan-tiebreak`` fence from LAST to FIRST and parses the
    first valid one. STRICT — unlike ``parse_verdict``'s fail-safe ``revise``,
    a tiebreaker that does not satisfy the contract raises
    ``TiebreakerParseError`` (the dispatch demands "faalt LUID, je vult niet
    stil aan tot een besluit"). The raw text is attached to the error.

    Rejected (raised):
      - no fence, or a fence that is not valid JSON / not an object;
      - ``outcome`` not START or STOP (case-insensitive on read, stored upper);
      - a ``findings`` or ``blocking_findings`` list (a seat returns findings;
        the tiebreaker returns a decision — a list means the model answered as
        a seat, not a tiebreaker);
      - ``required_change`` that is a list, or more than one change (the brief
        allows at most one). A list of length > 1 is more than one change; a
        list of length 1 is accepted as that single change (some models wrap a
        single string in a one-element list); a list of length 0 reads as no
        change.
    """
    if not report_text:
        raise TiebreakerParseError("empty tiebreaker report", raw=report_text or "")
    bodies = _iter_tiebreak_fence_bodies(report_text)
    if not bodies:
        raise TiebreakerParseError(
            "no tiebreaker decision block found (expected a "
            f"```{TIEBREAKER_FENCE}``` fence)", raw=report_text,
        )

    last_reason = "tiebreaker block is not valid JSON"
    last_raw = report_text
    for body in reversed(bodies):
        last_raw = body
        data = pgp._parse_json_loose(body)
        if data is None:
            last_reason = "tiebreaker block is not valid JSON"
            continue
        if not isinstance(data, dict):
            last_reason = "tiebreaker block is not a JSON object"
            continue
        # A findings list means the model answered as a seat, not a tiebreaker.
        for findings_key in ("findings", "blocking_findings"):
            if findings_key in data and isinstance(data[findings_key], list) \
                    and len(data[findings_key]) > 0:
                raise TiebreakerParseError(
                    f"tiebreaker returned a {findings_key!r} list — a seat "
                    "returns findings; the tiebreaker returns a decision "
                    "(START/STOP). Refusing to fill in a decision.",
                    raw=report_text,
                )
        outcome_raw = str(data.get("outcome", "")).strip().upper()
        if outcome_raw not in _VALID_OUTCOMES:
            last_reason = f"unknown outcome {str(data.get('outcome', ''))!r} (need START or STOP)"
            continue
        # required_change: at most one. A string (incl. empty) is fine. A list
        # of 0/1 elements is fine (collapsed). A list of >1 is more than one
        # change -> reject.
        change = data.get("required_change", "")
        if isinstance(change, list):
            if len(change) > 1:
                raise TiebreakerParseError(
                    f"tiebreaker demanded {len(change)} required changes — the "
                    "brief allows at most ONE. Refusing to pick one.",
                    raw=report_text,
                )
            change = str(change[0]) if change else ""
        elif change is None:
            change = ""
        else:
            change = str(change)
        rationale = str(data.get("rationale", "") or "")
        # We do NOT carry the model/round here — the caller sets those (it knows
        # which model it dispatched and which round it is at). parse_tiebreaker
        # only validates the model's ANSWER.
        return TiebreakerResult(
            outcome=outcome_raw,
            model="",
            round=0,
            required_change=change.strip(),
            rationale=rationale.strip(),
        )
    raise TiebreakerParseError(last_reason, raw=last_raw)


# ---------------------------------------------------------------------------
# Tiebreaker dispatch (punt 8)
# ---------------------------------------------------------------------------

def _default_tiebreaker_dispatcher(
    data_dir: Optional[str], timeout_seconds: int,
) -> DispatcherFn:
    """Real dispatcher for the tiebreaker — the panel's default dispatcher.

    The tiebreaker routes through the SAME governed lane a panel seat uses
    (``plan_gate_panel._make_default_dispatcher``): the model comes from the
    registry (fable-5 -> anthropic -> the claude/tmux lane, billing on the
    subscription), so it reuses the panel's dispatcher factory rather than
    growing a second dispatch path. The only difference is the instruction
    (``build_tiebreaker_instruction`` vs ``build_plan_review_instruction``),
    which the caller passes in.
    """
    return pgp._make_default_dispatcher(data_dir, timeout_seconds, role="plan-reviewer")


def run_tiebreaker(
    doc_path: str | Path | None = None,
    *,
    doc_text: Optional[str] = None,
    track_id: str,
    project_id: str = "vnx-dev",
    round_number: int,
    last_round_findings: Optional[List[str]] = None,
    dispatcher: Optional[DispatcherFn] = None,
    data_dir: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    model_arg: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> TiebreakerResult:
    """Run the single tiebreaker over ``doc_path`` (or ``doc_text``) and return
    its decision.

    Exactly one plan source is required: ``doc_text`` when the caller already
    holds the plan text (e.g. a track's ``goal_state`` standing in for the doc),
    otherwise the text is read from ``doc_path``. ``doc_text`` wins when both are
    given — the SAME contract ``plan_gate_panel.run_panel`` uses, so the batch
    (which gates a track's goal_state directly) and the single-track command
    (which may pass a ``--doc`` file) resolve the plan identically.

    The model is resolved from the registry (``resolve_tiebreaker_model``),
    never a Python literal. ``dispatcher`` is injectable; when omitted the
    governed default dispatcher is used (the same factory the panel uses, so
    the tiebreaker is in the audit trail like any seat). The returned
    ``TiebreakerResult`` carries the resolved model key + the round number so
    the caller can build a ``resolution_reason`` that names WHO decided and WHEN.

    Raises ``TiebreakerParseError`` when the model's answer does not satisfy
    the strict contract (no fence, wrong outcome, a findings list, more than one
    change) — the caller treats that as a loud failure, never silently filling
    in a decision.
    """
    import uuid  # noqa: PLC0415

    # Registry-sourced model identity (ADR-036): no literal on the dispatch path.
    # When the caller passes an explicit model_arg (a test injecting a
    # dispatcher does not need a real registry resolution), it wins; otherwise
    # the registry resolves the configured provider+model to a cli arg.
    resolved_model = model_arg
    if resolved_model is None:
        _provider_key, model_key, cli_arg = resolve_tiebreaker_model(config)
        resolved_model = cli_arg

    resolved_timeout = pgp._seat_timeout(timeout_seconds)
    disp = dispatcher or _default_tiebreaker_dispatcher(data_dir, resolved_timeout)
    if doc_text is None:
        if doc_path is None:
            raise ValueError(
                "run_tiebreaker: a plan source is required — pass doc_path or doc_text"
            )
        doc_text = Path(doc_path).read_text(encoding="utf-8")
    instruction = build_tiebreaker_instruction(
        doc_text, track_id,
        rounds_done=max(round_number - 1, 0),
        last_round_findings=list(last_round_findings or []),
    )
    did = f"plan-tiebreak-{track_id}-{uuid.uuid4().hex[:8]}"
    report_text = disp("claude", resolved_model, instruction, did)
    result = parse_tiebreaker(report_text)
    # Stamp the model + round the parser cannot know.
    result.model = resolved_model
    result.round = round_number
    return result


# ---------------------------------------------------------------------------
# STOP aftermath (punt 9) — open items + resolution_reason
# ---------------------------------------------------------------------------

def _resolution_reason(
    result: TiebreakerResult, *, outcome_label: str,
) -> str:
    """The canonical ``resolution_reason`` for a tiebreaker-cleared blocker.

    Names the tiebreaker explicitly: which model, which round, which outcome.
    A blocker may not vanish without a reason since #1511; this is the reason.
    ``outcome_label`` is "START" or "STOP" (carried separately so the reason is
    explicit even when the TiebreakerResult's outcome is already that value).
    """
    change = result.required_change.strip()
    change_clause = f"; required change: {change!r}" if change else ""
    return (
        f"tiebreaker {outcome_label} (model={result.model}, "
        f"round={result.round}){change_clause}"
    )


def stop_open_items_reason(result: TiebreakerResult) -> str:
    """The ``resolution_reason`` for a STOP outcome."""
    return _resolution_reason(result, outcome_label=STOP)


def start_open_items_reason(result: TiebreakerResult) -> str:
    """The ``resolution_reason`` for a START outcome (clears the gate)."""
    return _resolution_reason(result, outcome_label=START)


def remaining_findings_to_open_items(
    *,
    track_id: str,
    project_id: str,
    findings: List[str],
    dispatch_id: str,
    state_dir: Optional[str] = None,
    source: str = "plan_gate_tiebreaker",
) -> List[Tuple[str, bool]]:
    """Record the last round's remaining findings as open items (punt 9, STOP).

    The STOP aftermath: the findings the seats raised in the last round become
    real open items via ``open_items_manager.add_item_programmatic`` — not a
    loose note in a doc. Each finding becomes one open item (severity ``warn``:
    a finding that stopped the gate is a warning the track carries forward, not
    a fresh blocker, because the tiebreaker already lifted the plan-gate
    blocker). A finding that is an acceptance-criterion phrasing (a passing
    check-off) is rejected by the open-items guard; such a finding is skipped
    with a logged warning rather than aborting the whole STOP aftermath (one
    badly-phrased finding must not lose the rest).

    ``state_dir`` overrides the open-items store resolution (the same override
    contract the fabric uses elsewhere): when given, the manager's module-level
    STATE_DIR + derived paths are pointed at it so tests can write to a temp
    store. When omitted, the manager resolves its own store from CWD (the
    production path — the CLI runs from the project CWD, so CWD resolution is
    correct in production).

    Returns ``[(item_id, created), ...]`` per finding: ``created`` is False
    when the finding deduplicated against an existing item (the manager's
    dedup_key). An empty list when there were no findings to record.
    """
    if not findings:
        return []
    import open_items_manager as oim  # noqa: PLC0415

    results: List[Tuple[str, bool]] = []
    # When a state_dir override is given, repoint the manager's module-level
    # store paths for the duration of this call so items land in the caller's
    # store (a test temp dir) rather than the CWD-resolved production store.
    # The manager resolves STATE_DIR at import time from CWD; the functions
    # read the module globals at call time, so swapping them here takes effect.
    with _open_items_store_override(oim, state_dir):
        for finding in findings:
            title = (finding or "").strip()
            if not title:
                continue
            dedup_key = f"plan-gate-tiebreaker:{project_id}:{track_id}:{title}"
            try:
                item_id, created = oim.add_item_programmatic(
                    title=title,
                    severity="warn",
                    dispatch_id=dispatch_id,
                    details=(
                        f"Carried forward from plan-gate tiebreaker STOP "
                        f"(track {track_id}, project {project_id})."
                    ),
                    dedup_key=dedup_key,
                    source=source,
                )
                results.append((item_id, created))
            except ValueError:
                # The acceptance-criterion guard rejected the title phrasing.
                # Skip this one finding, keep the rest — one badly-phrased
                # finding must not lose the others.
                warnings.warn(
                    f"plan_gate_tiebreaker: open-item guard rejected finding "
                    f"{title!r} (phrased as a passing check, not a problem); "
                    "skipped, remaining findings still recorded",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            except Exception as exc:  # vnx-silent-except: STOP aftermath must not crash the gate
                warnings.warn(
                    f"plan_gate_tiebreaker: failed to record open item for finding "
                    f"{title!r}: {exc}; skipped",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
    return results


class _open_items_store_override:
    """Temporarily repoint ``open_items_manager``'s module-level store paths.

    The manager resolves ``STATE_DIR`` at import time from CWD and derives
    ``OPEN_ITEMS_FILE`` / ``DIGEST_FILE`` / ``MARKDOWN_FILE`` / ``AUDIT_LOG``
    from it. The functions read those globals at call time, so swapping them
    here takes effect for the duration of the ``with`` block and is restored
    on exit (also on exception). ``None`` for ``state_dir`` makes this a no-op
    so the production path (CWD resolution) is untouched.

    This is the consumer-namespace override the dispatch's "patch de
    consumer-namespace, niet de bronmodule" rule asks for: it patches the
    module the tiebreaker CONSUMES (open_items_manager), not the tiebreaker's
    own internals, and only for the call's lifetime.
    """

    _DERIVED = ("OPEN_ITEMS_FILE", "DIGEST_FILE", "MARKDOWN_FILE", "AUDIT_LOG")

    def __init__(self, oim_module: Any, state_dir: Optional[str]) -> None:
        self._oim = oim_module
        self._state_dir = state_dir
        self._saved: Dict[str, Any] = {}

    def __enter__(self) -> "_open_items_store_override":
        if self._state_dir is None:
            return self
        from pathlib import Path as _P  # noqa: PLC0415
        new_state = _P(self._state_dir)
        # Save + repoint STATE_DIR first (the lock + mkdir read it).
        self._saved["STATE_DIR"] = self._oim.STATE_DIR
        self._oim.STATE_DIR = new_state
        for key in self._DERIVED:
            self._saved[key] = getattr(self._oim, key)
            # Each derived path is <STATE_DIR>/<filename>; keep the same name.
            setattr(self._oim, key, new_state / getattr(self._oim, key).name)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._state_dir is None:
            return
        for key, val in self._saved.items():
            setattr(self._oim, key, val)


def open_items_state_dir_for(seat_ledger_path: Optional[Path]) -> Optional[str]:
    """Derive the open-items state_dir from the seat-ledger path, or ``None``.

    The seat ledger lives under ``<central-data-root>/.vnx-attest/``; the
    open-items store lives under ``<central-data-root>/state/``. Deriving the
    state_dir from the seat-ledger path keeps the STOP aftermath writing to the
    SAME central root the plan-gate state lives in, without a second resolution
    that could drift. ``None`` when no ledger path is resolvable (the manager
    then resolves its own store from CWD — the production path).
    """
    if seat_ledger_path is None:
        return None
    # .vnx-attest/plan-gate-seats.ndjson -> <root>/.vnx-attest -> <root>/state
    attest_dir = seat_ledger_path.parent
    root = attest_dir.parent
    state_dir = root / "state"
    return str(state_dir)
