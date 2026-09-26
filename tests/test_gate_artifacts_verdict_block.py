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

The guard covers the harness-lane gates (glm_gate/kimi_gate/deepseek_gate, the
ones that run VERDICT_CONTRACT via gate_runner's harness-lane strategy) and, since
OI-1770, codex_gate. Which gates it covers is decided by
``gate_artifacts._verdict_is_required`` on the provider registry: the harness-lane
kind, or a path binary whose stream ``extract_verdict_block`` unwraps. It is not
decided by a gate name.

Not covered, each for a stated reason (TestOI1770VerdictScope pins the table):
the ``gh`` gates claude_github_optional, ci_gate and wiring_gate (they book GitHub
state, no model writes a verdict).

The old tests that pinned "plain prose, no verdict block, still completed" for
codex_gate encoded the defect OI-1767 describes. Their fixtures now end in a
real verdict and their assertions stayed.
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

import gate_artifacts
from gate_artifacts import materialize_artifacts
from gate_lane_contract import VERDICT_CONTRACT
from gate_prompt import sanitize_diff
from gate_runner import _REVIEWER_VERDICT_TEMPLATE
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

    def test_echoed_sanitized_diff_then_death_books_unavailable(self, env):
        """OI-1782, measured on PR 1871: a harness-lane gate that dies after
        echoing the diff of its own prompt must book `unavailable`. The diff went
        through ``sanitize_diff``, which neutralizes the ```json opener and leaves
        a bare verdict object in a line of code untouched. Read without a position
        rule, that quoted object booked `completed` with a verdict nobody wrote.
        """
        diff = (
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n"
            "+++ b/tests/test_x.py\n"
            "@@ -1,1 +1,3 @@\n"
            " import pytest\n"
            "+def test_the_verdict_value_is_trimmed_and_lowercased_before_the_check():\n"
            "+    result = extract_verdict_block('{\"verdict\": \" PASS \", \"findings\": []}')\n"
        )
        stdout = (
            "Ik ga de diff reviewen. Dit is wat ik kreeg:\n"
            + sanitize_diff(diff)
            + "\n...Het oordeel is geslaagd. Laat me het neerschrijven.\n"
        )
        assert "```json" not in stdout, "test drifted: the sanitizer must have run"
        assert '{"verdict": " PASS "' in stdout, "test drifted: the echo must quote a verdict-shaped object"

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789640005")

        assert result["status"] == "unavailable", (
            f"a quoted verdict in an echoed diff booked {result['status']!r} instead of unavailable — {result}"
        )
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]

    def test_echoed_diff_followed_by_a_written_verdict_still_books_completed(self, env):
        """Control: the echo does not poison the run. A gate that quotes the diff
        and then writes its answer on a line of its own booked `completed` before
        OI-1782 and still does, on the answer and not on the quote.
        """
        diff = (
            "+    result = extract_verdict_block('{\"verdict\": \" PASS \", \"findings\": []}')\n"
        )
        stdout = (
            "Ik ga de diff reviewen. Dit is wat ik kreeg:\n"
            + sanitize_diff(diff)
            + "\nNa onderzoek is er een blokkerend probleem.\n"
            '{"verdict": "fail", "findings": [{"severity": "error", "message": "swallowed exception", '
            '"file_path": "scripts/lib/x.py", "line": 7}], "residual_risk": null}\n'
        )

        result, _ = _run_glm_gate(env, stdout, dispatch_id="glm-gate-pr1862-1789640006")

        assert result["status"] == "completed", f"got {result}"
        assert [f["message"] for f in result["blocking_findings"]] == ["swallowed exception"]

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


# ---------------------------------------------------------------------------
# OI-1770: the guard covers codex_gate, decided on the provider registry
# ---------------------------------------------------------------------------


