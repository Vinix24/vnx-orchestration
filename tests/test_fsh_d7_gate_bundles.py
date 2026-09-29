"""D7 fabric-state-herstel: gate bundles leave ``dispatches/pending/``.

Measured on 28-09: 3.368 directories in one store's ``pending/``, nearly all of
them gate bundles holding only ``final_prompt.md`` (glm 844, kimi 118,
deepseek 22, plan-gate-harness). provider_dispatch writes them, gate_artifacts
reads the prompt sha back from them, dispatch_cleanup skipped exactly this
shape, and nothing moved them on.

Three behaviours are pinned here:

1. A finished gate run moves its own bundle to ``completed/`` (a verdict) or
   ``failed/`` (no verdict). Moved, never deleted.
2. gate_artifacts still recovers the prompt sha after the move.
3. dispatch_cleanup moves a leftover final-prompt-only bundle only on proof: a
   gate result or ``review_gate_result`` receipt for the same dispatch-id with
   the same prompt sha. Without proof the bundle stays and reads ``unproven``.

Every store is a tmp dir. The isolation tests seed a second project store with
a colliding dispatch-id and sha that must not leak into the first (ADR-007
shape: the cleanup reads only the store it was pointed at).
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import dispatch_cleanup as dc
import gate_depth
import gate_recorder
from final_prompt_integrity import final_prompt_sha_for_dispatch

PROMPT = "You are reviewing PR #1974.\n\nDiff:\n- one line\n"
PROMPT_SHA = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
KIMI_ID = "kimi-gate-pr1974-1790000000"
GLM_ID = "glm-gate-pr1974-1790000001"
_OK_DEPTH = gate_depth.single_shot_depth(50000, False)

PASS_STDOUT = (
    "## Review\n\nNo blocking findings. Two advisories follow.\n"
    "1. scripts/x.py:4 — the loop re-reads config each pass.\n"
    "2. tests/test_x.py:9 — the fixture builds its own tmp dir.\n"
    "Residual risk: reviewed against main.\n\n"
    "```json\n"
    '{\n  "verdict": "pass",\n  "findings": [],\n  "residual_risk": "reviewed against main"\n}\n'
    "```\n"
)


def _store(root: Path):
    data_dir = root / "data"
    state_dir = data_dir / "state"
    results = state_dir / "review_gates" / "results"
    requests = state_dir / "review_gates" / "requests"
    reports = data_dir / "unified_reports"
    for d in (results, requests, reports, data_dir / "dispatches" / "pending"):
        d.mkdir(parents=True, exist_ok=True)
    return data_dir, state_dir, requests, results, reports


@pytest.fixture
def store(tmp_path):
    return _store(tmp_path / "project-a")


@pytest.fixture
def other_store(tmp_path):
    return _store(tmp_path / "project-b")


def _seed_gate_bundle(data_dir: Path, dispatch_id: str, text: str = PROMPT, state: str = "pending") -> Path:
    bundle = data_dir / "dispatches" / state / dispatch_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "final_prompt.md").write_text(text, encoding="utf-8")
    return bundle


def _seed_result(results: Path, name: str, **fields) -> None:
    (results / name).write_text(json.dumps(fields), encoding="utf-8")


def _seed_receipt(state_dir: Path, **fields) -> None:
    with open(state_dir / "t0_receipts.ndjson", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(fields) + "\n")


def _pending(data_dir: Path, dispatch_id: str) -> Path:
    return data_dir / "dispatches" / "pending" / dispatch_id


def _materialize_pass(requests, results, reports, dispatch_id=KIMI_ID):
    from gate_artifacts import materialize_artifacts

    return materialize_artifacts(
        gate="kimi_gate", pr_number=1974, pr_id="",
        stdout=PASS_STDOUT,
        request_payload={
            "gate": "kimi_gate", "pr_id": "1974", "pr_number": 1974,
            "branch": "fix/x", "commit_sha": "b" * 40,
            "report_path": str(reports / "kimi-1974.md"),
            "contract_hash": "183bed973031720a",
            "dispatch_id": dispatch_id,
        },
        duration_seconds=60.0,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )


# ---------------------------------------------------------------------------
# 1. A finished gate run moves its own bundle
# ---------------------------------------------------------------------------


def test_a_finished_gate_leaves_no_bundle_in_pending(store):
    data_dir, _state, requests, results, reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)

    _materialize_pass(requests, results, reports)

    assert not _pending(data_dir, KIMI_ID).exists(), (
        "a finished gate run left its bundle in dispatches/pending/"
    )
    moved = data_dir / "dispatches" / "completed" / KIMI_ID / "final_prompt.md"
    assert moved.read_text(encoding="utf-8") == PROMPT, "the bundle must be moved, not deleted"


def test_gate_artifacts_still_finds_the_prompt_sha_after_the_move(store):
    data_dir, _state, requests, results, reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)

    _materialize_pass(requests, results, reports)

    record = json.loads((results / "pr-1974-kimi_gate.json").read_text(encoding="utf-8"))
    assert record.get("final_prompt_sha256") == PROMPT_SHA
    assert final_prompt_sha_for_dispatch(KIMI_ID, data_dir) == PROMPT_SHA


def test_a_run_without_a_verdict_moves_its_bundle_to_failed(store):
    data_dir, _state, requests, results, _reports = store
    _seed_gate_bundle(data_dir, GLM_ID)

    gate_recorder.record_failure(
        gate="glm_gate", pr_number=1974, pr_id="",
        result={
            "reason": "harness_lane_dispatch_error",
            "reason_detail": "provider refused",
            "duration_seconds": 1.0,
            "partial_output_lines": 0,
            "runner_pid": 1,
        },
        request_payload={"gate": "glm_gate", "pr_number": 1974, "dispatch_id": GLM_ID},
        requests_dir=requests, results_dir=results,
    )

    assert not _pending(data_dir, GLM_ID).exists()
    assert (data_dir / "dispatches" / "failed" / GLM_ID / "final_prompt.md").is_file()


def test_the_standalone_terminal_write_moves_its_bundle_too(store):
    """glm_gate.py/kimi_gate.py book through record_terminal_result, not materialize_artifacts."""
    data_dir, _state, _requests, results, _reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)

    gate_recorder.record_terminal_result(
        gate="kimi_gate", pr_id="1974",
        result_path=results / "pr-1974-kimi_gate.json",
        payload={
            "gate": "kimi_gate", "pr_id": "1974", "status": "pass",
            "contract_hash": "abc", "report_path": "/tmp/r.md",
            "dispatch_id": KIMI_ID,
        },
        execution_depth=_OK_DEPTH,
    )

    assert not _pending(data_dir, KIMI_ID).exists()
    assert (data_dir / "dispatches" / "completed" / KIMI_ID / "final_prompt.md").is_file()


def test_a_refused_write_still_ends_the_run_and_moves_the_bundle(store):
    """The overwrite guard keeps the older decided record; the run is still over."""
    data_dir, _state, requests, results, reports = store
    _materialize_pass(requests, results, reports, dispatch_id="kimi-gate-pr1974-1780000000")
    _seed_gate_bundle(data_dir, KIMI_ID)

    gate_recorder.record_failure(
        gate="kimi_gate", pr_number=1974, pr_id="",
        result={
            "reason": "harness_lane_dispatch_error", "reason_detail": "x",
            "duration_seconds": 1.0, "partial_output_lines": 0, "runner_pid": 1,
        },
        request_payload={"gate": "kimi_gate", "pr_number": 1974, "dispatch_id": KIMI_ID},
        requests_dir=requests, results_dir=results,
    )

    assert json.loads((results / "pr-1974-kimi_gate.json").read_text())["status"] == "completed"
    assert not _pending(data_dir, KIMI_ID).exists()
    assert (data_dir / "dispatches" / "failed" / KIMI_ID).is_dir()


def test_a_builder_bundle_is_never_moved_by_a_gate_result(store):
    """codex_gate carries the builder's dispatch-id by design; that bundle is the door's."""
    data_dir, _state, requests, results, _reports = store
    builder_id = "20260929-fsh-d7-gate-bundles"
    bundle = _seed_gate_bundle(data_dir, builder_id)
    (bundle / "dispatch-spec.json").write_text("{}", encoding="utf-8")

    gate_recorder.record_failure(
        gate="codex_gate", pr_number=1974, pr_id="",
        result={
            "reason": "timeout", "reason_detail": "x",
            "duration_seconds": 1.0, "partial_output_lines": 0, "runner_pid": 1,
        },
        request_payload={"gate": "codex_gate", "pr_number": 1974, "dispatch_id": builder_id},
        requests_dir=requests, results_dir=results,
    )

    assert _pending(data_dir, builder_id).is_dir()


def test_a_gate_id_directory_with_a_staged_spec_is_not_a_gate_bundle(store):
    data_dir, _state, requests, results, _reports = store
    bundle = _seed_gate_bundle(data_dir, GLM_ID)
    (bundle / "instruction.md").write_text("# staged\n", encoding="utf-8")

    gate_recorder.record_failure(
        gate="glm_gate", pr_number=1974, pr_id="",
        result={
            "reason": "harness_lane_dispatch_error", "reason_detail": "x",
            "duration_seconds": 1.0, "partial_output_lines": 0, "runner_pid": 1,
        },
        request_payload={"gate": "glm_gate", "pr_number": 1974, "dispatch_id": GLM_ID},
        requests_dir=requests, results_dir=results,
    )

    assert _pending(data_dir, GLM_ID).is_dir()


def test_an_occupied_destination_is_never_overwritten(store):
    data_dir, _state, requests, results, reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)
    _seed_gate_bundle(data_dir, KIMI_ID, text="older copy\n", state="completed")

    _materialize_pass(requests, results, reports)

    assert (_pending(data_dir, KIMI_ID) / "final_prompt.md").read_text() == PROMPT
    assert (data_dir / "dispatches" / "completed" / KIMI_ID / "final_prompt.md").read_text() == "older copy\n"


def test_the_result_receipt_carries_the_prompt_sha(store, monkeypatch):
    """The receipt is the second proof source the cleanup reads."""
    data_dir, _state, requests, results, reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)
    captured = []
    monkeypatch.setattr(
        gate_recorder, "emit_governance_receipt",
        lambda event_type, **kw: captured.append((event_type, kw)),
    )

    _materialize_pass(requests, results, reports)

    results_receipts = [kw for et, kw in captured if et == "review_gate_result"]
    assert len(results_receipts) == 1
    assert results_receipts[0].get("final_prompt_sha256") == PROMPT_SHA


# ---------------------------------------------------------------------------
# 2. The cleanup moves a leftover gate bundle only on proof
# ---------------------------------------------------------------------------


def _entry(entries, dispatch_id):
    return [e for e in entries if e.dispatch_id == dispatch_id]


def test_the_cleanup_moves_a_proven_final_prompt_only_bundle(store):
    data_dir, state_dir, _requests, results, _reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)
    _seed_result(
        results, "pr-1974-kimi_gate.json",
        gate="kimi_gate", status="completed", dispatch_id=KIMI_ID,
        final_prompt_sha256=PROMPT_SHA,
    )

    entries = dc.scan_pending(data_dir, state_dir)
    found = _entry(entries, KIMI_ID)
    assert len(found) == 1, "the cleanup skipped a final_prompt-only gate bundle"
    assert found[0].action == "move-to-completed"

    dc.execute_cleanup(entries, data_dir, dry_run=False)
    assert not _pending(data_dir, KIMI_ID).exists()
    assert (data_dir / "dispatches" / "completed" / KIMI_ID / "final_prompt.md").is_file()


def test_a_receipt_with_the_same_sha_is_proof_as_well(store):
    data_dir, state_dir, *_ = store
    _seed_gate_bundle(data_dir, GLM_ID)
    _seed_receipt(
        state_dir, event_type="review_gate_result", gate="glm_gate",
        gate_status="unavailable", dispatch_id=GLM_ID, final_prompt_sha256=PROMPT_SHA,
    )

    found = _entry(dc.scan_pending(data_dir, state_dir), GLM_ID)
    assert len(found) == 1
    assert found[0].classification == "gate-result-proven"
    assert found[0].action == "move-to-failed"


def test_a_result_with_another_sha_is_no_proof(store):
    data_dir, state_dir, _requests, results, _reports = store
    _seed_gate_bundle(data_dir, KIMI_ID)
    _seed_result(
        results, "pr-1974-kimi_gate.json",
        gate="kimi_gate", status="completed", dispatch_id=KIMI_ID,
        final_prompt_sha256="0" * 64,
    )

    entries = dc.scan_pending(data_dir, state_dir)
    found = _entry(entries, KIMI_ID)
    assert len(found) == 1
    assert (found[0].classification, found[0].action) == ("unproven", "skip")

    dc.execute_cleanup(entries, data_dir, dry_run=False)
    assert _pending(data_dir, KIMI_ID).is_dir()


def test_another_projects_result_does_not_prove_this_projects_bundle(store, other_store):
    """ADR-007 shape: project B carries the same dispatch-id and sha; A reads only A."""
    data_dir, state_dir, *_ = store
    _b_data, b_state, _b_req, b_results, _b_rep = other_store
    _seed_gate_bundle(data_dir, KIMI_ID)
    _seed_result(
        b_results, "pr-1974-kimi_gate.json",
        gate="kimi_gate", status="completed", dispatch_id=KIMI_ID,
        final_prompt_sha256=PROMPT_SHA,
    )
    _seed_receipt(
        b_state, event_type="review_gate_result", gate="kimi_gate",
        gate_status="completed", dispatch_id=KIMI_ID, final_prompt_sha256=PROMPT_SHA,
    )

    found = _entry(dc.scan_pending(data_dir, state_dir), KIMI_ID)
    assert len(found) == 1
    assert found[0].classification == "unproven"


def test_dry_run_invariant_every_move_has_a_matching_result_and_the_rest_stays(store, other_store):
    """Invariant on the dry-run the operator runs first against the real store."""
    data_dir, state_dir, _requests, results, _reports = store
    _b_data, b_state, *_ = other_store
    proven = {"kimi-gate-pr1-1790000010": "completed", "glm-gate-pr2-1790000011": "unavailable"}
    # The proof this project's store actually carries: dispatch-id -> result sha.
    seeded_results = {}
    for did, status in proven.items():
        _seed_gate_bundle(data_dir, did, text=f"prompt {did}\n")
        seeded_results[did] = hashlib.sha256(f"prompt {did}\n".encode()).hexdigest()
        _seed_result(
            results, f"{did}.json", gate="x", status=status, dispatch_id=did,
            final_prompt_sha256=seeded_results[did],
        )
    unproven = ["deepseek-gate-pr3-1790000012", "plan-gate-harness-xyz"]
    for did in unproven:
        _seed_gate_bundle(data_dir, did, text=f"prompt {did}\n")
        # A colliding result in the OTHER project must not count.
        _seed_receipt(
            b_state, event_type="review_gate_result", dispatch_id=did, gate_status="completed",
            final_prompt_sha256=hashlib.sha256(f"prompt {did}\n".encode()).hexdigest(),
        )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dc.main(["--data-dir", str(data_dir), "--state-dir", str(state_dir), "--json"])
    assert rc == 0
    report = json.loads(buf.getvalue())
    by_id = {e["dispatch_id"]: e for e in report["entries"]}

    movers = [e for e in report["entries"] if e["action"].startswith("move-to-")]
    assert {e["dispatch_id"] for e in movers} == set(proven)
    for e in movers:
        bundle_sha = hashlib.sha256(
            (_pending(data_dir, e["dispatch_id"]) / "final_prompt.md").read_bytes()
        ).hexdigest()
        assert e["final_prompt_sha256"] == bundle_sha
        assert seeded_results.get(e["dispatch_id"]) == bundle_sha, "a move without a gate result for the same sha"
    assert by_id["kimi-gate-pr1-1790000010"]["action"] == "move-to-completed"
    assert by_id["glm-gate-pr2-1790000011"]["action"] == "move-to-failed"
    for did in unproven:
        assert (by_id[did]["classification"], by_id[did]["action"]) == ("unproven", "skip")

    # Dry-run changed nothing.
    for did in [*proven, *unproven]:
        assert _pending(data_dir, did).is_dir()

    with redirect_stdout(io.StringIO()):
        dc.main(["--data-dir", str(data_dir), "--state-dir", str(state_dir), "--apply", "--stale-days", "0"])
    for did in unproven:
        assert (_pending(data_dir, did) / "final_prompt.md").is_file(), "an unproven bundle was moved"
    for did, status in proven.items():
        outcome = "completed" if status == "completed" else "failed"
        assert (data_dir / "dispatches" / outcome / did / "final_prompt.md").is_file()


def test_the_text_report_names_the_unproven_bundles(store):
    data_dir, state_dir, *_ = store
    _seed_gate_bundle(data_dir, KIMI_ID)

    report = dc.execute_cleanup(dc.scan_pending(data_dir, state_dir), data_dir, dry_run=True)
    text = dc.format_report(report)

    assert "unproven: 1" in text
    assert f"[unproven] {KIMI_ID}" in text
