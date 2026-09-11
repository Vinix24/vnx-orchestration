"""A runner-refusal note must not displace a real provider-outcome record (OI-1707).

Live measurement, 2026-09-10, on PRs #1830 and #1832 (T0). Three poortruns
recorded real provider outages::

    codex_gate  unavailable  "Subprocess exited with code 1: You have hit your usage limit"
    glm_gate    unavailable  exit_code=1, 0.005s, litellm-proxy draaide niet
    kimi_gate   unavailable  exit_code=1, 6.6s, Moonshot wees de auth af

After ``gate_obligation_runner.py --dispatch-prefix 20260909-golfc`` ran, the
glm and kimi records carried ``not_executable`` with the runner's own refusal
text, and the codex record was gone. The overwrite guard protects a DECIDED
verdict (``is_terminal`` + ``has_complete_evidence``) but not an outage record:
``unavailable`` is deliberately non-terminal (a rerun can still decide), so the
old ``not is_terminal(existing) -> return`` let the refusal write straight
through and erased the only trace of what the provider actually did.

OI-1669 already closed ONE direction on this axis: a runner-refusal note may
not HOLD the slot against the gate's own result. OI-1707 closes the OTHER:
that same note may not TAKE the slot from a real provider outcome. The
distinction inside ``not_executable`` is now explicit — a runner refusal
(``gate_runner_missing``, ``unsupported_gate_type``,
``gate_not_subprocess_routable``) is the executor describing itself; a provider
refusal (``provider_not_installed``, ``provider_disabled``, a quota/auth
refusal) is the reader being asked and unable to answer. Only the former is
refused from TAKING a slot; a provider outcome in the slot is the one thing a
runner refusal must never displace.
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
    record_not_executable,
    record_terminal_result,
    write_result_guarded,
)

# The two names the fix introduces are reached through the MODULE, never
# imported at the top of this file. Against a tree without the fix that keeps
# the behavioural tests below failing on BEHAVIOUR (a write that should land
# and does not) instead of collapsing the whole module into one collection-time
# ImportError — the model tests in section 1 then fail on their own, on the
# absence of the model itself.

# The head both records name in the live case (PR #1830, 2026-09-10).
SHA_A = "a49cfc8130a316a625404b9b5895f6feaa038ef1"
# The head after a fix-forward — the OI-1668 control below writes for this one.
SHA_B = "30f7396d1b0f2c8f0e2f6a1c9d4b7e35c8a10f42"

# OI-1618: record_terminal_result requires a depth on every call. This file is
# about the overwrite guard, so every call passes one non-degenerate depth —
# just enough to never trip the (unrelated) gate_execution_degenerate branch.
_OK_DEPTH = gate_depth.single_shot_depth(70151, True)


def _outage(commit_sha: str = SHA_A) -> dict:
    """A real provider outage, booked ``unavailable`` — non-terminal by design,
    so the old guard let a runner refusal write straight over it."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1830",
        "pr_number": 1830,
        "status": "unavailable",
        "reason": "exit_nonzero",
        "reason_detail": "Subprocess exited with code 1: You have hit your usage limit",
        "contract_hash": "",
        "report_path": "",
        "dispatch_id": "kimi-gate-pr1830-1",
        "commit_sha": commit_sha,
    }


def _runner_refusal(commit_sha: str = SHA_A, reason: str = "gate_not_subprocess_routable") -> dict:
    """Verbatim shape of the runner's own refusal note — the executor saying
    'I could not take this path', not a statement about the head."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1830",
        "pr_number": 1830,
        "status": "not_executable",
        "reason": reason,
        "reason_detail": (
            "kimi_gate is a script runner (scripts/kimi_gate.py) with its own "
            "contract, dispatch and result-writing lifecycle; it is not a CLI "
            "this runner can drive with a prompt. Run it directly: "
            "python3 scripts/kimi_gate.py --pr 1830"
        ),
        "summary": "kimi_gate not executable: kimi_gate is a script runner",
        "contract_hash": "",
        "report_path": "",
        "blocking_findings": [],
        "advisory_findings": [],
        "required_reruns": [],
        "residual_risk": "Gate evidence not available. Compensating evidence required.",
        "recorded_at": "2026-09-10T09:00:00Z",
        "branch": "dispatch/20260910-golfc-obligatie",
        "commit_sha": commit_sha,
        "dispatch_id": "kimi-gate-pr1830-runner-refusal",
    }


def _provider_refusal(reason: str = "provider_not_installed", commit_sha: str = SHA_A) -> dict:
    """A not_executable that IS about this head: the reader was asked and the
    provider could not answer."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1830",
        "pr_number": 1830,
        "status": "not_executable",
        "reason": reason,
        "reason_detail": f"{reason} detail",
        "contract_hash": "",
        "report_path": "",
        "commit_sha": commit_sha,
    }


