"""M5c exercises the real receipt, proposal, verdict and inbox writers."""
import json
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest
import operator_queue as queue
from daily_metrics import proposal_ledger as ledger
from .test_verdict_command import (PID, OTHER, PROJECT_ID, _applied, _approved, _args, _doc, _events, _ledger, _run,
                                   _seed_ledger, _seed_post)

DID = "dlv-0123456789ab"
MERGE = dict(event_type="pr_merged", conclusion="merged", project_id=PROJECT_ID,
             dispatch_id=DID, pr_number=123, merge_sha="a" * 40,
             timestamp="2026-11-01T23:30:00-01:00")


def seed(root, receipts):
    _seed_ledger(root, _approved(day=date(2026, 10, 1)), applied=False)
    ledger._write(_ledger(root), [dict(event="deliverable_created", project_id=PROJECT_ID,
                                      proposal_id=PID, deliverable_id=DID)])
    state = root / "state"
    state.mkdir()
    (state / "t0_receipts.ndjson").write_text("".join(json.dumps(r) + "\n" for r in receipts))
    return state


@pytest.mark.parametrize("project,pid", [("other", PID), (PROJECT_ID, PID + "b")])
def test_no_application_even_thirty_days_after_approval(tmp_path, project, pid):
    _seed_ledger(tmp_path, _approved(), applied=False)
    ledger._write(_ledger(tmp_path), [dict(_applied(_approved()), project_id=project, proposal_id=pid)])
    proc = _run(tmp_path, ["verdict", *_args(tmp_path, "2026-12-02T02:00:00Z")])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"judged": [], "waiting": [{"proposal_id": PID, "reason": "not_applied"}]}
    assert not any(e["event"] == "verdict" for e in _events(tmp_path))


@pytest.mark.parametrize("value,outcome", [(45.0, "helped"), (55.0, "not_helped"), (None, "undetermined")])
@pytest.mark.parametrize("dispatch", [DID, DID + "-r1"])
def test_merge_clock_once_and_one_inbox_point(tmp_path, value, outcome, dispatch):
    state = seed(tmp_path, [dict(MERGE, dispatch_id=dispatch), dict(MERGE, project_id="other", pr_number=999), dict(MERGE, dispatch_id=DID + "x", pr_number=888)])
    _seed_post(tmp_path, {date(2026, 11, 2) + timedelta(days=i): _doc(value) for i in range(1, 8)})
    with ThreadPoolExecutor(2) as pool:
        runs = list(pool.map(lambda _: _run(tmp_path, ["propose", *_args(tmp_path)]), range(2)))
    for proc in runs:
        assert proc.returncode == 0, proc.stderr
    applied = [e for e in _events(tmp_path) if e["event"] == "applied"]
    assert len(applied) == 1
    assert (applied[0]["application_day"], applied[0]["deliverable_id"], applied[0]["pr_number"], applied[0]["merge_sha"]) == ("2026-11-02", DID, 123, "a" * 40)
    for now in ("2026-11-08T02:00:00Z", "2026-11-10T01:59:59Z"):
        proc = _run(tmp_path, ["verdict", *_args(tmp_path, now)])
        assert proc.returncode == 0 and not json.loads(proc.stdout)["judged"]
    for _ in range(2):
        proc = _run(tmp_path, ["verdict", *_args(tmp_path, "2026-11-10T02:00:00Z")])
        assert proc.returncode == 0, proc.stderr
    (event,) = [e for e in _events(tmp_path) if e["event"] == "verdict"]
    assert event["window"] == {"from": "2026-11-03", "to": "2026-11-09"}
    assert (event["frozen_baseline"], event["verdict"], event["final_days"]) == (50.0, outcome, 7)
    points = list(queue.read_state(PROJECT_ID, state_dir=state)["points"].values())
    assert len(points) == 1 and outcome in points[0]["vraag"]
    queue.close_point(PROJECT_ID, points[0]["point_id"], uitkomst="beantwoord", bewijs="ack", state_dir=state)
    assert _run(tmp_path, ["verdict", *_args(tmp_path, "2026-11-10T02:00:00Z")]).returncode == 0
    assert len(queue.read_state(PROJECT_ID, state_dir=state)["points"]) == 1


@pytest.mark.parametrize("fault", ["unmerged", "missing", "corrupt", "torn", "object", "unreadable", "conflict", "timestamp", "merge_sha", "pr_number", "conclusion"])
@pytest.mark.parametrize("bad", [None, [], "unknown", "absent"])
def test_merge_evidence_failure_stays_visible(tmp_path, fault, bad):
    records = [dict(MERGE, event_type="pr_merge_refused", conclusion="refused")] if fault == "unmerged" else [dict(MERGE, **{fault: bad})]
    if bad == "absent" and records:
        records[0].pop(fault, None)
    if fault == "conflict":
        records.append(dict(MERGE, pr_number=456))
    state = seed(tmp_path, records)
    evidence = state / "t0_receipts.ndjson"
    if fault == "missing":
        evidence.rename(state / "saved.ndjson")
    if fault in ("corrupt", "torn", "object"):
        evidence.write_text({"corrupt": '{"event_type":"pr_merged",broken}\n', "torn": json.dumps(MERGE), "object": "null\n"}[fault])
    if fault == "unreadable":
        evidence.rename(state / "saved.ndjson")
        evidence.mkdir()
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    failed = fault not in ("unmerged", "missing", "torn", "object")
    assert proc.returncode == int(failed), proc.stderr
    if failed:
        assert "merge evidence" in proc.stderr and PID in proc.stderr
    assert not any(e["event"] == "applied" for e in _events(tmp_path))


