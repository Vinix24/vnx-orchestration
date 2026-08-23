"""End-to-end recovery-wiring tests for glm_gate.main() / kimi_gate.main()
(dispatch-20260823-beta2-j: "de tweede rapportput").

Covers, for BOTH gate modules:
  - the gate's own ```json``` fence is used directly, no search attempted
    (control case 1)
  - the plan-reviewer role's ```vnx-plan-verdict``` fence is recognized and
    translated INLINE (no search), per word (pass/revise/block), with status
    and mapped verdict value asserted separately
  - the bounded companion search: zero candidates (control case 2), two+
    candidates (control case 3, fail-closed), one candidate that conflicts
    with the primary response (control case 4)
  - --reprocess: recovers from on-disk evidence with NO model call, refuses
    on a stale commit_sha (control case 5), refuses when no addressable
    original identity exists

``_make_default_dispatcher``/``_get_diff``/``get_pr_head_branch``/
``get_pr_head_sha`` are patched on each gate MODULE namespace, not on their
source modules — matches the convention in test_dlv2_kimi_gate_evidence.py.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pytest

import glm_gate
import kimi_gate

_GATES = [
    pytest.param(glm_gate, "glm_gate", "glm-gate", id="glm"),
    pytest.param(kimi_gate, "kimi_gate", "kimi-gate", id="kimi"),
]

_FAKE_DIFF = "diff --git a/x b/x\n+ok\n"

_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

# Verbatim from ~/.vnx-data/vnx-dev/unified_reports/glm-gate-pr1677-1787477675.md
# — the real run where glm answered inline, correctly, but under the
# plan-reviewer role's fence label instead of the gate's own.
REAL_RELABELED_REPORT = (
    "## Residual risk\n\n"
    "The honest-retirement model is sound.\n\n"
    "```vnx-plan-verdict\n"
    "{\n"
    '  "verdict": "pass",\n'
    '  "blocking_findings": [],\n'
    '  "rationale": "The retired-status design is sound and verified against the real API."\n'
    "}\n"
    "```\n"
)

_NO_FENCE_REPORT = "## Findings\n\nNone that I could formalize inline.\n"


def _gate_json_name(gate_key: str) -> str:
    return gate_key


def _write(data_dir: Path, dispatch_id: str, text: str) -> Path:
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{dispatch_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _touch(path: Path, mtime: float) -> None:
    import os
    os.utime(path, (mtime, mtime))


def _fake_dispatcher_factory(data_dir: Path, primary_text: str, *, companions=None):
    """Build a ``_make_default_dispatcher``-shaped double. Writes the
    companion file(s) FIRST (matching the measured real ordering: the
    companion lands 7-15s BEFORE the gate's own report), then the primary
    report — both inside the SAME call as the real governed lane, so both
    mtimes fall naturally inside the caller's recovery window."""
    companions = companions or []

    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            for name, text in companions:
                _write(data_dir, name.replace(".md", ""), text)
            _write(data_dir, dispatch_id, primary_text)
            return primary_text
        return _dispatch
    return _make


def _run(module, gate_name, tmp_path, monkeypatch, dispatcher_factory, *, pr="777"):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(module, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(module, "_make_default_dispatcher", dispatcher_factory(data_dir))
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "deadbeefcafe")
    rc = module.main(["--pr", pr, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-{pr}-{gate_name}.json"
    record = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, record, data_dir


# ---------------------------------------------------------------------------
# Control case 1: direct ```json``` fence is used, search never attempted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_direct_fence_used_without_search(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("find_recovery_candidate must not be called when the direct fence parses")

    monkeypatch.setattr(module, "find_recovery_candidate", _boom)
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, _REAL_PASS_REPORT),
    )
    assert rc == 0
    assert record["status"] == "pass"
    assert record["reason"] == "verdict"


# ---------------------------------------------------------------------------
# Relabeled inline fence — no search needed, per-word mapping (T0 point 4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_relabeled_inline_fence_recovers_without_search(
    module, gate_name, dispatch_prefix, tmp_path, monkeypatch,
):
    """Real evidence: glm-gate-pr1677-1787477675.md — glm answered inline, at
    the end of its response, under ```vnx-plan-verdict``` instead of
    ```json```. Must be recognized WITHOUT any bounded search."""
    def _boom(*a, **k):
        raise AssertionError("find_recovery_candidate must not be called for an inline relabeled fence")

    monkeypatch.setattr(module, "find_recovery_candidate", _boom)
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, REAL_RELABELED_REPORT),
    )
    assert record["reason"] == "relabeled_verdict"
    assert record["status"] == "pass"
    assert rc == 0
    assert record["contract_hash"] != ""
    assert record["report_path"] != ""


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_relabeled_word_pass_maps_to_pass(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    text = '```vnx-plan-verdict\n{"verdict": "pass", "blocking_findings": [], "rationale": "ok"}\n```\n'
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, text),
    )
    assert record["status"] == "pass"
    assert record["reason"] == "relabeled_verdict"
    assert rc == 0


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_relabeled_word_block_maps_to_fail_status_with_blocking_findings(
    module, gate_name, dispatch_prefix, tmp_path, monkeypatch,
):
    text = (
        '```vnx-plan-verdict\n{"verdict": "block", "blocking_findings": ["unsafe migration"], '
        '"rationale": "no rollback"}\n```\n'
    )
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, text),
    )
    # separate asserts: status, then the mapped verdict word is never surfaced
    # as anything but "fail" for this gate's own status vocabulary
    assert record["status"] == "fail"
    assert record["reason"] == "relabeled_verdict"
    assert rc == 2
    assert len(record["blocking_findings"]) == 1
    assert record["blocking_findings"][0]["message"] == "unsafe migration"


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_relabeled_word_revise_maps_to_fail_never_pass(
    module, gate_name, dispatch_prefix, tmp_path, monkeypatch,
):
    """T0 point 3/4: "revise" ("real, fixable gaps remain") must never
    silently resolve to pass. The review-gate has no middle verdict, so it
    maps to the conservative side: fail."""
    text = (
        '```vnx-plan-verdict\n{"verdict": "revise", "blocking_findings": ["missing test"], '
        '"rationale": "gaps remain"}\n```\n'
    )
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, text),
    )
    assert record["status"] == "fail"
    assert record["status"] != "pass"
    assert rc == 2


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_block_word_never_maps_to_a_passing_status(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    """The costliest possible regression (T0): a reviewer that wants to STOP
    a PR writes "block", and that verdict must never come out the other end
    as pass. Even with zero blocking_findings listed, the verdict WORD alone
    must keep this off "pass"."""
    text = '```vnx-plan-verdict\n{"verdict": "block", "blocking_findings": [], "rationale": "unsafe"}\n```\n'
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, text),
    )
    assert record["status"] != "pass"
    assert record["status"] == "fail"