def _codex_events(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _codex_command(index: int) -> dict:
    return {"type": "item.completed", "item": {
        "id": f"item_{index}", "type": "command_execution",
        "command": "/bin/zsh -lc \"rg -n 'schema_version' tests scripts\"",
        "aggregated_output": "", "exit_code": 0, "status": "completed",
    }}


def _codex_message(index: int, text: str) -> dict:
    return {"type": "item.completed", "item": {
        "id": f"item_{index}", "type": "agent_message", "text": text,
    }}


def _run_codex_gate(env, stdout: str, gate: str = "codex_gate", pr_number: int = 1869):
    """Materialize a codex-shaped run. A path_binary gate carries the builder's
    dispatch-id BY DESIGN (OI-1725 applies to harness-lane gates only), so the
    id here is a builder id and the run reaches the verdict guard.
    """
    report_file = env["reports_dir"] / f"test-report-{gate}-{pr_number}.md"
    payload = {
        "gate": gate,
        "status": "requested",
        "provider": "codex",
        "branch": "dispatch/20260919-082000-rolelane-overname",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/lib/gate_artifacts.py"],
        "requested_at": "20260919T101223Z",
        "prompt": "Review this code",
        "dispatch_id": "20260919-082000-rolelane-overname",
        "report_path": str(report_file),
    }
    result = materialize_artifacts(
        gate=gate,
        pr_number=pr_number,
        pr_id="",
        stdout=stdout,
        request_payload=payload,
        duration_seconds=37.0,
        requests_dir=env["requests_dir"],
        results_dir=env["results_dir"],
        reports_dir=env["reports_dir"],
    )
    return result, report_file


class TestOI1770CodexGate:

    def test_the_real_bare_verdict_run_still_books_completed(self, env):
        """The precondition of OI-1770, on the real report. codex_gate PR 1869
        (2026-09-19) wrote its verdict bare: 0 fences in 3845 characters. Widening
        the guard while the reader demanded a fence booked exactly this run as
        `unavailable`. It must keep booking `completed`.
        """
        stdout = _load_fixture("pr-1869-codex_gate-bare-verdict.ndjson")
        assert "```" not in stdout, "fixture drifted: the real codex run wrote its verdict bare"

        result, report_file = _run_codex_gate(env, stdout)

        assert result["status"] == "completed", f"the guard refused a real, good codex run: {result}"
        assert result["blocking_findings"] == []
        assert report_file.exists()

    def test_a_run_that_wrote_no_verdict_books_unavailable(self, env):
        """The defect the old codex tests encoded. Three lines of prose and no
        verdict used to book `completed`, and those tests asserted it. A record
        that says `completed` over a review nobody concluded reads as a clean PASS.
        """
        stdout = "Review line one.\nReview line two.\nReview line three.\n"

        result, report_file = _run_codex_gate(env, stdout)

        assert result["status"] == "unavailable", (
            f"a codex_gate run with no verdict booked {result['status']!r}: {result}"
        )
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]
        assert report_file.exists(), "the refused run's report is the evidence and must stay"

        result_file = gate_recorder.result_file_path(
            env["results_dir"], "codex_gate", pr_number=1869, pr_id="",
        )
        assert json.loads(result_file.read_text(encoding="utf-8"))["status"] == "unavailable"

    def test_a_run_that_stopped_mid_investigation_books_unavailable(self, env):
        """The shape of the one real codex_gate report of 308 that carries no
        verdict (pr-861, 2026-06-14): the stream ends on a command result. It took
        an action, so the depth check does not refuse it, and only this guard can.
        """
        stdout = _codex_events(
            {"type": "thread.started", "thread_id": "01a0-dead-run"},
            {"type": "turn.started"},
            _codex_message(0, "Ik lees eerst de diff en de tests eromheen."),
            _codex_command(1),
        )

        result, _ = _run_codex_gate(env, stdout)

        assert result["status"] == "unavailable"
        assert result["reason"] == "validation_failed", (
            f"refused for the wrong reason (the depth check must not be what stops this): {result}"
        )
        assert "no_verdict_block" in result["reason_detail"]

    def test_an_echoed_reviewer_template_then_death_books_unavailable(self, env):
        """The OI-1767 echo case for codex: a reviewer that repeats its own
        instructions (the real ``_REVIEWER_VERDICT_TEMPLATE``, whose verdict is the
        placeholder "pass|fail|blocked") and then stops has not decided anything.
        """
        stdout = _codex_events(
            {"type": "thread.started", "thread_id": "01a0-echo"},
            _codex_command(0),
            _codex_message(
                1,
                "Mijn opdracht is:\n" + _REVIEWER_VERDICT_TEMPLATE
                + "...Het oordeel is geslaagd. Laat me het neerschrijven.",
            ),
        )

        result, _ = _run_codex_gate(env, stdout)

        assert result["status"] == "unavailable", f"got {result}"
        assert result["reason"] == "validation_failed"
        assert "no_verdict_block" in result["reason_detail"]

    def test_the_findings_of_a_bare_verdict_land_in_the_record(self, env):
        """Control: refusing the verdict-less run must not cost a good run its
        findings. A bare fail verdict is booked as completed with its finding.
        """
        stdout = _codex_events(
            {"type": "thread.started", "thread_id": "01a0-fail"},
            _codex_command(0),
            _codex_message(1, json.dumps({
                "verdict": "fail",
                "findings": [{"severity": "error", "message": "swallowed exception",
                              "file_path": "scripts/lib/x.py", "line": 7}],
                "residual_risk": "geen aanvullend risico gemeten",
            }, indent=2)),
        )

        result, _ = _run_codex_gate(env, stdout)

        assert result["status"] == "completed"
        assert [f["message"] for f in result["blocking_findings"]] == ["swallowed exception"]


