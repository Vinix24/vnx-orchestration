"""A routing announcement is not a verdict, so it cannot hold a gate slot
shut against the gate it announced (OI-1669).

Live measurement, 2026-09-07, twice on the same evening (T0). The seat walk
handed kimi_gate the codex seat after codex hit its usage limit
(``takeover_from: codex_gate``, ``takeover_reason: exit_nonzero``). The
executor that received the seat cannot drive a script-runner gate, so
``gate_runner.GateRunner.run`` booked, via ``record_not_executable``, a
``not_executable`` RESULT with::

    reason:        gate_not_subprocess_routable
    reason_detail: kimi_gate is a script runner (scripts/kimi_gate.py) with its
                   own contract, dispatch and result-writing lifecycle; it is
                   not a CLI this runner can drive with a prompt. Run it
                   directly: python3 scripts/kimi_gate.py --pr 1810

The very command that record names then failed, verbatim::

    gate_recorder: REFUSING to overwrite terminal result gate=kimi_gate pr=1810
      existing_status='not_executable' with non-terminal status='unavailable'
    kimi_gate: FAILED to write result record to .../pr-1810-kimi_gate.json

Both records name the SAME head (``a49cfc81...``), so the head-scoped escape
hatch of OI-1668 (#1810, main ``30f7396d``) does not engage. The slot was held
by a record that says nothing about the PR: the executor was describing
ITSELF. With codex exhausted and kimi unable to sign, all four review lanes
stood still at once.

The distinction this file pins down is inside ``not_executable``, which books
two different kinds of fact under one status:

  - a PROVIDER refusal (``provider_not_installed``, ``provider_disabled``,
    ``provider_not_configured``, a quota/auth refusal) is a statement about
    this head: the reader was asked and could not answer. Protected, unchanged.
  - a ROUTER announcement (``gate_not_subprocess_routable``) is a statement
    about the executor. Not evidence, so it never holds the slot.

Everything the guard protected before still holds: a decided, evidenced
verdict, a provider refusal, and the head scoping of OI-1668 are each
asserted here as controls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_depth
import gate_recorder
from gate_recorder import (
    ResultOverwriteRefused,
    record_terminal_result,
    write_result_guarded,
)

# The two names the fix introduces are reached through the MODULE, never
# imported at the top of this file. Against a tree without the fix that keeps
# the behavioural tests below failing on BEHAVIOUR (a write that should land
# and does not) instead of collapsing the whole module into one collection-time
# ImportError — the model tests in section 1 then fail on their own, on the
# absence of the model itself.

# The head both records name in the live case (PR #1810, 2026-09-07).
SHA_A = "a49cfc8130a316a625404b9b5895f6feaa038ef1"
# The head after a fix-forward — the OI-1668 control below writes for this one.
SHA_B = "30f7396d1b0f2c8f0e2f6a1c9d4b7e35c8a10f42"

# OI-1618: record_terminal_result requires a depth on every call. This file is
# about the overwrite guard, so every call passes one non-degenerate depth —
# just enough to never trip the (unrelated) gate_execution_degenerate branch.
_OK_DEPTH = gate_depth.single_shot_depth(70151, True)


def _routing_announcement(commit_sha: str = SHA_A) -> dict:
    """Verbatim shape of ``state/review_gates/results/pr-1810-kimi_gate.json``
    as it stood after the live measurement (fields trimmed to the ones the
    guard reads, values unmodified)."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1810",
        "pr_number": 1810,
        "status": "not_executable",
        "reason": "gate_not_subprocess_routable",
        "reason_detail": (
            "kimi_gate is a script runner (scripts/kimi_gate.py) with its own "
            "contract, dispatch and result-writing lifecycle; it is not a CLI "
            "this runner can drive with a prompt. Run it directly: "
            "python3 scripts/kimi_gate.py --pr 1810"
        ),
        "summary": "kimi_gate not executable: kimi_gate is a script runner",
        "contract_hash": "",
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [],
        "residual_risk": "Gate evidence not available. Compensating evidence required.",
        "recorded_at": "2026-09-07T17:17:08Z",
        "branch": "dispatch/20260907-golfb-shafix-poortbewijs-per-head",
        "commit_sha": commit_sha,
        "takeover": True,
        "takeover_from": "codex_gate",
        "takeover_reason": "exit_nonzero",
        "takeover_source_status": "unavailable",
    }


