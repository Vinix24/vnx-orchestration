"""GE-8 (track ``gate-engine-consolidation``): the orphan-request cleanup is a gate writer.

``scripts/cleanup_orphan_gates.py`` used to land a ``review_gates/results/``
record with a bare ``write_text`` and the non-canonical status ``abandoned`` —
no slot lock, no receipt, no request end, no writer guard. It now resolves an
orphan through ``gate_outcome.commit_gate_outcome``: one ``unavailable`` record,
one ``review_gate_result`` receipt and the request's terminal end, all under
the slot's lock.

The tests drive the real script. ``review_stack_support.build_project_store``
gives a throwaway store; every write lands there, never in this repo.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_stack_support  # noqa: E402

PR = 1926
STEM = f"pr-{PR}-codex_gate"


@pytest.fixture
def env(tmp_path, monkeypatch):
    return review_stack_support.build_project_store(tmp_path, monkeypatch)


def _write_request(env, payload: dict, age_hours: float = 25.0, stem: str = STEM) -> Path:
    path = env["requests_dir"] / f"{stem}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    mtime = time.time() - age_hours * 3600
    os.utime(path, (mtime, mtime))
    return path


def _request_payload(**over) -> dict:
    payload = {
        "gate": "codex_gate",
        "pr_number": PR,
        "branch": review_stack_support.BRANCH,
        "dispatch_id": "ge8-orphan-fixture",
        "status": "executing",
    }
    payload.update(over)
    return payload


def _receipts(env) -> list:
    out = []
    for name in ("t0_receipts.ndjson", "gate_events.ndjson"):
        path = env["state_dir"] / name
        if path.exists():
            out.extend(
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
    return [r for r in out if r.get("event_type") == "review_gate_result"]


def _main(m, env, *args: str) -> int:
    return m.main([
        "--requests-dir", str(env["requests_dir"]),
        "--results-dir", str(env["results_dir"]),
        *args,
    ])


def test_one_orphan_becomes_one_unavailable_record_receipt_and_request_end(env):
    """The Goal: one writer call, one record, one receipt, one terminal request."""
    import cleanup_orphan_gates as m

    request_path = _write_request(env, _request_payload())

    assert _main(m, env) == 0

    record = json.loads((env["results_dir"] / f"{STEM}.json").read_text(encoding="utf-8"))
    assert record["status"] == "unavailable", "ADR-046 §1 has no 'abandoned' outcome"
    assert record["reason"] == "abandoned_orphan_request"
    assert record["abandoned_by"] == "cleanup_orphan_gates"
    assert record["age_hours"] >= 24.0
    assert record["source_request"] == STEM
    assert record["attempt_seq"] == 1, "the record lands like every other outcome"
    assert "receipt_pending" not in record, "the receipt was booked in the same commit"

    receipts = _receipts(env)
    assert len(receipts) == 1, "exactly one review_gate_result receipt"
    assert receipts[0]["gate"] == "codex_gate"
    assert receipts[0]["status"] == "unavailable"
    assert receipts[0]["gate_status"] == "unavailable", "an outage is not a rejection (ADR-037)"

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["status"] == "unavailable", "REQUEST_STATUS_OF ends the request unavailable"
    assert request["dispatch_id"] == "ge8-orphan-fixture", "the request keeps its own fields"


def test_a_contract_slot_request_is_ended_at_its_own_path(env):
    """The request end must target the file the orphan was found at, not a slot
    re-derived from a payload ``pr_id`` (which would mint a second request)."""
    import cleanup_orphan_gates as m

    stem = "pr1691-codex_gate-contract"
    request_path = _write_request(
        env, {"gate": "codex_gate", "pr_id": "PR-1691", "status": "executing"}, stem=stem,
    )

    assert _main(m, env) == 0

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["status"] == "unavailable"
    assert [p.name for p in env["requests_dir"].glob("*.json")] == [f"{stem}.json"]
    record = json.loads((env["results_dir"] / f"{stem}.json").read_text(encoding="utf-8"))
    assert (record["gate"], record["pr_id"]) == ("codex_gate", "pr1691")


def test_a_second_run_writes_nothing_new(env):
    import cleanup_orphan_gates as m

    _write_request(env, _request_payload())
    assert _main(m, env) == 0
    slot = env["results_dir"] / f"{STEM}.json"
    first_bytes = slot.read_bytes()
    receipts_after_first = len(_receipts(env))

    assert _main(m, env) == 0, "the slot has a result now, so there is no orphan left to resolve"

    assert slot.read_bytes() == first_bytes, "no rewrite, no new attempt_seq"
    assert len(_receipts(env)) == receipts_after_first, "no second receipt"


def test_an_existing_terminal_record_is_left_alone_and_reported(env, capsys):
    import cleanup_orphan_gates as m

    _write_request(env, _request_payload())
    decided = {
        "gate": "codex_gate", "pr_number": PR, "status": "pass",
        "contract_hash": "abc123", "report_path": "/tmp/codex-pr1926.md",
        "commit_sha": review_stack_support.HEAD_SHA, "blocking_findings": [],
    }
    slot = env["results_dir"] / f"{STEM}.json"
    slot.write_text(json.dumps(decided), encoding="utf-8")
    before = slot.read_bytes()

    orphan = {
        "request_path": env["requests_dir"] / f"{STEM}.json",
        "result_path": slot,
        "gate_name": "codex_gate",
        "pr_number": PR,
        "pr_id": "",
        "stem": STEM,
        "age_hours": 30.0,
        "request_data": _request_payload(),
    }
    ok = m._write_abandoned_result(orphan, dry_run=False, requests_dir=env["requests_dir"])

    assert ok is True, "an already-resolved slot is not a write failure"
    assert slot.read_bytes() == before, "the decided verdict survives byte for byte"
    assert "already resolved" in capsys.readouterr().out
    assert _receipts(env) == [], "a refused write books no receipt"


def test_dry_run_writes_nothing(env):
    import cleanup_orphan_gates as m

    request_path = _write_request(env, _request_payload())
    before = request_path.read_bytes()

    assert _main(m, env, "--dry-run") == 0

    assert not (env["results_dir"] / f"{STEM}.json").exists(), "dry run writes no record"
    assert request_path.read_bytes() == before, "dry run leaves the request alone"
    assert _receipts(env) == [], "dry run books no receipt"


def test_the_store_comes_from_the_resolver_not_the_repo(env, monkeypatch):
    """``$VNX_DATA_DIR`` + ``VNX_DATA_DIR_EXPLICIT=1`` decide the store (issue #225)."""
    import cleanup_orphan_gates as m

    unique_pr = 918273
    stem = f"pr-{unique_pr}-codex_gate"
    _write_request(env, _request_payload(pr_number=unique_pr), stem=stem)
    monkeypatch.chdir(env["project_root"])

    assert m._gate_dirs() == (env["requests_dir"], env["results_dir"])
    assert m.main([]) == 0, "no --requests-dir/--results-dir: the resolver supplies the store"

    assert (env["results_dir"] / f"{stem}.json").exists(), "the fixture store got the record"
    repo_slot = VNX_ROOT / ".vnx-data" / "state" / "review_gates" / "results" / f"{stem}.json"
    assert not repo_slot.exists(), "never the repo-relative .vnx-data fallback"
