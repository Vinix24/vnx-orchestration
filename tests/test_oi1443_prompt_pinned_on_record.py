"""The record pins the prompt the run actually sent (OI-1443).

Two glm_gate runs on the same commit differed by 227 input tokens — 26829
against 27056 — and nobody could ask what was different about them. The record
names the commit and the `contract_hash`, and neither covers the assembled
prompt: the contract hash covers the review CONTRACT, while injected context,
skill bodies and lane preludes all land in the prompt afterwards.

Measured across the central store on 2026-08-29:

    gate result records with final_prompt_sha256:   0 of 472
    receipts        with final_prompt_sha256:    1991 of 28455

So the fact is captured — on the receipt, and in the dispatch bundle — and it
simply never reaches the record a merge decision reads. A gate dispatch bundle
holds `final_prompt.md` but no `dispatch-spec.json`, so the field
`persist_final_prompt` would have stamped was never written for gate runs at
all. The prompt itself is on disk, so nothing new has to be captured: it is
recoverable.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

PROMPT = "You are reviewing PR #1443.\n\nDiff:\n- one line\n"
PROMPT_SHA = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
DISPATCH_ID = "kimi-gate-pr1443-1788000000"


@pytest.fixture
def store(tmp_path):
    data_dir = tmp_path / "data"
    results = data_dir / "state" / "review_gates" / "results"
    requests = data_dir / "state" / "review_gates" / "requests"
    reports = data_dir / "unified_reports"
    for d in (results, requests, reports):
        d.mkdir(parents=True, exist_ok=True)
    return data_dir, requests, results, reports


def _seed_bundle(data_dir: Path, state: str = "pending", text: str = PROMPT):
    bundle = data_dir / "dispatches" / state / DISPATCH_ID
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "final_prompt.md").write_text(text, encoding="utf-8")
    return bundle


@pytest.mark.parametrize("state", ["pending", "completed", "failed"])
def test_the_sha_is_recovered_from_wherever_the_bundle_sits(store, state):
    """A dispatch moves between pending, completed and failed while the gate
    record is written. Looking in only one of them finds nothing most of the
    time, and finding nothing is indistinguishable from having no prompt."""
    from final_prompt_integrity import final_prompt_sha_for_dispatch

    data_dir, *_ = store
    _seed_bundle(data_dir, state)

    assert final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir) == PROMPT_SHA


def test_an_unrecoverable_sha_is_empty_and_not_an_error(store):
    """A gate result must not fail to be recorded over a provenance field."""
    from final_prompt_integrity import final_prompt_sha_for_dispatch

    data_dir, *_ = store

    assert final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir) == ""
    assert final_prompt_sha_for_dispatch("", data_dir) == ""
    assert final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir / "nope") == ""


def test_two_runs_of_the_same_prompt_pin_the_same_sha(store):
    from final_prompt_integrity import final_prompt_sha_for_dispatch

    data_dir, *_ = store
    _seed_bundle(data_dir)
    first = final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir)
    second = final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir)

    assert first == second == PROMPT_SHA


def test_a_different_prompt_pins_a_different_sha(store):
    """The whole point: two runs on one commit that sent different prompts must
    be distinguishable afterwards."""
    from final_prompt_integrity import final_prompt_sha_for_dispatch

    data_dir, *_ = store
    _seed_bundle(data_dir, text=PROMPT + "\n<injected-context>extra</injected-context>\n")

    other = final_prompt_sha_for_dispatch(DISPATCH_ID, data_dir)
    assert other != PROMPT_SHA
    assert len(other) == 64


def test_the_result_record_carries_the_pin(store):
    from gate_artifacts import materialize_artifacts

    data_dir, requests, results, reports = store
    _seed_bundle(data_dir)

    materialize_artifacts(
        gate="kimi_gate", pr_number=1443, pr_id="",
        stdout="## Review\n\nNo blocking findings. Two advisories follow.\n"
               "1. scripts/x.py:4 — the loop re-reads config each pass.\n"
               "2. tests/test_x.py:9 — the fixture builds its own tmp dir.\n"
               "Residual risk: reviewed against main.\n",
        request_payload={
            "gate": "kimi_gate", "pr_id": "1443", "pr_number": 1443,
            "branch": "fix/x", "commit_sha": "b" * 40,
            "report_path": str(reports / "kimi-1443.md"),
            "contract_hash": "183bed973031720a",
            "dispatch_id": DISPATCH_ID,
        },
        duration_seconds=60.0,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )

    record = json.loads(
        (results / "pr-1443-kimi_gate.json").read_text(encoding="utf-8")
    )
    assert record.get("final_prompt_sha256") == PROMPT_SHA, (
        "the record names the commit and the contract but not the prompt, so "
        "two runs on one commit cannot be told apart"
    )


def test_a_record_without_a_recoverable_prompt_is_not_stamped_with_an_empty_sha(store):
    """An empty string means "not recovered", never "no prompt".

    Stamping "" would put a field on the record that reads as a pinned prompt
    until someone compares it, at which point every unrecovered run matches
    every other one.
    """
    from gate_artifacts import materialize_artifacts

    data_dir, requests, results, reports = store  # no bundle seeded

    materialize_artifacts(
        gate="kimi_gate", pr_number=1443, pr_id="",
        stdout="## Review\n\nNo blocking findings. Two advisories follow.\n"
               "1. scripts/x.py:4 — the loop re-reads config each pass.\n"
               "2. tests/test_x.py:9 — the fixture builds its own tmp dir.\n"
               "Residual risk: reviewed against main.\n",
        request_payload={
            "gate": "kimi_gate", "pr_id": "1443", "pr_number": 1443,
            "branch": "fix/x", "commit_sha": "b" * 40,
            "report_path": str(reports / "kimi-1443.md"),
            "contract_hash": "183bed973031720a",
            "dispatch_id": DISPATCH_ID,
        },
        duration_seconds=60.0,
        requests_dir=requests, results_dir=results, reports_dir=reports,
    )

    record = json.loads(
        (results / "pr-1443-kimi_gate.json").read_text(encoding="utf-8")
    )
    assert "final_prompt_sha256" not in record
