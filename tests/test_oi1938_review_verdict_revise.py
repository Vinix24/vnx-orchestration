"""OI-1938 / OI-1939: a review verdict REVISE is a fail, the verdict value decides.

Operator decision 2026-09-30. deepseek_gate wrote a valid verdict block with
``"verdict": "REVISE"`` and medium/low findings. The reader accepted only
pass/fail/blocked, so the run booked ``unavailable``. Accepting the word alone is
not enough either: the booking derived pass/fail from finding severities, so
medium/low findings would have booked a PASS that clears the merge door.

Every fixture here is built synthetically with the measured shape. No report of a
reviewed private repository is copied into this public repository.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import gate_lane_contract
import gate_status
from codex_parser import extract_verdict_block
from gate_artifacts import materialize_artifacts
from gate_seat_line import format_seat_line

PROSE = (
    "Ik heb de diff gelezen.\n"
    "Ik heb de aangeraakte bestanden geopend.\n"
    "Ik heb de tests nagelopen.\n\n"
)

TWO_FINDINGS = [
    {"severity": "medium", "file": "scripts/a.py", "line": 10, "summary": "eerste bevinding"},
    {"severity": "low", "file": "scripts/b.py", "line": 20, "summary": "tweede bevinding"},
]


def _block(verdict: str, findings) -> str:
    body = json.dumps({"verdict": verdict, "findings": findings, "residual_risk": None}, indent=2)
    return f"{PROSE}```json\n{body}\n```\n"


def _shell_call(command: str = "git diff --stat") -> str:
    event = {"type": "item.completed", "item": {
        "id": "item_0", "type": "command_execution", "command": command,
        "aggregated_output": "", "exit_code": 0, "status": "completed"}}
    return json.dumps(event)


def _codex_stream(text: str) -> str:
    events = [
        {"type": "thread.started", "thread_id": "t-oi1938"},
        {"type": "turn.started"},
        json.loads(_shell_call()),
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {"requests_dir": requests_dir, "results_dir": results_dir,
            "reports_dir": reports_dir, "state_dir": state_dir}


def _run(env, gate: str, stdout: str, *, pr_number: int = 1215, provider: str = "deepseek-harness",
         model: str = "deepseek-flash"):
    if gate == "codex_gate":
        dispatch_id = "codex-gate-pr%d-1790000000" % pr_number
    else:
        dispatch_id = "%s-pr%d-1790000000" % (gate.replace("_", "-"), pr_number)
    report_file = env["reports_dir"] / f"report-{gate}-{pr_number}.md"
    payload = {
        "gate": gate, "status": "requested", "provider": provider, "model": model,
        "pr_number": pr_number, "review_mode": "per_pr", "risk_class": "medium",
        "changed_files": ["scripts/a.py"], "requested_at": "20261001T080000Z",
        "dispatch_id": dispatch_id, "report_path": str(report_file),
    }
    result = materialize_artifacts(
        gate=gate, pr_number=pr_number, pr_id="", stdout=stdout, request_payload=payload,
        duration_seconds=12.0, requests_dir=env["requests_dir"],
        results_dir=env["results_dir"], reports_dir=env["reports_dir"],
    )
    return result


def _on_disk(env, gate: str, pr_number: int = 1215) -> dict:
    return json.loads((env["results_dir"] / f"pr-{pr_number}-{gate}.json").read_text(encoding="utf-8"))


class TestReviseIsAFail:

    @pytest.mark.parametrize("word", ["REVISE", "revise", " Revise "])
    def test_a_revise_with_non_blocking_findings_books_a_fail(self, env, word):
        """A1/A2. Base: unavailable / no_verdict_block."""
        result = _run(env, "deepseek_gate", _block(word, TWO_FINDINGS))

        assert result["status"] == "completed", result.get("reason_detail")
        assert gate_status.is_pass(result)[0] is False
        assert len(result["blocking_findings"]) == 1
        entry = result["blocking_findings"][0]
        assert "REVISE" in entry["message"].upper()
        assert "verdict_without_blocking_finding" in entry["message"]
        assert "2 finding(s)" in entry["message"]
        assert [f["message"] for f in result["advisory_findings"]] == ["eerste bevinding", "tweede bevinding"]
        assert [f["file_path"] for f in result["advisory_findings"]] == ["scripts/a.py", "scripts/b.py"]
        assert len(result["findings"]) == 3
        assert _on_disk(env, "deepseek_gate") == json.loads(json.dumps(result))

    def test_the_record_is_a_decided_fail_with_evidence(self, env):
        result = _run(env, "deepseek_gate", _block("REVISE", TWO_FINDINGS))

        assert gate_status.is_terminal(result)
        assert gate_status.has_complete_evidence(result)

    def test_a_revise_without_findings_is_a_fail_with_an_explicit_reason(self, env):
        """A3. Base: unavailable / no_verdict_block."""
        result = _run(env, "deepseek_gate", _block("REVISE", []))

        assert result["status"] == "completed", result.get("reason_detail")
        assert gate_status.is_pass(result)[0] is False
        assert len(result["blocking_findings"]) == 1
        message = result["blocking_findings"][0]["message"]
        assert "REVISE" in message and "no findings" in message

    def test_a_literal_fail_with_only_a_warning_is_a_fail(self, env):
        """A4. Base: completed, no blocking findings, is_pass True."""
        warning = [{"severity": "warning", "message": "kleine zorg", "file_path": "scripts/a.py", "line": 3}]
        result = _run(env, "deepseek_gate", _block("fail", warning))

        assert gate_status.is_pass(result)[0] is False
        assert len(result["blocking_findings"]) == 1
        assert "verdict_without_blocking_finding" in result["blocking_findings"][0]["message"]
        assert [f["message"] for f in result["advisory_findings"]] == ["kleine zorg"]

    def test_the_rule_is_not_keyed_on_the_gate_name(self, env):
        result = _run(env, "glm_gate", _block("REVISE", TWO_FINDINGS), provider="glm-harness", model="glm-5.2")

        assert result["status"] == "completed", result.get("reason_detail")
        assert gate_status.is_pass(result)[0] is False

    def test_a_revise_on_a_truncated_diff_books_completed_with_the_entry(self, env):
        """C8. A rejection of the part the model saw still stands."""
        with mock.patch("gate_artifacts.gate_depth.coverage_gap", return_value="diff cut at 50000 chars"):
            result = _run(env, "deepseek_gate", _block("REVISE", TWO_FINDINGS))

        assert result["status"] == "completed"
        assert len(result["blocking_findings"]) == 1


class TestCodexRegister:

    def test_a_codex_revise_registers_gate_failed(self, env):
        """A5. Base: unavailable by the guard, no gate_failed registered."""
        bare = '{"verdict": "REVISE", "findings": [{"severity": "warning", "message": "let op"}]}'
        stdout = _codex_stream(f"Mijn oordeel:\n{bare}")
        with mock.patch("gate_register_emit.emit_codex_gate_to_register") as emit:
            result = _run(env, "codex_gate", stdout, provider="codex", model="")

        assert gate_status.is_pass(result)[0] is False
        assert emit.call_count == 1
        assert emit.call_args.args[0] == "gate_failed"


class TestSeatLine:

    def test_a_decided_fail_booked_as_completed_reads_fail(self, env):
        """A6. Base: UNAVAILABLE (1 blocking finding(s))."""
        record = _run(env, "deepseek_gate", _block("REVISE", TWO_FINDINGS))
        entry = {
            "gate": "deepseek_gate", "request_status": "completed",
            "execution_status": record["status"], "passed": False,
            "pass_reason": "1 blocking finding(s)", "detail": record,
        }

        line, passed = format_seat_line(
            "deepseek_gate", pr_number=1215, state_dir=env["state_dir"],
            seat_entries={"deepseek_gate": entry},
        )

        assert passed is False
        assert line.startswith("Gate 'deepseek_gate': FAIL (")

    def test_an_unavailable_record_still_reads_unavailable(self, env):
        entry = {
            "gate": "deepseek_gate", "request_status": "completed",
            "execution_status": "unavailable", "passed": False,
            "detail": {"status": "unavailable", "reason": "quota"},
        }

        line, _ = format_seat_line(
            "deepseek_gate", pr_number=1215, state_dir=env["state_dir"],
            seat_entries={"deepseek_gate": entry},
        )

        assert line.startswith("Gate 'deepseek_gate': UNAVAILABLE (")

    def test_a_passing_record_downgraded_for_a_sha_mismatch_stays_unavailable(self, env):
        record = _run(env, "deepseek_gate", _block("pass", []))
        entry = {
            "gate": "deepseek_gate", "request_status": "completed",
            "execution_status": record["status"], "passed": False,
            "pass_reason": "result records another commit", "detail": record,
        }

        line, _ = format_seat_line(
            "deepseek_gate", pr_number=1215, state_dir=env["state_dir"],
            seat_entries={"deepseek_gate": entry},
        )

        assert line.startswith("Gate 'deepseek_gate': UNAVAILABLE (")


class TestControls:

    def test_a_pass_with_info_findings_stays_a_pass(self, env):
        """C1."""
        info = [{"severity": "info", "message": "stijl", "file_path": "", "line": 0}]
        result = _run(env, "deepseek_gate", _block("pass", info))

        assert gate_status.is_pass(result)[0] is True
        assert result["blocking_findings"] == []

    def test_a_fail_with_an_error_finding_gets_no_extra_entry(self, env):
        """C2."""
        err = [{"severity": "error", "message": "echt kapot", "file_path": "scripts/a.py", "line": 1}]
        result = _run(env, "deepseek_gate", _block("fail", err))

        assert [f["message"] for f in result["blocking_findings"]] == ["echt kapot"]

    @pytest.mark.parametrize("word", ["pass|fail|blocked", "maybe", "approve", "block", "changes_requested"])
    def test_unknown_words_stay_refused(self, env, word):
        """C3."""
        assert extract_verdict_block(_block(word, [])) == {}
        result = _run(env, "deepseek_gate", _block(word, []))

        assert result["status"] == "unavailable"
        assert result["reason_detail"].startswith("no_verdict_block:")

    def test_valid_verdicts_is_unchanged_and_shared(self):
        """C4."""
        import codex_parser
        import glm_gate
        import kimi_gate

        assert gate_lane_contract.VALID_VERDICTS == frozenset({"pass", "fail", "blocked"})
        assert glm_gate.VALID_VERDICTS is gate_lane_contract.VALID_VERDICTS
        assert kimi_gate.VALID_VERDICTS is gate_lane_contract.VALID_VERDICTS
        assert codex_parser.VALID_VERDICTS is gate_lane_contract.VALID_VERDICTS

    def test_the_plan_gate_still_reads_revise(self):
        """C5."""
        import plan_gate_panel

        assert "revise" in plan_gate_panel._VALID_VERDICTS

    def test_the_reader_keeps_the_raw_verdict_value(self):
        block = extract_verdict_block(_block(" Revise ", TWO_FINDINGS))

        assert block["verdict"] == " Revise "

    def test_findings_read_summary_and_file(self):
        from codex_parser import _normalize_findings

        out = _normalize_findings([{"severity": "low", "file": "x.py", "line": 4, "summary": "s"}])

        assert out == [{"severity": "low", "message": "s", "file_path": "x.py", "line": 4}]


class TestDeepseekGateDefault:

    def test_the_default_model_is_flash_41(self):
        """OI-1939."""
        assert gate_lane_contract.MODEL_DEFAULTS["deepseek_gate"] == (
            "VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash",
        )

    def test_the_build_lane_default_is_untouched(self):
        """C7."""
        from provider_spawns.deepseek_harness_spawn import DEFAULT_DEEPSEEK_HARNESS_MODEL

        assert DEFAULT_DEEPSEEK_HARNESS_MODEL == "deepseek-v4-pro"