def _provider_refusal(reason: str = "provider_not_installed", commit_sha: str = SHA_A) -> dict:
    """A not_executable that IS about this head: the reader was asked and the
    provider could not answer."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1810",
        "pr_number": 1810,
        "status": "not_executable",
        "reason": reason,
        "reason_detail": f"{reason} detail",
        "contract_hash": "",
        "report_path": "",
        "commit_sha": commit_sha,
    }


def _kimi_outage(commit_sha: str = SHA_A) -> dict:
    """The write kimi_gate.py actually attempted: its own provider outage,
    booked ``unavailable`` (non-terminal — a rerun can still decide)."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1810",
        "pr_number": 1810,
        "status": "unavailable",
        "reason": "dispatch_error",
        "reason_detail": "kimi lane produced no verdict",
        "contract_hash": "",
        "report_path": "",
        "dispatch_id": "kimi-gate-pr1810-1788815204",
        "commit_sha": commit_sha,
    }


def _kimi_pass(commit_sha: str = SHA_A) -> dict:
    """A real, evidenced kimi verdict."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1810",
        "pr_number": 1810,
        "status": "pass",
        "contract_hash": "c0ffee1234567890",
        "report_path": "/tmp/kimi-report-1810.md",
        "blocking_findings": [],
        "dispatch_id": "kimi-gate-pr1810-1788815999",
        "commit_sha": commit_sha,
    }


def _decided_pass(commit_sha: str = SHA_A) -> dict:
    """A decided, fully-evidenced verdict — the shape OI-1469/OI-1470 protect."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1810",
        "pr_number": 1810,
        "status": "pass",
        "contract_hash": "b37403cb1d40fc3f",
        "report_path": "/tmp/kimi-report-earlier.md",
        "blocking_findings": [],
        "dispatch_id": "kimi-gate-pr1810-1788800000",
        "commit_sha": commit_sha,
    }


# ---------------------------------------------------------------------------
# 1. The model: which reasons are not verdicts
# ---------------------------------------------------------------------------


class TestNonVerdictReasonModel:

    def test_the_measured_reason_is_in_the_set(self):
        assert "gate_not_subprocess_routable" in gate_recorder.NON_VERDICT_NOT_EXECUTABLE_REASONS

    @pytest.mark.parametrize("reason", [
        "provider_not_installed",
        "provider_disabled",
        "provider_not_configured",
        "provider_quota_exhausted",
        "gate_runner_missing",
        "unsupported_gate_type",
        "contract_missing",
    ])
    def test_provider_and_environment_reasons_stay_out_of_the_set(self, reason):
        """The set is narrow on purpose: only a reason measured to be the
        executor describing itself belongs in it. A provider refusal is
        evidence about this head; an environment fact
        (``gate_runner_missing``/``unsupported_gate_type``) is a routing bug
        nobody can write a verdict over anyway, so widening the hole for it
        buys nothing and costs the protection."""
        assert reason not in gate_recorder.NON_VERDICT_NOT_EXECUTABLE_REASONS

    def test_predicate_recognises_the_live_record(self):
        assert gate_recorder.is_routing_announcement(_routing_announcement()) is True

    def test_predicate_rejects_a_provider_refusal(self):
        assert gate_recorder.is_routing_announcement(_provider_refusal()) is False

    def test_predicate_requires_the_not_executable_status(self):
        """The reason string alone is not enough. A record with another status
        carrying this reason is not the shape that was measured, and must not
        pick up an escape hatch built for one."""
        smuggled = _kimi_pass()
        smuggled["reason"] = "gate_not_subprocess_routable"
        assert gate_recorder.is_routing_announcement(smuggled) is False

    def test_predicate_tolerates_a_missing_reason(self):
        record = _routing_announcement()
        record.pop("reason")
        assert gate_recorder.is_routing_announcement(record) is False


# ---------------------------------------------------------------------------
# 2. The five behaviours named in the dispatch
# ---------------------------------------------------------------------------


