"""The tiebreaker at the B3 review cap: one registry model picks one outcome (R2a, ADR-041).

Track ``review-stopregel-en-lichte-stoelen``, plan section 2. When R1 puts a PR
at the cap (``review_rounds.cap_state``), ``vnx review cap-verdict --pr N
--for-dispatch <id>`` asks one model for the next step and records it in
``review_cap_verdicts`` (``review_cap_verdicts.record_verdict``).

- **The model** comes from the registry (ADR-036), through the mechanism the
  plan-gate tiebreaker uses (``plan_gate_tiebreaker.resolve_tiebreaker_model``
  and the panel's governed dispatcher). Its own config key,
  ``review_cap_tiebreaker`` in ``configs/plan_gate_panel.yaml`` (plan, open
  question 3): the same model as the plan-gate tiebreaker today, free to move
  on its own. Without the key it follows the plan-gate tiebreaker.
- **The input** is the cap rule, the codex-seat blockers per head from the
  round ledger and the PR's diff stat. Never the findings: the cap exists
  because the findings did not converge.
- **The answer** is exactly one outcome of the rule, and for ``verkleinen``
  exactly one named intervention. A findings list, a second intervention or an
  outcome the rule does not allow fails loud (``TiebreakerParseError``); a lane
  without an answer is ``TiebreakerNoAnswerError``. Neither records anything.
- **The input is not the answer.** Every text the brief embeds (the diff
  stat's file paths, the ledger rows) has its answer fences disarmed, so a
  block that came from the input never parses as the decision (OI-2203).
- **No second question.** A verdict on the head that is due is looked up
  first, in the store and then in the receipt ledger (a receipt whose row did
  not land, OI-2201); a repeat after a crash returns it and calls no model. When only one
  outcome is left (after the extra round), no model is asked either: the rule
  decides, and the record says so (``tiebreaker_model`` empty).

BILLING SAFETY: no Anthropic SDK. The model runs through the governed lane of
``plan_gate_panel._make_default_dispatcher``; the diff stat comes from ``gh``.
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import plan_gate_panel as pgp
import plan_gate_tiebreaker as pgt
import review_cap_verdicts as rcv
import review_rounds
from plan_gate_tiebreaker import TiebreakerNoAnswerError, TiebreakerParseError

CONFIG_KEY = "review_cap_tiebreaker"
FENCE = "vnx-review-cap-tiebreak"
ROLE = "deliberation-panelist"
_DISPATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_MEANING = {
    review_rounds.B3_1: "a design problem: many blockers in one round",
    review_rounds.B3_2: "no convergence: new blockers keep coming round after round",
}
_OUTCOME_BRIEF = {
    rcv.VERKLEINEN: "make the PR smaller with exactly ONE named intervention; it gets one extra review round",
    rcv.TERUG_NAAR_PLAN: "close the PR and send the deliverable back to its plan",
    rcv.MERGEN_MET_OI: "merge on this head and carry the remaining blockers as open items",
}

DispatcherFn = Callable[[str, str, str, str], str]
DiffStatFn = Callable[[int], str]


class CapTiebreakerError(RuntimeError):
    """The tiebreaker cannot be asked: no cap, no dispatch to bind, no diff stat, an unreadable ledger."""


@dataclass(frozen=True)
class CapAnswer:
    """The model's answer: one outcome, at most one intervention, a short rationale."""

    outcome: str
    intervention: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class CapDecision:
    """What ``decide`` did: ``recorded`` a new verdict, ``restored`` one from its receipt, or found one ``present``."""

    status: str
    verdict: rcv.CapVerdict
    step: rcv.CapStep = field(compare=False, default=None)


# ---------------------------------------------------------------------------
# Config and model (ADR-036)
# ---------------------------------------------------------------------------