def _decided_pass(commit_sha: str = SHA_A) -> dict:
    """A decided, fully-evidenced verdict — the shape OI-1469/OI-1470 protect."""
    return {
        "gate": "kimi_gate",
        "pr_id": "1830",
        "pr_number": 1830,
        "status": "pass",
        "contract_hash": "b37403cb1d40fc3f",
        "report_path": "/tmp/kimi-report-1830.md",
        "blocking_findings": [],
        "dispatch_id": "kimi-gate-pr1830-1788800000",
        "commit_sha": commit_sha,
    }


def _request_payload(commit_sha: str = SHA_A) -> dict:
    """Fresh per-call: record_not_executable mutates its request_payload."""
    return {
        "gate": "kimi_gate",
        "pr_number": 1830,
        "pr_id": "1830",
        "commit_sha": commit_sha,
        "branch": "dispatch/20260910-golfc-obligatie",
        "contract_hash": "",
    }


# ---------------------------------------------------------------------------
# 1. The model: which reasons are the runner describing itself
# ---------------------------------------------------------------------------


class TestRunnerRefusalModel:

    def test_the_measured_reason_is_in_the_set(self):
        assert "gate_not_subprocess_routable" in gate_recorder.RUNNER_REFUSAL_NOT_EXECUTABLE_REASONS

    def test_a_missing_runner_and_an_unregistered_gate_are_runner_refusals(self):
        """The dispatch names both kinds explicitly: 'een ontbrekende runner'
        and 'een geweigerde routering'. All three are the executor describing
        itself, and none may take a slot from a provider outcome."""
        assert "gate_runner_missing" in gate_recorder.RUNNER_REFUSAL_NOT_EXECUTABLE_REASONS
        assert "unsupported_gate_type" in gate_recorder.RUNNER_REFUSAL_NOT_EXECUTABLE_REASONS

    @pytest.mark.parametrize("reason", [
        "provider_not_installed",
        "provider_disabled",
        "provider_not_configured",
        "provider_quota_exhausted",
        "contract_missing",
    ])
    def test_provider_and_environment_reasons_stay_out_of_the_set(self, reason):
        """A provider refusal is evidence about this head and keeps its slot;
        the runner-refusal set must not swallow it."""
        assert reason not in gate_recorder.RUNNER_REFUSAL_NOT_EXECUTABLE_REASONS

    def test_predicate_recognises_the_live_record(self):
        assert gate_recorder.is_runner_refusal(_runner_refusal()) is True

    @pytest.mark.parametrize("reason", ["gate_runner_missing", "unsupported_gate_type"])
    def test_predicate_recognises_the_other_two_runner_reasons(self, reason):
        record = _runner_refusal()
        record["reason"] = reason
        assert gate_recorder.is_runner_refusal(record) is True

    def test_predicate_rejects_a_provider_refusal(self):
        assert gate_recorder.is_runner_refusal(_provider_refusal()) is False

    def test_predicate_requires_the_not_executable_status(self):
        """The reason string alone is not enough. A record with another status
        carrying this reason is not the shape that was measured, and must not
        pick up a refusal built for one."""
        smuggled = _decided_pass()
        smuggled["reason"] = "gate_not_subprocess_routable"
        assert gate_recorder.is_runner_refusal(smuggled) is False

    def test_predicate_tolerates_a_missing_reason(self):
        record = _runner_refusal()
        record.pop("reason")
        assert gate_recorder.is_runner_refusal(record) is False


