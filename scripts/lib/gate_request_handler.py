"""Gate request creation and orchestration (GateRequestHandlerMixin).

Extracted from review_gate_manager.py as part of F27 batch refactor.
Methods handle creating gate request payloads for Gemini, Codex, and Claude GitHub.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from atomic_io import atomic_write_json, slot_lock
from auto_merge_policy import codex_final_gate_required
from dispatch_spec import REGISTERED_GATE_NAMES, RETIRED_GATE_NAMES, retired_gate_hint
from review_contract import ReviewContract
from gemini_prompt_renderer import render_gemini_prompt
from gate_recorder import (
    PROVIDER_SWITCHED_OFF_REASON,
    QUOTA_REFUSAL_REASON,
    annotate_result_guarded,
    gate_is_available,
    gate_provider_reachability,
    gate_switch_refusal,
    get_pr_base_branch,
    get_pr_head_sha,
    resolve_gate_provider,
    result_is_for_head,
    write_result_guarded,
)
from gate_executor import _classify_sha_binding
from gate_result_parser import evidence_error
from gate_status import decided_verdict, has_complete_evidence
from governance_emit import _classify_lane_log_text
from provider_switch import SwitchDecision
from review_vendor import (
    MODEL_MATCH_STATUS,
    BuilderVendor,
    resolve_builder_vendor,
    same_vendor_record,
    vendor_match_hop,
)
from claude_github_receipt import (
    ClaudeGitHubReviewReceipt,
    STATE_NOT_CONFIGURED,
    STATE_CONFIGURED_DRY_RUN,
    STATE_REQUESTED,
    STATE_BLOCKED,
    STATE_COMPLETED,
)

logger = logging.getLogger(__name__)



class ReviewGateTakeoverConfigError(ValueError):
    """The configured review-gate takeover chain is malformed: an unknown
    gate name, or a gate repeated (which would build a cycle in what must
    stay a simple linear chain). Raised at READ time -- never discovered
    later as an infinite walk, nor silently dropped into a partial chain.
    """


# Ordered review-gate takeover chain (BETA3-E1, 26-08 operator decision,
# dispatch 20260826-beta3-e1-overname-keten-en-uitputtingstoets): codex_gate
# -> kimi_gate -> glm_gate -> deepseek_gate. OI-1984 (operator decision
# 2026-10-04) reordered the last two links: the chain is now codex_gate ->
# kimi_gate -> deepseek_gate -> glm_gate, so glm is the last link and no longer
# the fallback after kimi. Both API-credit gates stay behind both subscription
# gates. The historical rationale below describes the 22-08 pairing that the
# 26-08 chain built on; it is kept as history.
#
# Historical rationale for kimi_gate -> glm_gate (22-08): on 22-08 glm_gate and kimi_gate gave OPPOSITE verdicts on the
# identical diff/contract_hash -- glm FAIL with a blocking finding, kimi PASS
# with zero findings -- so there is no measured basis for which reader is the
# better fallback, only that a working reader beats an unfilled seat. The
# measured variant (ordered by defect-recall) is a follow-up track (plan open
# question 1); this mapping stays a choice under uncertainty until that
# lands.
#
# glm_gate left the DEFAULT chain on 2026-10-17 (operator decision, R4: 101 runs
# in the 11 Sep to 17 Oct baseline, not one FAIL). It stays a registered gate
# and an explicit VNX_REVIEW_GATE_TAKEOVER_CHAIN that names it still reaches it.
# deepseek_gate is the default chain's END-LINK (it follows kimi_gate since
# 2026-10-04, OI-1984): no gate is configured after it, so a takeover walk
# stops there. With codex and kimi both out, deepseek reads one seat and the
# other ends as chain_exhausted. Since OI-1714/OI-1838 deepseek_gate is a
# real harness-lane gate (gate_recorder.GATE_PROVIDERS, "deepseek-harness"), available by
# REGISTRATION -- `gate_is_available("deepseek_gate")` is True and
# `_request_deepseek` (below) actually requests it, the same as any other
# harness-lane gate. It no longer resolves not_executable/gate_runner_missing
# by design; that was the pre-OI-1714 defect (a script-runner registration
# pointing at a file that never existed), not the current end-link behaviour.
#
# R7 (operator decision 2026-10-22, dispatch dlv-9906a482950d): claude_gate, the
# claude review seat on the operator's subscription, stands between kimi_gate and
# deepseek_gate. Every subscription reviewer (codex, kimi, claude) comes before
# the API-credit end-link, and a seat never gets a reviewer of the builder's
# vendor (review_vendor): such a reviewer is passed over like an unavailable one,
# so a DeepSeek-built head with kimi out reads claude, and a claude-built head
# with kimi out passes claude over and reads deepseek.
_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN = "codex_gate,kimi_gate,claude_gate,deepseek_gate"


def _parse_review_gate_takeover_chain(raw: str) -> Dict[str, str]:
    """Parse a comma-separated ORDERED gate list into a {gate: next_gate}
    successor map. An empty (post-strip) ``raw`` means NO takeover chain at
    all -- returns ``{}`` -- the operator's explicit choice, distinct from an
    absent config falling back to the built-in default (see
    ``_build_review_gate_takeover_chain``).

    Never returns a partial/best-effort map: an unknown gate name, or a gate
    named more than once (the only way a strictly linear list can cycle back
    on itself), raises ``ReviewGateTakeoverConfigError`` naming the offending
    value.
    """
    names = [item.strip() for item in raw.split(",") if item.strip()]
    if not names:
        return {}
    seen: "set[str]" = set()
    for name in names:
        if name not in REGISTERED_GATE_NAMES:
            raise ReviewGateTakeoverConfigError(
                f"VNX_REVIEW_GATE_TAKEOVER_CHAIN names an unknown gate {name!r}"
                f"{retired_gate_hint(name)} (full chain: {raw!r}); known gates: "
                f"{', '.join(sorted(REGISTERED_GATE_NAMES))}"
            )
        if name in seen:
            raise ReviewGateTakeoverConfigError(
                f"VNX_REVIEW_GATE_TAKEOVER_CHAIN contains a cycle: {name!r} appears more than "
                f"once in {raw!r} -- a gate may take over for at most one predecessor"
            )
        seen.add(name)
    return {names[i]: names[i + 1] for i in range(len(names) - 1)}


def _build_review_gate_takeover_chain() -> Dict[str, str]:
    """Resolve the operator's review-gate takeover chain from config
    (BETA3-E1, 26-08), never a hardcoded dict. Reads through
    ``config_runtime.get`` -- the SAME canonical-resolver + DB-override
    precedence ``review_gate_manager._build_default_review_stack`` already
    uses for ``VNX_DEFAULT_REVIEW_STACK``, and the one the OI-1462
    cross-process contract test (PR #1694) pins -- never a raw
    ``os.environ`` read that could silently disagree with what this same
    module's ``_ci_gate_available`` resolves two lines away.

    Resolved FRESH on every call (unlike ``DEFAULT_REVIEW_STACK``, which
    caches at module-import time): a takeover decision is made per gate per
    review request, so a config edit takes effect on the very next request
    instead of needing a process restart.

    Logs, at each resolution, whether the active chain came from an operator
    override or the built-in default -- comparing the resolved string
    against ``_DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN`` verbatim, the same
    provenance technique ``config_registry.all_effective`` already uses
    (``is_default = value == entry.default``).
    """
    import config_runtime
    raw = config_runtime.get("VNX_REVIEW_GATE_TAKEOVER_CHAIN") or ""
    chain = _parse_review_gate_takeover_chain(raw)
    source = "standaard (registry default)" if raw == _DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN else "operator-configuratie"
    logger.info(
        "gate_request_handler: review-gate takeover chain actief -- bron=%s waarde=%r keten=%r",
        source, raw, chain,
    )
    return chain


def _resolve_report_path(report_path: str, data_dir: Path) -> Optional[Path]:
    """Path-safe resolve of a gate result record's ``report_path`` field.

    Mirrors ``governance_emit._resolve_lane_log_path``'s defense-in-depth
    shape (that function's docstring calls it out by name): ``report_path``
    comes from a result record on disk -- state, not trusted input -- so a
    ``relative_to`` escape check against the reports dir runs before any
    read. An unconstrained ``report_path`` could point anywhere the process
    can read; the sibling lane-log reader two functions above already gets
    this right, this one previously did not. Returns None (refuse, no read)
    on anything unsafe or unresolvable -- a bad lookup must never block
    classification, only skip the report-fallback source.
    """
    if not report_path:
        return None
    reports_dir = (Path(data_dir) / "unified_reports").resolve()
    try:
        candidate = Path(report_path).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        candidate.relative_to(reports_dir)
    except ValueError:
        logger.warning(
            "gate_request_handler: report-path read refused -- path %s escaped %s",
            candidate, reports_dir,
        )
        return None
    return candidate


def _scan_seat_failure_text(result: Dict[str, Any], manager: "Optional[GateRequestHandlerMixin]") -> "tuple[str, str]":
    """Scan every available raw-text source for the provider's own
    exhaustion marker via ``governance_emit._classify_lane_log_text`` --
    NEVER a second, hand-rolled marker list. Tries, in order: the JSON
    result record's own ``residual_risk``/``reason_detail``/``summary``
    fields (populated when the #1683 lane-log lift already ran), then --
    only when ``manager`` is not None, since the file lookup needs
    ``manager.paths`` to resolve a data dir -- ``manager._read_lane_log_or_report_text``
    (lane log, then the gate's own report; BETA3-E1 fix-forward).

    A MODULE-LEVEL function, not a method: ``_classify_review_seat_failure``
    is called unbound (``self=None``) by
    ``tests/test_oi1452_lane_log_lift_provider_failed_detail.py``, so the
    file-fallback half must be a plain, optional argument rather than
    something that blows up on a None receiver.

    Returns ``(state, detail)`` where ``state`` is ``'lane_exhausted'`` or
    ``'no_response'``, and ``detail`` is a bounded, marker-anchored snippet
    when ``state == 'lane_exhausted'`` (never the full multi-KB source) --
    the SAME text ``_dispatch_review_seat`` embeds into its takeover
    annotation, so the annotation PRESERVES the actual failure text rather
    than only referencing whichever record it came from (a takeover
    overwriting the source record must never erase the reason it was
    granted on). Never raises: a missing/unreadable file source is silently
    skipped, not a classification failure.
    """
    field_text = " ".join(
        str(result.get(key, "")) for key in ("residual_risk", "reason_detail", "summary")
    ).strip()
    if field_text:
        state, snippet = _classify_lane_log_text(field_text)
        if state == "lane_exhausted":
            return "lane_exhausted", snippet or field_text

    if manager is not None:
        file_text = manager._read_lane_log_or_report_text(result)
        if file_text:
            state, snippet = _classify_lane_log_text(file_text)
            if state == "lane_exhausted":
                return "lane_exhausted", snippet or file_text

    return "no_response", (field_text or "no detail recorded")


# OI-1569 tak 1+2 (dispatch 20260830-153000-oi1569-quota-heeft-geen-tijddimensie):
# a lane_exhausted classification had no time dimension at all -- a quota
# refusal is inherently temporary, but the walk in _dispatch_review_seat
# reads it as a permanent property of the lane. Measured live: once EVERY
# gate in the configured chain (codex_gate -> kimi_gate -> deepseek_gate ->
# glm_gate) has a lane_exhausted record on file, the walk always lands
# on _chain_exhausted_result and NOTHING ever gets a fresh attempt again --
# PR #1719 stayed blocked on a 10:13 record five hours later, at 15:08, with
# five OTHER PRs completing real codex runs in between (no provider outage).
# This constant is the deliberately simple fix: a lane_exhausted record
# older than this TTL no longer counts as "still exhausted" -- the walk
# stops at that hop and re-dispatches it fresh, exactly as it would for a
# gate with no prior failure at all. Retrying too early is cheap (a
# still-exhausted gate re-records lane_exhausted in 3-15s, per the dispatch's
# own measurements) -- a short, simple TTL is safer than a long one or a
# parsed reset-time from provider-specific prose.
_LANE_EXHAUSTION_TTL_SECONDS = 3600


def _seat_failure_age_seconds(recorded_at: Any) -> Optional[float]:
    """Age, in seconds, of a gate result's ``recorded_at`` timestamp.

    Returns None -- not 0, not a huge sentinel -- when ``recorded_at`` is
    missing, not a string, or not a parseable ISO8601 timestamp. OI-1569 tak
    1+2's third branch (see :func:`_lane_exhausted_or_expired`) depends on
    this distinction: "no usable timestamp" must never silently read as
    either "just recorded" (age 0) or "ancient" (a huge sentinel) -- both
    would smuggle a specific answer into what is actually unknown.
    """
    if not isinstance(recorded_at, str) or not recorded_at.strip():
        return None
    from datetime import datetime, timezone  # noqa: PLC0415

    text = recorded_at.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


def _lane_exhausted_or_expired(result: Dict[str, Any]) -> str:
    """Recency-gate a ``lane_exhausted`` classification (OI-1569 tak 1+2).

    Three outcomes, never two -- collapsing the third into either of the
    first two is exactly the defect this exists to fix:

      - the record's ``recorded_at`` is recent (age < TTL): still
        exhausted -- returns ``'lane_exhausted'`` unchanged, the walk keeps
        skipping this hop.
      - ``recorded_at`` is old: EXPIRED -- a quota refusal is not a
        permanent lane property. Returns ``'lane_exhausted_expired'``
        (anything other than the literal ``'lane_exhausted'`` already stops
        the caller's walk and re-dispatches this gate).
      - the record has no usable ``recorded_at`` at all: THIS IS ITS OWN
        BRANCH, not "still exhausted" and not "expired" -- logged loudly
        (an operator must be able to see an unverifiable-age record, never
        have it pass through silently either way) and returns
        ``'lane_exhausted_unknown_age'``. Behaviourally this also stops the
        walk (the safe default when staleness cannot be proven is to allow
        a retry, never to perpetuate an unverifiable block forever) -- but
        it is a NAMED, distinct outcome so the two cases stay
        distinguishable in logs and tests.
    """
    age_seconds = _seat_failure_age_seconds(result.get("recorded_at"))
    if age_seconds is None:
        logger.warning(
            "gate_request_handler: lane_exhausted record for gate=%s pr=%s has "
            "no usable recorded_at (%r) -- cannot verify staleness; treating "
            "as retry-eligible rather than perpetuating an unverifiable block "
            "(OI-1569 tak 1+2)",
            result.get("gate", "?"), result.get("pr_number", "?"), result.get("recorded_at"),
        )
        return "lane_exhausted_unknown_age"
    if age_seconds >= _LANE_EXHAUSTION_TTL_SECONDS:
        logger.info(
            "gate_request_handler: lane_exhausted record for gate=%s pr=%s is "
            "%.0fs old (TTL=%ds) -- treating as EXPIRED, re-dispatching fresh "
            "(OI-1569 tak 1+2)",
            result.get("gate", "?"), result.get("pr_number", "?"),
            age_seconds, _LANE_EXHAUSTION_TTL_SECONDS,
        )
        return "lane_exhausted_expired"
    return "lane_exhausted"


# Statuses of a request payload that say the seat was REFUSED at request time:
# nothing was asked of a reader (binary missing, gate not configured, a hook that
# would not fire, a chain that ran out). Only a request that WENT OUT holds its
# gate for the rest of the round (``_held_reviewers``), and a refusal is not one
# of them -- a later seat that resolves to the same gate must still try it.
_REVIEW_REQUEST_REFUSED_STATUSES = frozenset({
    "not_executable",
    "unavailable",
    "chain_exhausted",
    STATE_BLOCKED,
    STATE_NOT_CONFIGURED,
})


# Seat states (``_classify_review_seat_failure``) whose record does not hold the
# seat: the walk decides the seat as if the record were absent, reachability
# included. An expired or unverifiable exhaustion (OI-1569 tak 1+2), and a
# ``provider_switched_off`` note left by a round in which the family was off.
_RECORD_DOES_NOT_HOLD_THE_SEAT = frozenset({
    "lane_exhausted_expired",
    "lane_exhausted_unknown_age",
    "switched_off",
})


# The one event type every request receipt books under (GE-4c-2, E4): four
# emitters wrote four slightly different lines under this name until then, and
# ``_emit_review_request_receipt`` is now the only place it is spelled.
_REVIEW_REQUEST_EVENT = "review_gate_request"

# The one field set a ``review_gate_request`` receipt carries (GE-4c-2, E4).
# The emitter fills every key its calling path has a value for and omits the
# rest: ``pr_number``/``contract_hash`` are part of the append's idempotency
# key, so stamping one on a path that never carried it would change which
# lines dedupe against which. Nothing new without a line here.
REVIEW_REQUEST_RECEIPT_FIELDS = (
    "pr_id",
    "pr_number",
    "branch",
    "gate",
    "review_mode",
    "risk_class",
    "changed_files",
    "contract_hash",
    "request",
    "contributed_evidence",
    "was_intentionally_absent",
    "failure_reason",
    "takeover_from",
    "dispatch_id",
)

# The consumer name every review-seat switch decision carries in its detail line.
_SEAT_SWITCH_CONSUMER = _REVIEW_REQUEST_EVENT


class _PrHead:
    """The PR head for one request round: handed in, or looked up at most once.

    ``request_and_execute`` hands in the head it observed, so the round and the
    executor judge every record against one answer. ``request_reviews`` on its
    own looks the head up lazily, on the first seat that needs it, so a round in
    which no seat has a record costs no gh round-trip. ``""`` is an answer too:
    the head could not be resolved.

    :meth:`base` is the branch the PR merges into, looked up the same way and
    at most once: the risk class of the round is read against it (R3b).
    """

    def __init__(self, pr_number: int, sha: Optional[str] = None) -> None:
        self._pr_number = pr_number
        self._sha = sha
        self._base: Optional[str] = None

    def sha(self) -> str:
        if self._sha is None:
            self._sha = get_pr_head_sha(self._pr_number)
        return self._sha

    def base(self) -> str:
        if self._base is None:
            self._base = get_pr_base_branch(self._pr_number)
        return self._base


class _PrRounds:
    """The round ledger as one request round sees it for one PR (R1): read once, judged per seat.

    ``unreadable`` is the refusal text when the ledger cannot be read for this
    PR (it names the way out); ``history_missing`` the refusal text for a PR
    that is not opened in the ledger while any earlier review of it is found;
    ``lines`` are the PR's verdicts in order, and ``cap`` the B3 count over its
    codex seat (``review_rounds.cap_state``). ``state_dir`` is the store the
    ledger lives in, where the tiebreaker's verdicts are read (R2a), and
    ``since`` the PR's count boundary (``Ledger.since``).
    """

    def __init__(
        self, pr_number: int, lines: Iterable[Any] = (), unreadable: str = "", history_missing: str = "",
        *, state_dir: Optional[Path] = None, since: str = "",
    ) -> None:
        import review_rounds

        self.pr_number = pr_number
        self.lines = tuple(lines)
        self.unreadable = unreadable
        self.history_missing = history_missing
        self.cap = review_rounds.cap_state(self.lines, pr_number)
        self.state_dir = state_dir
        self.since = since

    @classmethod
    def read(cls, review_gates_dir: Path, pr_number: int) -> "_PrRounds":
        """Read the ledger for one PR, and open the PR when the ledger holds its whole history.

        A PR that is not opened is checked for earlier reviews
        (``review_rounds.prior_evidence``, which fails closed): with any, every
        seat is refused until the operator opened it with ``vnx review rounds
        open``; without any, the PR is new and is opened here (``basis``
        ``new``) before its first run. The records and receipts it checks are
        those of the store the ledger lives in (``review_gates_dir.parent``),
        never of the store the environment names.
        """
        import review_rounds

        path = review_rounds.ledger_path(review_gates_dir)
        project_id = review_rounds.store_project_id(review_gates_dir)
        try:
            ledger = review_rounds.read_ledger(path)
            text = review_rounds.unreadable_text(ledger, pr_number)
            if text:
                return cls(pr_number, unreadable=text)
            if not ledger.is_opened(project_id, pr_number):
                evidence = review_rounds.prior_evidence(
                    review_rounds.store_data_dir(), pr_number, state_dir=Path(review_gates_dir).parent,
                )
                if evidence:
                    return cls(pr_number, history_missing=review_rounds.history_missing_text(pr_number, evidence))
                review_rounds.open_pr(path, project_id, pr_number, basis=review_rounds.BASIS_NEW, evidence=0)
        except review_rounds.RoundLedgerError as exc:
            return cls(pr_number, unreadable=str(exc))
        return cls(
            pr_number, ledger.for_pr(project_id, pr_number),
            state_dir=Path(review_gates_dir).parent, since=ledger.since(project_id, pr_number),
        )

    def grant_on(self, head_sha: str) -> Optional[Any]:
        """The extra round a tiebreaker granted on this PR's cap, qualified for ``head_sha`` (R2a)."""
        import review_cap_verdicts

        if self.state_dir is None or not self.cap.capped:
            return None
        return review_cap_verdicts.grant_or_none(
            self.state_dir, self.cap, self.lines, head_sha=head_sha, since=self.since,
        )

    def line_on_head(self, seat: str, head: _PrHead) -> Optional[Any]:
        """The verdict ``seat`` already has on the round's head; looks the head up only when there are lines."""
        if not self.lines:
            return None
        head_sha = head.sha()
        if not head_sha:
            return None
        return next((line for line in self.lines if line.seat == seat and line.head_sha == head_sha), None)


def _round_refused_payload(
    gate: str, *, pr_number: int, branch: str, head_sha: str, reason: str, detail: str,
    seat_fields: Dict[str, Any],
) -> Dict[str, Any]:
    """A seat the round ledger refuses (R1): nothing is requested, nothing is written.

    ``blocked`` is a refusal status (``_REVIEW_REQUEST_REFUSED_STATUSES``), so the
    gate holds no seat, and a non-pass, so the call that asked for the seat fails.
    """
    from review_gate_manager import _utc_now

    now = _utc_now()
    return {
        "gate": gate, "status": "blocked", "reason": reason, "reason_detail": detail,
        "failure_reason": detail, "branch": branch, "pr_number": pr_number,
        "commit_sha": head_sha, "requested_at": now, "resolved_at": now, **seat_fields,
    }


def _verdict_stands(existing: Optional[Dict[str, Any]], head_sha: str) -> bool:
    """Does ``existing`` hold a decided pass or fail about ``head_sha``?

    The switch governs calls, never evidence: a switched-off seat whose own
    decided verdict on this head is on disk keeps the seat. It is neither
    re-dispatched nor passed over to a successor, so switching a family off can
    never turn a standing FAIL into a successor's PASS. Whether that verdict may
    then be reported as the seat's verdict is :func:`_standing_verdict_gap`.
    """
    return existing is not None and bool(decided_verdict(existing)) and result_is_for_head(existing, head_sha)


def _standing_verdict_gap(existing: Dict[str, Any], head_sha: str) -> str:
    """Why ``existing`` may not stand as a switched-off seat's verdict on ``head_sha``; ``""`` when it may.

    A standing verdict is reported in place of a run, so it clears the bar a
    fresh result clears before the executor counts it, with the same code:

    - evidence: ``gate_status.has_complete_evidence``, the executor's own check
      (terminal, a populated ``contract_hash`` and ``report_path``, the whole
      diff reviewed), and ``gate_result_parser.evidence_error``, the result
      writer's check that the report file is on disk;
    - head: the record's ``commit_sha`` against the head this request observed,
      classified by the executor's ``_classify_sha_binding``. Only ``match``
      stands. The executor lets an ``unknown`` binding through for a result it
      just produced; a verdict reused instead of a run is evidence about this
      head only when it provably names it.
    """
    record_sha = str(existing.get("commit_sha") or "")
    binding = _classify_sha_binding(head_sha, record_sha)
    if binding != "match":
        return (
            f"head binding {binding}: the record names commit {record_sha[:8] or '(none)'}, "
            f"the observed head is {head_sha[:8] or '(unknown)'}"
        )
    if not has_complete_evidence(existing):
        return "evidence incomplete: contract_hash or report_path not populated, or a partial review"
    return evidence_error(existing)


# A hop for a gate that already reads ANOTHER seat of the same request (OI-2058,
# R5a). The gate is not unavailable: it is taken, and a reviewer fills at most one
# seat, so the seat walks on to the next distinct reviewer.
_HELD_HOP_STATUS = "held"
_HELD_HOP_REASON = "reads_another_seat"


def _held_seat_hop(gate: str, holder: str) -> Dict[str, Any]:
    return {
        "gate": gate,
        "reason": _HELD_HOP_REASON,
        "status": _HELD_HOP_STATUS,
        "detail": f"{gate} already reads seat {holder} in this request",
    }


def _with_reader_models(
    seat_fields: Optional[Dict[str, Any]], reader: str, builder: Optional[BuilderVendor],
) -> Dict[str, Any]:
    """``seat_fields`` with the seat's ``builder_vendor`` record saying ``reader``'s model (R7b)."""
    fields = dict(seat_fields or {})
    if isinstance(fields.get("builder_vendor"), dict):
        fields["builder_vendor"] = same_vendor_record(reader, builder, fields["builder_vendor"])
    return fields


def _hop_text(hop: Dict[str, Any]) -> str:
    if hop.get("status") == _HELD_HOP_STATUS:
        return f"{hop['gate']} reads another seat ({hop['detail']})"
    if hop.get("status") == MODEL_MATCH_STATUS:
        return f"{hop['gate']} passed over ({hop['reason']}): {hop['detail']}"
    return f"{hop['gate']} unavailable ({hop['reason']}): {hop['detail']}"


def _takeover_failure_reason(path: List[Dict[str, Any]], reader: str) -> str:
    """The canonical ``failure_reason`` of a takeover: every hop with its own reason."""
    return " -- ".join(_hop_text(hop) for hop in path) + f" -- {reader} substituted as reader"


def _standing_verdict_payload(
    existing: Dict[str, Any], gate: str, verdict: str, decision: SwitchDecision,
    path: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The seat payload that reports a switched-off ``gate``'s standing ``verdict``.

    Status and evidence as recorded, plus the switch decision, plus the takeover
    path when the walk passed over other seats to reach ``gate``. Built in memory
    only: nothing is written over the evidence record.
    """
    payload = dict(existing)
    payload["gate"] = gate
    payload["provider_switch"] = decision.detail()
    payload["redispatch_refused"] = (
        f"{gate} is switched off; its own {verdict} on this head stands and is not re-dispatched"
    )
    if path:
        last_hop = path[-1]
        payload.update({
            "failure_reason": _takeover_failure_reason(path, gate),
            "takeover": True,
            "takeover_from": last_hop["gate"],
            "takeover_reason": last_hop["reason"],
            "takeover_source_status": last_hop["status"],
            "takeover_path": path,
        })
    return payload


def _held_reviewers(requested: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """Reviewer gate -> the stack entry it reads for, for every request of this
    ``request_reviews`` round that actually went out, in the order requested.

    A payload the request itself refused (see ``_REVIEW_REQUEST_REFUSED_STATUSES``)
    holds nothing: it asked no reader anything. The entry is the payload's
    ``seat`` when it fills one, else its own gate (an extra gate such as
    ``ci_gate`` holds itself)."""
    return {
        payload["gate"]: str(payload.get("seat") or payload["gate"])
        for payload in requested
        if payload.get("status") not in _REVIEW_REQUEST_REFUSED_STATUSES
    }


class GateRequestHandlerMixin:
    """Mixin providing gate request creation methods for ReviewGateManager."""

    def _gemini_available(self) -> bool:
        return os.environ.get("VNX_GEMINI_REVIEW_ENABLED", "1") != "0" and shutil.which("gemini") is not None

    def _codex_headless_available(self) -> bool:
        return os.environ.get("VNX_CODEX_HEADLESS_ENABLED", "1") != "0" and shutil.which("codex") is not None

    def _claude_github_configured(self) -> bool:
        return os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_ENABLED", "0") == "1" and shutil.which("gh") is not None

    def _ci_gate_available(self) -> bool:
        import config_runtime
        return config_runtime.get_bool("VNX_CI_GATE_REQUIRED") and shutil.which("gh") is not None

    def _check_ci_gate_requirement_mismatch(self, dispatch_id: str) -> None:
        """OI-1462: compare THIS process's (the vervuller's) own resolution of
        ``VNX_CI_GATE_REQUIRED`` against what the obligation's writer (the
        eiser, running earlier in a possibly different environment) stamped
        at registration time. A finding is a benoemde condition — logged
        loudly, ledgered (ADR-005: an NDJSON event before the obligation
        record mutation, never only the mutation), and persisted onto the
        obligation record — never a silent skip, since the whole defect
        class is two processes disagreeing on the same flag with nothing
        noticing. Two distinct finding kinds
        (``gate_obligations.check_gate_requirement_mismatch``):
        ``value_mismatch`` (both sides captured a value, and they differ) and
        ``writer_capture_failed`` (the eiser's OWN flag-read broke — a fault,
        not a value to compare against).

        Best-effort: dispatch_id may be blank (non-obligation call sites),
        the obligation may not exist yet, or bookkeeping may fail — none of
        that may ever break an actual gate request.
        """
        if not dispatch_id:
            return
        try:
            import config_runtime
            from gate_obligations import (
                check_gate_requirement_mismatch,
                obligation_path,
                update_obligation,
            )
            from review_gate_manager import emit_governance_receipt

            path = obligation_path(self.state_dir, dispatch_id)
            if not path.exists():
                return
            record = json.loads(path.read_text(encoding="utf-8"))
            mismatch = check_gate_requirement_mismatch(
                record, flag="VNX_CI_GATE_REQUIRED",
                reader_value=config_runtime.get_bool("VNX_CI_GATE_REQUIRED"),
            )
            if mismatch is None:
                return
            if mismatch["kind"] == "writer_capture_failed":
                logger.warning(
                    "gate_request_handler: dispatch=%s flag=%s -- the eiser's "
                    "OWN gate-requirement capture FAILED at registration time "
                    "(%s); this obligation's requirement was never reliably "
                    "established and cannot be cross-checked",
                    dispatch_id, mismatch["flag"], mismatch["writer_error"],
                )
            else:
                logger.warning(
                    "gate_request_handler: cross-process gate-requirement mismatch "
                    "for dispatch=%s flag=%s writer=%s reader=%s -- the eiser and "
                    "the vervuller resolved VNX_CI_GATE_REQUIRED differently",
                    dispatch_id, mismatch["flag"], mismatch["writer_value"], mismatch["reader_value"],
                )
            # ADR-005: the ledger is canonical, before any other durable
            # mutation -- emit the NDJSON event first, then mutate the
            # obligation record, so an operator tailing t0_receipts.ndjson
            # sees this finding without having to know to go read obligation
            # JSON files.
            emit_governance_receipt(
                "gate_requirement_mismatch",
                receipt_kind="review_gate",
                status="mismatch",
                dispatch_id=dispatch_id,
                gate="ci_gate",
                **mismatch,
            )
            update_obligation(path, gate_requirement_mismatch=mismatch)
        except Exception as exc:  # vnx-silent-except: mismatch detection is diagnostic tooling on the read path -- it must never block or fail an actual gate request; failures here are logged at debug with dispatch_id + reason so they stay visible without risking the gate itself
            logger.debug(
                "gate_request_handler: ci_gate requirement-mismatch check skipped for dispatch=%s: %s",
                dispatch_id, exc,
            )

    def _kimi_gate_available(self) -> bool:
        return gate_is_available("kimi_gate", state_dir=getattr(self, "state_dir", None))

    def _glm_gate_available(self) -> bool:
        return gate_is_available("glm_gate", state_dir=getattr(self, "state_dir", None))

    def _deepseek_gate_available(self) -> bool:
        return gate_is_available("deepseek_gate", state_dir=getattr(self, "state_dir", None))

    def _claude_gate_available(self) -> bool:
        return gate_is_available("claude_gate", state_dir=getattr(self, "state_dir", None))

    def _builder_vendor(self, dispatch_id: str) -> BuilderVendor:
        """The vendor of the builder the review request names, read from this store's specs (R7).

        The store is the one the review gates live in (``results_dir`` is
        ``<data_dir>/state/review_gates/results``), never the store the
        environment names.
        """
        state_dir = getattr(self, "state_dir", None) or Path(self.results_dir).parent.parent
        builder = resolve_builder_vendor(dispatch_id, data_dir=Path(state_dir).parent)
        logger.info(
            "gate_request_handler: builder vendor for dispatch=%r -- %s (rule %s)",
            dispatch_id, builder.vendor or "unknown", "applied" if builder.applies else "not applied",
        )
        return builder

    def _seat_switch_refusal(self, gate: str) -> Optional[SwitchDecision]:
        """The provider switch's refusal for a review seat, or ``None`` when it may be called.

        A retired gate is left to ``_dispatch_one_review``, which books it
        ``retired_review_gate``: it is no reviewer at all, switched on or off.
        """
        if gate in RETIRED_GATE_NAMES:
            return None
        return gate_switch_refusal(
            gate, consumer=_SEAT_SWITCH_CONSUMER, state_dir=getattr(self, "state_dir", None),
        )

    def _dispatch_one_review(
        self,
        gate: str,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
    ) -> Dict[str, Any]:
        if gate in RETIRED_GATE_NAMES:
            return {
                "gate": gate, "status": "blocked", "reason": "retired_review_gate",
                "reason_detail": f"{gate} is a retired gate and no longer a reviewer; "
                                 "remove it from the review stack",
            }
        if gate == "codex_gate":
            return self._request_codex(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "claude_github_optional":
            from review_contract import ReviewContract
            contract = ReviewContract(
                pr_id=str(pr_number),
                branch=branch,
                risk_class=risk_class,
                changed_files=list(changed_files),
                dispatch_id=dispatch_id,
            )
            receipt = self.request_claude_github_with_contract(
                contract=contract,
                mode=mode,
                dispatch_id=dispatch_id,
                pr_number=pr_number,
            )
            payload = receipt.to_dict()
            payload["status"] = receipt.state  # backwards compat for emit_governance_receipt
            return payload
        if gate == "ci_gate":
            return self._request_ci_gate(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "wiring_gate":
            return self._request_wiring_gate(pr_number, branch, dispatch_id)
        if gate == "kimi_gate":
            return self._request_kimi(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "glm_gate":
            return self._request_glm(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "deepseek_gate":
            return self._request_deepseek(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        if gate == "claude_gate":
            return self._request_claude(pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        return {"gate": gate, "status": "blocked", "reason": "unknown_review_gate"}

    def _read_existing_gate_result(self, gate: str, pr_number: Optional[int]) -> Optional[Dict[str, Any]]:
        """Read a previously-recorded terminal result for (gate, pr_number) from
        disk, if one exists.

        Backs the REQUEST-time takeover decision (deliverable 5): before
        (re-)requesting a gate, this checks whether it already produced a
        terminal not_executable/unavailable result for this PR on an earlier
        call, so a known-broken seat can be routed around instead of
        re-requested. Never invents a live outcome by executing anything
        itself. Returns None on a missing or unreadable file so a corrupt or
        absent result reads as "no signal" -- not as a failure to take over.
        """
        if pr_number is None:
            return None
        result_file = self._result_path(gate, pr_number)
        if not result_file.exists():
            return None
        try:
            return json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _classify_review_seat_failure(self, result: Dict[str, Any]) -> str:
        """Classify a previously-recorded gate result into one of the plan's
        three failure states, or 'ok' when it is not a takeover-eligible
        failure at all.

        Returns:
            'lane_exhausted'     -- toestand 1: exhaustion with body (a
                                     payment/quota code, or a permanent
                                     not_executable refusal with a
                                     provider-owned reason). Take over.
            'lane_exhausted_expired' / 'lane_exhausted_unknown_age' --
                                     OI-1569 tak 1+2: a marker WAS found, but
                                     the recency gate below (see
                                     :func:`_lane_exhausted_or_expired`)
                                     refuses to keep treating it as a
                                     currently-exhausted lane. Any value that
                                     is not the literal string
                                     'lane_exhausted' already stops the
                                     caller's chain walk and re-dispatches
                                     this seat -- these two names exist so
                                     the reason is loud and distinguishable
                                     in logs, never to be pattern-matched by
                                     a caller.
            'unreadable_verdict' -- toestand 2: a response existed, the
                                     verdict block did not parse. Abstain --
                                     never take over.
            'no_response'        -- toestand 3: a bare non-zero exit with no
                                     body, or no signal at all. Never take
                                     over.
            'switched_off'       -- a ``provider_switched_off`` note: the
                                     operator's switch refused the call in an
                                     earlier round. Classified by its reason,
                                     before any text scan, so an operator's
                                     reason text that happens to read like a
                                     quota message is never ``lane_exhausted``.
                                     The record does not hold the seat; the
                                     switch is consulted fresh instead.
            'ok'                 -- a real terminal verdict (pass/fail), or a
                                     not_executable refusal that is not a
                                     provider reason (e.g. glm's pre-dlv1
                                     "runner not shipped yet" refusal).
                                     Dispatch normally.
        """
        status = result.get("status", "")
        if status in ("not_executable", "unavailable") and result.get("reason") == PROVIDER_SWITCHED_OFF_REASON:
            return "switched_off"
        if status == "not_executable":
            # A code-not-shipped refusal is not a provider outage -- taking
            # over FROM a seat that was never real would misattribute cause.
            if result.get("reason") == "gate_runner_missing":
                return "ok"
            return _lane_exhausted_or_expired(result)
        if status != "unavailable":
            return "ok"
        reason = result.get("reason", "")
        if reason == "parse_error":
            return "unreadable_verdict"
        if reason == "no_verdict":
            return "no_response"
        if reason == QUOTA_REFUSAL_REASON:
            # The record SAYS it is a quota refusal, because gate_recorder
            # classified it at write time (D-gate-quota-105245). Read it as
            # what it says. The text scan below still finds the same answer
            # in reason_detail -- it is the fallback for records written
            # before that change, and for any gate whose quota text never
            # reaches the reason field -- but a reader that can only learn
            # the cause by re-parsing prose is a reader that can forget to,
            # which is precisely how stop_conditions came to trip E6 on
            # three quota refusals it read as three generic crashes.
            return _lane_exhausted_or_expired(result)
        # reason == "dispatch_error" (or an unrecognised reason): scan for
        # the provider's own exhaustion marker via _scan_seat_failure_text --
        # the SAME classifier deliverable 0 built for the lane-log lift,
        # extended (BETA3-E1) to also try the gate's own report when no lane
        # log carries it. Never a second hand-rolled marker scan, and never
        # keyed on a parser-side msg field. Measured: 29 kimi cases carry
        # msg='Expecting value: line 1' beside raw='Error code: 403'; a
        # msg-keyed check reads toestand 2 while the raw body says toestand 1.
        state, _detail = _scan_seat_failure_text(result, self)
        if state == "lane_exhausted":
            return _lane_exhausted_or_expired(result)
        return state

    def _read_lane_log_or_report_text(self, result: Dict[str, Any]) -> str:
        """Best-effort raw-text read backing the exhaustion-marker fallback
        scan (BETA3-E1 fix-forward, T0 live finding glm-gate-pr1691-1787754901,
        26-08): the per-dispatch lane log first (the SAME source the #1683
        lift already reads via ``governance_emit._read_lane_log_text``), then
        the gate's own REPORT file at ``result['report_path']`` when no lane
        log exists for this dispatch_id at all, then (OI-1477) the SAME
        report path DERIVED from ``result['dispatch_id']`` when
        ``report_path`` itself is empty.

        Root cause the report-path source covers (#1683/BETA3-E1): the
        lane-log lift only ever reads the lane log. When a provider error
        lands in the gate's own report instead (no lane log for that
        dispatch), the marker never reaches residual_risk/reason_detail/
        summary -- even though the SAME classifier on the report's raw text
        finds it correctly. Measured: glm-gate-pr1691-1787754901 had NO lane
        log at all, but its 9345-byte report carried the real 402
        ``openrouter_credits`` body.

        Root cause the derived-path source covers (OI-1477): a result
        record whose ``report_path`` field is itself empty -- measured on
        pr-1696-glm_gate.json (``reason=dispatch_error``, ``report_path=''``)
        -- read as ``no_response`` even though the report existed on disk
        (``unified_reports/glm-gate-pr1696-1787774904.md``, 9354 bytes,
        carrying a real OpenRouter 402) and this SAME classifier reads
        ``lane_exhausted`` off its text without trouble. The precondition
        for a fallback to matter is exactly the failure path where evidence
        is scarcest, so ``report_path`` being unpopulated on that path
        (glm_gate.py/kimi_gate.py did not fill it before OI-1477; other
        gates or older records may still not) must not end the search --
        ``dispatch_id`` is still real on those records, and the report lives
        at the SAME predictable location every gate writes to -- built via
        ``report_path.canonical_report_path``, the fleet's own SSOT for this
        exact "where does this dispatch_id's report live" question, never a
        second hand-rolled format string. This is the one GUARD (as opposed
        to path construction) OI-1477 spec calls for reusing verbatim rather
        than reinventing: derive, then run the exact same
        ``_resolve_report_path`` escape check the populated-``report_path``
        source already uses two lines below, never a second path-safety
        mechanism -- ``canonical_report_path`` returns an UNRESOLVED path, so
        a ``dispatch_id`` crafted with ``../`` segments is caught there, not
        trusted just because it came from the SSOT.

        Returns "" on any missing dispatch_id/report_path (both sources),
        missing/empty file, or an unresolvable data dir -- a failed lookup
        must never raise out of a classification decision, and never
        fabricates text from nothing. An UNREADABLE report (exists, but
        ``OSError`` on read) is logged with its path and which source
        produced it (BETA3-E1b fix-forward, extended OI-1477) rather than
        swallowed indistinguishably from "no report exists" -- the classifier
        two frames up falls back to ``no_response`` either way, but an
        operator reading logs must be able to tell "nothing to read" apart
        from "a read failed", or a broken report-fallback source looks
        identical to a real absence of exhaustion evidence.

        Both ``report_path`` and the derived path are on-disk STATE (a
        result record, or a string built from one of its fields), not
        trusted input, so both are resolved via the SAME
        ``_resolve_report_path`` and refused if they would read outside the
        reports dir -- the SAME defense-in-depth shape
        ``governance_emit._resolve_lane_log_path`` already applies to the
        lane-log source two lines above.
        """
        try:
            data_dir = Path(self.paths["VNX_DATA_DIR"])
        except (AttributeError, KeyError, TypeError):
            data_dir = None

        dispatch_id = str(result.get("dispatch_id") or "")
        if dispatch_id and data_dir is not None:
            from governance_emit import _read_lane_log_text
            lane_text = _read_lane_log_text(dispatch_id, data_dir)
            if lane_text:
                return lane_text

        if data_dir is None:
            return ""

        report_path = result.get("report_path") or ""
        source = "report_path"
        if not report_path and dispatch_id:
            # report_path.py is the fleet's SSOT for "where does this
            # dispatch_id's report live" -- never hand-roll the format string
            # a second time (its own module docstring calls this out by
            # name). Still passed through _resolve_report_path below, the
            # SAME escape guard the populated-report_path source already
            # uses: canonical_report_path builds an UNRESOLVED path, so a
            # dispatch_id crafted with ``../`` segments must still be caught
            # here, not trusted just because it came from the SSOT.
            from report_path import canonical_report_path
            report_path = str(canonical_report_path(dispatch_id, data_dir))
            source = "derived from dispatch_id"
        if not report_path:
            return ""

        report_file = _resolve_report_path(report_path, data_dir)
        if report_file is None:
            return ""
        try:
            if not report_file.is_file():
                return ""
            text = report_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(
                "gate_request_handler: report read failed dispatch=%s path=%s (source=%s): %s",
                dispatch_id or "<unknown>", report_file, source, exc,
            )
            return ""
        return text if text.strip() else ""

    def _write_request_record(self, path: Path, payload: Dict[str, Any]) -> None:
        """The ONE writer for every request record this module produces (GE-4c-2, Q5-Q7).

        ``atomic_write_json`` inside ``slot_lock`` on the request path: the
        same pair GE-4b gave ``gate_recorder.persist_request`` (Q1), so a
        reader never sees half a request and two writers of one request never
        interleave. That is not theoretical: ``commit_gate_outcome`` ends a
        request through ``persist_request`` on this same lock while this
        module writes it, and an executor reads the request it just wrote to
        decide whether to run. ``path`` is the caller's, because this module
        names a request two ways (``_request_path``, ``_contract_request_path``).
        """
        with slot_lock(path):
            atomic_write_json(path, payload)

    def _stamp_takeover_annotations(
        self,
        payload: Dict[str, Any],
        *,
        pr_number: Optional[int],
        takeover_from: str,
        failure_reason: str,
        takeover_reason: str,
        takeover_source_status: str,
        takeover_path: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Annotate a fallback gate's payload with takeover provenance and
        persist it into whichever on-disk record(s) ``_dispatch_one_review``
        already wrote.

        An annotation that lives only in the returned dict and never reaches
        the file an operator or ``closure_verifier`` actually reads back is
        evidence that exists and guards nothing (the OI-1178 lesson,
        reapplied here) -- so both the request record and, when present, the
        terminal result record are rewritten with the same annotation
        fields. ``failure_reason`` is the CANONICAL reason field (#1666 /
        OI-1415) -- never a one-off field name a generic receipt reader would
        not recognise.

        ``takeover_path`` (BETA3-E1): the FULL sequence of hops the seat took
        to reach this gate -- one entry per exhausted predecessor, in order
        -- so a reader sees "codex_gate exhausted, kimi_gate exhausted, glm_gate
        decided" instead of only the last jump. Defaults to a single-entry
        list built from the other (single-hop) arguments when the caller has
        no multi-hop path to report, so the field is NEVER absent on a
        takeover record.

        The request write goes through :meth:`_write_request_record`
        (``atomic_write_json`` inside ``slot_lock`` on the request path,
        GE-4c-2 Q5), never a bare ``write_text`` or an unlocked write. That
        file is an EVIDENCE file: a torn write here passes the existence
        check a reader does first and only fails on the content it trusts --
        worse than no record at all, because it surfaces exactly when the
        record is needed, and two writers of one request (this annotation and
        the executor reading it) must not interleave.

        The result write (OI-1630, GE-4c-2 R11) goes through
        ``gate_recorder.annotate_result_guarded``, which does the
        read-merge-write inside ONE ``slot_lock`` behind the overwrite guard.
        It is not ``commit_gate_outcome`` and must not be: an annotation is
        provenance about a record someone else decided, so it chooses no
        status and stamps no ``attempt_seq``. A concurrent GUARDED writer (the
        real gate run this same takeover request just kicked off) can land a
        decided, evidenced verdict; the guard refuses a stomp of it, and
        because the merge now happens under the lock it also cannot merge a
        stale copy of the record the verdict replaced.
        """
        gate = payload.get("gate", "")
        if takeover_path is None:
            takeover_path = [{
                "gate": takeover_from,
                "reason": takeover_reason,
                "detail": failure_reason,
                "status": takeover_source_status,
            }]
        annotations = {
            "failure_reason": failure_reason,
            "takeover": True,
            "takeover_from": takeover_from,
            "takeover_reason": takeover_reason,
            "takeover_source_status": takeover_source_status,
            "takeover_path": takeover_path,
        }
        payload.update(annotations)

        if pr_number is not None:
            request_file = self._request_path(gate, pr_number)
            if request_file.exists():
                self._write_request_record(request_file, payload)

            result_file = self._result_path(gate, pr_number)
            if result_file.exists():
                _on_disk, written = annotate_result_guarded(
                    result_file, annotations, gate=gate, pr_ref=str(pr_number),
                )
                if not written:
                    # The guard logged its own refusal; this names the caller's
                    # (OI-1630): a decided verdict stands unannotated rather
                    # than stomped, and an unreadable record is never annotated.
                    logger.warning(
                        "gate_request_handler: takeover-annotation write "
                        "REFUSED for gate=%s pr=%s -- an existing terminal, "
                        "evidenced result stands; this stamp never reached "
                        "results/",
                        gate, pr_number,
                    )
        return payload

    def _chain_exhausted_path(self, gate: str, pr_number: int) -> Path:
        return self.results_dir / f"pr-{pr_number}-{gate}-chain-exhausted.json"

    def _chain_exhausted_result(
        self,
        *,
        gate: str,
        pr_number: int,
        branch: str,
        dispatch_id: str,
        path: List[Dict[str, Any]],
        seat_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Named terminal end-state (BETA3-E1): the takeover chain starting
        from ``gate`` walked every configured hop and every one of them was
        passed over (``lane_exhausted``, recorded unreachable, switched off by
        the operator's provider switch, or already reading another seat of
        this request), with no further successor configured. The failure
        reason names each hop's own reason, so a chain that ran out on
        ``provider_switched_off`` reads differently from one that ran out on
        quota. Written to its OWN file (``*-chain-exhausted.json``) -- it never
        overwrites any individual gate's own request/result record, and it is
        NEVER a silent re-dispatch of ``gate`` itself, which ``path`` already
        proves dead.

        When at least one hop was passed over because it already reads another
        seat (OI-2058, R5a), the seat is not out of live readers but out of
        DISTINCT ones: the record says so by name (``reason=no_distinct_reviewer``)
        and lists ``reviewers_tried``. Two seats read by one reviewer is one seat,
        so such a seat is booked unfillable, never shared.
        """
        from review_gate_manager import _utc_now

        tried = [hop["gate"] for hop in path]
        unfillable = any(hop.get("status") == _HELD_HOP_STATUS for hop in path)
        failure_reason = " -- ".join(_hop_text(hop) for hop in path) + (
            " -- no distinct reviewer remains for this seat" if unfillable
            else " -- takeover chain exhausted, no live reader remains"
        )
        summary = (
            f"seat {gate} is unfillable: no distinct reviewer is available (tried {', '.join(tried)}): "
            if unfillable else f"review-gate takeover chain exhausted starting from {gate}: "
        ) + failure_reason
        now = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "pr_number": pr_number,
            "branch": branch,
            "status": "chain_exhausted",
            "reason": "no_distinct_reviewer" if unfillable else "takeover_chain_exhausted",
            "reason_detail": failure_reason,
            "failure_reason": failure_reason,
            "reviewers_tried": tried,
            "takeover": True,
            "takeover_from": gate,
            "takeover_path": path,
            "summary": summary,
            "requested_at": now,
            "resolved_at": now,
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        payload.update(seat_fields or {})
        atomic_write_json(self._chain_exhausted_path(gate, pr_number), payload)
        return payload

    def _unreachable_seat_hop(self, gate: str) -> Optional[Dict[str, Any]]:
        """A takeover-path hop for ``gate`` when the provider behind it is
        recorded unreachable, else ``None`` (OI-1454).

        Reads the MEASURED reachability record (quota spent, credential
        refused, seen on a real call and still inside its window), the answer
        to "would asking this seat produce a verdict". Presence is not
        consulted here: a missing binary is booked by the request itself and
        taken over next round, as before. An unmeasured provider returns
        ``None`` (nobody has proven it dead, so it is asked). The hop carries
        the record's own description as its detail, so the annotation says WHY
        the seat was passed over and where that was seen.
        """
        state_dir = getattr(self, "state_dir", None)
        if state_dir is None:
            return None
        status = gate_provider_reachability(gate, state_dir=Path(state_dir))
        if status is None or not status.is_known_unreachable:
            return None
        logger.info(
            "gate_request_handler: passing over gate=%s -- %s (OI-1454)", gate, status.describe(),
        )
        return {
            "gate": gate,
            "reason": status.reason,
            "detail": status.describe(),
            "status": "unreachable",
        }

    def _switched_off_seat_hop(self, gate: str, decision: SwitchDecision) -> Dict[str, Any]:
        """A takeover-path hop for a seat the operator's provider switch refuses.

        Same shape as :meth:`_unreachable_seat_hop`. The hop is the only place
        the refusal is recorded on the walk: it is never written to the
        reachability record that hop reads, so switching the family back on
        leaves no cooldown and no unreachable fact behind.
        """
        logger.info(
            "gate_request_handler: passing over gate=%s -- %s", gate, decision.detail(),
        )
        return {
            "gate": gate,
            "reason": PROVIDER_SWITCHED_OFF_REASON,
            "status": "disabled",
            "detail": decision.detail(),
        }

    def _walk_takeover_chain(
        self, gate: str, pr_number: int, chain: Dict[str, str],
        refusal: Optional[SwitchDecision], *, head: _PrHead,
        held: Optional[Dict[str, str]] = None, builder: Optional[BuilderVendor] = None,
    ) -> "tuple[Optional[str], List[Dict[str, Any]], Optional[SwitchDecision]]":
        """Walk the takeover ``chain`` from ``gate`` and return ``(reader, path, reader_refusal)``.

        ``reader`` is the first gate on the chain that is not recorded as
        exhausted or unreachable for this PR, not switched off, and not already
        reading another seat of this request (``held``: reviewer gate -> the
        seat it reads), and ``path`` is one hop per candidate passed over on the
        way (empty when ``gate`` itself reads). ``reader`` is ``None`` when the
        chain ran out with the last hop still passed over: a named terminal
        end-state the caller books via ``_chain_exhausted_result``, never a
        fall-back to ``gate``. The rules the walk applies (switch, lane
        exhaustion, head scoping, reachability, one seat per reviewer) are
        documented on ``_dispatch_review_seat``.

        ``refusal`` is the switch decision the caller already made for ``gate``;
        every later candidate is decided here, fresh. ``reader_refusal`` is not
        ``None`` only when the walk stopped at a switched-off seat because it
        holds its own decided verdict on this head: the caller decides whether
        that verdict stands (``_refuse_switched_off_seat``) and dispatches
        nothing. ``head`` is the round's PR head (``_PrHead``).

        ``builder`` (R7, R7b) is the builder of the head: a candidate of that
        vendor that runs the builder's model (or whose model, or the builder's, is
        unknown) is passed over first, before its own record, its switch or its
        reachability is read, with a ``model_matches_builder`` hop. A builder
        whose vendor is unknown passes nobody over.
        """
        path: List[Dict[str, Any]] = []
        current = gate
        current_refusal = refusal
        while True:
            hop, verdict_stands = vendor_match_hop(current, builder), False
            if hop is None:
                hop, verdict_stands = self._candidate_hop(
                    current, current_refusal, pr_number, head=head, held=held or {},
                )
            if verdict_stands:
                return current, path, current_refusal
            if hop is None:
                return current, path, None
            path.append(hop)
            next_gate = chain.get(current)
            if next_gate is None:
                return None, path, None
            current = next_gate
            current_refusal = self._seat_switch_refusal(current)

    def _candidate_hop(
        self, current: str, current_refusal: Optional[SwitchDecision], pr_number: int,
        *, head: _PrHead, held: Dict[str, str],
    ) -> "tuple[Optional[Dict[str, Any]], bool]":
        """Decide one candidate of a takeover walk: ``(hop, verdict_stands)``.

        ``hop`` is the takeover-path entry when ``current`` is passed over, else
        ``None`` (``current`` reads). ``verdict_stands`` is True only for a
        switched-off candidate whose own decided verdict on this head holds the
        seat. A candidate already reading another seat of this request is passed
        over first, whatever its record says: a reviewer fills at most one seat.
        """
        if current in held:
            return _held_seat_hop(current, held[current]), False
        hop: Optional[Dict[str, Any]] = None
        consult_reachability = True
        existing_result = self._read_existing_gate_result(current, pr_number)
        if existing_result is not None:
            head_sha = head.sha()
            if not result_is_for_head(existing_result, head_sha):
                logger.info(
                    "gate_request_handler: ignoring gate=%s result for pr=%s "
                    "recorded against head=%r while the PR head is %r -- a "
                    "verdict about another commit cannot decide whether this "
                    "seat is skipped; the seat is decided as if it had no "
                    "record (OI-1668)",
                    current, pr_number,
                    (existing_result.get("commit_sha") or ""), head_sha,
                )
            elif current_refusal is not None:
                if _verdict_stands(existing_result, head_sha):
                    logger.info(
                        "gate_request_handler: gate=%s is switched off, but its own "
                        "%s on pr=%s head=%r holds the seat -- not re-dispatched and "
                        "not passed over (%s)",
                        current, decided_verdict(existing_result), pr_number, head_sha,
                        current_refusal.detail(),
                    )
                    return None, True
            else:
                seat_state = self._classify_review_seat_failure(existing_result)
                if seat_state == "lane_exhausted":
                    # B3: the takeover annotation is a MANDATORY, never-empty field --
                    # an empty reason here would make this a silent refusal, not a
                    # documented overname. The detail is the ACTUAL marker-anchored
                    # snippet _scan_seat_failure_text found (lane log or report),
                    # embedded here as a value -- never a pointer back to
                    # existing_result -- so a later overwrite of that gate's own
                    # result record (a separate, known issue T0 tracks independently)
                    # cannot erase the reason this hop was recorded on.
                    hop_reason = existing_result.get("reason", "unknown_reason")
                    _hop_state, hop_detail = _scan_seat_failure_text(existing_result, self)
                    hop = {
                        "gate": current,
                        "reason": hop_reason,
                        "detail": hop_detail,
                        "status": existing_result.get("status", ""),
                    }
                elif seat_state not in _RECORD_DOES_NOT_HOLD_THE_SEAT:
                    # The seat answered (or abstained) on THIS head: its own
                    # record decides, and a provider-level record does not
                    # overrule a result this PR already has.
                    consult_reachability = False
        # A switched-off seat is passed over before a live dispatch would
        # happen, ahead of reachability: the hop names the switch, and a
        # refusal is never read from or written to the reachability record.
        if hop is None and current_refusal is not None:
            hop = self._switched_off_seat_hop(current, current_refusal)
        if hop is None and consult_reachability:
            hop = self._unreachable_seat_hop(current)
        return hop, False

    def _dispatch_review_seat(
        self,
        gate: str,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
        chain: Optional[Dict[str, str]] = None,
        held: Optional[Dict[str, str]] = None,
        head: Optional[_PrHead] = None,
        seat_fields: Optional[Dict[str, Any]] = None,
        builder: Optional[BuilderVendor] = None,
    ) -> Dict[str, Any]:
        """Dispatch one review-stack seat, walking the configured takeover
        CHAIN (BETA3-E1) forward while every candidate's own last-recorded
        result is ``lane_exhausted``, then dispatching the first candidate
        that is not. Overname happens on REQUEST-time -- decided here,
        before issuing a new request -- never on attest-time (rewriting an
        already-attested final verdict), since that would let evidence
        change owners after the fact.

        CHAIN WALK: starting at ``gate``, each candidate's own last result is
        read via ``_read_existing_gate_result``. A candidate with no prior
        result yet, or a non-``lane_exhausted`` one (``unreadable_verdict`` /
        ``no_response`` abstain -- never take over), STOPS the walk there and
        that candidate is (re-)dispatched live. A ``lane_exhausted``
        candidate is recorded into ``path`` and the walk continues to
        ``chain.get(current)``. This means a hop through an
        ALREADY-PROVEN-dead gate (one whose own exhaustion was recorded in
        an earlier round) costs no extra round, but a NEWLY-discovered dead
        gate still needs its own round before the NEXT hop can act on it --
        the two-round latency documented below is per NEW gate along the
        path, not per round of ``request_reviews``. This is a deliberate
        choice (dispatch 20260826-beta3-e1, plan open question "per ronde
        een stap of binnen een ronde door?"): it lets an already-known-dead
        multi-hop chain resolve in ONE round instead of needing N rounds to
        walk N already-recorded hops, while still respecting the two-round
        rule for any hop that has not yet been proven dead by an actual
        dispatch attempt.

        The decision reads the LAST SAVED result, never a run in flight: a
        synchronous refusal (binary missing) and an asynchronous one (quota 403
        surfacing mid-execution) are both read from the record they leave, one
        uniform rule. A request round on its own therefore fills a seat whose
        reviewer died in it only on the next round. ``request_and_execute``
        closes that gap without a second rule (OI-2058 R5a, case d): after the
        round's reviewers ran, a seat whose reviewer left its own record on this
        head that this walk passes over as run out is walked on, forward from
        that reviewer (``_advance_seat``), and its next reviewer is requested and
        run in the same call. A reviewer that decided, PASS or FAIL, keeps its
        seat.

        If the chain runs out (no ``chain.get(current)``) while ``current``
        is still ``lane_exhausted``, that is a NAMED terminal end-state
        (``_chain_exhausted_result``) -- never a silent fallback to
        re-dispatching the originally-requested ``gate``.

        REACHABILITY (OI-1454): a seat is also passed over when the provider
        behind it is recorded unreachable (quota spent, credential refused --
        ``provider_reachability``), even with no result of its own for this
        PR. That record is a fact about the provider, not about a commit, so
        it is not head-scoped; it expires (an unreachable record lives one
        quota-cooldown window) and past that the seat is asked again. It is
        consulted only where this PR has no answer of its own from the seat:
        a seat that already produced a verdict (or abstained) on this head is
        dispatched as before, and reachability never overrules it. Every hop
        it adds to ``path`` names the reason and the record it came from.

        HEAD SCOPING (OI-1668): every one of those decisions rests on a
        candidate's last recorded result, and a result is a statement about
        ONE commit. A record produced against an earlier head says nothing
        about the head under review now, so it may not decide whether this
        seat gets skipped -- ``gate_recorder.result_is_for_head`` answers
        that, and a record belonging to another head (or to none) STOPS the
        walk exactly as a missing record does: the candidate is dispatched
        live. Live shape this closes, PR #1808 on 2026-09-07: codex's seat
        held an exhaustion recorded against head ``5fd261fd`` while the PR had
        already moved to ``1f8c985f``, and the walk kept routing around a seat
        that had never been asked about the current code.

        The head sha comes from ``head`` (``_PrHead``), shared by every seat of
        the round: the head ``request_and_execute`` observed, or one resolved
        LAZILY -- once, on the first candidate that actually has a prior result
        -- so the common first-round shape (no record anywhere) still costs no
        GitHub round-trip, and a multi-hop walk costs one lookup rather than
        one per hop. When it cannot be resolved at all (``""``: gh missing,
        unauthenticated, or a PR that does not resolve) the head question is
        unanswerable, and ``result_is_for_head`` answers True so the walk
        behaves exactly as it did before -- a failed lookup must never silently
        redirect a takeover decision. A standing verdict is held to the
        stricter bar: an unanswerable head never lets one stand
        (``_standing_verdict_gap``).

        ONE REQUEST PER GATE, ONE SEAT PER REVIEWER (``held``, OI-2058 R5a):
        the gates that earlier seats of the SAME ``request_reviews`` round
        actually got dispatched, each mapped to the seat it reads
        (``_held_reviewers``). A gate is requested once per round: requesting it
        again re-wrote the request record (dropping the takeover annotation the
        first request carried) and made the executor run the same reader twice.
        And a reviewer fills at most one seat: with the default stack
        ``codex_gate,kimi_gate`` and codex at its limit, kimi takes codex's seat,
        and kimi's own seat used to request nothing at all, so one reviewer stood
        in for two seats. Now a candidate that already reads another seat is
        passed over with a ``reads_another_seat`` hop, and the seat walks on to
        the next DISTINCT reviewer (deepseek, then glm). A seat with no distinct
        reviewer left is booked unfillable by name (``_chain_exhausted_result``,
        ``reason=no_distinct_reviewer``, ``reviewers_tried``), also when the
        seat's own gate is held and has no successor.

        Only a request that went out counts. An earlier request the gate itself
        refused (``not_executable``, ``unavailable``, ``blocked``,
        ``not_configured``, ``chain_exhausted``) asked no reader anything, so a
        later seat that resolves to the same gate is dispatched, not skipped: a
        refusal must not suppress the one seat that could still get a verdict.

        ``seat_fields`` (``seat`` and ``review_seats``, from ``request_reviews``)
        land on the payload and on the request record it writes, so the record
        says which seat it fills and which seat list was requested.

        PROVIDER SWITCH (provider-schakelaar C): the operator's switch is
        consulted for every seat before the chain branch, and for every
        candidate the walk reaches, read fresh each time. A switched-off
        candidate is passed over with a ``provider_switched_off`` hop before a
        live dispatch would happen, in the same round (no second round is
        needed: the refusal is known now, not measured from a failed run). A
        chain that runs out that way books ``chain_exhausted`` with each hop's
        reason named. A switched-off seat with no successor to pass over to
        (not a chain key: the chain tail, an empty chain, a profile stack) is
        booked ``not_executable``/``provider_switched_off``, a record the gate's
        own later result for the same head replaces. The switch governs calls,
        never evidence: a switched-off seat whose own decided pass or fail on
        this head is on disk keeps the seat -- it is neither re-dispatched nor
        passed over, and nothing is written over that record. The verdict is
        reported as the seat's only when it clears the bar a fresh result
        clears before the executor counts it (complete evidence, its report on
        disk, the observed head: ``_standing_verdict_gap``); otherwise the seat
        is booked ``not_executable``/``provider_switched_off`` and never counts
        as a pass. The refusal never reaches the reachability record, the
        known-rejected ledger or a cooldown, so switching the family back on
        lets the next request dispatch it.

        MODEL (R7b, operator decision 2026-10-22): a seat never gets a reviewer
        that runs the builder's model (``builder``, ``review_vendor``); the same
        vendor on another model may take the seat, and the seat's
        ``builder_vendor`` record then says ``rule: model_differs``. Such a
        reviewer is passed over like an unavailable one, first choice or
        takeover alike: the seat rolls to the next gate in the chain and the
        record names it (``takeover_from``, ``takeover_reason``
        ``model_matches_builder``). A seat whose own gate runs the builder's model and
        has no successor is booked unfillable. A builder whose vendor is unknown
        applies no rule; the seat's ``builder_vendor`` field says why.
        """
        if chain is None:
            chain = _build_review_gate_takeover_chain()
        if head is None:
            head = _PrHead(pr_number)

        reader, path, refusal = self._resolve_seat_reader(
            gate, pr_number, chain, held or {}, head, builder=builder,
        )
        if reader is None:
            return self._chain_exhausted_result(
                gate=gate, pr_number=pr_number, branch=branch, dispatch_id=dispatch_id, path=path,
                seat_fields=seat_fields,
            )
        return self._request_seat_reader(
            reader, refusal, path, pr_number=pr_number, branch=branch, risk_class=risk_class,
            changed_files=changed_files, mode=mode, dispatch_id=dispatch_id, head=head,
            seat_fields=_with_reader_models(seat_fields, reader, builder),
        )

    def _resolve_seat_reader(
        self, gate: str, pr_number: int, chain: Dict[str, str], held: Dict[str, str], head: _PrHead,
        *, builder: Optional[BuilderVendor] = None,
    ) -> "tuple[Optional[str], List[Dict[str, Any]], Optional[SwitchDecision]]":
        """``(reader, path, refusal)`` for the seat ``gate``; dispatches nothing.

        A gate with no configured successor -- the chain is empty (operator
        explicitly disabled takeover) or this gate was never wired into it --
        reads its own seat, exactly as it always has, and is NEVER walked into a
        chain_exhausted state (that state is reserved for a chain that WAS
        entered and then ran out). The one exception is a gate that already
        reads another seat of this request: it has nobody to hand the seat to,
        so the seat has no distinct reviewer (``reader`` ``None``). A gate that
        shares the builder's vendor (R7) has nobody to hand the seat to either.
        """
        refusal = self._seat_switch_refusal(gate)
        if gate in chain:
            return self._walk_takeover_chain(
                gate, pr_number, chain, refusal, head=head, held=held, builder=builder,
            )
        if gate in held:
            return None, [_held_seat_hop(gate, held[gate])], None
        vendor_hop = vendor_match_hop(gate, builder)
        if vendor_hop is not None:
            return None, [vendor_hop], None
        return gate, [], refusal

    def _request_seat_reader(
        self, reader: str, refusal: Optional[SwitchDecision], path: List[Dict[str, Any]], *,
        pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str, head: _PrHead, seat_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Request ``reader`` for a seat whose walk ended on it, ``path`` the hops passed over.

        The request record ``_dispatch_one_review`` just wrote is rewritten with
        ``seat_fields`` (in one write with the takeover annotation when there is a
        path), the same "write the payload back" ``_stamp_takeover_annotations``
        has always done for a takeover.
        """
        if refusal is not None:
            return self._refuse_switched_off_seat(
                reader, refusal, pr_number=pr_number, branch=branch, risk_class=risk_class,
                changed_files=changed_files, mode=mode, dispatch_id=dispatch_id, path=path, head=head,
                seat_fields=seat_fields,
            )

        payload = self._dispatch_one_review(reader, pr_number, branch, risk_class, changed_files, mode, dispatch_id)
        payload.update(seat_fields)
        if not path:
            request_file = self._request_path(reader, pr_number)
            if seat_fields and request_file.exists():
                self._write_request_record(request_file, payload)
            return payload

        last_hop = path[-1]
        return self._stamp_takeover_annotations(
            payload,
            pr_number=pr_number,
            takeover_from=last_hop["gate"],
            failure_reason=_takeover_failure_reason(path, reader),
            takeover_reason=last_hop["reason"],
            takeover_source_status=last_hop["status"],
            takeover_path=path,
        )

    def _refuse_switched_off_seat(
        self,
        gate: str,
        decision: SwitchDecision,
        *,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
        path: List[Dict[str, Any]],
        head: _PrHead,
        seat_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """The seat payload for a switched-off ``gate`` that is not passed over.

        Two shapes, and neither dispatches anything:

        - ``gate``'s own decided verdict is on disk and stands
          (:func:`_standing_verdict_gap`: complete evidence, its report on disk,
          and the head ``head`` observed). The payload reports it (status and
          evidence as recorded, plus the switch decision and ``seat_fields``)
          and NOTHING is written: no request, no result, no takeover annotation
          on the evidence record.
        - otherwise, including a decided verdict that does not stand: ``gate``
          is booked ``not_executable``/``provider_switched_off`` through the
          same request-time writers every other refusal uses (request record,
          guarded result record, skip-rationale), which never counts as a pass.
          A verdict that does not stand is named on the booked request; the
          guard leaves the record itself on disk.
        """
        existing = self._read_existing_gate_result(gate, pr_number)
        verdict = decided_verdict(existing) if existing is not None else ""
        gap = ""
        if verdict:
            # The head lookup is a gh round-trip, so it runs only when there is a verdict to bind.
            gap = _standing_verdict_gap(existing, head.sha())
            if not gap:
                payload = _standing_verdict_payload(existing, gate, verdict, decision, path)
                payload.update(seat_fields or {})
                return payload
            gap = f"{gate}'s own {verdict} on disk does not stand: {gap}"
            logger.warning("gate_request_handler: pr=%s %s", pr_number, gap)
        return self._book_switched_off_seat(
            gate, decision, pr_number=pr_number, branch=branch, risk_class=risk_class,
            changed_files=changed_files, mode=mode, dispatch_id=dispatch_id, head=head,
            standing_verdict_refused=gap, seat_fields=seat_fields,
        )

    def _book_switched_off_seat(
        self,
        gate: str,
        decision: SwitchDecision,
        *,
        pr_number: int,
        branch: str,
        risk_class: str,
        changed_files: List[str],
        mode: str,
        dispatch_id: str,
        head: _PrHead,
        standing_verdict_refused: str = "",
        seat_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Book a switched-off seat ``not_executable``/``provider_switched_off``.

        Written with the decision already made for this seat, never a second
        one, so the request record and the result record name the same
        refusal. The result write goes through the overwrite guard, where
        ``provider_switched_off`` is a runner-refusal note: it never displaces
        what the provider already did on this head, and the gate's own later
        result replaces it. ``commit_sha`` is the round's head (``head``), and
        ``standing_verdict_refused`` names why the seat's own verdict on disk
        did not stand, when it had one. ``seat_fields`` go on the request
        record only: the result record is the gate's evidence, not the round's.
        """
        from review_gate_manager import _utc_now

        logger.warning(
            "gate_request_handler: switched-off seat %s for pr=%s is not passed over -- "
            "booking %s (%s)", gate, pr_number, PROVIDER_SWITCHED_OFF_REASON, decision.detail(),
        )
        provider = resolve_gate_provider(gate)
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "status": "not_executable",
            "provider": provider[1] if provider else "",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": head.sha(),
            "report_path": "",
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if standing_verdict_refused:
            payload["standing_verdict_refused"] = standing_verdict_refused
        self._mark_gate_unavailable(
            payload, gate=gate, pr_number=pr_number, pr_id="", dispatch_id=dispatch_id,
            classification=(PROVIDER_SWITCHED_OFF_REASON, decision.detail()),
        )
        payload.update(seat_fields or {})
        self._write_request_record(self._request_path(gate, pr_number), payload)
        return payload

    def request_reviews(
        self,
        *,
        pr_number: int,
        branch: str,
        review_stack: Optional[Iterable[str]] = None,
        risk_class: str,
        changed_files: Iterable[str],
        mode: str,
        dispatch_id: str = "",
        head_sha: Optional[str] = None,
        override_b3: str = "",
        force_rerun: str = "",
    ) -> Dict[str, Any]:
        """Request every seat of the review stack for ``pr_number``, each from its own reviewer.

        The seats come from ``review_seats.resolve_review_seats`` (OI-2058,
        R5a): ``review_stack`` when the caller named one, else the project's
        ``VNX_DEFAULT_REVIEW_STACK`` read fresh, never the import-time
        ``DEFAULT_REVIEW_STACK``. An unknown or retired name raises
        ``review_seats.ReviewSeatError`` before anything is requested. The
        entries are requested in stack order; a seat walks the takeover chain
        past every reviewer an earlier seat of this round already holds
        (``_dispatch_review_seat``), an extra gate (``ci_gate``, an optional
        gate) is requested once and fills no seat.

        Every seat's payload, and the request record it writes, carries
        ``seat`` and ``review_seats`` (the seat list, its source and the value
        it was read from); the return value carries ``review_seats`` too, so an
        evaluator can hold a merge to the stack that was requested even after
        the config changed.

        The round's risk class is derived from the PR diff, not taken from the
        caller (R3a, :meth:`_review_risk`): ``risk_class`` can only raise it.
        Every seat carries it as ``review_risk`` next to ``review_seats``, every
        entry is requested with the derived class (so the codex reasoning effort
        follows it), and the return value carries ``review_risk``.

        ``head_sha`` is the PR head the caller already observed
        (``request_and_execute``), shared by every seat of the round; ``None``
        resolves it once per round (``_PrHead``), for the risk class.

        Every seat passes the round ledger first (R1, :meth:`_round_refusal`),
        before its takeover walk: this is where every run starts (the CLI, the
        obligation runner, ``vnx gate``, a takeover), so no entry runs past it.
        ``override_b3`` is the reason of an ``--override-b3``: it wins over the
        B3 cap for the codex seat and travels on that seat's request.

        ``force_rerun`` (R10) is the reason of an operator force-rerun from the
        obligation runner. It is logged once and travels as ``force_rerun`` on
        every request of the round, into the result record; it lifts no
        refusal of the round ledger.
        """
        from review_seats import resolve_review_seats

        changed_files = [str(path).strip() for path in changed_files if str(path).strip()]
        seats = resolve_review_seats(explicit_stack=review_stack, changed_files=changed_files)
        seat_record = seats.to_record()
        requested: List[Dict[str, Any]] = []
        operator_fields = self._force_rerun_fields(force_rerun, pr_number=pr_number, head_sha=head_sha)

        # Resolved ONCE per request_reviews() call (not per gate in the
        # stack): every seat in this stack takes over against the SAME
        # operator-configured chain, and a single log line per PR-review
        # cycle is enough operator visibility without repeating it per gate.
        chain = _build_review_gate_takeover_chain()
        head = _PrHead(pr_number, head_sha)
        rounds = _PrRounds.read(self.results_dir.parent, pr_number)
        builder = self._builder_vendor(dispatch_id)
        risk = self._review_risk(pr_number=pr_number, head=head, caller_class=risk_class, dispatch_id=dispatch_id)
        risk_record = risk.to_record()

        for gate in seats.stack:
            seat_fields = {
                "seat": gate, "review_seats": seat_record, "review_risk": risk_record,
                "builder_vendor": builder.to_record(),
            } if gate in seats.seats else {}
            seat_fields.update(operator_fields)
            payload = self._round_refusal(
                rounds, head, pr_number=pr_number, branch=branch, seat_fields=seat_fields,
                override_b3=override_b3,
            ) or self._dispatch_review_seat(
                gate, pr_number, branch, risk.risk_class, changed_files, mode, dispatch_id, chain=chain,
                held=_held_reviewers(requested), head=head, seat_fields=seat_fields, builder=builder,
            )
            requested.append(payload)
            self._emit_review_request_receipt(
                payload, gate=payload["gate"], pr_id=str(pr_number), pr_number=pr_number,
                branch=branch, mode=mode, risk_class=risk.risk_class, changed_files=changed_files,
                request=payload, dispatch_id=dispatch_id,
            )

        return {
            "pr_number": pr_number,
            "branch": branch,
            "requested": requested,
            "review_seats": seat_record,
            "review_risk": risk_record,
        }

    def _review_risk(self, *, pr_number: int, head: _PrHead, caller_class: str, dispatch_id: str) -> Any:
        """The round's risk class, derived from the PR diff (R3a, ``review_risk_class``).

        The diff is read with local git in the project's checkout
        (``PROJECT_ROOT``, else the process cwd) against the PR's own base
        (``baseRefName``, :meth:`_PrHead.base`); nothing is fetched, and a head
        or a base that is not there, or a base the PR does not name, makes the
        class ``high``. ``irreversible`` is read from the dispatch's spec in this
        store (the one the review gates live in, as :meth:`_builder_vendor`
        reads it). ``caller_class`` only raises.
        """
        import review_risk_class

        root = (getattr(self, "paths", None) or {}).get("PROJECT_ROOT")
        base_ref, why_no_base = review_risk_class.base_ref_for(head.base())
        reading = (
            review_risk_class.read_pr_diff(head.sha(), base_ref=base_ref, cwd=Path(root) if root else None)
            if base_ref is not None else review_risk_class.DiffReading(None, error=why_no_base)
        )
        state_dir = getattr(self, "state_dir", None) or Path(self.results_dir).parent.parent
        spec = review_risk_class.spec_flag(dispatch_id, data_dir=Path(state_dir).parent)
        risk = review_risk_class.derive_risk_class(reading, caller_class=caller_class, spec=spec, head_sha=head.sha())
        logger.info("gate_request_handler: pr=%s %s", pr_number, risk.explain())
        return risk

    @staticmethod
    def _force_rerun_fields(force_rerun: str, *, pr_number: int, head_sha: Optional[str]) -> Dict[str, Any]:
        """``{"force_rerun": record}`` for every request of a forced round (R10), ``{}`` without a reason."""
        import gate_run_guard

        reason = (force_rerun or "").strip()
        if not reason:
            return {}
        logger.warning(
            "gate_request_handler: %s for pr=%s -- reason: %s", gate_run_guard.FORCE_RERUN_USAGE, pr_number, reason,
        )
        return {"force_rerun": gate_run_guard.force_rerun_record(
            reason, head_sha=head_sha or "", set_by="gate_obligation_runner --force-rerun",
        )}

    def _round_refusal(
        self, rounds: _PrRounds, head: _PrHead, *, pr_number: int, branch: str,
        seat_fields: Dict[str, Any], override_b3: str,
    ) -> Optional[Dict[str, Any]]:
        """The seat's payload when the round ledger refuses its run (R1), else ``None``.

        Four refusals, checked in this order, and only for a seat (an extra gate
        such as ``ci_gate`` is no seat and is never refused here):

        - the ledger is unreadable for this PR: every seat is refused until the
          operator has set the file aside and reopened the PR;
        - the PR was reviewed before the ledger counted it and is not opened in
          it (``round_history_missing``): every seat is refused until the
          operator opens it with ``vnx review rounds open --pr N --reason``;
          ``override_b3`` does not lift this, it is about the cap;
        - the seat already has a verdict on this head: no second run on a head
          with a verdict, in any seat (:meth:`_standing_round_payload`);
        - the codex seat of a PR at the B3 cap, on every head, unless a
          tiebreaker granted the round (``review_rounds.round_granted_past_cap``,
          R2a: a ``verkleinen`` verdict and a head with a commit of its
          dispatch) or ``override_b3`` carries a reason. An applied override is
          put on ``seat_fields``, so it travels on the request into the verdict,
          the ledger line and the receipt.
        """
        import review_cap_verdicts
        import review_rounds

        seat = str(seat_fields.get("seat") or "")
        if not seat:
            return None
        if rounds.unreadable:
            return _round_refused_payload(
                seat, pr_number=pr_number, branch=branch, head_sha="",
                reason=review_rounds.REFUSED_LEDGER_UNREADABLE, detail=rounds.unreadable, seat_fields=seat_fields,
            )
        if rounds.history_missing:
            logger.warning("gate_request_handler: seat %s refused -- %s", seat, rounds.history_missing)
            return _round_refused_payload(
                seat, pr_number=pr_number, branch=branch, head_sha="",
                reason=review_rounds.REFUSED_HISTORY_MISSING, detail=rounds.history_missing, seat_fields=seat_fields,
            )
        standing = rounds.line_on_head(seat, head)
        if standing is not None:
            return self._standing_round_payload(standing, pr_number=pr_number, branch=branch, seat_fields=seat_fields)
        cap = rounds.cap
        if seat != review_rounds.CODEX_SEAT or not cap.capped:
            return None
        grant = rounds.grant_on(head.sha())
        if review_rounds.round_granted_past_cap(cap, head_sha=head.sha(), grant=grant, lines=rounds.lines):
            return None
        if override_b3.strip():
            seat_fields["override_b3"] = review_rounds.override_record(override_b3.strip(), cap)
            logger.warning(
                "gate_request_handler: --override-b3 applied for pr=%s seat %s -- %s; reason: %s",
                pr_number, seat, cap.summary(), override_b3.strip(),
            )
            return None
        detail = review_rounds.cap_refusal_text(cap, where=f"codex-seat run refused for PR #{pr_number}")
        detail = " ".join(part for part in (detail, review_cap_verdicts.grant_note(grant, rounds.lines, head.sha())) if part)
        logger.warning("gate_request_handler: %s", detail)
        return _round_refused_payload(
            seat, pr_number=pr_number, branch=branch, head_sha=head.sha(),
            reason=review_rounds.REFUSED_B3_CAP, detail=detail, seat_fields=seat_fields,
        )

    def _standing_round_payload(
        self, line: Any, *, pr_number: int, branch: str, seat_fields: Dict[str, Any],
    ) -> Dict[str, Any]:
        """A seat that already has a verdict on this head: reported, never run again (R1).

        When the reviewer's result record is that very verdict (this head, its
        report, its verdict), the record is reported as the seat's answer, so a
        PASS reads as a pass and a FAIL as a fail, and nothing is written. Any
        other record on disk does not carry the ledger's verdict: the seat is
        then refused as ``blocked``, which never counts as a pass.
        """
        import review_rounds

        detail = (
            f"seat {line.seat} of PR #{pr_number} already has a {line.verdict.upper()} on head "
            f"{line.head_sha[:12]} ({line.reviewer}, {line.report_name}): a second run on a head with a "
            "verdict is refused (R1). Push a new head to have it reviewed again"
        )
        logger.info("gate_request_handler: %s", detail)
        record = self._read_existing_gate_result(line.reviewer, pr_number)
        if (
            record is not None and str(record.get("commit_sha") or "") == line.head_sha
            and Path(str(record.get("report_path") or "")).name == line.report_name
            and review_rounds.ledger_verdict(record) == line.verdict
        ):
            payload = dict(record)
            payload.update(seat_fields)
            payload.update({"redispatch_refused": detail, "round_refusal": review_rounds.REFUSED_VERDICT_ON_HEAD})
            return payload
        detail += (
            ". Its result record does not carry this verdict (an interrupted publication, state contract "
            f"rule 2): only the operator releases the head, with `{review_rounds.open_command(pr_number)}`"
        )
        return _round_refused_payload(
            line.reviewer, pr_number=pr_number, branch=branch, head_sha=line.head_sha,
            reason=review_rounds.REFUSED_VERDICT_ON_HEAD, detail=detail,
            seat_fields={**seat_fields, "ledger_verdict": line.verdict},
        )

    def _emit_review_request_receipt(
        self, payload: Dict[str, Any], *, gate: str, pr_id: str, branch: str, mode: str,
        risk_class: str, changed_files: List[str], dispatch_id: str,
        status: Optional[str] = None,
        pr_number: Optional[int] = None, contract_hash: Optional[str] = None,
        request: Optional[Dict[str, Any]] = None,
        optional_gate: Optional[Tuple[bool, bool]] = None,
    ) -> None:
        """The ONE ``review_gate_request`` emitter: one event, one field set (GE-4c-2, E4).

        ``payload`` is the request record the caller just wrote and supplies
        the receipt's status; ``status`` overrides it for the one path whose
        record names its state in a field of its own (the optional gate's
        ``state``). The optional arguments are the per-path fields, each
        omitted when the calling path has none -- ``pr_number`` and
        ``contract_hash`` are keys of the append's idempotency, so stamping
        one on a path that never carried it changes what dedupes against
        what. ``request`` is the whole record when the caller has a
        slot-shaped one, and a takeover request's ``failure_reason`` and
        ``takeover_from`` travel top-level next to it (OI-1415).
        """
        from review_gate_manager import emit_governance_receipt

        fields: Dict[str, Any] = {
            "pr_id": str(pr_id),
            "branch": branch,
            "gate": gate,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "dispatch_id": dispatch_id,
        }
        if pr_number is not None:
            fields["pr_number"] = pr_number
        if contract_hash is not None:
            fields["contract_hash"] = contract_hash
        if request is not None:
            fields["request"] = request
            if request.get("takeover"):
                fields["failure_reason"] = request["failure_reason"]
                fields["takeover_from"] = request["takeover_from"]
        if optional_gate is not None:
            fields["contributed_evidence"], fields["was_intentionally_absent"] = optional_gate
        emit_governance_receipt(
            _REVIEW_REQUEST_EVENT,
            receipt_kind="review_gate",
            status=status if status is not None else payload["status"],
            terminal="T0",
            **fields,
        )

    def _advance_seat(
        self, payload: Dict[str, Any], held: Dict[str, str], *, pr_number: int, branch: str,
        risk_class: str, changed_files: List[str], mode: str, dispatch_id: str, head_sha: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Hand ``payload``'s seat on after its reviewer ran out (OI-2058 R5a, case d).

        Only a confirmed exhaustion moves a seat (codex FAIL on #2073). The
        reviewer that just ran for the seat (``payload['gate']``) must have left
        its own record on this head that the request round passes over as run
        out (:meth:`_exhaustion_hop`): kimi at its weekly limit, measured four
        times on 2026-10-15/16. A reviewer that decided, PASS or FAIL, or
        abstained, or left no fresh record keeps the seat, and ``None`` is
        returned with nothing written.

        The walk then runs FORWARD from that reviewer, never again from the
        seat: the chain names each gate once, so an earlier reviewer of the
        chain can never get the seat back. Re-walking from the seat let an
        exhaustion record of codex that expired while kimi read codex's seat
        pull the seat back to codex, superseding kimi's FAIL with codex's PASS.
        When the walk lands on a reviewer, it is requested for the seat
        (takeover annotation with the seat's whole path, seat fields, receipt)
        and its payload returned.

        ``held`` is every reviewer of this call that still holds a seat; the
        seat's own reviewer is left out, so its fresh record decides, not the
        fact that it held the seat. A walk that runs out, or stops at a
        switched-off seat's standing verdict, books nothing here: the seat keeps
        the answer it has, and the next round books the end-state. ``payload``
        is a seat's payload (it carries ``seat``); a booked end-state has no
        reviewer to replace.

        The successor passes the round ledger first, read again and judged by
        :meth:`_round_refusal` exactly as the seat's first request was (R1c,
        OI-2181): while the reviewer ran, a verdict may have landed on this
        head, the cap may have fallen, or the ledger may have turned
        unreadable. A refused advance books nothing and spends no run.
        """
        seat, reviewer = payload["seat"], payload["gate"]
        if payload.get("status") == "chain_exhausted":
            return None
        head = _PrHead(pr_number, head_sha)
        ran_out = self._exhaustion_hop(reviewer, pr_number, head)
        if ran_out is None:
            return None
        chain = _build_review_gate_takeover_chain()
        successor = chain.get(reviewer)
        if successor is None:
            return None
        others = {gate: holder for gate, holder in held.items() if holder != seat}
        builder = self._builder_vendor(dispatch_id)
        reader, walked, refusal = self._walk_takeover_chain(
            successor, pr_number, chain, self._seat_switch_refusal(successor), head=head, held=others,
            builder=builder,
        )
        if reader is None or refusal is not None:
            return None
        # An --override-b3 the seat's request carried goes with the seat (R1).
        seat_fields = {
            "seat": seat, "review_seats": payload.get("review_seats"), "review_risk": payload.get("review_risk"),
            "builder_vendor": same_vendor_record(reader, builder, builder.to_record()),
        }
        override = payload.get("override_b3") if isinstance(payload.get("override_b3"), dict) else {}
        refused = self._round_refusal(
            _PrRounds.read(self.results_dir.parent, pr_number), head, pr_number=pr_number, branch=branch,
            seat_fields=seat_fields, override_b3=str(override.get("reason") or ""),
        )
        if refused is not None:
            logger.warning(
                "gate_request_handler: seat %s for pr=%s is not handed from %s to %s -- the round ledger "
                "refuses it (%s)", seat, pr_number, reviewer, reader, refused.get("round_refusal") or refused.get("reason"),
            )
            return None
        path = list(payload.get("takeover_path") or []) + [ran_out] + walked
        logger.info(
            "gate_request_handler: seat %s for pr=%s moves from %s to %s in the same call -- "
            "%s left a record that passes it over", seat, pr_number, reviewer, reader, reviewer,
        )
        advanced = self._request_seat_reader(
            reader, None, path, pr_number=pr_number, branch=branch, risk_class=risk_class,
            changed_files=changed_files, mode=mode, dispatch_id=dispatch_id, head=head,
            seat_fields=seat_fields,
        )
        self._emit_review_request_receipt(
            advanced, gate=advanced["gate"], pr_id=str(pr_number), pr_number=pr_number,
            branch=branch, mode=mode, risk_class=risk_class, changed_files=changed_files,
            request=advanced, dispatch_id=dispatch_id,
        )
        return advanced

    def _exhaustion_hop(self, reviewer: str, pr_number: int, head: _PrHead) -> Optional[Dict[str, Any]]:
        """The takeover hop for ``reviewer`` when its own record on ``head`` says it ran out, else ``None``.

        The record must exist and be about this head: a missing record, or one
        about another commit, confirms nothing about this run. On that record the
        request round's own candidate rule decides (``_candidate_hop``, not
        switched off, holding no seat): a ``lane_exhausted`` record within its
        TTL, or a record that does not hold the seat while the provider is
        recorded unreachable, is passed over; a decided verdict, an abstention
        or an expired exhaustion is not.
        """
        record = self._read_existing_gate_result(reviewer, pr_number)
        if record is None or not result_is_for_head(record, head.sha()):
            return None
        hop, _stands = self._candidate_hop(reviewer, None, pr_number, head=head, held={})
        return hop

    def _mark_gate_unavailable(
        self,
        payload: Dict[str, Any],
        *,
        gate: str,
        pr_number: Optional[int],
        pr_id: str,
        contract_hash: str = "",
        dispatch_id: str = "",
        classification: Optional[Tuple[str, str]] = None,
    ) -> None:
        """Record unavailability in payload and write skip/result records.

        ``classification`` is ``(reason, detail)`` when the caller already
        decided why the gate is unavailable (a provider-switch refusal, decided
        once per seat); otherwise ``_classify_unavailable`` answers.

        The in-memory ``payload`` (the REQUEST record) always carries
        ``reason``/``reason_detail``/``failure_reason`` regardless of what
        happens to the RESULT record below -- a request-time refusal is
        never allowed to go quiet even when the guard blocks the write it
        would otherwise cause (OI-1469/OI-1470/OI-1471).

        OI-1624: ``branch``/``commit_sha`` are read straight off the
        caller's own ``payload`` -- every real caller (``_request_codex``,
        ``_request_gemini``, ``_request_kimi``, ``_request_ci_gate``, the
        contract-flow builders) already stamped both before deciding
        availability, so this never re-resolves anything; it only stops
        dropping what the caller already knew on the way into the RESULT
        record (see ``_write_not_executable_result``'s docstring for why
        that mattered live on PR #1777).

        ``audit_refusal=False`` (OI-1750 second finding): this method
        already calls ``_write_skip_rationale`` below UNCONDITIONALLY, on
        every call regardless of whether the guard refuses the result write
        -- the exact shape ``gate_recorder.record_not_executable`` opted out
        for (OI-1707). Without the opt-out, a guard refusal here would land
        TWO lines in the ledger for the same event: this call's own
        ``gate_result_write_refused`` plus the skip-rationale line below,
        which already explains the same refusal under its own ``reason``.
        The head that line would have carried moves to the skip-rationale
        call instead (``commit_sha=`` below), so nothing about the refused
        write is lost.
        """
        reason, detail = classification or self._classify_unavailable(gate)
        payload["reason"] = reason
        payload["reason_detail"] = detail
        # OI-1415 (same fix as #1666 for phantom_guard/pr_enforcement): stamp
        # the canonical failure_reason field with the SAME text as the
        # lane-own reason_detail above -- a seat that refuses on its very
        # first round (no prior recorded result, so `_dispatch_review_seat`
        # never reaches the takeover branch) must still carry a reason a
        # generic failure-reason reader can find, not only a lane-specific
        # field name.
        payload["failure_reason"] = detail
        payload["resolved_at"] = payload["requested_at"]
        _result_payload, written = self._write_not_executable_result(
            gate=gate, pr_number=pr_number, pr_id=pr_id,
            reason=reason, reason_detail=detail,
            contract_hash=contract_hash,
            dispatch_id=dispatch_id,
            branch=payload.get("branch") or "",
            commit_sha=payload.get("commit_sha") or "",
            audit_refusal=False,
        )
        if not written:
            # The skip-rationale call below already lands the durable line
            # for this refusal (reason=%s, with commit_sha); this log makes
            # the request-time caller's own awareness explicit
            # (OI-1469/OI-1470/OI-1471) -- the PR keeps its prior decided
            # verdict on disk, unpolluted.
            logger.warning(
                "gate_request_handler: not_executable write REFUSED for "
                "gate=%s pr=%s -- an existing terminal, evidenced result "
                "stands; this refusal (reason=%s) was request-time only "
                "and never reached results/",
                gate, pr_id or pr_number, reason,
            )
        self._write_skip_rationale(
            gate=gate, pr_id=pr_id or str(pr_number),
            reason=reason, reason_detail=detail,
            commit_sha=payload.get("commit_sha") or "",
        )

    def _request_gemini(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._gemini_available()
        requested_at = _utc_now()
        payload = {
            "gate": "gemini_review",
            "status": "requested" if available else "not_executable",
            "provider": "gemini_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._write_request_record(self._request_path("gemini_review", pr_number), payload)
        return payload

    def _build_gemini_contract_payload(
        self,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        available: bool,
        requested_at: str,
        prompt: str,
        pr_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "gate": "gemini_review",
            "status": "requested" if available else "not_executable",
            "provider": "gemini_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": None,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "prompt": prompt,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "dispatch_id": dispatch_id,
            "report_path": self._build_report_path(
                gate="gemini_review",
                requested_at=requested_at,
                pr_id=contract.pr_id,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="gemini_review",
                pr_number=None, pr_id=contract.pr_id,
                contract_hash=contract.content_hash,
                dispatch_id=dispatch_id,
            )
        return payload

    def request_gemini_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
        dispatch_id: str = "",
        pr_number: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Request a Gemini review driven by a canonical ReviewContract.

        Renders a deliverable-aware prompt from the contract and persists the
        request payload including the rendered prompt text and contract hash.

        Raises:
            gemini_prompt_renderer.MissingContractFieldError: when the contract is missing required fields.
        """
        from review_gate_manager import _utc_now

        prompt = render_gemini_prompt(contract)
        available = self._gemini_available()
        requested_at = _utc_now()
        payload = self._build_gemini_contract_payload(
            contract, mode, dispatch_id, available, requested_at, prompt, pr_number=pr_number,
        )

        self._write_request_record(
            self._contract_request_path("gemini_review", contract.pr_id), payload,
        )
        self._emit_review_request_receipt(
            payload, gate="gemini_review", pr_id=contract.pr_id, branch=contract.branch, mode=mode,
            risk_class=contract.risk_class, changed_files=contract.changed_files,
            contract_hash=contract.content_hash, dispatch_id=dispatch_id,
        )
        return payload

    def _validate_pr_number_for_github(
        self,
        pr_number: Optional[int],
        contract_pr_id: str,
    ) -> Optional[tuple]:
        if pr_number is None:
            return (
                STATE_BLOCKED,
                "missing_github_pr_number",
                "gh pr comment requires a real GitHub PR number; "
                f"governance pr_id {contract_pr_id!r} is not a valid PR ref",
            )
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            return (
                STATE_BLOCKED,
                "invalid_github_pr_number",
                f"pr_number must be a positive int (got {pr_number!r}); "
                f"the governance pr_id {contract_pr_id!r} is not a valid PR ref",
            )
        return None

    def _trigger_github_comment(self, pr_number: int, comment_body: str) -> tuple:
        proc = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", comment_body],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return (STATE_REQUESTED, None, None)
        return (STATE_BLOCKED, "claude_github_trigger_failed", proc.stderr.strip())

    def _determine_claude_github_state(
        self,
        configured: bool,
        contract_pr_id: str,
        comment_body: str,
        pr_number: Optional[int] = None,
    ) -> tuple:
        """Determine Claude GitHub review state from environment configuration.

        ``pr_number`` is the real GitHub PR number used to target ``gh pr comment``.
        ``contract_pr_id`` is the governance ID (e.g. "PR-4") and is *not* a valid
        GitHub PR reference. If we attempted to trigger a comment without a real
        ``pr_number``, ``gh`` would either fail outright or — worse, with a numeric
        contract id — target the wrong PR. Treat that as BLOCKED rather than
        silently issuing a bogus call.

        ``pr_number`` must be a positive ``int``. Any other value (including
        strings that look numeric, ``0``, or negative values) is rejected as a
        misuse — passing ``contract.pr_id`` here would silently target the
        wrong PR.

        Returns (state, reason, stderr_detail) tuple.
        """
        if not configured:
            return (STATE_NOT_CONFIGURED, "claude_github_not_configured", None)
        if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") != "1":
            return (STATE_CONFIGURED_DRY_RUN, None, None)
        invalid = self._validate_pr_number_for_github(pr_number, contract_pr_id)
        if invalid is not None:
            return invalid
        return self._trigger_github_comment(pr_number, comment_body)

    def _build_claude_github_payload(
        self,
        receipt: ClaudeGitHubReviewReceipt,
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        requested_at: str,
        stderr_detail: Optional[str],
    ) -> Dict[str, Any]:
        payload = receipt.to_dict()
        if stderr_detail:
            payload["stderr"] = stderr_detail
        payload["review_mode"] = mode
        payload["risk_class"] = contract.risk_class
        payload["changed_files"] = contract.changed_files
        payload["commit_sha"] = get_pr_head_sha(receipt.pr_number)
        payload["dispatch_id"] = dispatch_id
        payload["report_path"] = self._build_report_path(
            gate="claude_github_optional",
            requested_at=requested_at,
            pr_id=contract.pr_id,
        )
        return payload

    def _persist_claude_github_files(
        self,
        payload: Dict[str, Any],
        contract: ReviewContract,
        requested_at: str,
    ) -> None:
        """Write the optional gate's request record and its result-slot mirror (GE-4c-2, R10).

        The request goes through :meth:`_write_request_record` (Q7). The
        mirror stays on ``gate_recorder.write_result_guarded`` and not
        ``commit_gate_outcome``: every state it records — ``not_configured``,
        ``configured_dry_run``, ``requested``, ``blocked`` — is in flight or
        refused, not one of ADR-046 §1's four outcomes, so ``GateOutcome``
        refuses it by construction (as with the ci_gate ``running`` snapshot,
        R7b). Nothing is lost: the same slot lock, overwrite guard, atomic
        write and receipt emitter are already there, and a status owing no
        receipt books no ``review_gate_result`` line and no ``attempt_seq``.
        """
        request_file = self._contract_request_path("claude_github_optional", contract.pr_id)
        self._write_request_record(request_file, payload)

        # Persist the explicit state as a result record so closure_verifier can
        # observe optional-gate state via review_gates/results/ — without this
        # mirror, no_op / dry_run / requested / blocked configurations are
        # invisible to the verifier and break closure for normal optional-gate
        # paths.
        result_file = self._contract_result_path("claude_github_optional", contract.pr_id)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_payload = dict(payload)
        result_payload["gate"] = "claude_github_optional"
        result_payload["recorded_at"] = requested_at
        # GE-3: through the shared guard (slot lock, overwrite guard, atomic
        # write) instead of a bare write_text. This mirror shares its slot
        # with ``gate_result_parser.record_claude_github_result``, so a fresh
        # request must never erase the decided result recorded there. A
        # refusal is audited by the guard and does NOT fail the request: the
        # request file and the ``review_gate_request`` receipt still stand,
        # and the decided record stays on disk. Nothing is emitted for a
        # write that did not land.
        write_result_guarded(
            result_file,
            result_payload,
            gate="claude_github_optional",
            pr_ref=contract.pr_id,
        )

    def _build_claude_github_receipt(
        self,
        contract: ReviewContract,
        state: str,
        reason: Optional[str],
        requested_at: str,
        pr_number: Optional[int],
        comment_body: str,
    ) -> ClaudeGitHubReviewReceipt:
        return ClaudeGitHubReviewReceipt(
            pr_id=contract.pr_id,
            state=state,
            contract_hash=contract.content_hash,
            branch=contract.branch,
            pr_number=pr_number,
            gh_comment_body=comment_body if state == STATE_REQUESTED else "",
            reason=reason,
            requested_at=requested_at,
        )

    def _emit_claude_github_request_receipt(
        self,
        payload: Dict[str, Any],
        contract: ReviewContract,
        mode: str,
        dispatch_id: str,
        receipt: ClaudeGitHubReviewReceipt,
    ) -> None:
        """The optional gate's request receipt through the one emitter (GE-4c-2, E4).

        ``status`` is the receipt's ``state``: this path has always booked the
        optional gate's state as the line's status, and its record has no
        ``status`` field to read it from.
        """
        optional_gate = (receipt.contributed_evidence(), receipt.was_intentionally_absent())
        self._emit_review_request_receipt(
            payload, gate="claude_github_optional", pr_id=contract.pr_id, branch=contract.branch,
            mode=mode, risk_class=contract.risk_class, changed_files=contract.changed_files,
            contract_hash=contract.content_hash, optional_gate=optional_gate, dispatch_id=dispatch_id,
            status=receipt.state,
        )

    def request_claude_github_with_contract(
        self,
        *,
        contract: ReviewContract,
        mode: str = "per_pr",
        dispatch_id: str = "",
        pr_number: Optional[int] = None,
    ) -> ClaudeGitHubReviewReceipt:
        """Request a Claude GitHub review driven by a canonical ReviewContract.

        Determines the explicit review state from environment configuration and
        persists the request payload linked to the contract hash.

        ``pr_number`` is the real GitHub PR number; required only when the
        environment opts in to actually triggering ``gh pr comment``. The
        closure verifier requires the resulting state to be visible in the
        review_gates ``results/`` directory regardless of the state value, so
        the state is always materialised as a result record (not just a
        request) to keep the optional-gate evidence loop closed.
        """
        from review_gate_manager import _utc_now

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        comment_body = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")

        state, reason, stderr_detail = self._determine_claude_github_state(
            configured, contract.pr_id, comment_body, pr_number=pr_number,
        )

        receipt = self._build_claude_github_receipt(contract, state, reason, requested_at, pr_number, comment_body)
        payload = self._build_claude_github_payload(receipt, contract, mode, dispatch_id, requested_at, stderr_detail)
        self._persist_claude_github_files(payload, contract, requested_at)
        self._emit_claude_github_request_receipt(payload, contract, mode, dispatch_id, receipt)
        return receipt

    def _request_codex(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        required = mode == "final" or codex_final_gate_required(changed_files)
        available = self._codex_headless_available()
        # Model from env only; empty string means "use the registry's
        # openai.default_model", resolved in gate_runner._build_gate_cmd (OI-2062).
        # ~/.codex/config.toml never decides the gate's model.
        model = os.environ.get("VNX_CODEX_HEADLESS_MODEL") or os.environ.get("VNX_CODEX_MODEL") or ""
        requested_at = _utc_now()
        payload = {
            "gate": "codex_gate",
            "status": "requested" if available else "not_executable",
            "provider": "codex_headless",
            "model": model,
            "required": required,
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="codex_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="codex_gate",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._write_request_record(self._request_path("codex_gate", pr_number), payload)
        return payload

    def _request_kimi(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._kimi_gate_available()
        requested_at = _utc_now()
        payload = {
            "gate": "kimi_gate",
            "status": "requested" if available else "not_executable",
            "provider": "kimi",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="kimi_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="kimi_gate",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._write_request_record(self._request_path("kimi_gate", pr_number), payload)
        return payload

    def _request_glm(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """glm_gate is a recognised Gate.GLM_GATE member, since C6 step 1 a
        harness-lane gate: the governed dispatcher drives it and its runner
        file (scripts/glm_gate.py) is only the standalone entry point. Its
        availability is kind-based (gate_recorder.gate_is_available) —
        decided by REGISTRATION and the operator's provider switch, not by a
        file on disk. A registered harness-lane gate is unavailable only when
        its family is switched off (booked ``provider_switched_off``); the
        ``gate_runner_missing`` answer stays as the defensive one. Either way it
        refuses with a reason distinct from ``unknown_review_gate``
        (dispatch_spec Gate rejects an unknown request before it ever
        reaches here) so an operator can tell "not a real gate" apart from
        "real gate, route missing".
        """
        from review_gate_manager import _utc_now

        available = self._glm_gate_available()
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "glm_gate",
            "status": "requested" if available else "not_executable",
            "provider": "glm",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="glm_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ) if available else "",
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            # Classified by name (gate_result_parser._classify_unavailable): a
            # switched-off glm is provider_switched_off, never gate_runner_missing.
            reason, reason_detail = self._classify_unavailable("glm_gate")
            payload["reason"] = reason
            payload["reason_detail"] = reason_detail
            payload["resolved_at"] = payload["requested_at"]
            # audit_refusal=False (OI-1750 second finding): this branch
            # already calls _write_skip_rationale below unconditionally, so
            # the default audit-on-refusal would double-log the same
            # refused write -- same reasoning as _mark_gate_unavailable and
            # gate_recorder.record_not_executable (OI-1707). The head moves
            # to the skip-rationale call's commit_sha= instead.
            _result_payload, written = self._write_not_executable_result(
                gate="glm_gate", pr_number=pr_number, pr_id="",
                reason=reason, reason_detail=reason_detail,
                dispatch_id=dispatch_id,
                branch=branch,
                commit_sha=payload.get("commit_sha") or "",
                audit_refusal=False,
            )
            if not written:
                logger.warning(
                    "gate_request_handler: not_executable write REFUSED for "
                    "gate=glm_gate pr=%s -- an existing terminal, evidenced "
                    "result stands; this refusal (reason=%s) was request-time "
                    "only and never reached results/",
                    pr_number, reason,
                )
            self._write_skip_rationale(
                gate="glm_gate", pr_id=str(pr_number),
                reason=reason, reason_detail=reason_detail,
                commit_sha=payload.get("commit_sha") or "",
            )
        self._write_request_record(self._request_path("glm_gate", pr_number), payload)
        return payload

    def _request_deepseek(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """deepseek_gate is a config-based harness-lane gate (OI-1714, dispatch
        20260911-c6 step 2), reached through the "deepseek-harness" provider
        lane rather than a repo script, so its availability is decided by
        REGISTRATION and the provider switch (gate_recorder.gate_is_available)
        and it is requestable with no runner file on disk. It is a ``dispatch_spec.Gate``
        enum member (OI-1984) and a registered gate name
        (``REGISTERED_GATE_NAMES``): a legal review-gate-takeover-CHAIN link
        (BETA3-E1, 26-08 operator decision), a legal spec/staging gate and a
        legal ``VNX_DEFAULT_REVIEW_STACK`` entry.
        """
        return self._request_harness_seat(
            "deepseek_gate", "deepseek", self._deepseek_gate_available(),
            pr_number=pr_number, branch=branch, risk_class=risk_class,
            changed_files=changed_files, mode=mode, dispatch_id=dispatch_id,
        )

    def _request_claude(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """claude_gate (R7, operator decision 2026-10-22) is the claude review seat:
        a harness-lane gate on the "claude" lane (``claude_gate_lane``,
        ``claude -p`` on the operator's subscription, never provider_dispatch).
        Requested exactly like deepseek_gate: available by REGISTRATION, and the
        provider switch never refuses it (claude is not switchable, ADR-044 v1).
        """
        return self._request_harness_seat(
            "claude_gate", "claude", self._claude_gate_available(),
            pr_number=pr_number, branch=branch, risk_class=risk_class,
            changed_files=changed_files, mode=mode, dispatch_id=dispatch_id,
        )

    def _request_harness_seat(
        self, gate: str, provider: str, available: bool, *, pr_number: int, branch: str,
        risk_class: str, changed_files: List[str], mode: str, dispatch_id: str,
    ) -> Dict[str, Any]:
        """Request a harness-lane review ``gate`` whose availability the caller decided.

        An unavailable gate is classified by name (gate_result_parser
        ``_classify_unavailable``, same as ``_request_glm``) and booked
        ``not_executable`` in the result record and the skip rationale.
        """
        from review_gate_manager import _utc_now

        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": gate,
            "status": "requested" if available else "not_executable",
            "provider": provider,
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate=gate,
                requested_at=requested_at,
                pr_number=pr_number,
            ) if available else "",
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            reason, reason_detail = self._classify_unavailable(gate)
            payload["reason"] = reason
            payload["reason_detail"] = reason_detail
            payload["resolved_at"] = payload["requested_at"]
            # audit_refusal=False (OI-1750 second finding): same shape as
            # _mark_gate_unavailable/_request_glm above -- _write_skip_rationale
            # below already writes an unconditional line, so the default
            # audit-on-refusal would double-log the same refused write. The
            # head moves to the skip-rationale call's commit_sha= instead.
            self._write_not_executable_result(
                gate=gate, pr_number=pr_number, pr_id="",
                reason=reason, reason_detail=reason_detail,
                dispatch_id=dispatch_id,
                branch=branch,
                commit_sha=payload.get("commit_sha") or "",
                audit_refusal=False,
            )
            self._write_skip_rationale(
                gate=gate, pr_id=str(pr_number),
                reason=reason, reason_detail=reason_detail,
                commit_sha=payload.get("commit_sha") or "",
            )
        self._write_request_record(self._request_path(gate, pr_number), payload)
        return payload

    def _apply_claude_github_configured_state(
        self,
        payload: Dict[str, Any],
        pr_number: int,
    ) -> None:
        payload["status"] = "queued"
        if os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_TRIGGER", "0") == "1":
            comment = os.environ.get("VNX_CLAUDE_GITHUB_REVIEW_COMMENT", "@claude review")
            proc = subprocess.run(
                ["gh", "pr", "comment", str(pr_number), "--body", comment],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                payload["status"] = "requested"
            else:
                payload["status"] = "blocked"
                payload["reason"] = "claude_github_trigger_failed"
                payload["stderr"] = proc.stderr.strip()
        else:
            payload["status"] = "configured_dry_run"

    def _request_claude_github(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        configured = self._claude_github_configured()
        requested_at = _utc_now()
        payload = {
            "gate": "claude_github_optional",
            "status": "not_configured",
            "provider": "claude_github",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="claude_github_optional",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if configured:
            self._apply_claude_github_configured_state(payload, pr_number)
        self._write_request_record(self._request_path("claude_github_optional", pr_number), payload)
        return payload

    def _request_ci_gate(
        self, pr_number: int, branch: str, risk_class: str, changed_files: List[str], mode: str,
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now

        available = self._ci_gate_available()
        self._check_ci_gate_requirement_mismatch(dispatch_id)
        requested_at = _utc_now()
        payload: Dict[str, Any] = {
            "gate": "ci_gate",
            "status": "requested" if available else "not_executable",
            "provider": "gh_cli",
            "branch": branch,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": risk_class,
            "changed_files": changed_files,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "report_path": self._build_report_path(
                gate="ci_gate",
                requested_at=requested_at,
                pr_number=pr_number,
            ),
        }
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        if not available:
            self._mark_gate_unavailable(
                payload, gate="ci_gate",
                pr_number=pr_number, pr_id="",
                dispatch_id=dispatch_id,
            )
        self._write_request_record(self._request_path("ci_gate", pr_number), payload)
        return payload

    def _build_ci_gate_contract_payload(
        self,
        contract: "ReviewContract",
        pr_number: int,
        mode: str,
        dispatch_id: str,
        available: bool,
        requested_at: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "gate": "ci_gate",
            "status": "requested" if available else "not_executable",
            "provider": "gh_cli",
            "branch": contract.branch,
            "pr_id": contract.pr_id,
            "pr_number": pr_number,
            "review_mode": mode,
            "risk_class": contract.risk_class,
            "changed_files": contract.changed_files,
            "contract_hash": contract.content_hash,
            "requested_at": requested_at,
            "commit_sha": get_pr_head_sha(pr_number),
            "dispatch_id": dispatch_id,
            "report_path": self._build_report_path(
                gate="ci_gate",
                requested_at=requested_at,
                pr_id=contract.pr_id,
            ),
        }
        if not available:
            self._mark_gate_unavailable(
                payload, gate="ci_gate",
                pr_number=pr_number, pr_id=contract.pr_id,
                contract_hash=contract.content_hash,
                dispatch_id=dispatch_id,
            )
        return payload

    def _emit_ci_gate_contract_receipt(
        self,
        payload: Dict[str, Any],
        contract: "ReviewContract",
        mode: str,
        dispatch_id: str,
    ) -> None:
        """The ci_gate contract request receipt through the one emitter (GE-4c-2, E4)."""
        self._emit_review_request_receipt(
            payload, gate="ci_gate", pr_id=contract.pr_id, branch=contract.branch, mode=mode,
            risk_class=contract.risk_class, changed_files=contract.changed_files,
            contract_hash=contract.content_hash, dispatch_id=dispatch_id,
        )

    def request_ci_gate_with_contract(
        self,
        *,
        contract: "ReviewContract",
        pr_number: int,
        mode: str = "per_pr",
        dispatch_id: str = "",
    ) -> Dict[str, Any]:
        """Request a ci_gate execution driven by a canonical ReviewContract.

        Writes a contract-scoped request file ({pr_slug}-ci_gate-contract.json)
        with the canonical pr_id and the contract's content_hash.  This enables
        closure_verifier._find_gate_result to locate the result via the contract
        path and ensures the result's contract_hash matches ReviewContract.content_hash.

        ``pr_number`` is the real GitHub PR number used by ``gh pr checks``.
        """
        from review_gate_manager import _utc_now

        if not contract.pr_id:
            raise ValueError("contract.pr_id is required for ci_gate contract request")

        available = self._ci_gate_available()
        requested_at = _utc_now()
        payload = self._build_ci_gate_contract_payload(contract, pr_number, mode, dispatch_id, available, requested_at)

        self._write_request_record(self._contract_request_path("ci_gate", contract.pr_id), payload)
        self._emit_ci_gate_contract_receipt(payload, contract, mode, dispatch_id)
        return payload

    def _wiring_gate_available(self) -> bool:
        return shutil.which("gh") is not None

    def _request_wiring_gate(
        self, pr_number: int, branch: str, dispatch_id: str = "",
    ) -> Dict[str, Any]:
        from review_gate_manager import _utc_now
        from wiring_gate import WiringGateError, check_pr_wiring
        import config_runtime
        import deterministic_gate

        available = self._wiring_gate_available()
        requested_at = _utc_now()
        # The gate's blocking posture is the same toggle that decides its status
        # (analyse_diff: status="fail" if required else "advisory"). Carrying it
        # explicitly lets the required-failure count in gate_executor honor the
        # advisory contract: a shadow-mode gate (VNX_WIRING_GATE_REQUIRED=0) is
        # required=False and must never gate the merge, while required=True blocks.
        required = config_runtime.get_bool("VNX_WIRING_GATE_REQUIRED")
        context = deterministic_gate.LocalContext(
            pr_number=pr_number, branch=branch, required=required, requested_at=requested_at,
        )

        if not available:
            # The GitHub half's own answer (no `gh`, no diff to read): it is the
            # caller's diagnosis, not the local check's, and it names gh_cli.
            payload: Dict[str, Any] = deterministic_gate.not_executable_record(
                "wiring_gate", "provider_not_installed", "gh binary not found in PATH",
                context, provider="gh_cli",
            )
        else:
            try:
                result = check_pr_wiring(pr_number)
            except WiringGateError as exc:
                payload = {
                    "gate": "wiring_gate",
                    "status": "fail",
                    "provider": "ast_grep",
                    "kind": deterministic_gate.DETERMINISTIC_KIND,
                    "required": required,
                    "branch": branch,
                    "pr_number": pr_number,
                    "requested_at": requested_at,
                    "completed_at": _utc_now(),
                    "summary": f"Wiring gate error: {exc}",
                    "blocking_findings": [{
                        "severity": "blocking",
                        "title": "Wiring gate subprocess failure",
                        "description": str(exc),
                    }],
                    "blocking_count": 1,
                    "advisory_count": 0,
                    "total_checked": 0,
                    "skipped_symbols": [],
                }
            else:
                # GE-6: the outcome record is the deterministic adapter's, not a
                # field dict built here. The analysis itself ran inside
                # check_pr_wiring (its `gh pr diff` read plus analyse_diff), and the
                # adapter turns it into the record this gate has always written.
                payload = deterministic_gate.run("wiring_gate", deterministic_gate.LocalContext(
                    pr_number=pr_number, branch=branch, required=required,
                    requested_at=requested_at, recorded_at=_utc_now(), wiring_result=result,
                )).record
        if dispatch_id:
            payload["dispatch_id"] = dispatch_id
        self._write_request_record(self._request_path("wiring_gate", pr_number), payload)
        return payload
