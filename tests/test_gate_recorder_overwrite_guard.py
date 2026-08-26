"""Tests for gate_recorder's terminal-overwrite guard (OI-1469/OI-1470).

The result-store slot ``results/pr-<N>-<gate>.json`` is one file per (PR,
gate) with three independent writers (glm_gate's live run, its
``--reprocess`` recovery path, and gate_obligation_runner) landing there with
no ordering guarantee. Whoever writes last wins, even when that write is a
provider outage. The guard sits in the SHARED write functions of
gate_recorder.py (``record_terminal_result``, ``record_not_executable``,
``record_failure``) — every writer routes through one of these three, so the
guard applies by construction, not per call site.

Rule (uses ONLY ``gate_status.is_terminal`` / ``gate_status.has_complete_evidence``,
no second vocabulary):
  - A terminal, evidenced record (is_terminal + has_complete_evidence) may
    only be replaced by another terminal record.
  - If the new record also lacks complete evidence (e.g. a fresh
    not_executable), the write is refused too — a decided verdict must not
    be demoted to an evidence-less placeholder.
  - A terminal record WITHOUT complete evidence (e.g. an existing
    not_executable) is not "decided" and stays freely overwritable.
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

from gate_recorder import (
    ResultOverwriteRefused,
    record_failure,
    record_not_executable,
    record_terminal_result,
    write_result_guarded,
)


def _make_pass(**overrides):
    payload = {
        "gate": "glm_gate",
        "pr_id": "1691",
        "status": "pass",
        "contract_hash": "dd5ac45f7e84535e",
        "report_path": "/tmp/glm-report.md",
        "dispatch_id": "glm-gate-pr1691-1787753000",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Writer 1: glm_gate.py / kimi_gate.py — gate_recorder.record_terminal_result
# (also exercises the --reprocess code path, which calls this same function)
# ---------------------------------------------------------------------------


class TestRecordTerminalResultOverwriteGuard:

    def test_unavailable_write_over_decided_pass_is_refused(self, tmp_path):
        """OI-1470 reproduction: a second glm_gate run that fails with an
        OpenRouter 402 must not erase the first run's real pass."""
        out = tmp_path / "pr-1691-glm_gate.json"
        pass_payload = _make_pass()
        record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=pass_payload)
        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "pass"

        outage_payload = {
            "gate": "glm_gate",
            "pr_id": "1691",
            "status": "unavailable",
            "contract_hash": "",
            "report_path": "",
            "reason": "dispatch_error",
        }
        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=outage_payload)

        # The existing pass must be completely untouched.
        assert json.loads(out.read_text(encoding="utf-8")) == pass_payload

    def test_reprocess_no_identity_anchor_over_decided_pass_is_refused(self, tmp_path):
        """The --reprocess recovery path's own not_executable booking
        (reason=reprocess_no_identity_anchor, all fields empty) must not
        erase a decided pass either — it goes through the SAME function."""
        out = tmp_path / "pr-1691-glm_gate.json"
        pass_payload = _make_pass()
        record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=pass_payload)

        reprocess_refusal = {
            "gate": "glm_gate",
            "pr_id": "1691",
            "status": "unavailable",
            "reason": "reprocess_no_identity_anchor",
            "contract_hash": "",
            "report_path": "",
        }
        with pytest.raises(ResultOverwriteRefused):
            record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=reprocess_refusal)
        assert json.loads(out.read_text(encoding="utf-8")) == pass_payload

    def test_terminal_over_terminal_is_allowed(self, tmp_path):
        """A hergate after a rebase must be able to lay down a fresh pass
        over an old pass — terminal may replace terminal."""
        out = tmp_path / "pr-1691-glm_gate.json"
        first_pass = _make_pass(dispatch_id="glm-gate-pr1691-1")
        record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=first_pass)

        second_pass = _make_pass(
            contract_hash="freshhash123456", report_path="/tmp/glm-report-2.md",
            dispatch_id="glm-gate-pr1691-2",
        )
        record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=second_pass)
        assert json.loads(out.read_text(encoding="utf-8")) == second_pass

    def test_fail_over_pass_is_allowed_when_both_carry_evidence(self, tmp_path):
        """A real fail (evidenced) may still supersede a real pass — the
        guard protects against DOWNGRADE, not against a real regression."""
        out = tmp_path / "pr-1691-glm_gate.json"
        record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=_make_pass())

        fail_payload = _make_pass(
            status="fail", contract_hash="failhash1", report_path="/tmp/glm-fail.md",
            dispatch_id="glm-gate-pr1691-3",
        )
        record_terminal_result(gate="glm_gate", pr_id="1691", result_path=out, payload=fail_payload)
        assert json.loads(out.read_text(encoding="utf-8"))["status"] == "fail"

    def test_not_executable_may_freely_overwrite_a_prior_not_executable(self, tmp_path):
        """A terminal record with NO complete evidence is not "decided" and
        must stay freely overwritable — it must never permanently freeze
        the slot."""
        out = tmp_path / "pr-1-kimi_gate.json"
        first = {
            "gate": "kimi_gate", "pr_id": "1", "status": "not_executable",
            "reason": "provider_disabled", "contract_hash": "", "report_path": "",
            "dispatch_id": "kimi-gate-pr1-1",
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(first), encoding="utf-8")

        second = {
            "gate": "kimi_gate", "pr_id": "1", "status": "not_executable",
            "reason": "provider_not_configured", "contract_hash": "", "report_path": "",
            "dispatch_id": "kimi-gate-pr1-2",
        }
        record_terminal_result(gate="kimi_gate", pr_id="1", result_path=out, payload=second)
        assert json.loads(out.read_text(encoding="utf-8"))["reason"] == "provider_not_configured"

    def test_no_existing_file_writes_unconditionally(self, tmp_path):
        out = tmp_path / "pr-99-kimi_gate.json"
        payload = {
            "gate": "kimi_gate", "pr_id": "99", "status": "unavailable",
            "contract_hash": "", "report_path": "",
        }
        record_terminal_result(gate="kimi_gate", pr_id="99", result_path=out, payload=payload)
        assert json.loads(out.read_text(encoding="utf-8")) == payload