@pytest.mark.parametrize("fault", ["merge_sha", "conflict", "not_approved"])
def test_bad_proposal_does_not_block_application_proposals_or_inbox(tmp_path, fault):
    incomplete = dict(MERGE)
    incomplete.pop("merge_sha")
    records = [incomplete] if fault == "merge_sha" else [MERGE, dict(MERGE, dispatch_id=DID + "-r1", pr_number=456)]
    state = seed(tmp_path, [*records, dict(MERGE, dispatch_id="dlv-abcdef012345")])
    _seed_ledger(tmp_path, *([] if fault == "not_approved" else [_approved()]), _approved(proposal_id=OTHER), applied=False)
    ledger._write(_ledger(tmp_path), [dict(event="deliverable_created", project_id=PROJECT_ID, proposal_id=pid, deliverable_id=did)
                                    for pid, did in ((PID, DID), (OTHER, "dlv-abcdef012345"))])
    _seed_post(tmp_path, {date(2026, 11, 1) + timedelta(days=i): _doc(50.0 if i == 7 else 10.0,
                     metric="pr_open_to_merge_p75_hours") for i in range(8)})
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert [e["proposal_id"] for e in _events(tmp_path) if e["event"] == "applied"] == [OTHER]
    payload = next(p for p in map(json.loads, proc.stdout.splitlines()) if "proposed" in p)
    assert len(payload["proposed"]) == 1
    assert proc.returncode == 1 and PID in proc.stderr and "merge evidence" in proc.stderr
    assert [p["ref"] for p in queue.read_state(PROJECT_ID, state_dir=state)["points"].values()] == [f"proposal:{payload['proposed'][0]}"]


@pytest.mark.parametrize("field", ["merge_sha", "timestamp", "conclusion", "pr_number"])
@pytest.mark.parametrize("bad", [None, [], "unknown", "absent"])
@pytest.mark.parametrize("complete_first", [False, True])
def test_complete_receipt_wins_over_unusable_match(tmp_path, field, bad, complete_first):
    incomplete = dict(MERGE, **{field: bad})
    if bad == "absent":
        incomplete.pop(field)
    seed(tmp_path, [MERGE, incomplete] if complete_first else [incomplete, MERGE])
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 0 and not proc.stderr, proc.stderr
    (applied,) = [e for e in _events(tmp_path) if e["event"] == "applied"]
    assert (applied["pr_number"], applied["merge_sha"], applied["application_day"]) == (123, "a" * 40, "2026-11-02")


def test_receipt_noise_and_torn_last_line_do_not_block_application(tmp_path):
    state = seed(tmp_path, [MERGE])
    with (state / "t0_receipts.ndjson").open("a") as handle:
        handle.write('null\n["pr_merged"]\n{broken unrelated}\n' + json.dumps(dict(MERGE, pr_number=456)))
    proc = _run(tmp_path, ["propose", *_args(tmp_path)])
    assert proc.returncode == 0 and not proc.stderr, proc.stderr
    assert [e["pr_number"] for e in _events(tmp_path) if e["event"] == "applied"] == [123]


def test_daily_applies_before_judging_and_inbox_failure_recovers(tmp_path):
    state = seed(tmp_path, [MERGE])
    inbox = state / queue.LEDGER_FILENAME
    inbox.mkdir()
    proc = _run(tmp_path, ["daily", *_args(tmp_path, "2026-11-10T02:00:00Z")])
    assert proc.returncode == 1 and "error:" in proc.stderr
    ci_run, *failures = [json.loads(line) for line in proc.stderr.splitlines() if line.startswith("{")]
    assert ci_run == {"ci_runs": {
        "project_id": PROJECT_ID, "coverage": "incomplete", "population_total": None,
        "reason": "writer_failed", "skipped_attempts": [],
        "error": "ValueError: VNX_CI_RECORD_MAX_CALLS: override must be a finite integer >= 1; got '0'",
    }}
    beacon = json.loads((tmp_path / "health/ci_runs.json").read_text())
    assert beacon["status"] == "fail" and beacon["details"] == ci_run["ci_runs"]
    assert failures == [{"command_failed": "verdict", "exit_code": 1}]
    assert [e["event"] for e in _events(tmp_path)][-2:] == ["applied", "verdict"]
    inbox.rename(state / "unreadable-inbox")
    for _ in range(2):
        proc = _run(tmp_path, ["verdict", *_args(tmp_path, "2026-11-10T02:00:00Z")])
        assert proc.returncode == 0, proc.stderr
    assert len(queue.read_state(PROJECT_ID, state_dir=state)["points"]) == 1
