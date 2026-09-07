"""Golf B / B8: takeover-velden op het result-record, volgorde-onafhankelijk.

``gate_request_handler._stamp_takeover_annotations`` (:665-734) stamps the
five takeover-provenance fields onto BOTH the request record and, separately,
the result record — but the result-side write only fires ``if
result_file.exists()`` at stamp time (:711). A successor gate
(``glm_gate.py``/``kimi_gate.py``) that writes its OWN terminal result AFTER
that stamp ran (the normal case: the stamp lands on a ``request_reviews()``
poll while the successor's own governed dispatch call is still running)
never inherited the annotation — even though ``stamp_request_identity``
(``gate_recorder.py:252-``) is the single place every production result
writer reaches disk through, and even though
``closure_verifier._find_takeover_successor_results`` (:616-697) reads
ONLY results, never requests.

Measured 2026-09-07 on the vnx-dev store: 33 request records carry
``takeover: true``; of 53 not_executable results since 2026-09-01, only 2
carry the fields (both ``deepseek_gate`` stubs the handler writes
synchronously itself) — zero ``glm_gate``/``kimi_gate`` results.
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

import closure_verifier
from gate_recorder import record_not_executable, stamp_request_identity

_TAKEOVER_PATH = [
    {"gate": "codex_gate", "reason": "lane_exhausted", "detail": "quota", "status": "unavailable"},
    {"gate": "kimi_gate", "reason": "lane_exhausted", "detail": "quota", "status": "unavailable"},
]

_TAKEOVER_REQUEST = {
    "gate": "glm_gate",
    "status": "requested",
    "branch": "dispatch/x",
    "pr_number": 1803,
    "commit_sha": "deadbeef",
    "takeover": True,
    "takeover_from": "kimi_gate",
    "takeover_reason": "lane_exhausted",
    "takeover_source_status": "unavailable",
    "takeover_path": _TAKEOVER_PATH,
    "failure_reason": "codex_gate unavailable -- kimi_gate unavailable -- glm_gate substituted as reader",
}

_TAKEOVER_FIELDS = (
    "takeover", "takeover_from", "takeover_reason", "takeover_source_status", "takeover_path",
)


# ---------------------------------------------------------------------------
# 1. stamp_request_identity itself
# ---------------------------------------------------------------------------


def test_stamp_request_identity_copies_takeover_fields_when_request_has_takeover():
    result = {"gate": "glm_gate", "pr_id": "1803", "status": "pass"}
    stamp_request_identity(result, dict(_TAKEOVER_REQUEST))

    assert result["takeover"] is True
    assert result["takeover_from"] == "kimi_gate"
    assert result["takeover_reason"] == "lane_exhausted"
    assert result["takeover_source_status"] == "unavailable"
    assert result["takeover_path"] == _TAKEOVER_PATH


def test_stamp_request_identity_omits_takeover_fields_when_request_has_none():
    request = {"gate": "glm_gate", "pr_id": "1803", "branch": "x", "commit_sha": "y"}
    result = {"gate": "glm_gate", "pr_id": "1803", "status": "pass"}
    stamp_request_identity(result, request)

    for field in _TAKEOVER_FIELDS:
        assert field not in result, f"{field} must be ABSENT, not merely falsy"


def test_stamp_request_identity_never_stamps_literal_takeover_false():
    """A request that explicitly carries ``takeover: false`` must still leave
    the key absent on the result -- ``takeover`` present-and-false is not the
    same signal as ``takeover`` absent, and the reader only ever checks
    ``is True``, but a writer that stamped ``False`` would still be lying
    about having considered the question."""
    request = {"gate": "glm_gate", "pr_id": "1803", "branch": "x", "commit_sha": "y", "takeover": False}
    result = {"gate": "glm_gate", "pr_id": "1803", "status": "pass"}
    stamp_request_identity(result, request)

    assert "takeover" not in result


def test_stamp_request_identity_never_copies_failure_reason():
    result = {"gate": "glm_gate", "pr_id": "1803", "status": "pass"}
    stamp_request_identity(result, dict(_TAKEOVER_REQUEST))

    assert "failure_reason" not in result, (
        "failure_reason is the PREDECESSOR's cause, not this result's own"
    )


def test_stamp_request_identity_still_stamps_branch_and_commit_sha():
    """The B8 addition must not regress the pre-existing OI-1307 behaviour."""
    result = {"gate": "glm_gate", "pr_id": "1803", "status": "pass"}
    stamp_request_identity(result, dict(_TAKEOVER_REQUEST))

    assert result["branch"] == "dispatch/x"
    assert result["commit_sha"] == "deadbeef"


# ---------------------------------------------------------------------------
# 2. record_not_executable (already routes through stamp_request_identity)
# ---------------------------------------------------------------------------


def test_record_not_executable_with_takeover_request_carries_takeover_path(tmp_path):
    requests_dir = tmp_path / "requests"
    results_dir = tmp_path / "results"
    state_dir = tmp_path / "state"
    requests_dir.mkdir(parents=True)
    results_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)

    result = record_not_executable(
        gate="deepseek_gate",
        pr_number=1803,
        pr_id="",
        reason="gate_runner_missing",
        reason_detail="scripts/deepseek_gate.py does not exist yet",
        request_payload=dict(_TAKEOVER_REQUEST, gate="deepseek_gate"),
        requests_dir=requests_dir,
        results_dir=results_dir,
        state_dir=state_dir,
    )

    assert result["takeover"] is True
    assert result["takeover_path"] == _TAKEOVER_PATH

    on_disk = json.loads((results_dir / "pr-1803-deepseek_gate.json").read_text(encoding="utf-8"))
    assert on_disk["takeover"] is True
    assert on_disk["takeover_path"] == _TAKEOVER_PATH


# ---------------------------------------------------------------------------
# 3. glm_gate.py / kimi_gate.py end-to-end: the request file is annotated
#    with takeover fields WHILE the model call is "in flight" (simulating a
#    request_reviews() poll landing mid-dispatch, per the measured ordering)
#    -- only the model call is mocked, never the recorder or the filesystem.
# ---------------------------------------------------------------------------

_REAL_PASS_REPORT = (
    "Reviewed the diff, no issues.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

_FAKE_DIFF = "diff --git a/x b/x\n+ok\n"


def _write_unified_report(data_dir: Path, dispatch_id: str, text: str) -> Path:
    reports_dir = data_dir / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{dispatch_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _late_takeover_dispatcher_factory(data_dir: Path, text: str, requests_dir: Path, pr: str, gate: str):
    """Build a dispatcher double that, as a side effect of the model call,
    annotates the SAME request file gate_request_handler
    ._stamp_takeover_annotations would have -- landing strictly AFTER this
    gate's own request write (main()'s top) and strictly BEFORE this gate
    reads the request record back for its own result stamp, exactly the
    ordering measured in production."""
    def _make(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            req_path = requests_dir / f"pr-{pr}-{gate}.json"
            existing = json.loads(req_path.read_text(encoding="utf-8"))
            existing.update({
                "takeover": True,
                "takeover_from": "kimi_gate" if gate == "glm_gate" else "codex_gate",
                "takeover_reason": "lane_exhausted",
                "takeover_source_status": "unavailable",
                "takeover_path": _TAKEOVER_PATH,
                "failure_reason": "kimi_gate unavailable -- glm_gate substituted as reader",
            })
            req_path.write_text(json.dumps(existing), encoding="utf-8")
            _write_unified_report(data_dir, dispatch_id, text)
            return text
        return _dispatch
    return _make


@pytest.mark.parametrize("gate_module_name,gate", [("glm_gate", "glm_gate"), ("kimi_gate", "kimi_gate")])
def test_gate_result_inherits_late_arriving_takeover_annotation(tmp_path, monkeypatch, gate_module_name, gate):
    import importlib
    gate_module = importlib.import_module(gate_module_name)

    pr = "1803"
    data_dir = tmp_path / "data"

    monkeypatch.setattr(gate_module, "_get_diff", lambda pr_arg, diff_file: _FAKE_DIFF)
    monkeypatch.setattr(gate_module, "get_pr_head_branch", lambda pr_number: "dispatch/x")
    monkeypatch.setattr(gate_module, "get_pr_head_sha", lambda pr_number: "deadbeef")

    requests_dir = data_dir / "state" / "review_gates" / "requests"
    monkeypatch.setattr(
        gate_module, "_make_default_dispatcher",
        _late_takeover_dispatcher_factory(data_dir, _REAL_PASS_REPORT, requests_dir, pr, gate),
    )

    rc = gate_module.main(["--pr", pr, "--data-dir", str(data_dir)])
    assert rc == 0

    results_dir = data_dir / "state" / "review_gates" / "results"
    result = json.loads((results_dir / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))

    assert result["status"] == "pass"
    assert result["takeover"] is True
    assert result["takeover_path"] == _TAKEOVER_PATH
    assert result["takeover_source_status"] == "unavailable"
    # failure_reason must stay the RESULT's own (pass -> empty), never the
    # predecessor's cause copied over from the request.
    assert result.get("failure_reason", "") == ""

    request_on_disk = json.loads((requests_dir / f"pr-{pr}-{gate}.json").read_text(encoding="utf-8"))
    assert request_on_disk["takeover"] is True

    # 4. closure_verifier._find_takeover_successor_results (OI-1576) finds
    #    this result as a successor for the ORIGINAL declared gate
    #    (codex_gate), with NO change to the reader itself.
    candidates, notes = closure_verifier._find_takeover_successor_results(
        "codex_gate", pr, results_dir,
    )
    assert notes == []
    assert len(candidates) == 1
    assert candidates[0]["gate"] == gate
    assert candidates[0]["status"] == "pass"


def test_offline_run_with_no_request_file_keeps_pre_b8_behaviour(tmp_path, monkeypatch):
    """--diff-file offline runs still write a request record for THEIR OWN
    pr_number (persist_request always fires), so this exercises the other
    fallback branch: a request file that legitimately carries no takeover
    fields at all -- the pre-B8 minimal-dict path must produce the exact same
    outcome as before (no takeover key on the result, branch/commit_sha still
    stamped empty for an offline run)."""
    import glm_gate

    diff_file = tmp_path / "x.diff"
    diff_file.write_text(_FAKE_DIFF, encoding="utf-8")
    data_dir = tmp_path / "data"

    def _dispatcher_factory(*_a, **_k):
        def _dispatch(provider, model_arg, instruction, dispatch_id):
            _write_unified_report(data_dir, dispatch_id, _REAL_PASS_REPORT)
            return _REAL_PASS_REPORT
        return _dispatch

    monkeypatch.setattr(glm_gate, "_make_default_dispatcher", _dispatcher_factory)

    rc = glm_gate.main(["--pr", "0", "--diff-file", str(diff_file), "--data-dir", str(data_dir)])
    assert rc == 0

    out = data_dir / "state" / "review_gates" / "results" / "pr-0-glm_gate.json"
    result = json.loads(out.read_text(encoding="utf-8"))

    for field in _TAKEOVER_FIELDS:
        assert field not in result
    assert result["branch"] == ""
    assert result["commit_sha"] == ""