# ---------------------------------------------------------------------------
# Writer 2: record_not_executable (gate_runner.py, gate_executor.py's
# ci_gate provider_not_installed path, gate_obligation_runner's runner_error
# fallback)
# ---------------------------------------------------------------------------


class TestRecordNotExecutableOverwriteGuard:

    @pytest.fixture
    def env(self, tmp_path):
        state_dir = tmp_path / "state"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        return {"state_dir": state_dir, "requests_dir": requests_dir, "results_dir": results_dir}

    def test_not_executable_over_decided_pass_is_refused_and_existing_kept(self, env):
        rf = env["results_dir"] / "pr-1694-codex_gate.json"
        pass_payload = {
            "gate": "codex_gate", "pr_id": "", "pr_number": 1694, "status": "pass",
            "contract_hash": "realhash", "report_path": "/tmp/codex-report.md",
            "dispatch_id": "codex-gate-pr1694-1",
        }
        rf.write_text(json.dumps(pass_payload), encoding="utf-8")

        result = record_not_executable(
            gate="codex_gate", pr_number=1694, pr_id="",
            reason="provider_not_installed", reason_detail="codex binary not found in PATH",
            request_payload={"gate": "codex_gate", "pr_number": 1694},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
            state_dir=env["state_dir"],
        )

        # The function must report the true on-disk state, not the refused attempt.
        assert result["status"] == "pass"
        assert json.loads(rf.read_text(encoding="utf-8")) == pass_payload

    def test_not_executable_on_empty_slot_writes_normally(self, env):
        rf = env["results_dir"] / "pr-1700-codex_gate.json"
        result = record_not_executable(
            gate="codex_gate", pr_number=1700, pr_id="",
            reason="provider_not_installed", reason_detail="codex binary not found in PATH",
            request_payload={"gate": "codex_gate", "pr_number": 1700},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
            state_dir=env["state_dir"],
        )
        assert result["status"] == "not_executable"
        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "not_executable"


# ---------------------------------------------------------------------------
# Writer 3: record_failure (gate_runner.py / gate_executor.py timeouts,
# gate_artifacts.materialize_artifacts failure branches)
# ---------------------------------------------------------------------------


