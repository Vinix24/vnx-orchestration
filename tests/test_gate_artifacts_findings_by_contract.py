"""OI-1763: every gate's verdict lands in the result record, not just codex_gate.

Measured over ALL completed review_gates/results records (2026-09-17):

| gate | completed | with findings | with residual_risk |
|---|---|---|---|
| codex_gate | 237 | 139 | 220 |
| glm_gate | 13 | 0 | 0 |
| kimi_gate | 3 | 0 | 0 |
| deepseek_gate | 4 | 0 | 0 |

`materialize_artifacts` (scripts/lib/gate_artifacts.py) gated its ONLY
findings/residual_risk extraction behind ``if gate == "codex_gate":`` — every
other gate booked ``findings=[]``/``residual_risk=""`` unconditionally, even
when its report carried a real fenced ```json verdict block, because the
extraction was keyed on the gate's NAME rather than on whether its output
actually carried the shared verdict-block shape every gate contract asks for
(VERDICT_CONTRACT / _REVIEWER_VERDICT_TEMPLATE, both a fenced ```json block
with a "verdict" key).

The fixtures under tests/fixtures/gate_verdict/ are real glm_gate reports
from PR #1862 (2026-09-17), copied from
~/.vnx-data/vnx-dev/unified_reports/headless/ so this test never depends on
that runtime directory.

Companion regression guard for the codex_gate side of this fix:
tests/test_codex_parser_versions.py (unaffected — codex_gate's own dedicated
extraction path in materialize_artifacts is untouched by this fix) plus
test_codex_gate_behavior_identical_to_before_fix below (proves
materialize_artifacts's codex_gate branch still returns exactly what
parse_codex_findings itself returns).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures" / "gate_verdict"
CODEX_FIXTURES_DIR = TESTS_DIR / "fixtures" / "codex_streams"
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

from gate_artifacts import materialize_artifacts
from codex_parser import parse_codex_findings
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


def _run_gate(env, gate: str, stdout: str, dispatch_id: str, pr_number: int, model: str = "glm-5.2", provider: str = "glm-harness"):
    """Materialize a harness-lane gate run with a gate-eigen dispatch-id
    (OI-1725 shape) so the identity guard admits it.
    """
    report_file = env["reports_dir"] / f"test-report-{dispatch_id}.md"
    payload = {
        "gate": gate,
        "status": "requested",
        "provider": provider,
        "branch": "dispatch/20260917-oi1750-weigering-audit",
        "pr_number": pr_number,
        "review_mode": "per_pr",
        "risk_class": "medium",
        "changed_files": ["scripts/lib/gate_recorder.py"],
        "requested_at": "20260917T085500Z",
        "prompt": "Review this code",
        "dispatch_id": dispatch_id,
        "model": model,
        "report_path": str(report_file),
    }
    result = materialize_artifacts(
        gate=gate,
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


class TestOI1763FindingsLandForGlmGate:
    """The measured regression case: glm_gate with a real verdict block."""

    def test_findings_land_in_result_record(self, env):
        stdout = _load_fixture("pr-1862-glm_gate-good-verdict.md")
        assert '"verdict"' in stdout, "fixture drifted: must carry a verdict block"

        result, _ = _run_gate(env, "glm_gate", stdout, dispatch_id="glm-gate-pr1862-1789635333", pr_number=1862)

        assert result["status"] == "completed"
        assert result["findings"] != [], (
            f"OI-1763 regression: glm_gate has a real verdict block with findings "
            f"but the result record booked findings={result['findings']!r}"
        )
        assert result["residual_risk"] != "", (
            "OI-1763 regression: glm_gate's verdict block carries a residual_risk "
            "but the result record booked an empty string"
        )

    def test_findings_persisted_to_disk_record(self, env):
        stdout = _load_fixture("pr-1862-glm_gate-good-verdict.md")
        result, _ = _run_gate(env, "glm_gate", stdout, dispatch_id="glm-gate-pr1862-1789635333", pr_number=1862)

        result_file = gate_recorder.result_file_path(env["results_dir"], "glm_gate", pr_number=1862, pr_id="")
        on_disk = json.loads(result_file.read_text(encoding="utf-8"))
        assert on_disk["findings"] == result["findings"]
        assert on_disk["residual_risk"] == result["residual_risk"]
        assert on_disk["findings"] != []

    def test_severity_maps_to_blocking_and_advisory(self, env):
        """gate_result_parser.py:122-124's documented mapping: blocking/error ->
        blocking_findings, everything else -> advisory_findings. The fixture's
        one finding is severity=info, so it must land in advisory, not blocking.
        """
        stdout = _load_fixture("pr-1862-glm_gate-good-verdict.md")
        result, _ = _run_gate(env, "glm_gate", stdout, dispatch_id="glm-gate-pr1862-1789635333", pr_number=1862)

        assert result["blocking_findings"] == []
        assert len(result["advisory_findings"]) == 1
        assert result["advisory_findings"][0]["severity"] == "info"

    def test_finding_carries_file_path_and_line(self, env):
        """OI-1769: file_path/line from the model's verdict JSON must survive
        into the result record, not just severity/message.
        """
        stdout = _load_fixture("pr-1862-glm_gate-good-verdict.md")
        result, _ = _run_gate(env, "glm_gate", stdout, dispatch_id="glm-gate-pr1862-1789635333", pr_number=1862)

        finding = result["findings"][0]
        assert finding["file_path"] == "scripts/lib/gate_recorder.py"
        assert finding["line"] == 1417


class TestOI1763FindingsLandForKimiAndDeepseek:
    """Same fix must apply to kimi_gate and deepseek_gate too — proves the
    check is keyed on the verdict-block SHAPE, not on a single gate's name
    (mirrors how test_gate_artifacts_verdict_block.py already proves the
    OI-1767 guard is keyed on provider-kind, not name).
    """

    @pytest.mark.parametrize("gate,provider,model", [
        ("kimi_gate", "kimi", "kimi-k3"),
        ("deepseek_gate", "deepseek-harness", "deepseek-v4-pro"),
    ])
    def test_findings_land_in_result_record(self, env, gate, provider, model):
        # Re-use the glm_gate fixture text verbatim: the verdict-block shape
        # is gate-agnostic by contract (VERDICT_CONTRACT), so the SAME report
        # body is valid stdout for any harness-lane gate.
        stdout = _load_fixture("pr-1862-glm_gate-good-verdict.md")

        result, _ = _run_gate(
            env, gate, stdout,
            dispatch_id=f"{gate.replace('_', '-')}-pr1862-1789635333",
            pr_number=1862, model=model, provider=provider,
        )

        assert result["status"] == "completed"
        assert result["findings"] != [], f"{gate}: findings must land, not stay []"
        assert result["residual_risk"] != "", f"{gate}: residual_risk must land, not stay ''"


class TestOI1763TwoFindingsBothLineZero:
    """Second real fixture (a different glm_gate run on the same PR): two
    info findings, both explicitly file_path-addressed but line=0 — proves
    a present file_path with an absent/zero line is preserved as-is, not
    coerced to a guessed value.
    """

    def test_two_findings_preserved(self, env):
        stdout = _load_fixture("pr-1862-glm_gate-two-findings-line-zero.md")
        result, _ = _run_gate(env, "glm_gate", stdout, dispatch_id="glm-gate-pr1862-1789636624", pr_number=1862)

        assert len(result["findings"]) == 2
        for finding in result["findings"]:
            assert finding["file_path"] == "scripts/lib/gate_recorder.py"
            assert finding["line"] == 0
            assert finding["severity"] == "info"


class TestOI1763CodexUnaffected:
    """Klaar-conditie #2: codex_gate's own behavior must not change. Its
    dedicated NDJSON-unwrap + markdown-bullet fallback (parse_codex_findings)
    stays the sole source for codex_gate; the new verdict-block path added
    for every other gate must never be consulted for codex_gate.
    """

    @pytest.mark.parametrize("fixture_name", [
        "v0.118-failure.json",
        "v0.118-blocking-findings.json",
        "v0.118-success.json",
    ])
    def test_codex_gate_behavior_identical_to_parse_codex_findings(self, env, fixture_name):
        """Controlegeval: for every codex NDJSON fixture that reaches the
        findings-extraction stage, materialize_artifacts's codex_gate branch
        must produce EXACTLY what parse_codex_findings itself returns for
        that same stdout — proving the OI-1763 restructure (adding the shared
        verdict-block path for other gates) left codex's own extraction
        untouched. v0.118-rate-limit.json is excluded here: its 1-line stdout
        fails materialize_artifacts's pre-existing _validate_content sparse-
        content floor before findings extraction is ever reached — a failure
        path this fix does not touch, already covered at the parser level by
        TestV0118RateLimit in test_codex_parser_versions.py.
        """
        stdout = (CODEX_FIXTURES_DIR / fixture_name).read_text(encoding="utf-8")
        expected = parse_codex_findings(stdout)

        result, _ = _run_gate(
            env, "codex_gate", stdout,
            dispatch_id="codex-gate-pr9999-1789699999", pr_number=9999,
            model="codex-1", provider="codex",
        )

        assert result["findings"] == expected["findings"], (
            f"{fixture_name}: codex_gate findings changed by the OI-1763 fix — "
            f"got {result['findings']!r}, expected {expected['findings']!r}"
        )
        assert result["residual_risk"] == (expected.get("residual_risk", "") or "")
