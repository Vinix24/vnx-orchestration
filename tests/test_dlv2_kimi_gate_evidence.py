"""kimi_gate.py evidence-completeness tests (dispatch beta-dlv2-kimi-gate-bewijs).

Measured on the central store before this fix: every terminal (status=pass)
kimi_gate result carried neither ``contract_hash`` nor ``report_path`` — 9 of
9 records. ``gate_status.has_complete_evidence`` requires BOTH non-empty, so
a recognized kimi gate would have been PERMANENTLY unable to close a PR no
matter how good the review was, and ``closure_verifier.check_review_gate_
for_merge`` NO-GOs with "resultaat mist contract_hash en/of report_path".

kimi_gate.py now stamps both fields on a terminal verdict, reusing
``gate_artifacts._compute_contract_hash`` — the SAME hasher codex_gate's own
execution path (``gate_runner.run`` -> ``gate_artifacts.materialize_
artifacts``) calls — never a second hashing method, and pointing
``report_path`` at the governed dispatch's own unified report
(``<data_dir>/unified_reports/<dispatch_id>.md``, already on disk by the
time the dispatcher call returns; see ``governance_emit.emit_unified_report``
and ``provider_dispatch.py``).

An ``unavailable`` result (OI-1142: provider outage, no readable verdict)
must never look like complete evidence, and an offline ``--diff-file`` run
(``test_run: true``) must never count as evidence for a real PR — both
pre-existing invariants, both re-verified here so this fix cannot have
quietly broken either one.

``_make_default_dispatcher``/``_get_diff``/``get_pr_head_branch``/
``get_pr_head_sha`` are patched on the kimi_gate module namespace, not on
their source modules — ``from X import Y`` binds the name at import time, so
patching the source module would not affect kimi_gate's already-bound
reference (same convention as test_kimi_gate_provenance.py /
test_kimi_gate_unavailable.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import kimi_gate
import closure_verifier
import gate_artifacts
from gate_status import has_complete_evidence, is_terminal


_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

_FAKE_DIFF = "diff --git a/x b/x\n+ok\n"


def _write_unified_report(data_dir: Path, dispatch_id: str, text: str) -> Path:
    """Mimic ``governance_emit.emit_unified_report``'s write path.

    The real governed dispatch ALWAYS writes the report to
    ``<data_dir>/unified_reports/<dispatch_id>.md`` before the dispatcher call
    returns the text; kimi_gate.py never writes this file itself, it only
    reads the text back. This helper reproduces that ordering so the fake
    dispatcher below behaves like the real one instead of a bare stub.
    """
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{dispatch_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _fake_dispatcher_factory(data_dir: Path, text: str):
    """Build a ``_make_default_dispatcher``-shaped double that writes the
    report to disk exactly like the real governed lane, then returns it."""
    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            _write_unified_report(data_dir, dispatch_id, text)
            return text
        return _dispatch
    return _make


def _run_kimi_gate_for_real_pr(tmp_path, monkeypatch, report_text, *, pr="4242"):
    """Run kimi_gate.main() against a non-offline PR: no --diff-file (so
    test_run stays False, matching a real governed run), a stubbed diff
    source (no network), a dispatcher double that writes the unified report
    like the real lane does, and stubbed gh-identity lookups (no real `gh`
    subprocess calls)."""
    data_dir = tmp_path / "data"
    monkeypatch.setattr(kimi_gate, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, report_text),
    )
    monkeypatch.setattr(kimi_gate, "get_pr_head_branch", lambda pr_number: "feature/dlv2-test")
    monkeypatch.setattr(kimi_gate, "get_pr_head_sha", lambda pr_number: "deadbeefcafe")

    rc = kimi_gate.main(["--pr", pr, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    return rc, record, data_dir


# ---------------------------------------------------------------------------
# 1. A real verdict carries complete evidence — has_complete_evidence(record) is True
# ---------------------------------------------------------------------------


def test_pass_verdict_carries_complete_evidence(tmp_path, monkeypatch):
    rc, record, _data_dir = _run_kimi_gate_for_real_pr(tmp_path, monkeypatch, _REAL_PASS_REPORT)

    assert rc == 0
    assert record["status"] == "pass"
    assert record["contract_hash"], "contract_hash must be non-empty on a terminal verdict"
    assert record["report_path"], "report_path must be non-empty on a terminal verdict"
    assert Path(record["report_path"]).is_file(), (
        "report_path must point at a file that already exists on disk when the record is written"
    )
    # The real function, not a reimplementation of its logic.
    assert has_complete_evidence(record) is True


# ---------------------------------------------------------------------------
# 2. That same record clears the merge door's evidence check — no more NO-GO
#    on "resultaat mist contract_hash en/of report_path"
# ---------------------------------------------------------------------------


def test_pass_verdict_clears_closure_verifier_merge_check(tmp_path, monkeypatch):
    rc, record, data_dir = _run_kimi_gate_for_real_pr(tmp_path, monkeypatch, _REAL_PASS_REPORT, pr="4242")
    assert rc == 0

    results_dir = data_dir / "state" / "review_gates" / "results"
    gate = closure_verifier.check_review_gate_for_merge("4242", "kimi_gate", results_dir)

    assert gate["verdict"] == "GO", gate["message"]
    assert "mist contract_hash" not in gate["message"]


# ---------------------------------------------------------------------------
# 3. An unavailable result never counts as complete evidence (OI-1142
#    separation preserved)
# ---------------------------------------------------------------------------


def test_unavailable_result_never_has_complete_evidence(tmp_path, monkeypatch):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"
    # A provider outage: empty report, no readable verdict.
    monkeypatch.setattr(kimi_gate, "_make_default_dispatcher", lambda *a, **k: (lambda *a2, **k2: ""))

    rc = kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["contract_hash"] == "", "contract_hash is the field that must stay empty on an outage"
    # OI-1477: report_path is populated on an unavailable result too, so a
    # takeover-time reader can find the failure text without re-deriving the
    # path itself — it points at the same place report_path_informational
    # always has (the file may not exist here since this test's fake
    # dispatcher never writes one, but the path itself is real).
    assert record["report_path"] == record["report_path_informational"]
    # has_complete_evidence stays False regardless: is_terminal() is checked
    # BEFORE contract_hash/report_path are ever consulted, and is_terminal is
    # False on an unavailable record no matter what those two fields hold.
    assert has_complete_evidence(record) is False, "an outage must never look like a decided verdict"
    assert is_terminal(record) is False, "an outage is retryable, not a decided pass/fail outcome"


# ---------------------------------------------------------------------------
# 4. An offline (--diff-file) run never counts as evidence for a real PR,
#    even though it now carries complete evidence fields
# ---------------------------------------------------------------------------


def test_offline_run_is_test_run_and_rejected_by_merge_check_despite_complete_evidence(
    tmp_path, monkeypatch,
):
    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        kimi_gate, "_make_default_dispatcher", _fake_dispatcher_factory(data_dir, _REAL_PASS_REPORT),
    )

    rc = kimi_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / "pr-0-kimi_gate.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 0
    assert record["status"] == "pass"
    assert record["test_run"] is True
    # The record DOES carry complete evidence fields now...
    assert has_complete_evidence(record) is True
    # ...but the merge door must still refuse it: is_test_run_record is
    # checked before contract_hash/report_path are ever consulted.
    results_dir = data_dir / "state" / "review_gates" / "results"
    gate = closure_verifier.check_review_gate_for_merge("0", "kimi_gate", results_dir)
    assert gate["verdict"] == "NO-GO"
    assert "geen review-gate resultaat" in gate["message"]


# ---------------------------------------------------------------------------
# 5. contract_hash is byte-identical to the existing route's hash for the
#    same prompt — never a second hashing method
# ---------------------------------------------------------------------------


def test_kimi_gate_reuses_the_canonical_hasher_not_a_second_implementation():
    """kimi_gate imports gate_artifacts._compute_contract_hash directly — this
    is an identity check, not a behavioral one: it proves there is exactly
    ONE hashing implementation in the codebase, not two that happen to agree
    today and can silently diverge tomorrow."""
    assert kimi_gate._compute_contract_hash is gate_artifacts._compute_contract_hash


def test_contract_hash_byte_equal_to_existing_route_for_same_contract(tmp_path, monkeypatch):
    pr = "777"
    rc, record, _data_dir = _run_kimi_gate_for_real_pr(tmp_path, monkeypatch, _REAL_PASS_REPORT, pr=pr)
    assert rc == 0

    # Reconstruct the exact prompt kimi_gate built for this run, then hash it
    # via the SAME function the existing (codex_gate) route calls — gate name
    # only affects the fallback branch (no "prompt" key), so a different gate
    # name here still proves it is the same hash for the same contract.
    prompt = kimi_gate._build_prompt(_FAKE_DIFF, pr)
    existing_route_hash = gate_artifacts._compute_contract_hash({"prompt": prompt}, "codex_gate")

    assert record["contract_hash"] != ""
    assert record["contract_hash"] == existing_route_hash
