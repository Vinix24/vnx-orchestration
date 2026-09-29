"""D7 fabric-state-herstel: a finished gate's bundle leaves dispatches/pending/.

3.368 gate bundles (final_prompt.md only) sat in pending/ because nothing moved
them and dispatch_cleanup skipped every directory without a spec. Now:

  * materialize_artifacts moves the bundle to completed/ or failed/, where
    final_prompt_sha_for_dispatch also looks, so the record keeps its prompt sha;
  * dispatch_cleanup moves a final_prompt-only bundle only when a gate result
    (results/ record or review_gate_result receipt) of the SAME project carries
    the same prompt sha, and reports the rest as ``unproven``.

ADR-007: the cleanup filters gate evidence on project_id; every evidence test
has a second project with a colliding sha that must not leak.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import dispatch_cleanup as dc  # noqa: E402
from final_prompt_integrity import archive_gate_bundle, final_prompt_sha_for_dispatch  # noqa: E402
from gate_artifacts import materialize_artifacts  # noqa: E402

PROJECT = "vnx-dev"
OTHER_PROJECT = "seocrawler"
PROMPT = "You are reviewing PR #2001.\n\nDiff:\n- one line\n"
PROMPT_SHA = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
DISPATCH_ID = "kimi-gate-pr2001-1788000000"

PASS_STDOUT = (
    "## Review\n\nNo blocking findings. Two advisories follow.\n"
    "1. scripts/x.py:4 — the loop re-reads config each pass.\n"
    "2. tests/test_x.py:9 — the fixture builds its own tmp dir.\n"
    "Residual risk: reviewed against main.\n\n"
    "```json\n"
    '{\n  "verdict": "pass",\n  "findings": [],\n  "residual_risk": "reviewed against main"\n}\n'
    "```\n"
)


@pytest.fixture(autouse=True)
def _project(monkeypatch):
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)


@pytest.fixture
def store(tmp_path):
    data_dir = tmp_path / "data"
    results = data_dir / "state" / "review_gates" / "results"
    requests = data_dir / "state" / "review_gates" / "requests"
    reports = data_dir / "unified_reports"
    for d in (results, requests, reports, data_dir / "dispatches" / "pending"):
        d.mkdir(parents=True, exist_ok=True)
    return data_dir, requests, results, reports


def _seed_bundle(data_dir: Path, dispatch_id: str = DISPATCH_ID, text: str = PROMPT) -> Path:
    bundle = data_dir / "dispatches" / "pending" / dispatch_id
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "final_prompt.md").write_text(text, encoding="utf-8")
    return bundle


def _run_gate(store, stdout: str = PASS_STDOUT, dispatch_id: str = DISPATCH_ID):
    data_dir, requests, results, reports = store
    return materialize_artifacts(
        gate="kimi_gate", pr_number=2001, pr_id="", stdout=stdout,
        request_payload={
            "gate": "kimi_gate", "pr_id": "2001", "pr_number": 2001,
            "branch": "fix/x", "commit_sha": "b" * 40,
            "report_path": str(reports / "kimi-2001.md"),
            "contract_hash": "183bed973031720a",
            "dispatch_id": dispatch_id,
        },
        duration_seconds=60.0,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )


# ── archive on gate completion ─────────────────────────────────────────────

def test_finished_gate_leaves_no_bundle_in_pending(store):
    data_dir, *_ = store
    _seed_bundle(data_dir)

    payload = _run_gate(store)

    assert payload["status"] == "completed"
    assert not (data_dir / "dispatches" / "pending" / DISPATCH_ID).exists()
    assert (data_dir / "dispatches" / "completed" / DISPATCH_ID / "final_prompt.md").read_text(
        encoding="utf-8"
    ) == PROMPT


def test_record_still_carries_the_prompt_sha_after_the_move(store):
    data_dir, _, results, _ = store
    _seed_bundle(data_dir)

    _run_gate(store)

    record = json.loads((results / "pr-2001-kimi_gate.json").read_text(encoding="utf-8"))
    assert record["final_prompt_sha256"] == PROMPT_SHA
    assert final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir) == PROMPT_SHA


def test_failed_gate_moves_the_bundle_to_failed_and_keeps_it(store):
    data_dir, *_ = store
    _seed_bundle(data_dir)

    payload = _run_gate(store, stdout="too short\n")

    assert payload["status"] != "completed"
    assert not (data_dir / "dispatches" / "pending" / DISPATCH_ID).exists()
    assert (data_dir / "dispatches" / "failed" / DISPATCH_ID / "final_prompt.md").is_file()
    assert final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir) == PROMPT_SHA


def test_gate_without_a_bundle_still_completes(store):
    payload = _run_gate(store)
    assert payload["status"] == "completed"


def test_archive_never_overwrites_an_existing_destination(store):
    data_dir, *_ = store
    _seed_bundle(data_dir)
    existing = data_dir / "dispatches" / "completed" / DISPATCH_ID
    existing.mkdir(parents=True)
    (existing / "final_prompt.md").write_text("older prompt", encoding="utf-8")

    assert archive_gate_bundle(DISPATCH_ID, data_dir, succeeded=True) is None

    assert (data_dir / "dispatches" / "pending" / DISPATCH_ID / "final_prompt.md").read_text(
        encoding="utf-8"
    ) == PROMPT
    assert (existing / "final_prompt.md").read_text(encoding="utf-8") == "older prompt"


@pytest.mark.parametrize("bad_id", ["", "..", "../escape", "a/b"])
def test_archive_refuses_unsafe_dispatch_ids(store, bad_id):
    data_dir, *_ = store
    _seed_bundle(data_dir)
    assert archive_gate_bundle(bad_id, data_dir, succeeded=True) is None
    assert (data_dir / "dispatches" / "pending" / DISPATCH_ID).is_dir()


# ── cleanup of final_prompt-only bundles ───────────────────────────────────

def _write_result(results: Path, name: str, sha: str, project_id: str = "") -> None:
    rec = {"gate": "kimi_gate", "final_prompt_sha256": sha}
    if project_id:
        rec["project_id"] = project_id
    (results / name).write_text(json.dumps(rec), encoding="utf-8")


def _write_receipt(state_dir: Path, sha: str, project_id: str, event_type: str = "review_gate_result") -> None:
    line = json.dumps({
        "event_type": event_type, "final_prompt_sha256": sha,
        "project_id": project_id, "dispatch_id": "whatever",
    })
    with open(state_dir / "t0_receipts.ndjson", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _scan(data_dir: Path):
    return {e.dispatch_id: e for e in dc.scan_pending(data_dir, data_dir / "state")}


def _entry(data_dir: Path, dispatch_id: str = DISPATCH_ID):
    entry = _scan(data_dir).get(dispatch_id)
    assert entry is not None, "the cleanup does not list the final_prompt-only bundle at all"
    return entry


def test_cleanup_moves_final_prompt_only_bundle_with_a_result_of_the_same_sha(store):
    data_dir, _, results, _ = store
    _seed_bundle(data_dir)
    _write_result(results, "pr-2001-kimi_gate.json", PROMPT_SHA, PROJECT)

    entry = _entry(data_dir)
    assert (entry.classification, entry.action) == ("gate-result-found", "move-to-completed")

    report = dc.execute_cleanup(list(_scan(data_dir).values()), data_dir, dry_run=False)
    assert report.action_counts == {"move-to-completed": 1}
    assert not (data_dir / "dispatches" / "pending" / DISPATCH_ID).exists()
    assert (data_dir / "dispatches" / "completed" / DISPATCH_ID / "final_prompt.md").is_file()


def test_cleanup_accepts_a_review_gate_result_receipt_as_proof(store):
    data_dir, *_ = store
    _seed_bundle(data_dir)
    _write_receipt(data_dir / "state", PROMPT_SHA, PROJECT)

    assert _entry(data_dir).action == "move-to-completed"


def test_bundle_without_a_result_stays_and_is_reported_unproven(store):
    data_dir, *_ = store
    _seed_bundle(data_dir)

    entry = _entry(data_dir)
    assert (entry.classification, entry.action) == ("unproven", "skip")

    dc.execute_cleanup(list(_scan(data_dir).values()), data_dir, dry_run=False)
    assert (data_dir / "dispatches" / "pending" / DISPATCH_ID / "final_prompt.md").is_file()
    assert "unproven" in dc.format_report(dc.execute_cleanup(list(_scan(data_dir).values()), data_dir))


def test_result_with_another_sha_does_not_clear_the_bundle(store):
    data_dir, _, results, _ = store
    _seed_bundle(data_dir)
    _write_result(results, "pr-2001-kimi_gate.json", "0" * 64, PROJECT)
    _write_receipt(data_dir / "state", "1" * 64, PROJECT)

    assert _entry(data_dir).action == "skip"


def test_other_projects_evidence_with_a_colliding_sha_does_not_leak(store):
    """ADR-007: same sha, second project. Neither its result nor its receipt
    may clear this project's bundle."""
    data_dir, _, results, _ = store
    _seed_bundle(data_dir)
    _write_result(results, "pr-9-kimi_gate.json", PROMPT_SHA, OTHER_PROJECT)
    _write_receipt(data_dir / "state", PROMPT_SHA, OTHER_PROJECT)

    entry = _entry(data_dir)
    assert (entry.classification, entry.action) == ("unproven", "skip")