class TestRoutingAnnouncementDoesNotHoldTheSlot:

    def test_kimi_may_write_its_own_unavailable_over_the_announcement(self, tmp_path):
        """RED on main ``30f7396d``: refused, and kimi could not sign at all.

        The exact live pair: an announcement for head ``a49cfc81`` in the slot,
        kimi's own ``unavailable`` for the SAME head attempting to land."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        out.write_text(json.dumps(_routing_announcement()), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _kimi_outage(), gate="kimi_gate", pr_ref="1810",
        )

        assert written is True, (
            "a record saying 'this executor cannot drive kimi_gate' is not a "
            "statement about the PR, so it must not block kimi_gate's own "
            "verdict for the same head (OI-1669)"
        )
        on_disk = json.loads(out.read_text(encoding="utf-8"))
        assert on_disk["status"] == "unavailable"
        assert on_disk["reason"] == "dispatch_error"
        assert payload["status"] == "unavailable"

    def test_the_live_writer_path_lands_too(self, tmp_path):
        """Through the function kimi_gate.py actually calls
        (``record_terminal_result``, scripts/kimi_gate.py:894), which RAISES on
        refusal — that raise is what printed ``kimi_gate: FAILED to write
        result record``."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        out.write_text(json.dumps(_routing_announcement()), encoding="utf-8")

        record_terminal_result(
            gate="kimi_gate", pr_id="1810", result_path=out,
            payload=_kimi_outage(), execution_depth=_OK_DEPTH,
        )

        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "unavailable"

    def test_a_real_pass_over_the_announcement_lands(self, tmp_path):
        """The successor case that matters most: kimi run directly, as the
        announcement's own reason_detail instructs, producing a real verdict."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        out.write_text(json.dumps(_routing_announcement()), encoding="utf-8")

        record_terminal_result(
            gate="kimi_gate", pr_id="1810", result_path=out,
            payload=_kimi_pass(), execution_depth=_OK_DEPTH,
        )

        on_disk = json.loads(out.read_text(encoding="utf-8"))
        assert on_disk["status"] == "pass"
        assert on_disk["contract_hash"] == "c0ffee1234567890"

    @pytest.mark.parametrize("reason", [
        "provider_not_installed",
        "provider_quota_exhausted",
    ])
    def test_a_provider_refusal_still_holds_the_slot(self, tmp_path, reason):
        """Control: the escape hatch is keyed on the REASON, not on the
        status. A provider refusal is a statement about this head — the gate
        was asked and produced nothing — and an outage write must still not
        replace it."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        existing = _provider_refusal(reason)
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _kimi_outage(), gate="kimi_gate", pr_ref="1810",
        )

        assert written is False
        assert payload == existing
        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_a_decided_pass_still_holds_the_slot(self, tmp_path):
        """Control: OI-1469/OI-1470 unchanged. An outage may never erase a
        decided, evidenced verdict on the same head."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        existing = _decided_pass()
        out.write_text(json.dumps(existing), encoding="utf-8")

        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(
                gate="kimi_gate", pr_id="1810", result_path=out,
                payload=_kimi_outage(), execution_depth=_OK_DEPTH,
            )
        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_a_write_for_another_head_still_lands(self, tmp_path):
        """Control: OI-1668 unchanged. A record about head A does not gate
        head B — this must keep working through the head branch, independently
        of the new reason branch."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        out.write_text(json.dumps(_decided_pass(SHA_A)), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _kimi_outage(SHA_B), gate="kimi_gate", pr_ref="1810",
        )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["commit_sha"] == SHA_B


# ---------------------------------------------------------------------------
# 3. The escape hatch does not leak into the other direction
# ---------------------------------------------------------------------------


class TestTheHatchOnlyOpensForTheRecordBeingDisplaced:

    def test_a_new_announcement_may_not_erase_a_decided_verdict(self, tmp_path):
        """Asymmetry check. Being a routing announcement excuses a record from
        HOLDING the slot; it does not license one to TAKE the slot from a real
        verdict. The executor booking 'I cannot drive this gate' after kimi
        already signed must be refused exactly as it is today."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        existing = _decided_pass()
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _routing_announcement(), gate="kimi_gate", pr_ref="1810",
        )

        assert written is False
        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_an_announcement_may_replace_an_announcement(self, tmp_path):
        """Two seat walks in one evening produced this record twice. The
        second must land — a slot frozen on the first would be the same
        defect wearing a different reason."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        first = _routing_announcement()
        first["recorded_at"] = "2026-09-07T17:17:08Z"
        out.write_text(json.dumps(first), encoding="utf-8")

        second = _routing_announcement()
        second["recorded_at"] = "2026-09-07T21:44:02Z"
        _payload, written = write_result_guarded(
            out, second, gate="kimi_gate", pr_ref="1810",
        )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["recorded_at"] == "2026-09-07T21:44:02Z"

    def test_a_corrupt_slot_still_fails_closed(self, tmp_path):
        """The unreadable-existing-file refusal runs before any reason can be
        read, and must stay ahead of the new branch: a torn write may be
        hiding a decided verdict, and 'it might have been an announcement' is
        not something the guard is allowed to assume."""
        out = tmp_path / "pr-1810-kimi_gate.json"
        torn = '{"gate": "kimi_gate", "status": "not_executable", "reason": "gate_not_su'
        out.write_text(torn, encoding="utf-8")

        _payload, written = write_result_guarded(
            out, _kimi_outage(), gate="kimi_gate", pr_ref="1810",
        )

        assert written is False
        assert out.read_text(encoding="utf-8") == torn
