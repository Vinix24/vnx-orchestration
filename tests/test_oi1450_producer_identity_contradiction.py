"""A record and its own report must name the same model (OI-1450).

Measured across the central store on 2026-08-29 by joining each result record to
its report through ``dispatch_id``:

    records carrying a model:   48
      agree:                    40
      contradict:                7
      report has no model line:  1
      sub_provider missing:     48 of 48

All seven contradictions are the same pair — ``kimi-k2-7-code`` on the record
against ``kimi-default`` in the report:

    pr-904-kimi_gate.json  pr-905  pr-906  pr-907  pr-0  pr-1  pr-2

Both are producer-identity claims about one run and they cannot both be true.
This is not tidiness. Producer identity is what ``record_terminal_result``
refuses a terminal write without, so a record whose identity disagrees with its
own evidence is not authenticated by that evidence — it is merely accompanied by
it. Cost attribution, model comparison and "which model missed this" each read
one of the two and none of them knows the other exists.

An earlier pass of this same measurement reported **zero** contradictions. That
was a join failure, not an absence: it joined on ``report_path``, and every one
of the seven records has ``report_path = None``. The same blind spot is why the
existing contradiction detector never saw them — it skips a record with no
``report_path``, and skipping silently reads as "consistent" in a list of PASS
lines.
"""
from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

# Imported inside each test that needs them, so the end-to-end test below
# fails on its own assertion against the pre-fix code instead of erroring at
# collection on a symbol that does not exist there yet. A red run that only
# says "new symbol missing" proves nothing about the behaviour.

REPORT_WITH_MODEL = """# kimi_gate — Gate Report

**PR**: 904
**Model**: kimi-default
**Provider**: kimi

## Findings

No blocking findings.
"""


def _result(**over):
    payload = {"gate": "kimi_gate", "pr_id": "904", "status": "pass",
               "model": "kimi-k2-7-code"}
    payload.update(over)
    return payload


def test_the_measured_contradiction_is_caught():
    from closure_verifier import _detect_producer_identity_contradiction

    check = _detect_producer_identity_contradiction(
        "kimi_gate", _result(), REPORT_WITH_MODEL,
    )

    assert check is not None
    assert check.status == "FAIL", (
        "the record says kimi-k2-7-code and its own report says kimi-default; "
        "nothing flagged it"
    )
    assert "kimi-k2-7-code" in check.detail
    assert "kimi-default" in check.detail


def test_agreement_is_reported_as_agreement():
    """A check that only ever speaks up on failure cannot be distinguished from
    a check that is not running."""
    from closure_verifier import _detect_producer_identity_contradiction

    check = _detect_producer_identity_contradiction(
        "kimi_gate", _result(model="kimi-default"), REPORT_WITH_MODEL,
    )

    assert check is not None
    assert check.status == "PASS"
    assert "kimi-default" in check.detail


def test_a_record_without_a_model_is_not_judged():
    """Silence about an unmeasurable pair is the honest answer.

    424 of 470 records in the store carry no model at all. Asserting agreement
    for them would be inventing it, and asserting disagreement would fail the
    overwhelming majority of the history for saying nothing.
    """
    from closure_verifier import _detect_producer_identity_contradiction

    assert _detect_producer_identity_contradiction(
        "kimi_gate", _result(model=None), REPORT_WITH_MODEL,
    ) is None
    assert _detect_producer_identity_contradiction(
        "kimi_gate", _result(model=""), REPORT_WITH_MODEL,
    ) is None


def test_a_report_without_a_model_line_is_not_judged():
    from closure_verifier import _detect_producer_identity_contradiction

    assert _detect_producer_identity_contradiction(
        "kimi_gate", _result(), "# report\n\nNo blocking findings.\n",
    ) is None


def test_the_report_model_is_read_in_the_forms_reports_actually_use():
    """Bold field, bare field, and frontmatter all occur in this store."""
    from closure_verifier import _report_declared_model

    assert _report_declared_model("**Model**: kimi-k3\n") == "kimi-k3"
    assert _report_declared_model("Model: glm-5.2\n") == "glm-5.2"
    assert _report_declared_model("- Model: kimi-default\n") == "kimi-default"
    assert _report_declared_model("model = opus-5\n") == "opus-5"
    assert _report_declared_model("The model was chosen carefully.\n") == ""


def test_a_claimed_model_with_no_report_to_check_it_against_is_surfaced(tmp_path):
    """The shape that hid all seven: identity claimed, nothing to check it with.

    A detector keyed on report_path never looked at these records, and a silent
    skip renders as "consistent" among the PASS lines. It has to say what is
    actually true, which is that the claim is uncheckable.
    """
    import closure_verifier
    from review_contract import ReviewContract

    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    import json
    (results_dir / "904-kimi_gate-contract.json").write_text(json.dumps({
        "gate": "kimi_gate", "pr_id": "904", "status": "pass",
        "model": "kimi-k2-7-code", "report_path": None,
        "branch": "fix/x", "blocking_findings": [], "advisory_findings": [],
    }), encoding="utf-8")

    contract = ReviewContract(
        pr_id="904", branch="fix/x", risk_class="medium",
        review_stack=["kimi_gate"], changed_files=[], content_hash="d" * 16,
    )

    checks = closure_verifier._detect_gate_report_contradictions(contract, results_dir)

    identity = [c for c in checks if c.name == "producer_identity_kimi_gate"]
    assert identity, (
        "a record claiming a model with no report_path produced no check at "
        "all — the claim was skipped silently"
    )
    assert identity[0].status == "WARN"
    assert "cannot be checked" in identity[0].detail