# gate -> (a run that wrote no verdict is refused, why). Every gate in
# GATE_PROVIDERS has to be classified here. A gate registered without a decision
# fails test_every_registered_gate_is_classified instead of silently booking
# `completed` over a review with no verdict, which is what OI-1763 did to findings.
_VERDICT_SCOPE = {
    "glm_gate": (True, "harness lane: the lane's report text is read fenced or bare"),
    "kimi_gate": (True, "harness lane: the lane's report text is read fenced or bare"),
    "deepseek_gate": (True, "harness lane: the lane's report text is read fenced or bare"),
    "codex_gate": (True, "path binary `codex`: its exec --json stream is unwrapped by the reader"),
    "claude_github_optional": (False, "path binary `gh`: reads GitHub state, no model writes a verdict block"),
    "ci_gate": (False, "path binary `gh`: reads GitHub state, no model writes a verdict block"),
    "wiring_gate": (False, "path binary `gh`: reads GitHub state, no model writes a verdict block"),
}


class TestOI1770VerdictScope:

    def test_every_registered_gate_is_classified(self):
        """A new gate must arrive with a decision about the verdict guard."""
        assert set(_VERDICT_SCOPE) == set(gate_recorder.GATE_PROVIDERS), (
            "GATE_PROVIDERS and the verdict-scope table drifted: classify the new "
            f"gate. registered={sorted(gate_recorder.GATE_PROVIDERS)} "
            f"classified={sorted(_VERDICT_SCOPE)}"
        )

    @pytest.mark.parametrize("gate", sorted(_VERDICT_SCOPE))
    def test_coverage_matches_the_table(self, gate):
        expected, why = _VERDICT_SCOPE[gate]
        assert gate_artifacts._verdict_is_required(gate) is expected, (
            f"{gate}: expected required={expected} ({why})"
        )

    def test_an_unregistered_gate_is_not_covered(self):
        """The runner refuses it before it gets here (OI-1490); the guard does not
        invent an answer for a gate the registry has never heard of."""
        assert gate_artifacts._verdict_is_required("no_such_gate") is False

    def test_a_second_gate_on_the_codex_binary_is_covered_without_an_edit(self, env, monkeypatch):
        """The kenmerk is the provider's output, not the gate's name: a gate
        registered on the ``codex`` binary under any name is held to a verdict,
        and one registered on a binary the reader cannot unwrap is not."""
        monkeypatch.setitem(
            gate_recorder.GATE_PROVIDERS, "codex_second_pass",
            (gate_recorder.GATE_PROVIDER_PATH_BINARY, "codex"),
        )
        monkeypatch.setitem(
            gate_recorder.GATE_PROVIDERS, "other_second_pass",
            (gate_recorder.GATE_PROVIDER_PATH_BINARY, "otherbin"),
        )
        assert gate_artifacts._verdict_is_required("codex_second_pass") is True
        assert gate_artifacts._verdict_is_required("other_second_pass") is False

        result, _ = _run_codex_gate(
            env, "Review line one.\nReview line two.\nReview line three.\n",
            gate="codex_second_pass", pr_number=1870,
        )
        assert result["status"] == "unavailable"
        assert "no_verdict_block" in result["reason_detail"]
