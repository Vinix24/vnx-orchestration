"""A provider quota refusal is booked AS a quota refusal (D-gate-quota-105245).

The property under test, stated once
--------------------------------------
A gate-failure record whose own failure text carries a provider quota marker
is NEVER booked under a generic process-lifecycle reason.

That is the property. "pr-1127 yields lane_exhausted" would be a snapshot: it
would keep passing while a thirteenth marker, a fourth lifecycle reason, or a
second writer quietly fell outside the classification. So the write-side tests
below are parameterized over the REAL vocabularies —
``governance_emit._LANE_EXHAUSTED_MARKERS`` and
``gate_recorder._CAUSE_AGNOSTIC_FAILURE_REASONS`` — rather than over a list
copied into this file. Add a marker there and it is covered here on the next
run, without anyone remembering to.

What was actually broken
--------------------------
Measured 2026-09-17 on the live mission-control store. codex hit its ChatGPT
weekly limit; three consecutive PRs each recorded:

    status = unavailable | reason = exit_nonzero
    reason_detail = "Subprocess exited with code 1: You've hit your usage
                     limit. ... try again at Sep 19th, 2026 10:11 AM."

The provider named its cause in full. The ``reason`` ENUM recorded only that a
process exited non-zero — indistinguishable from a crash.

The review-gate takeover walk survived that only because it carries its own
text scan on top (``_scan_seat_failure_text`` re-reads the prose). The walk
demonstrably fired: pr-1127-deepseek_gate.json carries takeover=true with a
codex -> kimi -> glm path. ``stop_conditions._gate_result_cause`` has no such
scan: it read ``reason:exit_nonzero`` three times, concluded "same cause three
times, therefore systemic", and tripped E6 — halting every dispatch in
mission-control on an external condition with a stated end date.

So the classification moved to the single write-side choke point, and E6 stops
counting a cause it cannot fix by halting.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from gate_recorder import (  # noqa: E402
    EXECUTION_FAILURE_REASONS,
    QUOTA_REFUSAL_REASON,
    _CAUSE_AGNOSTIC_FAILURE_REASONS,
    is_quota_refusal_text,
    record_failure,
)
from gate_request_handler import GateRequestHandlerMixin  # noqa: E402
from governance_emit import _LANE_EXHAUSTED_MARKERS  # noqa: E402
from stop_conditions import (  # noqa: E402
    DEFAULT_REPEAT_THRESHOLD,
    CheckStatus,
    check_repeated_gate_failure_cause,
)

# Verbatim from pr-1127-codex_gate.json on the live mission-control store,
# 2026-09-16T13:32:49Z — the record that, three times over, tripped E6.
# Quoted here as EVIDENCE for the shape, never as the thing under test: every
# assertion below holds for any text carrying any marker.
_PR1127_REAL_CODEX_QUOTA_DETAIL = (
    "Subprocess exited with code 1: You've hit your usage limit. Upgrade to Pro "
    "(https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage "
    "to purchase more credits or try again at Sep 19th, 2026 10:11 AM."
)

# Verbatim from pr-1127-deepseek_gate.json on that same store. This record is
# the trap: its `failure_reason` is the TAKEOVER ANNOTATION and carries
# CODEX's quota prose, while its own `reason_detail` says the deepseek runner
# was never built. Classifying on failure_reason would attribute codex's
# billing state to deepseek.
_PR1127_DEEPSEEK_OWN_DETAIL = "scripts/deepseek_gate.py does not exist yet — ships in dispatch E2"
_PR1127_DEEPSEEK_TAKEOVER_ANNOTATION = (
    "codex_gate unavailable (exit_nonzero): Gate exit_nonzero. Re-run required. "
    + _PR1127_REAL_CODEX_QUOTA_DETAIL
)


def _book(tmp_path, *, reason, reason_detail, gate="codex_gate", pr_number=1127, extra=None):
    """Run a failure through the real write path and return the record on disk."""
    results_dir = tmp_path / "results"
    requests_dir = tmp_path / "requests"
    results_dir.mkdir(exist_ok=True)
    requests_dir.mkdir(exist_ok=True)
    record = record_failure(
        gate=gate,
        pr_number=pr_number,
        pr_id="",
        result={
            "reason": reason,
            "reason_detail": reason_detail,
            "duration_seconds": 1.0,
            "partial_output_lines": 4,
            "runner_pid": 4242,
        },
        request_payload={"gate": gate, "pr_number": pr_number},
        requests_dir=requests_dir,
        results_dir=results_dir,
        extra=extra,
    )
    on_disk = json.loads((results_dir / f"pr-{pr_number}-{gate}.json").read_text(encoding="utf-8"))
    assert on_disk["reason"] == record["reason"], "returned record and on-disk record disagree"
    return on_disk


# ---------------------------------------------------------------------------
# The property itself, over the REAL marker and reason vocabularies
# ---------------------------------------------------------------------------


class TestQuotaTextIsNeverBookedAsGenericLifecycleReason:

    @pytest.mark.parametrize("marker", _LANE_EXHAUSTED_MARKERS)
    @pytest.mark.parametrize("reason", sorted(_CAUSE_AGNOSTIC_FAILURE_REASONS))
    def test_every_marker_under_every_cause_agnostic_reason(self, tmp_path, marker, reason):
        """THE property. Parameterized over the live vocabularies, so a marker
        added to governance_emit or a reason added to the cause-agnostic set is
        covered here without anyone editing this file."""
        detail = f"Subprocess exited with code 1: the provider said {marker} and gave up."
        rec = _book(tmp_path, reason=reason, reason_detail=detail)

        assert rec["reason"] == QUOTA_REFUSAL_REASON, (
            f"{reason!r} + marker {marker!r} was booked as {rec['reason']!r} — a quota "
            f"refusal recorded under a generic lifecycle reason is invisible to every "
            f"reader that does not carry its own text scan"
        )
        assert rec["status"] == "unavailable", (
            "a quota refusal is a non-execution: it must never read as a rejected PR"
        )
        assert rec["reason_before_classification"] == reason, (
            "reclassification must preserve the original reason, never destroy it"
        )
        assert rec["reason_detail"] == detail, "the provider's own words must survive verbatim"

    @pytest.mark.parametrize("reason", sorted(_CAUSE_AGNOSTIC_FAILURE_REASONS))
    def test_same_reasons_without_a_marker_keep_their_reason(self, tmp_path, reason):
        """The complement, and the guard against over-reach: a real crash under
        the SAME reasons must still book as that crash. A classifier that
        rewrites everything measures nothing."""
        rec = _book(
            tmp_path,
            reason=reason,
            reason_detail="Subprocess exited with code 139: Segmentation fault (core dumped)",
        )
        assert rec["reason"] == reason
        assert "reason_before_classification" not in rec

    def test_quota_reason_books_unavailable_not_failed(self):
        """Registration check: if QUOTA_REFUSAL_REASON ever drops out of
        EXECUTION_FAILURE_REASONS, every quota refusal silently starts reading
        as `failed` — a rejected PR — instead of `unavailable`."""
        assert QUOTA_REFUSAL_REASON in EXECUTION_FAILURE_REASONS


class TestReasonsThatNameTheirOwnCauseAreNotRewritten:
    """A reason that already names a cause is a measurement. The text scan does
    not get to overrule it — doing so would claim a provider refused a run it
    was never asked to perform."""

    @pytest.mark.parametrize(
        "reason",
        [
            "gate_runner_missing",
            "gate_not_subprocess_routable",
            "unsupported_gate_type",
            "provider_not_installed",
            "timeout",
            "stall",
            "auth_error",
            "network_error",
        ],
    )
    def test_cause_naming_reason_survives_full_quota_prose(self, tmp_path, reason):
        rec = _book(
            tmp_path,
            gate="deepseek_gate",
            reason=reason,
            reason_detail=_PR1127_REAL_CODEX_QUOTA_DETAIL,
        )
        assert rec["reason"] == reason
        assert "reason_before_classification" not in rec

    def test_takeover_annotation_does_not_classify_this_gate(self, tmp_path):
        """pr-1127-deepseek_gate.json, reproduced. `failure_reason` is the
        takeover annotation carrying the PREVIOUS gate's quota prose; only
        `reason_detail` is a statement about THIS gate's own run."""
        rec = _book(
            tmp_path,
            gate="deepseek_gate",
            reason="gate_runner_missing",
            reason_detail=_PR1127_DEEPSEEK_OWN_DETAIL,
            extra={
                "failure_reason": _PR1127_DEEPSEEK_TAKEOVER_ANNOTATION,
                "takeover": True,
                "takeover_from": "glm_gate",
            },
        )
        assert rec["reason"] == "gate_runner_missing", (
            "codex's billing state was attributed to deepseek — the takeover "
            "annotation was read as this gate's own failure"
        )
        assert rec["failure_reason"] == _PR1127_DEEPSEEK_TAKEOVER_ANNOTATION

    def test_extra_cannot_forge_a_reclassification(self, tmp_path):
        """`reason_before_classification` is derived from what the caller
        actually passed. A caller able to set it could claim a
        reclassification that never happened."""
        rec = _book(
            tmp_path,
            reason="exit_nonzero",
            reason_detail="Subprocess exited with code 139: Segmentation fault",
            extra={"reason_before_classification": "harness_lane_dispatch_error"},
        )
        assert rec["reason"] == "exit_nonzero"
        assert "reason_before_classification" not in rec