def test_non_gate_receipt_with_the_sha_is_not_proof(store):
    data_dir, *_ = store
    _seed_bundle(data_dir)
    _write_receipt(data_dir / "state", PROMPT_SHA, PROJECT, event_type="task_complete")

    assert _entry(data_dir).action == "skip"


def test_invariant_every_planned_move_has_a_result_with_the_same_sha(store):
    """Dry-run invariant over a mixed pending/: whatever the cleanup would move
    is backed by a same-sha, same-project result; everything else stays."""
    data_dir, _, results, _ = store
    proven_prompts = {f"proven-gate-{i}": f"prompt {i}\n" for i in range(3)}
    unproven_prompts = {f"unproven-gate-{i}": f"other prompt {i}\n" for i in range(3)}
    for did, text in {**proven_prompts, **unproven_prompts}.items():
        _seed_bundle(data_dir, did, text)
    for i, text in enumerate(proven_prompts.values()):
        _write_result(results, f"r{i}.json", hashlib.sha256(text.encode()).hexdigest(), PROJECT)
    # colliding evidence from a second project for the unproven prompts
    for text in unproven_prompts.values():
        _write_result(results, f"x-{abs(hash(text))}.json", hashlib.sha256(text.encode()).hexdigest(), OTHER_PROJECT)

    report = dc.execute_cleanup(dc.scan_pending(data_dir, data_dir / "state"), data_dir, dry_run=True)

    shas_with_result = {
        json.loads(p.read_text())["final_prompt_sha256"]
        for p in results.glob("r*.json")
    }
    moves = [e for e in report.entries if e.action == "move-to-completed"]
    assert {e.dispatch_id for e in moves} == set(proven_prompts)
    assert all(e.final_prompt_sha in shas_with_result for e in moves)
    stays = [e for e in report.entries if e.action != "move-to-completed"]
    assert {e.dispatch_id for e in stays} == set(unproven_prompts)
    assert all(e.classification == "unproven" for e in stays)
    for did in {**proven_prompts, **unproven_prompts}:
        assert (data_dir / "dispatches" / "pending" / did).is_dir(), "dry-run moved something"


def test_cleanup_still_ignores_directories_without_any_bundle_file(store):
    data_dir, *_ = store
    (data_dir / "dispatches" / "pending" / "empty-dir").mkdir()
    assert _scan(data_dir) == {}
