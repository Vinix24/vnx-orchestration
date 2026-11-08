"""required_contexts_gate on the door context (ADR-061 §6, track deur-afgedwongen D6).

Drives ``check_required_contexts`` -> ``ci_contexts.evaluate_commit`` -> the
real classifier, with only the ``gh`` subprocess replaced by fixture payloads
per endpoint. No live GitHub call. The CI-context verdicts are covered in
tests/test_pre_merge_gate_required_contexts.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts"):
    sys.path.insert(0, str(_p))

import ci_contexts
import pre_merge_gate
import required_contexts_gate
from required_contexts_gate import SKIPPED_UNVERIFIED, check_required_contexts

HEAD, DOOR, PROFILE = "d" * 40, "vnx-gate/door", "Profile A (doctor + core tests)"
DOOR_REQ = {"context": DOOR, "app_id": 4869217}


def _run(name=DOOR, conclusion="success", run_id=7, app=4869217):
    return {"id": run_id, "name": name, "status": "completed", "conclusion": conclusion, "app": {"id": app}}


def _serve(monkeypatch, runs, door_req):
    """Answer every gh endpoint evaluate_commit reads; any other call fails the test."""
    required = [{"context": PROFILE, "app_id": 15368}] + ([door_req] if door_req else [])

    def fake(args, project_root, timeout):
        endpoint = args[1]
        if endpoint.endswith("/branches/main/protection"):
            return {"required_status_checks": {"contexts": [], "checks": required}}
        if f"/commits/{HEAD}/check-runs" in endpoint:
            return [{"check_runs": runs}]
        if f"/commits/{HEAD}/status" in endpoint:
            return {"statuses": []}
        if f"actions/runs?head_sha={HEAD}" in endpoint:
            return {"workflow_runs": []}
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(ci_contexts, "_gh_json", fake)


def test_pre_merge_gate_inherits_the_gate_unchanged():
    assert pre_merge_gate.check_required_contexts is required_contexts_gate.check_required_contexts


_PROFILE_OK = _run(PROFILE, run_id=100, app=15368)
_SCENARIOS = [
    ("a-no-run", [_PROFILE_OK], DOOR_REQ, "GO", [DOOR], {}, "1 of 2 required contexts passed"),
    ("b-revoked", [_PROFILE_OK, _run(conclusion="failure")], DOOR_REQ, "HOLD", [],
     {DOOR: "python3 scripts/pr_merge.py --pr N"}, "[permit_revoked]"),
    ("c-repermitted", [_PROFILE_OK, _run()], DOOR_REQ, "GO", [], {}, "all 2 required contexts passed"),
    ("d-two-door-runs", [_PROFILE_OK, _run(conclusion="failure"), _run(run_id=8)], DOOR_REQ, "HOLD", [],
     {DOOR: "push een nieuwe commit"}, "nieuwe commit"),
    ("e-foreign-success", [_PROFILE_OK, _run(app=15368)], DOOR_REQ, SKIPPED_UNVERIFIED, [], {}, DOOR),
    ("e-foreign-failure", [_PROFILE_OK, _run(conclusion="failure", app=15368)], DOOR_REQ, SKIPPED_UNVERIFIED, [], {}, DOOR),
    ("f-misspelled", [_PROFILE_OK], {"context": "vnx-gate/dor", "app_id": 4869217}, SKIPPED_UNVERIFIED, [], {}, "vnx-gate/dor"),
    ("f-any-app-sentinel", [_PROFILE_OK], {"context": DOOR, "app_id": -1}, SKIPPED_UNVERIFIED, [], {}, DOOR),
    ("g-not-required", [_PROFILE_OK, _run(conclusion="failure")], None, "GO", [], {}, "all 1 required contexts passed"),
    ("ci-failed-beside-door", [_run(PROFILE, conclusion="failure", run_id=100, app=15368)], DOOR_REQ, "HOLD",
     [DOOR], {}, "1 of 2 required contexts block the merge"),
]


@pytest.mark.parametrize(
    "runs,door_req,status,published,recovery,fragment", [s[1:] for s in _SCENARIOS], ids=[s[0] for s in _SCENARIOS]
)
def test_the_gate_verdict_per_door_state(monkeypatch, tmp_path, runs, door_req, status, published, recovery, fragment):
    _serve(monkeypatch, runs, door_req)
    result = check_required_contexts(tmp_path, head_sha=HEAD)
    assert result["status"] == status
    assert [c["context"] for c in result["published_at_merge"]] == published
    assert result["summary"]["published_at_merge"] == len(published)
    assert result["summary"]["unverified"] == (1 if status == SKIPPED_UNVERIFIED else 0)
    assert result["recovery"] == recovery
    assert fragment in result["detail"]
    for command in recovery.values():
        assert f"herstel: {command}" in result["detail"]
    if published and status == "GO":
        assert "de deur publiceert deze bij de merge" in result["detail"]