# ---------------------------------------------------------------------------
# Control case 2: zero candidates -> unavailable, reason="recovery_empty"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_zero_candidates_is_unavailable_recovery_empty(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(data_dir, _NO_FENCE_REPORT),
    )
    assert record["status"] == "unavailable"
    assert record["reason"] == "recovery_empty"
    assert rc == 1
    assert record["contract_hash"] == ""
    assert record["report_path"] == ""


# ---------------------------------------------------------------------------
# Search recovers a verdict from a real companion file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_search_recovers_verdict_from_companion(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    rc, record, data_dir = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(
            data_dir, _NO_FENCE_REPORT,
            companions=[("pr-777-second-pit.md", _REAL_PASS_REPORT)],
        ),
    )
    assert record["status"] == "pass"
    assert record["reason"] == "recovered_verdict"
    assert rc == 0
    assert record["contract_hash"] != ""
    assert "second-pit" in record["report_path"]
    assert Path(record["report_path"]).is_file()


# ---------------------------------------------------------------------------
# Control case 3: two+ candidates -> fail-closed, reason="recovery_ambiguous"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_two_companions_is_unavailable_recovery_ambiguous(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(
            data_dir, _NO_FENCE_REPORT,
            companions=[
                ("pr-777-second-pit-a.md", _REAL_PASS_REPORT),
                ("pr-777-second-pit-b.md", _REAL_PASS_REPORT),
            ],
        ),
    )
    assert record["status"] == "unavailable"
    assert record["reason"] == "recovery_ambiguous"
    assert rc == 1
    assert "second-pit-a" in record["residual_risk"]
    assert "second-pit-b" in record["residual_risk"]