def load_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """``provider`` and ``model`` from ``review_cap_tiebreaker``; the plan-gate tiebreaker's without the key.

    A present key that is no mapping, or that leaves provider or model empty,
    fails loud: a misconfigured tiebreaker never degrades silently.
    """
    path = Path(config_path) if config_path is not None else pgt._tiebreaker_config_path()
    fallback = pgt.load_tiebreaker_config(path)
    block: Any = None
    if path.is_file():
        try:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.warn(f"review_cap_tiebreaker: failed to load {path}: {exc}", RuntimeWarning, stacklevel=2)
            loaded = None
        block = loaded.get(CONFIG_KEY) if isinstance(loaded, dict) else None
    if block is None:
        return {"provider": fallback["provider"], "model": fallback["model"]}
    if not isinstance(block, dict):
        raise ValueError(f"review_cap_tiebreaker: {path} {CONFIG_KEY!r} must be a mapping")
    provider, model = str(block.get("provider") or "").strip(), str(block.get("model") or "").strip()
    if not provider or not model:
        raise ValueError(f"review_cap_tiebreaker: {path} {CONFIG_KEY}.provider and .model must both be set")
    return {"provider": provider, "model": model}


def resolve_model(config: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """``(dispatch provider, registry model key, cli model arg)``, validated against the registry."""
    return pgt.resolve_tiebreaker_model(config if config is not None else load_config())


def _default_dispatcher(data_dir: Optional[str], timeout_seconds: int) -> DispatcherFn:
    return pgp._make_default_dispatcher(data_dir, timeout_seconds, role=ROLE)


# ---------------------------------------------------------------------------
# The question and the answer
# ---------------------------------------------------------------------------


def _disarm_fences(text: str) -> str:
    """Input embedded in the brief, with this brief's answer fence and the plan gate's broken (OI-2203).

    A file path in the diff stat or a text from the ledger is not the model's
    answer. Quoted back in its report, an intact ``vnx-review-cap-tiebreak``
    fence would be read by :func:`parse_answer` as the decision; a space after
    the backticks breaks the exact opener it matches.
    """
    return pgt._disarm_fences(text).replace("```" + FENCE, "``` " + FENCE)


def _blockers_block(cap: review_rounds.CapState) -> str:
    rows = [
        f"  round {index}: head {line.head_sha[:12]}, {line.verdict.upper()} with {line.blocking_count} blocker(s)"
        + (" (the extra round)" if line.granted_by else "")
        for index, line in enumerate(cap.rounds, start=1)
    ]
    return "\n".join(rows)


def build_instruction(
    pr_number: int, step: rcv.CapStep, cap: review_rounds.CapState, diff_stat: str,
) -> str:
    """The tiebreaker's brief: the rule, the blockers per head, the diff stat, the allowed outcomes.

    Every text the brief embeds is disarmed (:func:`_disarm_fences`): the only
    armed answer fence in it is the brief's own template.
    """
    choices = "\n".join(f"  - {outcome}: {_OUTCOME_BRIEF[outcome]}" for outcome in step.allowed)
    return (
        f"You are the tiebreaker at the B3 review cap of PR #{pr_number}. The cap rule is {step.rule}: "
        f"{_MEANING[step.rule]}. The review loop has stopped; you decide the next step, you do not review.\n\n"
        f"Codex-seat blockers per head (round ledger):\n{_disarm_fences(_blockers_block(cap))}\n\n"
        f"Diff stat of the PR:\n{_disarm_fences(diff_stat)}\n\n"
        f"Choose exactly ONE outcome:\n{choices}\n\n"
        "Rules: never return findings or a list of problems. For verkleinen name exactly ONE intervention, "
        "a single concrete change that makes the PR smaller. Answer with one fenced block and nothing after it:\n"
        f"```{FENCE}\n"
        '{"outcome": "<one of the outcomes above>", "intervention": "<one intervention, or empty>", '
        '"rationale": "<one or two sentences>"}\n'
        "```\n"
    )


def _fence_bodies(text: str) -> Sequence[str]:
    marker = "```" + FENCE
    parts = text.split(marker)[1:]
    return [part.split("```", 1)[0] for part in parts]


def _one_intervention(data: Dict[str, Any], raw: str) -> str:
    value = data.get("intervention", "")
    if isinstance(value, list):
        if len(value) > 1:
            raise TiebreakerParseError(f"tiebreaker named {len(value)} interventions; the brief allows one", raw=raw)
        value = value[0] if value else ""
    return str(value or "").strip()


def parse_answer(report_text: str, allowed: Sequence[str]) -> CapAnswer:
    """The last valid ``vnx-review-cap-tiebreak`` block, strictly: one allowed outcome, one intervention.

    Raises :class:`TiebreakerParseError` for no block, a findings list, an
    outcome the rule does not allow, a second intervention, or ``verkleinen``
    without an intervention. Nothing is filled in.
    """
    bodies = _fence_bodies(report_text or "")
    if not bodies:
        raise TiebreakerParseError(f"no ```{FENCE}``` block in the tiebreaker's answer", raw=report_text or "")
    reason = "tiebreaker block is not a JSON object"
    for body in reversed(bodies):
        data = pgp._parse_json_loose(body)
        if not isinstance(data, dict):
            continue
        for key in ("findings", "blocking_findings"):
            if isinstance(data.get(key), list) and data[key]:
                raise TiebreakerParseError(
                    f"tiebreaker returned a {key!r} list: a seat returns findings, the tiebreaker one outcome",
                    raw=report_text,
                )
        outcome = str(data.get("outcome") or "").strip()
        if outcome not in allowed:
            reason = f"outcome {outcome!r} is not one of {list(allowed)}"
            continue
        intervention = _one_intervention(data, report_text)
        if outcome == rcv.VERKLEINEN and not intervention:
            raise TiebreakerParseError("verkleinen without its one named intervention", raw=report_text)
        return CapAnswer(outcome, intervention, str(data.get("rationale") or "").strip())
    raise TiebreakerParseError(reason, raw=report_text)


def ask(
    instruction: str, allowed: Sequence[str], *, dispatcher: Optional[DispatcherFn] = None,
    config: Optional[Dict[str, Any]] = None, data_dir: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> Tuple[CapAnswer, str]:
    """Run the one model and parse its answer: ``(answer, registry model key)``.

    A lane that raises, a synthesized report, a deadline stop and an empty
    completion are :class:`TiebreakerNoAnswerError`, never an outcome.
    """
    provider, model_key, cli_arg = resolve_model(config)
    disp = dispatcher or _default_dispatcher(data_dir, pgp._seat_timeout(timeout_seconds))
    did = f"review-cap-tiebreak-{uuid.uuid4().hex[:8]}"
    try:
        report = disp(provider, cli_arg, instruction, did)
    except Exception as exc:  # vnx-silent-except: a raised lane is a no-answer, re-raised as one
        raise TiebreakerNoAnswerError(status=pgt._raised_lane_status(exc), raw=str(exc)) from exc
    if pgp.SYNTHESIZED_REPORT_MARKER in (report or ""):
        raise TiebreakerNoAnswerError(status=pgt._extract_synthesized_status(report), raw=report)
    if pgp.lane_deadline_detail(report) is not None:
        raise TiebreakerNoAnswerError(status="timeout", raw=report)
    if not (report or "").strip():
        raise TiebreakerNoAnswerError(status="empty", raw=report or "")
    return parse_answer(report, allowed), model_key


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def pr_diff_stat(pr_number: int) -> str:
    """The PR's diff stat from GitHub (``gh pr view``): totals and one line per file."""
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", "additions,deletions,changedFiles,files"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise CapTiebreakerError(f"the diff stat of PR #{pr_number} is unavailable: {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout)
    files = [f"  {f['path']} +{f['additions']} -{f['deletions']}" for f in data.get("files") or []][:80]
    head = f"{data['changedFiles']} files changed, +{data['additions']} -{data['deletions']}"
    return "\n".join([head, *files])


def _forced(pr_number: int, step: rcv.CapStep) -> rcv.CapVerdict:
    """The verdict when one outcome is left: the rule decides, no model is asked."""
    (outcome,) = step.allowed
    return rcv.CapVerdict(
        project_id="", pr_number=pr_number, verdict_id=rcv.verdict_id_for(pr_number, step.head_sha),
        head_sha=step.head_sha, cap_rule=step.rule, outcome=rcv.UNAVAILABLE, chosen_outcome=outcome,
        rationale=(
            f"the extra round of {step.round_line.granted_by} ended without a mergeable PASS; one verkleinen per "
            f"PR, so only {outcome} is left under {step.rule}. Not available in R2a: the cap stays closed"
        ),
        created_at=rcv.utc_now(),
    )


def _asked(pr_number: int, step: rcv.CapStep, answer: CapAnswer, model: str, for_dispatch: str) -> rcv.CapVerdict:
    granted = answer.outcome == rcv.VERKLEINEN
    return rcv.CapVerdict(
        project_id="", pr_number=pr_number, verdict_id=rcv.verdict_id_for(pr_number, step.head_sha),
        head_sha=step.head_sha, cap_rule=step.rule, outcome=answer.outcome if granted else rcv.UNAVAILABLE,
        chosen_outcome=answer.outcome, intervention=answer.intervention if granted else "",
        granted_dispatch_id=for_dispatch if granted else "", tiebreaker_model=model,
        rationale=answer.rationale, created_at=rcv.utc_now(),
    )


def _ledger_view(state_dir: Path, pr_number: int) -> Tuple[Tuple[review_rounds.RoundLine, ...], str]:
    review_gates = Path(state_dir) / "review_gates"
    try:
        ledger = review_rounds.read_ledger(review_rounds.ledger_path(review_gates))
    except review_rounds.RoundLedgerError as exc:
        raise CapTiebreakerError(str(exc)) from exc
    unreadable = review_rounds.unreadable_text(ledger, pr_number)
    project_id = review_rounds.store_project_id(review_gates)
    if unreadable or not ledger.is_opened(project_id, pr_number):
        raise CapTiebreakerError(unreadable or f"PR #{pr_number} is not opened in the round ledger: nothing is counted")
    return ledger.for_pr(project_id, pr_number), ledger.since(project_id, pr_number)


def decide(
    state_dir: Path, pr_number: int, *, for_dispatch: str = "", diff_stat: Optional[DiffStatFn] = None,
    dispatcher: Optional[DispatcherFn] = None, config: Optional[Dict[str, Any]] = None,
    data_dir: Optional[str] = None,
) -> CapDecision:
    """The verdict on the cap of PR ``pr_number``: the one that stands, or a new one.

    A verdict whose receipt landed and whose row did not (OI-2201) is booked
    from that receipt, ``"restored"``: the receipt decided, no model is asked
    again. Raises :class:`CapTiebreakerError` (below the cap, no dispatch to
    bind, no diff stat, the ledger), the two tiebreaker errors (no verdict
    recorded), and what ``record_verdict`` raises.
    """
    lines, since = _ledger_view(state_dir, pr_number)
    cap = review_rounds.cap_state(lines, pr_number)
    step = rcv.next_step(cap, lines, rcv.read_verdicts(state_dir, pr_number), since=since)
    if step.kind == rcv.BELOW_CAP:
        raise CapTiebreakerError(f"{cap.summary()}: there is nothing for the tiebreaker to decide")
    if step.kind != rcv.DECIDE:
        return CapDecision("present", step.verdict, step)
    landed = rcv.receipted_verdict(state_dir, pr_number, rcv.verdict_id_for(pr_number, step.head_sha))
    if landed is not None:
        status, verdict = rcv.record_verdict(state_dir, landed)
        return CapDecision(status, verdict, step)
    if len(step.allowed) == 1:
        status, verdict = rcv.record_verdict(state_dir, _forced(pr_number, step))
        return CapDecision(status, verdict, step)
    if not _DISPATCH_ID_RE.match(for_dispatch or ""):
        raise CapTiebreakerError(
            "--for-dispatch <dispatch-id> is required: a verkleinen binds its one extra round to the dispatch "
            "that carries out the intervention"
        )
    stat = (diff_stat or pr_diff_stat)(pr_number)
    answer, model = ask(
        build_instruction(pr_number, step, cap, stat), step.allowed,
        dispatcher=dispatcher, config=config, data_dir=data_dir,
    )
    status, verdict = rcv.record_verdict(state_dir, _asked(pr_number, step, answer, model, for_dispatch))
    return CapDecision(status, verdict, step)