class TestRecordFailureOverwriteGuard:

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        requests_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        return {"state_dir": state_dir, "requests_dir": requests_dir, "results_dir": results_dir}

    @pytest.mark.parametrize("pr_number,contract_hash,head_sha", [
        (1691, "466cd2ca75d7a7fb", "ab11e0c3"),
        (1692, "9ebfe1f1c94b4243", "56b7f331"),
    ])
    def test_codex_usage_limit_over_completed_is_refused_live_fixture(
        self, env, pr_number, contract_hash, head_sha,
    ):
        """Live fixture (2026-08-26, third independent case, second provider):
        codex_gate held a valid `status=completed` verdict on two rebased PRs.
        Re-gating after the rebase hit codex's usage limit mid-run — gate_runner
        books that as ``status=failed, reason=exit_nonzero`` -> record_failure
        classifies exit_nonzero as an EXECUTION failure and books
        ``status=unavailable`` with empty contract_hash/report_path. Proves the
        bug (and the guard) is NOT glm-specific: a fourth writer
        (gate_runner._run_subprocess_path -> record_failure), a second
        provider, and a reason (exit_nonzero) outside any explicit reason
        allowlist all hit the exact same shared write path."""
        rf = env["results_dir"] / f"pr-{pr_number}-codex_gate.json"
        completed_payload = {
            "gate": "codex_gate", "pr_id": "", "pr_number": pr_number,
            "status": "completed", "contract_hash": contract_hash,
            "report_path": "/tmp/codex-report.md", "commit_sha": head_sha,
            "blocking_findings": [], "dispatch_id": f"codex-gate-pr{pr_number}-1",
        }
        rf.write_text(json.dumps(completed_payload), encoding="utf-8")

        result = record_failure(
            gate="codex_gate", pr_number=pr_number, pr_id="",
            result={
                "reason": "exit_nonzero",
                "reason_detail": (
                    "Subprocess exited with code 1: You've hit your usage limit. "
                    "Upgrade to Pro ..."
                ),
                "duration_seconds": 42.0, "partial_output_lines": 12, "runner_pid": 1,
            },
            request_payload={"gate": "codex_gate", "pr_number": pr_number, "dispatch_id": f"codex-gate-pr{pr_number}-2"},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )

        assert result["status"] == "completed"
        on_disk = json.loads(rf.read_text(encoding="utf-8"))
        assert on_disk == completed_payload, (
            "usage-limit unavailable must never overwrite a real completed "
            "verdict (OI-1469/OI-1470, live fixture 2026-08-26)"
        )

    def test_refusal_does_not_branch_on_reason_code(self, env):
        """The guard is a SHAPE check (terminal+evidence vs. non-terminal),
        never a reason-code allowlist. A reason no writer has produced yet
        must be refused identically to exit_nonzero/dispatch_error/
        provider_not_installed — otherwise the next outage reason slips
        through uncaught."""
        rf = env["results_dir"] / "pr-1-codex_gate.json"
        completed_payload = {
            "gate": "codex_gate", "pr_number": 1, "status": "completed",
            "contract_hash": "h", "report_path": "/tmp/r.md",
        }
        rf.write_text(json.dumps(completed_payload), encoding="utf-8")

        result = record_failure(
            gate="codex_gate", pr_number=1, pr_id="",
            result={
                "reason": "a_reason_nobody_has_seen_yet",
                "reason_detail": "novel failure mode",
                "duration_seconds": 1.0, "partial_output_lines": 0, "runner_pid": 1,
            },
            request_payload={"gate": "codex_gate", "pr_number": 1},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )

        assert result["status"] == "completed"
        assert json.loads(rf.read_text(encoding="utf-8")) == completed_payload

    def test_completed_over_completed_is_allowed(self, tmp_path):
        """Codex/gemini's own PASS_STATES member is "completed", not "pass" —
        a rebase re-gate producing a fresh completed verdict must still be
        able to replace an old completed verdict (the mirror requirement:
        freezing evidence on the first run is itself a new outage)."""
        out = tmp_path / "pr-1691-codex_gate.json"
        first = {
            "gate": "codex_gate", "pr_number": 1691, "status": "completed",
            "contract_hash": "466cd2ca75d7a7fb", "report_path": "/tmp/codex-1.md",
        }
        out.write_text(json.dumps(first), encoding="utf-8")

        second, written = write_result_guarded(
            out,
            {
                "gate": "codex_gate", "pr_number": 1691, "status": "completed",
                "contract_hash": "freshhash999", "report_path": "/tmp/codex-2.md",
            },
            gate="codex_gate", pr_ref="1691",
        )
        assert written is True
        assert second["contract_hash"] == "freshhash999"
        assert json.loads(out.read_text(encoding="utf-8"))["contract_hash"] == "freshhash999"

    def _fail_result(self, reason="timeout"):
        return {
            "reason": reason, "reason_detail": f"{reason} detail",
            "duration_seconds": 300.0, "partial_output_lines": 3, "runner_pid": 1,
        }

    def test_timeout_over_decided_pass_is_refused_and_existing_kept(self, env):
        """The exact OI-1470 shape: a real pass sits in the slot, a second
        run times out/errors and books ``unavailable`` via record_failure —
        the pass must survive."""
        rf = env["results_dir"] / "pr-1691-glm_gate.json"
        pass_payload = _make_pass()
        rf.write_text(json.dumps(pass_payload), encoding="utf-8")

        result = record_failure(
            gate="glm_gate", pr_number=1691, pr_id="",
            result=self._fail_result("timeout"),
            request_payload={"gate": "glm_gate", "pr_number": 1691, "dispatch_id": "glm-gate-pr1691-2"},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )

        assert result["status"] == "pass"
        assert json.loads(rf.read_text(encoding="utf-8")) == pass_payload

    def test_timeout_on_empty_slot_writes_normally(self, env):
        rf = env["results_dir"] / "pr-1701-glm_gate.json"
        result = record_failure(
            gate="glm_gate", pr_number=1701, pr_id="",
            result=self._fail_result("timeout"),
            request_payload={"gate": "glm_gate", "pr_number": 1701},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )
        assert result["status"] == "unavailable"
        assert json.loads(rf.read_text(encoding="utf-8"))["status"] == "unavailable"

    def test_refused_write_does_not_emit_gate_failed_to_register(self, env, monkeypatch):
        """A refused write never happened — telling the register 'gate_failed'
        would describe a write that never landed."""
        rf = env["results_dir"] / "pr-1691-codex_gate.json"
        pass_payload = {
            "gate": "codex_gate", "pr_id": "1691", "pr_number": 1691, "status": "pass",
            "contract_hash": "realhash", "report_path": "/tmp/codex-report.md",
            "dispatch_id": "codex-gate-pr1691-1",
        }
        rf.write_text(json.dumps(pass_payload), encoding="utf-8")

        emitted = []
        import gate_register_emit
        monkeypatch.setattr(
            gate_register_emit, "emit_codex_gate_to_register",
            lambda *a, **kw: emitted.append((a, kw)),
        )

        record_failure(
            gate="codex_gate", pr_number=1691, pr_id="",
            result=self._fail_result("review_verdict_blocked"),  # not an execution-failure reason
            request_payload={"gate": "codex_gate", "pr_number": 1691, "dispatch_id": "codex-gate-pr1691-2"},
            requests_dir=env["requests_dir"], results_dir=env["results_dir"],
        )

        assert emitted == []
        assert json.loads(rf.read_text(encoding="utf-8")) == pass_payload