# ---------------------------------------------------------------------------
# Control case 4: recovered verdict contradicts the primary response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_conflicting_companion_abstains_recovery_conflict(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    # Primary response is malformed/truncated JSON, but loosely mentions a
    # "fail" verdict word — the companion's clean "pass" verdict contradicts it.
    primary = 'Partial dump: "verdict": "fail", cut off here, no valid fence.\n'
    rc, record, _ = _run(
        module, gate_name, tmp_path, monkeypatch,
        lambda data_dir: _fake_dispatcher_factory(
            data_dir, primary,
            companions=[("pr-777-second-pit.md", _REAL_PASS_REPORT)],
        ),
    )
    assert record["status"] == "unavailable"
    assert record["reason"] == "recovery_conflict"
    assert rc == 1
    assert record["contract_hash"] == ""


# ---------------------------------------------------------------------------
# --reprocess: no model call, recovers from on-disk evidence
# ---------------------------------------------------------------------------


def _seed_reprocess_fixture(module, gate_name, data_dir, dispatch_id, *, pr, report_text, commit_sha) -> Path:
    """Write the primary report + the ORIGINAL pr-level result record (as a
    live run would have left them) so --reprocess has real on-disk state to
    read, exactly mirroring the real dispatch layout. Returns the primary
    report's path."""
    primary_path = _write(data_dir, dispatch_id, report_text)
    results_dir = data_dir / "state" / "review_gates" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    original_record = {
        "gate": gate_name,
        "pr_id": str(pr),
        "pr_number": int(pr),
        "test_run": False,
        "status": "unavailable",
        "reason": "recovery_empty",
        "dispatch_id": dispatch_id,
        "commit_sha": commit_sha,
        "branch": "feature/x",
        "contract_hash": "",
        "report_path": "",
    }
    (results_dir / f"pr-{pr}-{gate_name}.json").write_text(
        json.dumps(original_record), encoding="utf-8",
    )
    return primary_path


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_recovers_with_no_model_call(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    dispatch_id = f"{dispatch_prefix}-pr777-1700000000"
    _seed_reprocess_fixture(
        module, gate_name, data_dir, dispatch_id,
        pr="777", report_text=_REAL_PASS_REPORT, commit_sha="deadbeefcafe",
    )
    # No diff fetch, no dispatcher call — either one firing is a bug.
    monkeypatch.setattr(module, "_get_diff", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no diff fetch in --reprocess mode")
    ))
    monkeypatch.setattr(module, "_make_default_dispatcher", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("no dispatcher call in --reprocess mode")
    ))
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "deadbeefcafe")

    rc = module.main(["--pr", "777", "--reprocess", dispatch_id, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-777-{gate_name}.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 0
    assert record["status"] == "pass"
    assert record["reason"] == "verdict"
    assert record["evidence_source"] == "reprocessed"
    assert record["contract_hash"] != ""
    assert record["report_path"] != ""
    assert record["commit_sha"] == "deadbeefcafe"


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_recovers_relabeled_fence_with_no_model_call(
    module, gate_name, dispatch_prefix, tmp_path, monkeypatch,
):
    """The PR1677-shaped case: nothing to search for, just a wrong label
    sitting in the report the original run already wrote."""
    data_dir = tmp_path / "data"
    dispatch_id = f"{dispatch_prefix}-pr1677-1787477675"
    _seed_reprocess_fixture(
        module, gate_name, data_dir, dispatch_id,
        pr="1677", report_text=REAL_RELABELED_REPORT, commit_sha="1690a2af",
    )
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "1690a2af")

    rc = module.main(["--pr", "1677", "--reprocess", dispatch_id, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-1677-{gate_name}.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 0
    assert record["status"] == "pass"
    assert record["reason"] == "relabeled_verdict"
    assert record["evidence_source"] == "reprocessed"


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_recovers_from_companion_with_no_model_call(
    module, gate_name, dispatch_prefix, tmp_path, monkeypatch,
):
    data_dir = tmp_path / "data"
    window_start = 1787475223
    dispatch_id = f"{dispatch_prefix}-pr1674-{window_start}"
    # Companion must land INSIDE [window_start, primary's own mtime] — same
    # ordering the real measured pairs show (companion written 7-15s BEFORE
    # the gate's own report). Stamp both explicitly so this holds regardless
    # of how much wall-clock time the test itself takes to run.
    companion_path = _write(data_dir, "pr-1674-second-pit", _REAL_PASS_REPORT)
    _touch(companion_path, window_start + 10)
    primary_path = _seed_reprocess_fixture(
        module, gate_name, data_dir, dispatch_id,
        pr="1674", report_text=_NO_FENCE_REPORT, commit_sha="58d5ee9f",
    )
    _touch(primary_path, window_start + 20)
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "58d5ee9f")

    rc = module.main(["--pr", "1674", "--reprocess", dispatch_id, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-1674-{gate_name}.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 0
    assert record["status"] == "pass"
    assert record["reason"] == "recovered_verdict"
    assert record["evidence_source"] == "reprocessed"
    assert "second-pit" in record["report_path"]


# ---------------------------------------------------------------------------
# Control case 5: stale commit_sha at reprocessing -> refuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_stale_commit_sha_refuses(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    dispatch_id = f"{dispatch_prefix}-pr777-1700000000"
    _seed_reprocess_fixture(
        module, gate_name, data_dir, dispatch_id,
        pr="777", report_text=_REAL_PASS_REPORT, commit_sha="oldsha0000",
    )
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/x")
    # PR head has moved since the original run.
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "newsha1111")

    rc = module.main(["--pr", "777", "--reprocess", dispatch_id, "--data-dir", str(data_dir)])
    out = data_dir / "state" / "review_gates" / "results" / f"pr-777-{gate_name}.json"
    record = json.loads(out.read_text(encoding="utf-8"))

    assert rc == 1
    assert record["status"] == "unavailable"
    assert record["reason"] == "reprocess_stale_evidence"
    assert record["contract_hash"] == ""
    assert record["report_path"] == ""
    assert "oldsha0000" in record["residual_risk"]
    assert "newsha1111" in record["residual_risk"]


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_no_identity_anchor_refuses_when_result_slot_superseded(
    module, gate_name, dispatch_prefix, tmp_path, monkeypatch,
):
    """A LATER dispatch for the same PR overwrote pr-<N>-<gate>.json before
    this EARLIER dispatch_id was ever reprocessed — there is no addressable
    historical commit_sha for THIS exact dispatch_id left on disk."""
    data_dir = tmp_path / "data"
    earlier_id = f"{dispatch_prefix}-pr777-1700000000"
    later_id = f"{dispatch_prefix}-pr777-1700000999"
    _write(data_dir, earlier_id, _REAL_PASS_REPORT)
    results_dir = data_dir / "state" / "review_gates" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"pr-777-{gate_name}.json").write_text(
        json.dumps({
            "gate": gate_name, "pr_id": "777", "dispatch_id": later_id,
            "commit_sha": "somesha", "status": "pass",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "get_pr_head_branch", lambda pr_number: "feature/x")
    monkeypatch.setattr(module, "get_pr_head_sha", lambda pr_number: "somesha")

    rc = module.main(["--pr", "777", "--reprocess", earlier_id, "--data-dir", str(data_dir)])

    assert rc == 1
    out = results_dir / f"pr-777-{gate_name}.json"
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "unavailable"
    assert record["reason"] == "reprocess_no_identity_anchor"


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_missing_report_file_errors_loudly(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    rc = module.main([
        "--pr", "777", "--reprocess", f"{dispatch_prefix}-pr777-1700000000",
        "--data-dir", str(data_dir),
    ])
    assert rc == 1
    out = data_dir / "state" / "review_gates" / "results" / f"pr-777-{gate_name}.json"
    assert not out.exists()


@pytest.mark.parametrize("module,gate_name,dispatch_prefix", _GATES)
def test_reprocess_malformed_dispatch_id_refuses(module, gate_name, dispatch_prefix, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    bad_id = "not-the-expected-shape"
    _write(data_dir, bad_id, _REAL_PASS_REPORT)
    rc = module.main([
        "--pr", "777", "--reprocess", bad_id, "--data-dir", str(data_dir),
    ])
    assert rc == 1
