"""Regression tests for OI-1473: invariant 7 must compare structured fields,
not loose words in prose (dispatch 20260826-t0-invariant7-negatie).

Measured live on PR #1691 (``unified_reports/glm-gate-pr1691-1787770872.md``):
``closure_verifier._count_report_blocking_indicators`` scanned the report with
``r"BLOCK(?:ER|ING)\\s*:"`` (case-insensitive) and matched a NEGATION:

    regel 49: "**Twee bevindingen, beide niet-blocking:**"        -> matched "blocking:"
    regel 61: "Not blocking: no current code path returns None."  -> matched "blocking:"

The gate result record said ``status=pass``, ``blocking_findings=[]``, and the
report's own two findings carried severity ``warning``/``info`` — the report
said TWICE, explicitly, that nothing was blocking. Invariant 7
(``closure_verifier.check_review_gate_for_merge``) read the negated match as a
gate/report contradiction and refused the merge (PR #1691 was otherwise ready:
VNX CI green, glm pass with a real contract_hash, report present).

The fix makes ``_count_report_blocking_indicators`` compare STRUCTURED fields
in priority order — the report's own trailing ```json``` verdict fence, then
the normalized ``## Findings`` section's severity markers, and only then a
prose vangnet that now excludes matches immediately preceded by a negation
marker (``niet-``, ``non-``, ``no ``, ``not ``, ``geen ``). A report with none
of the three sources contradicts nothing: fail-open, not fail-closed on
absence of evidence.

Covers the dispatch's required evidence:
  1. Both measured negation phrasings, run through the REAL
     ``check_review_gate_for_merge`` merge check, with a json verdict fence
     present (the real glm_gate/kimi_gate report shape) -> GO, no
     contradiction.
  2. The SAME two phrasings with no fence and no normalized findings section
     (isolating the vangnet tier itself) -> zero indicators.
  3. The mandatory inverse: a genuine contradiction (fence findings carrying
     severity ``blocking``, or a fence ``verdict: fail``) paired with a
     record that claims ``status=pass`` -> invariant 7 MUST still refuse.
  4. A report with no fence, no findings section, and no vangnet match at all
     -> zero indicators (fail-open on absence of evidence, not fail-closed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier as cv


def _write_result(results_dir: Path, pr_id: str, gate: str, data: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    slug = pr_id.lower().replace("-", "")
    path = results_dir / f"{slug}-{gate}-contract.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _passing_record(report_file: Path, **overrides) -> dict:
    data = {
        "gate": "glm_gate",
        "pr_id": "PR-1691",
        "status": "pass",
        "blocking_count": 0,
        "blocking_findings": [],
        "contract_hash": "abcdef1234567890",
        "report_path": str(report_file),
        "branch": "feature/oi-1473",
    }
    data.update(overrides)
    return data


# The two measured negation strings from PR #1691's report, verbatim.
_MEASURED_NIET_BLOCKING_LINE = "**Twee bevindingen, beide niet-blocking:**"
_MEASURED_NOT_BLOCKING_LINE = "Not blocking: no current code path returns None here."


def _real_report_shape(negation_line: str) -> str:
    """A faithful shrink of the real PR #1691 report shape: prose carrying
    the measured negation line, followed by the worker's own trailing
    ```json``` verdict fence (the same shape glm_gate/kimi_gate's
    ``_VERDICT_CONTRACT`` requires) with a clean pass verdict and
    warning/info-severity findings — never ``blocking``/``error``."""
    return (
        "# glm_gate — Headless Gate Report\n\n"
        "Ik heb de diff beoordeeld op correctheid en governance-impact.\n\n"
        f"{negation_line}\n\n"
        "1. Kleine naming-inconsistentie in de nieuwe helper.\n"
        "2. Ontbrekende docstring op de publieke functie.\n\n"
        "```json\n"
        + json.dumps({
            "verdict": "pass",
            "findings": [
                {"severity": "warning", "message": "naming inconsistency"},
                {"severity": "info", "message": "missing docstring"},
            ],
            "residual_risk": None,
        })
        + "\n```\n"
    )


class TestInvariant7DoesNotBlockOnNegatedProse:
    """Evidence 1 (required): the real PR #1691 shape, through the REAL merge
    check, must clear invariant 7 — the fence is primary evidence and the
    report explicitly says nothing is blocking."""

    def test_niet_blocking_header_clears_merge_check(self, tmp_path):
        report = tmp_path / "glm-gate-pr1691.md"
        report.write_text(_real_report_shape(_MEASURED_NIET_BLOCKING_LINE), encoding="utf-8")
        results_dir = tmp_path / "results"
        _write_result(results_dir, "PR-1691", "glm_gate", _passing_record(report))

        gate = cv.check_review_gate_for_merge(
            "PR-1691", "glm_gate", results_dir, branch="feature/oi-1473",
        )

        assert gate["verdict"] == "GO", gate["message"]

    def test_not_blocking_line_clears_merge_check(self, tmp_path):
        report = tmp_path / "glm-gate-pr1691.md"
        report.write_text(_real_report_shape(_MEASURED_NOT_BLOCKING_LINE), encoding="utf-8")
        results_dir = tmp_path / "results"
        _write_result(results_dir, "PR-1691", "glm_gate", _passing_record(report))

        gate = cv.check_review_gate_for_merge(
            "PR-1691", "glm_gate", results_dir, branch="feature/oi-1473",
        )

        assert gate["verdict"] == "GO", gate["message"]


class TestProseVangnetExcludesNegations:
    """Evidence 2: isolate the vangnet tier itself (no fence, no normalized
    ``## Findings`` section) against the two measured negation lines — proves
    the fix is in the fallback scan, not merely masked by fence priority."""

    def test_niet_blocking_prefix_not_counted(self):
        report = (
            "# Gemini Review\n\n"
            f"{_MEASURED_NIET_BLOCKING_LINE}\n\n"
            "1. Style nit only.\n"
        )
        assert cv._count_report_blocking_indicators(report) == 0

    def test_not_blocking_prefix_not_counted(self):
        report = f"# Gemini Review\n\n{_MEASURED_NOT_BLOCKING_LINE}\n"
        assert cv._count_report_blocking_indicators(report) == 0

    def test_genuine_unnegated_blocker_still_counted(self):
        """Negative control: the negation exclusion must not blind the
        vangnet to a real, unnegated blocking marker."""
        report = "# Gemini Review\n\nBLOCKER: missing auth check on the new endpoint.\n"
        assert cv._count_report_blocking_indicators(report) >= 1


class TestInvariant7StillCatchesRealContradictions:
    """Evidence 3 (mandatory inverse): a control that can't fail is not a
    control. A record claiming ``status=pass`` paired with a report whose OWN
    verdict fence disagrees must still refuse the merge."""

    def test_fence_blocking_severity_finding_still_blocks(self, tmp_path):
        report = tmp_path / "glm-gate-pr1691.md"
        report.write_text(
            "# glm_gate — Headless Gate Report\n\n"
            "Ik heb de diff beoordeeld.\n\n"
            "```json\n"
            + json.dumps({
                "verdict": "pass",
                "findings": [
                    {"severity": "blocking", "message": "SQL injection in query_builder.py"},
                ],
                "residual_risk": "unsanitized input",
            })
            + "\n```\n",
            encoding="utf-8",
        )
        results_dir = tmp_path / "results"
        _write_result(results_dir, "PR-1691", "glm_gate", _passing_record(report))

        gate = cv.check_review_gate_for_merge(
            "PR-1691", "glm_gate", results_dir, branch="feature/oi-1473",
        )

        assert gate["verdict"] == "NO-GO"
        assert "bewijs spreekt zichzelf tegen" in gate["message"]

    def test_fence_verdict_fail_still_blocks(self, tmp_path):
        report = tmp_path / "glm-gate-pr1691.md"
        report.write_text(
            "# glm_gate — Headless Gate Report\n\n"
            "Ik heb de diff beoordeeld.\n\n"
            "```json\n"
            + json.dumps({
                "verdict": "fail",
                "findings": [{"severity": "warning", "message": "style nit only"}],
                "residual_risk": None,
            })
            + "\n```\n",
            encoding="utf-8",
        )
        results_dir = tmp_path / "results"
        _write_result(results_dir, "PR-1691", "glm_gate", _passing_record(report))

        gate = cv.check_review_gate_for_merge(
            "PR-1691", "glm_gate", results_dir, branch="feature/oi-1473",
        )

        assert gate["verdict"] == "NO-GO"
        assert "bewijs spreekt zichzelf tegen" in gate["message"]


class TestFailOpenOnAbsenceOfEvidence:
    """No fence, no normalized findings section, no vangnet match at all: an
    absence of evidence is not evidence of a contradiction."""

    def test_no_source_at_all_is_zero_never_blocks(self, tmp_path):
        report = tmp_path / "report.md"
        report.write_text("# Review\n\nEverything looks fine, approved.\n", encoding="utf-8")
        results_dir = tmp_path / "results"
        _write_result(results_dir, "PR-1691", "glm_gate", _passing_record(report))

        gate = cv.check_review_gate_for_merge(
            "PR-1691", "glm_gate", results_dir, branch="feature/oi-1473",
        )

        assert gate["verdict"] == "GO", gate["message"]
        assert cv._count_report_blocking_indicators(report.read_text(encoding="utf-8")) == 0