# ---------------------------------------------------------------------------
# The takeover chain still reads it — now without re-parsing prose
# ---------------------------------------------------------------------------


def _fresh(**overrides):
    now = datetime.now(timezone.utc)
    record = {
        "gate": "codex_gate",
        "pr_number": 1127,
        "status": "unavailable",
        "reason": QUOTA_REFUSAL_REASON,
        "reason_detail": _PR1127_REAL_CODEX_QUOTA_DETAIL,
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    record.update(overrides)
    return record


class TestTakeoverChainReadsTheBookedReason:

    def test_fresh_quota_record_is_lane_exhausted(self):
        """The seat is takeover-eligible: the chain
        (VNX_REVIEW_GATE_TAKEOVER_CHAIN) can roll the seat to the next gate."""
        state = GateRequestHandlerMixin._classify_review_seat_failure(None, _fresh())
        assert state == "lane_exhausted"

    def test_reason_alone_is_enough_without_any_quota_prose(self):
        """The point of classifying at write time: a reader must not have to
        re-derive the cause from text. Strip every trace of the prose and the
        record still reads as what it says it is."""
        state = GateRequestHandlerMixin._classify_review_seat_failure(
            None,
            _fresh(reason_detail="", residual_risk="", summary="", report_path=""),
        )
        assert state == "lane_exhausted"

    def test_stale_quota_record_still_expires(self):
        """OI-1569's recency gate is unchanged: a quota refusal is temporary,
        so an old one must not perpetuate a block."""
        stale = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = GateRequestHandlerMixin._classify_review_seat_failure(None, _fresh(recorded_at=stale))
        assert state == "lane_exhausted_expired"


# ---------------------------------------------------------------------------
# E6 stops counting an external, time-boxed cause — and still counts the rest
# ---------------------------------------------------------------------------


def _write_results(results_dir: Path, records):
    results_dir.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 9, 16, 11, 0, tzinfo=timezone.utc)
    for i, (gate, pr_number, status, reason) in enumerate(records):
        payload = {
            "gate": gate,
            "pr_number": pr_number,
            "status": status,
            "reason": reason,
            "recorded_at": (base + timedelta(minutes=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (results_dir / f"pr-{pr_number}-{gate}.json").write_text(json.dumps(payload), encoding="utf-8")


class TestE6DoesNotCountAnExternalTransientCause:

    def test_n_quota_refusals_do_not_trigger(self, tmp_path):
        """The 2026-09-17 halt, as a property: N consecutive quota refusals on
        one gate are one external outage read N times, not N failures of this
        fabric."""
        results = tmp_path / "results"
        _write_results(
            results,
            [("codex_gate", 1126 + i, "unavailable", QUOTA_REFUSAL_REASON) for i in range(DEFAULT_REPEAT_THRESHOLD)],
        )
        result = check_repeated_gate_failure_cause(results)
        assert result.status != CheckStatus.TRIGGERED, (
            "E6 halted the fabric on a spent provider quota — a cause halting "
            "cannot fix, and one the takeover chain already answers"
        )

    def test_the_refusal_is_still_reported_loudly(self, tmp_path):
        """Excluded from the streak is not the same as silently dropped: the
        operator must still see that a gate is being refused."""
        results = tmp_path / "results"
        _write_results(
            results,
            [("codex_gate", 1126 + i, "unavailable", QUOTA_REFUSAL_REASON) for i in range(DEFAULT_REPEAT_THRESHOLD)],
        )
        result = check_repeated_gate_failure_cause(results)
        notice = result.evidence["per_gate"]["repeated_gate_failure_cause:quota:codex_gate"]
        assert notice["status"] == CheckStatus.UNMEASURABLE.value
        assert notice["evidence"]["count"] == DEFAULT_REPEAT_THRESHOLD
        assert QUOTA_REFUSAL_REASON in notice["message"]

    def test_e6_still_triggers_on_a_cause_the_fabric_owns(self, tmp_path):
        """The condition is not weakened. The threshold is untouched and a
        genuine repeating failure still halts."""
        results = tmp_path / "results"
        _write_results(
            results,
            [("codex_gate", 1126 + i, "unavailable", "exit_nonzero") for i in range(DEFAULT_REPEAT_THRESHOLD)],
        )
        result = check_repeated_gate_failure_cause(results)
        assert result.status == CheckStatus.TRIGGERED

    def test_quota_refusals_do_not_mask_a_real_streak_on_another_gate(self, tmp_path):
        results = tmp_path / "results"
        _write_results(
            results,
            [("codex_gate", 1126 + i, "unavailable", QUOTA_REFUSAL_REASON) for i in range(DEFAULT_REPEAT_THRESHOLD)]
            + [("glm_gate", 1200 + i, "unavailable", "exit_nonzero") for i in range(DEFAULT_REPEAT_THRESHOLD)],
        )
        result = check_repeated_gate_failure_cause(results)
        assert result.status == CheckStatus.TRIGGERED


# ---------------------------------------------------------------------------
# Write path and E6 joined — the pipeline that actually failed
# ---------------------------------------------------------------------------


class TestEndToEndOnTheRealFailureText:

    def test_three_real_codex_refusals_written_then_measured_do_not_halt(self, tmp_path):
        """No hand-written record anywhere: the real provider text goes through
        the real `record_failure`, and E6 reads what that wrote. Before this
        change the same three writes produced reason:exit_nonzero three times
        and E6 TRIGGERED."""
        results_dir = tmp_path / "results"
        requests_dir = tmp_path / "requests"
        results_dir.mkdir()
        requests_dir.mkdir()

        for pr_number in (1126, 1127, 1128):
            record_failure(
                gate="codex_gate",
                pr_number=pr_number,
                pr_id="",
                result={
                    "reason": "exit_nonzero",
                    "reason_detail": _PR1127_REAL_CODEX_QUOTA_DETAIL,
                    "duration_seconds": 12.0,
                    "partial_output_lines": 4,
                    "runner_pid": 4242,
                },
                request_payload={"gate": "codex_gate", "pr_number": pr_number},
                requests_dir=requests_dir,
                results_dir=results_dir,
            )

        booked = {
            json.loads((results_dir / f"pr-{pr}-codex_gate.json").read_text(encoding="utf-8"))["reason"]
            for pr in (1126, 1127, 1128)
        }
        assert booked == {QUOTA_REFUSAL_REASON}
        assert check_repeated_gate_failure_cause(results_dir).status != CheckStatus.TRIGGERED

    def test_helper_agrees_with_what_record_failure_books(self, tmp_path):
        """The public predicate and the write path must never disagree — a
        caller asking `is_quota_refusal_text` beforehand should get the same
        answer the record ends up carrying."""
        for reason, detail in (
            ("exit_nonzero", _PR1127_REAL_CODEX_QUOTA_DETAIL),
            ("exit_nonzero", "Subprocess exited with code 139: Segmentation fault"),
            ("gate_runner_missing", _PR1127_REAL_CODEX_QUOTA_DETAIL),
            ("timeout", _PR1127_REAL_CODEX_QUOTA_DETAIL),
        ):
            predicted = is_quota_refusal_text(reason, detail)
            rec = _book(tmp_path, reason=reason, reason_detail=detail, pr_number=9000)
            assert (rec["reason"] == QUOTA_REFUSAL_REASON) is predicted, (
                f"predicate and write path disagree for reason={reason!r}"
            )