# ---------------------------------------------------------------------------
# 2. The defect: a runner refusal must not displace a provider outcome
# ---------------------------------------------------------------------------


class TestRunnerRefusalDoesNotDisplaceProviderOutcome:

    def test_write_result_guarded_refuses_over_an_outage(self, tmp_path):
        """RED on main ``962d57f8``: the refusal wrote straight over the
        outage because ``unavailable`` is non-terminal."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        existing = _outage()
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _runner_refusal(), gate="kimi_gate", pr_ref="1830",
        )

        assert written is False, (
            "a record saying 'the runner could not take this path' is not "
            "evidence about the PR, so it must never displace a record of what "
            "the provider actually did (OI-1707)"
        )
        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_record_terminal_result_raises_over_an_outage(self, tmp_path):
        """Through the raising writer too: the refusal raises, the outage stays."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        existing = _outage()
        out.write_text(json.dumps(existing), encoding="utf-8")

        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(
                gate="kimi_gate", pr_id="1830", result_path=out,
                payload=_runner_refusal(), execution_depth=_OK_DEPTH,
            )

        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_a_provider_refusal_in_the_slot_also_refuses_a_runner_refusal(self, tmp_path):
        """The two kinds of not_executable are now distinct: a runner refusal
        must not displace a provider refusal either — the provider refusal is a
        statement about this head."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        existing = _provider_refusal("provider_not_installed")
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _runner_refusal(), gate="kimi_gate", pr_ref="1830",
        )

        assert written is False
        assert json.loads(out.read_text(encoding="utf-8")) == existing


class TestRecordNotExecutableWriterPath:
    """The writer-path test, built on the slot the writer itself chooses.

    T0's first conclusion on this defect was wrong because the test read the
    wrong filename: the writer lands in ``<pr>-<gate>-contract.json`` when
    ``pr_id`` is set, NOT ``pr-<pr>-<gate>.json``. Two naming conventions sit
    next to each other. So the test below lets the writer write into an empty
    slot first, uses the filename it actually produced, seeds the outage there,
    and only then re-runs the refusal. A positive control on the path is the
    whole point.
    """

    @pytest.fixture
    def env(self, tmp_path):
        state_dir = tmp_path / "state"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        return {
            "state_dir": state_dir,
            "requests_dir": requests_dir,
            "results_dir": results_dir,
        }

    def test_refusal_does_not_displace_outage_at_the_writer_chosen_path(self, env):
        # Positive control: write into an EMPTY slot first, so the exact
        # filename this writer chooses is observed rather than guessed.
        first = record_not_executable(
            gate="kimi_gate", pr_number=1830, pr_id="1830",
            reason="gate_not_subprocess_routable",
            reason_detail="kimi_gate is a script runner; run it directly",
            request_payload=_request_payload(),
            requests_dir=env["requests_dir"],
            results_dir=env["results_dir"],
            state_dir=env["state_dir"],
        )
        result_files = list(env["results_dir"].glob("*.json"))
        assert len(result_files) == 1, "the positive control must write exactly one result file"
        slot = result_files[0]
        assert first["status"] == "not_executable"

        # Now the measured defect: a real provider outage sits in that SAME
        # slot, and the runner refusal runs again.
        slot.write_text(json.dumps(_outage()), encoding="utf-8")

        outcome = record_not_executable(
            gate="kimi_gate", pr_number=1830, pr_id="1830",
            reason="gate_not_subprocess_routable",
            reason_detail="kimi_gate is a script runner; run it directly",
            request_payload=_request_payload(),
            requests_dir=env["requests_dir"],
            results_dir=env["results_dir"],
            state_dir=env["state_dir"],
        )

        on_disk = json.loads(slot.read_text(encoding="utf-8"))
        assert on_disk["status"] == "unavailable"
        assert on_disk["reason_detail"] == _outage()["reason_detail"]
        assert outcome["write_refused"] is True
        assert outcome["attempted_status"] == "not_executable"

        # The diagnosis is not lost: the request record and the GATE-9
        # skip-rationale carry the runner's own refusal, and the outage stays
        # untouched in the result slot.
        request_files = list(env["requests_dir"].glob("*.json"))
        assert len(request_files) == 1
        req = json.loads(request_files[0].read_text(encoding="utf-8"))
        assert req["status"] == "not_executable"
        assert req["reason"] == "gate_not_subprocess_routable"

        audit_path = env["state_dir"] / "gate_execution_audit.ndjson"
        audit_lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(audit_lines) == 2, "one skip-rationale per call, both refused or not"
        assert json.loads(audit_lines[-1])["reason"] == "gate_not_subprocess_routable"


# ---------------------------------------------------------------------------
# 3. The refusal is not a freeze: where a runner refusal still lands
# ---------------------------------------------------------------------------


class TestRunnerRefusalStillAllowedToLand:

    def test_lands_on_an_empty_slot(self, tmp_path):
        out = tmp_path / "pr-1830-kimi_gate.json"
        payload, written = write_result_guarded(
            out, _runner_refusal(), gate="kimi_gate", pr_ref="1830",
        )
        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "not_executable"

    def test_may_replace_another_runner_refusal(self, tmp_path):
        """A slot holding a runner refusal is not frozen: another runner
        refusal may land, exactly as an announcement may replace an
        announcement (OI-1669)."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        out.write_text(json.dumps(_runner_refusal()), encoding="utf-8")

        second = _runner_refusal(reason="gate_runner_missing")
        second["recorded_at"] = "2026-09-10T10:00:00Z"
        payload, written = write_result_guarded(
            out, second, gate="kimi_gate", pr_ref="1830",
        )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["reason"] == "gate_runner_missing"

    def test_a_runner_refusal_for_another_head_still_lands(self, tmp_path):
        """Control: OI-1668 unchanged. An outage about head A does not gate a
        runner-refusal note about head B."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        out.write_text(json.dumps(_outage(SHA_A)), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _runner_refusal(SHA_B), gate="kimi_gate", pr_ref="1830",
        )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["commit_sha"] == SHA_B


# ---------------------------------------------------------------------------
# 4. Controls: the guard's existing protection is unchanged
# ---------------------------------------------------------------------------


class TestOverwriteGuardControlsUnchanged:

    def test_a_decided_pass_still_holds_the_slot(self, tmp_path):
        """Control: OI-1469/OI-1470 unchanged. An outage may never erase a
        decided, evidenced verdict on the same head."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        existing = _decided_pass()
        out.write_text(json.dumps(existing), encoding="utf-8")

        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(
                gate="kimi_gate", pr_id="1830", result_path=out,
                payload=_outage(), execution_depth=_OK_DEPTH,
            )
        assert json.loads(out.read_text(encoding="utf-8")) == existing

    def test_a_placeholder_not_executable_stays_overwritable(self, tmp_path):
        """Control: a terminal record with NO complete evidence (a placeholder
        not_executable) is not 'decided' and must not permanently freeze the
        slot. A provider refusal over a provider refusal still lands."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        out.write_text(json.dumps(_provider_refusal("provider_disabled")), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _provider_refusal("provider_not_configured"),
            gate="kimi_gate", pr_ref="1830",
        )

        assert written is True
        assert json.loads(out.read_text(encoding="utf-8"))["reason"] == "provider_not_configured"

    def test_a_provider_refusal_still_holds_the_slot_against_an_outage(self, tmp_path):
        """Control: a provider refusal is a statement about this head and an
        outage write must still not replace it (the OI-1669 axis, unchanged)."""
        out = tmp_path / "pr-1830-kimi_gate.json"
        existing = _provider_refusal("provider_not_installed")
        out.write_text(json.dumps(existing), encoding="utf-8")

        payload, written = write_result_guarded(
            out, _outage(), gate="kimi_gate", pr_ref="1830",
        )

        assert written is False
        assert json.loads(out.read_text(encoding="utf-8")) == existing
