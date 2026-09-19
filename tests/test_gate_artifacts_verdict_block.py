"""OI-1767: a gate run with no parseable verdict block must book `unavailable`,
never `completed`.

Live evidence (glm_gate, PR #1862, 2026-09-17): a run wrote "De bevindingen
zijn geen blokkerende problemen. Het oordeel is geslaagd. Laat me het
neerschrijven." and stopped there — 5808 characters, 0 fenced ```json blocks,
0 mentions of "verdict" — yet booked `completed`, because
materialize_artifacts's only content check (_validate_content, a 3-line
floor) has no way to tell "wrote enough text" from "actually decided".

The two fixtures under tests/fixtures/gate_verdict/ are the real reports from
that incident: the broken one (no verdict block, must now refuse) and one of
its two healthy siblings on the same PR (has a fenced verdict block, must
keep booking `completed` unchanged) — the control case that proves the fix
does not sweep up good runs along with the bad one.

The guard is scoped to harness-lane gates (glm_gate/kimi_gate/deepseek_gate —
the ones that actually run VERDICT_CONTRACT via gate_runner's harness-lane
strategy), via the SAME provider-kind lookup the OI-1725 guard already uses
just above it in materialize_artifacts. codex_gate/gemini_review/
claude_github_optional are untouched: their own pre-existing tests
(test_gate_artifacts_register.py, test_gate_artifacts_atomicity.py) pin
"plain prose, no verdict block, still completed" as accepted behavior today,
and widening this guard onto them turns those tests red — verified directly
by running an unscoped version of this guard against the suite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "gate_verdict"
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from gate_artifacts import materialize_artifacts
from gate_lane_contract import VERDICT_CONTRACT
import gate_recorder


def _load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    assert path.exists(), f"Fixture missing: {path}"
    return path.read_text(encoding="utf-8")


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


def _run_glm_gate(env, stdout: str, dispatch_id: str, pr_number: int = 1862):
    """Materialize a glm_gate run with a gate-eigen dispatch-id (OI-1725
    shape: 'glm-gate-pr<pr>-<timestamp>') so the identity guard admits it and
    the run actually reaches the OI-1767 verdict-block check.
    """
    report_file = env["reports_dir"] / f"test-report-{dispatch_id}.md"
    payload = {
        "gate": "glm_gate",
        "status": "requested",
        "provider": "glm-harness",
        "branch": "dispatch/20260917-oi1750-weigering-audit",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/lib/gate_recorder.py"],
        "requested_at": "20260917T085500Z",
        "prompt": "Review this code",
        "dispatch_id": dispatch_id,
        "model": "glm-5.2",
        "report_path": str(report_file),
    }
    result = materialize_artifacts(
        gate="glm_gate",
        pr_number=pr_number,
        pr_id="",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=165.4,
        requests_dir=env["requests_dir"],
        results_dir=env["results_dir"],
        reports_dir=env["reports_dir"],
    )
    return result, report_file


class TestOI1767VerdictBlockGuard:

    def test_broken_report_with_no_verdict_block_books_unavailable(self, env):
        """The exact broken pr-1862 report: 5808 chars, 0 fences, 0 "verdict".

        This is the regression case: before the fix this booked `completed`.
        Red before the fix, green after — the guard must refuse it.
        """
        stdout = _load_fixture("pr-1862-glm_gate-broken-no-verdict.md")
        assert "```json" not in stdout, "fixture drifted: broken report must have no fenced json block"
        assert "verdict" not in stdout.lower(), "fixture drifted: broken report must not mention verdict"

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789636345")

        assert result["status"] == "unavailable", (
            f"OI-1767 regression: a run with no parseable verdict block booked "
            f"{result['status']!r} instead of unavailable — {result}"
        )
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]

        # GATE-11: the failure record on disk must not claim status: completed.
        result_file = gate_recorder.result_file_path(
            env["results_dir"], "glm_gate", pr_number=1862, pr_id="",
        )
        on_disk = json.loads(result_file.read_text(encoding="utf-8"))
        assert on_disk["status"] != "completed"
        assert on_disk["status"] == "unavailable"

    def test_good_report_with_verdict_block_still_books_completed(self, env):
        """Control case: a healthy sibling report on the SAME PR, with a real
        fenced ```json verdict block, must keep booking `completed` unchanged.
        Without this, a fix could pass the negative test by refusing
        everything indiscriminately.
        """
        stdout = _load_fixture("pr-1862-glm_gate-good-verdict.md")
        assert "```json" in stdout
        assert '"verdict"' in stdout

        result, report_file = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789635333")

        assert result["status"] == "completed", (
            f"OI-1767 fix regressed a healthy run: {result}"
        )
        assert report_file.exists()

        result_file = gate_recorder.result_file_path(
            env["results_dir"], "glm_gate", pr_number=1862, pr_id="",
        )
        on_disk = json.loads(result_file.read_text(encoding="utf-8"))
        assert on_disk["status"] == "completed"

    def test_echoed_template_then_death_mid_sentence_books_unavailable(self, env):
        """OI-1767 fix-forward (third): a run that echoes VERDICT_CONTRACT back
        (a model that repeats its own instructions) and then dies mid-sentence
        must book `unavailable`, exactly like the no-verdict-block case above.

        Before this fix: ``extract_verdict_block`` took the FIRST fenced json
        block unconditionally, and the echoed VERDICT_CONTRACT IS a fenced json
        block with a "verdict" key (value: the literal placeholder text
        "pass|fail|blocked"). The guard read that as a real decision and
        booked `completed` — reproduced directly against this exact scenario.
        Built with the REAL VERDICT_CONTRACT constant, not a hand-copied one.
        """
        stdout = (
            "Ik ga de diff reviewen. Mijn opdracht is:\n"
            + VERDICT_CONTRACT
            + "...De bevindingen zijn geen blokkerende problemen. Het oordeel "
            "is geslaagd. Laat me het neerschrijven."
        )

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789640001")

        assert result["status"] == "unavailable", (
            f"OI-1767 echo-template regression: a report that echoes the "
            f"contract template booked {result['status']!r} instead of "
            f"unavailable — {result}"
        )
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]

    def test_template_echo_followed_by_real_verdict_still_books_completed(self, env):
        """Control: a worker that echoes the template and THEN writes a real
        verdict must still book `completed` on that real verdict — the fix
        must pick the LAST valid block, not refuse every report that happens
        to contain the template text anywhere.
        """
        stdout = (
            "Ik ga de diff reviewen. Mijn opdracht is:\n"
            + VERDICT_CONTRACT
            + "...Na onderzoek is er geen blokkerend probleem gevonden.\n"
            '```json\n{"verdict": "pass", "findings": [], "residual_risk": null}\n```\n'
        )

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789640002")

        assert result["status"] == "completed", (
            f"a real trailing verdict after an echoed template must still book "
            f"completed — {result}"
        )

    def test_bare_verdict_books_completed_and_lands_its_findings(self, env):
        """Format parity: a harness-lane run whose model wrote its verdict BARE,
        without a fence, did decide. ``extract_verdict_block`` used to see only
        a fenced verdict, so this booked `unavailable` (no_verdict_block) and
        the findings never reached the record. The same reader serves the guard
        and the findings booking, so both must follow it.
        """
        stdout = (
            "Ik heb de diff gelezen en naast de bijbehorende tests gelegd.\n"
            "Er zit een blokkerend probleem in de foutafhandeling.\n"
            "Het oordeel volgt hieronder.\n"
            '{"verdict": "fail", "findings": [{"severity": "error", "message": "swallowed exception", '
            '"file_path": "scripts/lib/x.py", "line": 7}], "residual_risk": "geen aanvullend risico gemeten"}\n'
        )
        assert "```" not in stdout, "test drifted: the verdict must be bare"

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789640003")

        assert result["status"] == "completed", (
            f"a bare, valid verdict must clear the OI-1767 guard, got {result}"
        )
        assert [f["message"] for f in result["blocking_findings"]] == ["swallowed exception"]
        assert result["blocking_findings"][0]["file_path"] == "scripts/lib/x.py"
        assert result["residual_risk"] == "geen aanvullend risico gemeten"

    def test_bare_echoed_template_then_death_still_books_unavailable(self, env):
        """The guard's strictness must survive the format widening: a worker
        that echoes the contract's JSON body WITHOUT its fence and then dies is
        no more a verdict than the fenced echo above.
        """
        contract_body = VERDICT_CONTRACT.split("```json\n", 1)[1].split("\n```", 1)[0]
        stdout = (
            "Ik ga de diff reviewen. Mijn opdracht is:\n"
            + contract_body
            + "\n...De bevindingen zijn geen blokkerende problemen. Het oordeel "
            "is geslaagd. Laat me het neerschrijven."
        )
        assert "```" not in stdout, "test drifted: the echoed template must be bare"

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789640004")

        assert result["status"] == "unavailable", (
            f"a bare echoed template booked {result['status']!r} instead of unavailable — {result}"
        )
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]

    def test_guard_applies_to_kimi_gate_too_not_just_glm(self, env, tmp_path, monkeypatch):
        """The same no-verdict-block shape must refuse kimi_gate too, not only
        glm_gate — both run VERDICT_CONTRACT via the SAME harness-lane
        strategy. Proves the check is keyed on provider-kind (the contract
        characteristic), not on a single gate's name.
        """
        state_dir = tmp_path / "state2"
        reports_dir = tmp_path / "reports2"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        for d in (requests_dir, results_dir, reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

        report_file = reports_dir / "kimi-no-verdict.md"
        stdout = (
            "De bevindingen zijn geen blokkerende problemen. Het oordeel is "
            "geslaagd. Laat me het neerschrijven. Genoeg tekst om de "
            "drie-regel-vloer van _validate_content te halen, met opzet, "
            "zodat alleen de OI-1767 verdict-blok-check dit kan weigeren "
            "en niet de bestaande sparse-content check.\n"
            "Nog een substantiële regel om zeker te zijn.\n"
            "En nog een derde substantiële regel voor de vloer.\n"
        )
        payload = {
            "gate": "kimi_gate",
            "status": "requested",
            "provider": "kimi",
            "branch": "feat/test",
            "pr_number": 4242,
            "review_mode": "per_pr",
            "risk_class": "medium",
            "changed_files": ["scripts/foo.py"],
            "requested_at": "20260917T085500Z",
            "prompt": "Review this code",
            "dispatch_id": "kimi-gate-pr4242-1789640000",
            "model": "kimi-k3",
            "report_path": str(report_file),
        }
        result = materialize_artifacts(
            gate="kimi_gate",
            pr_number=4242,
            pr_id="",
            stdout=stdout,
            request_payload=payload,
            duration_seconds=12.0,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=reports_dir,
        )

        assert result["status"] == "unavailable", (
            f"kimi_gate with no verdict block must refuse exactly like "
            f"glm_gate — got {result}"
        )
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]

    def test_non_harness_lane_gate_unaffected(self, env, tmp_path, monkeypatch):
        """gemini_review is a PATH_BINARY gate, not harness-lane: this guard is
        deliberately scoped to harness-lane providers only, because
        gemini_review's own tests (test_gate_artifacts_register.py) already
        pin "plain prose, no verdict block, still completed" as its accepted
        contract. Documents the scope boundary so a future edit that widens
        or narrows it does so on purpose, not by accident.
        """
        state_dir = tmp_path / "state3"
        reports_dir = tmp_path / "reports3"
        requests_dir = state_dir / "review_gates" / "requests"
        results_dir = state_dir / "review_gates" / "results"
        for d in (requests_dir, results_dir, reports_dir):
            d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

        report_file = reports_dir / "gemini-no-verdict.md"
        stdout = "Review line one.\nReview line two.\nReview line three.\n"
        payload = {
            "gate": "gemini_review",
            "status": "requested",
            "provider": "gemini",
            "branch": "feat/test",
            "pr_number": 4343,
            "review_mode": "per_pr",
            "risk_class": "medium",
            "changed_files": ["scripts/foo.py"],
            "requested_at": "20260917T085500Z",
            "prompt": "Review this code",
            "dispatch_id": "test-gemini-dispatch",
            "report_path": str(report_file),
        }
        result = materialize_artifacts(
            gate="gemini_review",
            pr_number=4343,
            pr_id="",
            stdout=stdout,
            request_payload=payload,
            duration_seconds=12.0,
            requests_dir=requests_dir,
            results_dir=results_dir,
            reports_dir=reports_dir,
        )

        assert result["status"] == "completed"
