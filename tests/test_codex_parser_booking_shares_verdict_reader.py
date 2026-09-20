"""OI-1786: the codex booking and the codex guard read ONE verdict.

Two readers looked at the same codex stdout and reached different verdicts.
``extract_verdict_block`` (the guard, OI-1767/OI-1770) scans from the END, takes
only a ``verdict`` from ``VALID_VERDICTS`` and, since OI-1782, only an object that
is WRITTEN (opens a line). ``parse_codex_findings`` (the booking, via
``_extract_codex_verdict``) took the FIRST fenced block and accepted any value.

A worker that echoes ``gate_lane_contract.VERDICT_CONTRACT`` and then writes a
real verdict made them disagree::

    WACHTER  extract_verdict_block  -> verdict fail        | 1 blocking, 0 advisory
    BOEKING  parse_codex_findings   -> "pass|fail|blocked" | 0 blocking, 1 advisory

The booking picked the placeholder out of the echoed template, placeholder finding
``error|warning|info`` included, and the real blocking finding was gone. The merge
door and the closure verifier join on that record.

Now ``parse_codex_findings`` asks ``extract_verdict_block`` first. What it returns
is the verdict, with its findings and its residual_risk. Only a run the guard finds
no verdict in falls back to the markdown-bullet heuristic, which exists for a model
that skips the JSON altogether and stays codex-specific.

Every stream below is built from the real ``VERDICT_CONTRACT``, never a hand-copied
template: a copy drifts from the original and the test would stop covering anything.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from codex_parser import _normalize_findings, extract_verdict_block, parse_codex_findings
from gate_artifacts import _classify_findings, materialize_artifacts
from gate_lane_contract import VERDICT_CONTRACT

_REAL_FAIL = '''```json
{"verdict": "fail", "findings": [{"severity": "blocking", "message": "echte blokkerende bevinding",
 "file_path": "scripts/x.py", "line": 12}]}
```'''

_REAL_MESSAGE = "echte blokkerende bevinding"


def _echo_then_real(real: str = _REAL_FAIL) -> str:
    """The stream of the OI-1786 measurement: the template echoed, then a real verdict."""
    return f"Ik review.\n\n{VERDICT_CONTRACT}\n\nMijn oordeel:\n\n{real}\n"


def _codex_ndjson(message_text: str) -> str:
    """A codex ``exec --json`` run that looked once and ended in *message_text*.

    The event vocabulary is the one a real run emits (see
    tests/fixtures/gate_verdict/pr-1869-codex_gate-bare-verdict.ndjson). The one
    command_execution keeps the run clear of the degenerate-run refusal.
    """
    events = [
        {"type": "thread.started", "thread_id": "01a0-oi1786"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "id": "item_0", "type": "command_execution",
            "command": "/bin/zsh -lc 'git diff --stat'",
            "aggregated_output": "", "exit_code": 0, "status": "completed",
        }},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": message_text}},
        {"type": "turn.completed"},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _split(parsed: dict):
    blocking, advisory = _classify_findings(parsed["findings"])
    return blocking, advisory


class TestBookingReadsTheVerdictTheGuardReads:

    def test_echoed_template_then_real_verdict_books_the_real_one(self):
        """The measured stream. Red before the fix: verdict ``pass|fail|blocked``,
        0 blocking, the placeholder ``error|warning|info`` booked as advisory.
        """
        parsed = parse_codex_findings(_echo_then_real())

        assert parsed["verdict"].get("verdict") == "fail"
        blocking, advisory = _split(parsed)
        assert [f["message"] for f in blocking] == [_REAL_MESSAGE]
        assert advisory == [], f"the template's placeholder finding was booked: {advisory}"
        assert blocking[0]["file_path"] == "scripts/x.py"
        assert blocking[0]["line"] == 12

    def test_same_stream_as_a_codex_agent_message(self):
        """codex hands its verdict over inside an ``agent_message`` event."""
        parsed = parse_codex_findings(_codex_ndjson(_echo_then_real()))

        assert parsed["verdict"].get("verdict") == "fail"
        blocking, advisory = _split(parsed)
        assert [f["message"] for f in blocking] == [_REAL_MESSAGE]
        assert advisory == []

    def test_the_last_written_verdict_wins(self):
        """A draft ``pass`` followed by a final ``fail``. The old booking took the
        FIRST fenced block, so it booked the draft the worker had already revised.
        """
        draft = '```json\n{"verdict": "pass", "findings": [], "residual_risk": null}\n```'
        stdout = f"Eerste indruk:\n\n{draft}\n\nNa nader onderzoek:\n\n{_REAL_FAIL}\n"

        parsed = parse_codex_findings(stdout)

        assert parsed["verdict"].get("verdict") == "fail"
        assert [f["message"] for f in parsed["findings"]] == [_REAL_MESSAGE]

    def test_residual_risk_comes_from_the_same_verdict(self):
        real = (
            '```json\n{"verdict": "fail", "findings": [{"severity": "high", "message": "m"}], '
            '"residual_risk": "concurrent writes"}\n```'
        )

        parsed = parse_codex_findings(_echo_then_real(real))

        assert parsed["residual_risk"] == "concurrent writes"

    def test_a_decided_verdict_is_not_topped_up_with_prose_bullets(self):
        """The verdict is the verdict. ``findings: []`` is an answer, so bullets
        elsewhere in the prose are not booked beside it (the old booking ran the
        bullet heuristic whenever the verdict's own findings were empty).
        """
        stdout = (
            "## Findings\n\n- high: a bullet the reviewer wrote in prose\n\n"
            '```json\n{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'
        )

        parsed = parse_codex_findings(stdout)

        assert parsed["verdict"].get("verdict") == "pass"
        assert parsed["findings"] == []

    STREAMS = {
        "echoed-template-then-real-fail": _echo_then_real(),
        "echoed-template-only": f"Ik review.\n\n{VERDICT_CONTRACT}\n",
        "echoed-template-then-death": f"Mijn opdracht is:\n{VERDICT_CONTRACT}...Het oordeel is geslaagd.",
        "draft-pass-then-final-fail": (
            '```json\n{"verdict": "pass", "findings": []}\n```\n\n' + _REAL_FAIL + "\n"
        ),
        "verdict-quoted-mid-line": 'Samenvatting: {"verdict": "pass", "findings": []}\n',
        "unknown-verdict-value": '{"verdict": "maybe", "findings": [{"severity": "high", "message": "m"}]}',
        "findings-without-a-verdict-key": '{"findings": [{"severity": "high", "message": "m"}]}',
        "bare-verdict-on-its-own-line": '{"verdict": "fail", "findings": [{"severity": "error", "message": "m"}]}',
        "padded-verdict-value": '{"verdict": " FAIL ", "findings": []}',
        "prose-bullets-no-json": "## Findings\n\n- high: unhandled exception\n",
        "empty": "",
    }

    @pytest.mark.parametrize("name", sorted(STREAMS))
    @pytest.mark.parametrize("wrap", [False, True], ids=["plain", "ndjson"])
    def test_booking_and_guard_agree_on_the_verdict(self, name, wrap):
        """One reader: whatever the guard finds is what is booked, and when the
        guard finds nothing the booking carries no verdict either.
        """
        stdout = self.STREAMS[name]
        if wrap:
            stdout = _codex_ndjson(stdout)

        guard = extract_verdict_block(stdout)
        parsed = parse_codex_findings(stdout)

        assert parsed["verdict"] == guard, f"{name}: booking read {parsed['verdict']}, guard read {guard}"
        if guard:
            assert parsed["findings"] == _normalize_findings(guard.get("findings") or [])
            assert parsed["residual_risk"] == (guard.get("residual_risk") or "")


class TestTheTextFallbackStillWorks:
    """The bullet heuristic is for a model that writes no verdict at all."""

    _BULLETS = "## Findings\n\n- critical: missing null check\n- high: unhandled exception\n"

    def test_bullets_without_any_json_block_still_yield_findings(self):
        stdout = f"Ik heb de diff bekeken.\n\n{self._BULLETS}\nKlaar.\n"

        parsed = parse_codex_findings(stdout)

        assert parsed["verdict"] == {}
        assert [(f["severity"], f["message"]) for f in parsed["findings"]] == [
            ("critical", "missing null check"),
            ("high", "unhandled exception"),
        ]

    def test_bullets_inside_a_codex_agent_message_still_yield_findings(self):
        parsed = parse_codex_findings(_codex_ndjson(self._BULLETS))

        assert parsed["verdict"] == {}
        assert len(parsed["findings"]) == 2
        assert {f["severity"] for f in parsed["findings"]} == {"critical", "high"}

    def test_the_fallback_fires_when_the_only_verdict_is_quoted(self):
        """A verdict object in the middle of a line is quoted, not written: the
        guard reads nothing there, so the booking books no verdict from it and
        falls back to the bullets.
        """
        stdout = 'Samenvatting: {"verdict": "pass", "findings": []}\n\n' + self._BULLETS

        parsed = parse_codex_findings(stdout)

        assert parsed["verdict"] == {}
        assert len(parsed["findings"]) == 2

    def test_the_fallback_fires_for_a_verdict_outside_the_closed_set(self):
        stdout = '{"verdict": "maybe", "findings": []}\n\n' + self._BULLETS

        parsed = parse_codex_findings(stdout)

        assert parsed["verdict"] == {}
        assert len(parsed["findings"]) == 2

    def test_empty_output_books_nothing(self):
        parsed = parse_codex_findings("")

        assert parsed == {"findings": [], "residual_risk": "", "verdict": {}, "raw_text": ""}


@pytest.fixture
def env(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    requests_dir = state_dir / "review_gates" / "requests"
    results_dir = state_dir / "review_gates" / "results"
    for d in (requests_dir, results_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    return {
        "state_dir": state_dir,
        "reports_dir": reports_dir,
        "requests_dir": requests_dir,
        "results_dir": results_dir,
    }


def _book(env, stdout: str) -> dict:
    payload = {
        "gate": "codex_gate",
        "status": "requested",
        "provider": "codex",
        "branch": "feat/test",
        "pr_number": 1786,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/lib/codex_parser.py"],
        "requested_at": "20260920T080034Z",
        "prompt": "Review this code",
        "dispatch_id": "codex-gate-pr1786-1789999999",
        "report_path": str(env["reports_dir"] / "oi1786-report.md"),
    }
    return materialize_artifacts(
        gate="codex_gate",
        pr_number=1786,
        pr_id="",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=1.5,
        requests_dir=env["requests_dir"],
        results_dir=env["results_dir"],
        reports_dir=env["reports_dir"],
    )


def _register_events(env) -> list[dict]:
    reg = env["state_dir"] / "dispatch_register.ndjson"
    if not reg.exists():
        return []
    return [json.loads(ln) for ln in reg.read_text().splitlines() if ln.strip()]


class TestTheBookedRecordDoesNotContradictTheGuard:
    """The end of the chain: what lands in the result record and the register."""

    def test_a_fail_behind_an_echoed_template_is_booked_as_a_fail(self, env):
        stdout = _codex_ndjson(_echo_then_real())
        assert extract_verdict_block(stdout).get("verdict") == "fail", "the guard reads a fail here"

        result = _book(env, stdout)

        assert result["status"] == "completed"
        assert [f["message"] for f in result["blocking_findings"]] == [_REAL_MESSAGE]
        assert [f["message"] for f in result["findings"]] == [_REAL_MESSAGE]
        assert result["advisory_findings"] == []
        events = _register_events(env)
        assert [e["event"] for e in events] == ["gate_failed"]

    def test_the_booked_findings_survive_to_the_record_on_disk(self, env):
        result = _book(env, _codex_ndjson(_echo_then_real()))

        record = next((env["results_dir"]).glob("*codex_gate*.json"))
        on_disk = json.loads(record.read_text(encoding="utf-8"))
        assert on_disk["blocking_findings"] == result["blocking_findings"]
        assert [f["message"] for f in on_disk["blocking_findings"]] == [_REAL_MESSAGE]

    def test_a_padded_verdict_value_reads_the_same_in_the_register(self, env):
        """The guard trims and lowercases the value before it checks it. The
        register event was derived from the raw value, so ``" FAIL "`` passed the
        guard as a fail and was registered as ``gate_passed``.
        """
        stdout = _codex_ndjson('{"verdict": " FAIL ", "findings": [], "residual_risk": null}')
        assert extract_verdict_block(stdout).get("verdict") == " FAIL "

        result = _book(env, stdout)

        assert result["status"] == "completed"
        assert [e["event"] for e in _register_events(env)] == ["gate_failed"]

    def test_a_clean_pass_is_still_a_pass(self, env):
        """Control: the change must not turn a clean review into a failure."""
        stdout = _codex_ndjson('{"verdict": "pass", "findings": [], "residual_risk": null}')

        result = _book(env, stdout)

        assert result["status"] == "completed"
        assert result["findings"] == []
        assert result["blocking_findings"] == []
        assert [e["event"] for e in _register_events(env)] == ["gate_passed"]

    def test_a_run_with_no_verdict_is_still_refused(self, env):
        """Control: the OI-1770 guard is untouched. Bullets alone are not a verdict."""
        stdout = _codex_ndjson("## Findings\n\n- high: unhandled exception\n\nKlaar met de review van de diff.")

        result = _book(env, stdout)

        assert result["status"] == "unavailable"
        assert "no_verdict_block" in result["reason_detail"]