# ---------------------------------------------------------------------------
# write_result_guarded: shared low-level primitive used directly by
# gate_executor's own ci_gate terminal write (bypasses the two helpers above)
# ---------------------------------------------------------------------------


class TestWriteResultGuardedDirect:

    def test_returns_written_true_on_success(self, tmp_path):
        out = tmp_path / "pr-1-ci_gate.json"
        payload = {"gate": "ci_gate", "pr_number": 1, "status": "pass", "contract_hash": "h", "report_path": "/tmp/r.md"}
        result, written = write_result_guarded(out, payload, gate="ci_gate", pr_ref="1")
        assert written is True
        assert result == payload
        assert json.loads(out.read_text(encoding="utf-8")) == payload

    def test_returns_written_false_and_existing_on_refusal(self, tmp_path):
        out = tmp_path / "pr-1-ci_gate.json"
        pass_payload = {"gate": "ci_gate", "pr_number": 1, "status": "pass", "contract_hash": "h", "report_path": "/tmp/r.md"}
        out.write_text(json.dumps(pass_payload), encoding="utf-8")

        running_payload = {"gate": "ci_gate", "pr_number": 1, "status": "running", "contract_hash": "", "report_path": ""}
        result, written = write_result_guarded(out, running_payload, gate="ci_gate", pr_ref="1")
        assert written is False
        assert result == pass_payload
        assert json.loads(out.read_text(encoding="utf-8")) == pass_payload
