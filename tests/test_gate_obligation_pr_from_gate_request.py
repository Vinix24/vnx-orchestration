"""OI-3085: request records link builders whose PR uses a different branch."""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gate_obligation_runner as runner
from lib.gate_obligations import register_obligation

DISPATCH = "dlv-faa3cfc16819"
REPO = "Vinix24/vnx-orchestration"
SHA = "127c1f9d" + "0" * 32


@pytest.fixture
def case(tmp_path, monkeypatch):
    state = tmp_path / "state"
    requests = state / "review_gates" / "requests"
    requests.mkdir(parents=True)
    path = register_obligation(state, dispatch_id=DISPATCH, gate="codex_gate",
                               project_id="vnx-dev", branch=f"dispatch/{DISPATCH}")
    lock = runner._occupancy_lock_path(state, DISPATCH)
    lock.parent.mkdir()
    lock.touch()  # Real, released occupancy lock: no live holder.
    request = requests / "pr-3181-claude_gate.json"
    request.write_text(json.dumps({
        "pr_number": 3181, "builder_vendor": {"dispatch_id": DISPATCH},
        "branch": f"feat/ci-run-github-{DISPATCH}", "requested_at": "2026-11-25T15:21:58Z",
    }))
    github = {"state": "OPEN", "snapshot": [], "calls": []}

    def gh(args, *, owner_repo=None, budget=None):
        github["calls"].append(args)
        assert owner_repo == REPO
        if args[:2] == ["pr", "list"]:
            return github["snapshot"] if "--head" not in args else []
        assert args[:2] == ["pr", "view"]
        return {} if github["state"] == "missing" else {"state": github["state"]}

    monkeypatch.setattr(runner, "_resolve_github_owner_repo", lambda sd: REPO)
    monkeypatch.setattr(runner, "_gh_json", gh)
    monkeypatch.setattr(runner, "_branch_exists_on_github", lambda *a, **kw: False)
    monkeypatch.setattr(runner, "_get_pr_head_sha_for_gate", lambda pr: SHA)
    return state, path, request, github


def test_3181_open_request_prevents_branch_gone_retirement(case):
    state, path, request, _ = case
    record = json.loads(path.read_text())
    assert record["branch"] == f"dispatch/{DISPATCH}"
    before = request.read_bytes()
    resolution = runner.resolve_pr_number(state, record)
    assert resolution.status == runner.RESOLUTION_RESOLVED
    assert (resolution.pr_number, resolution.owner_repo) == (3181, REPO)
    decision = runner._pre_execution_decision(state, DISPATCH, "codex_gate", resolution, 1,
                                               runner._index_gate_results(state))
    assert decision["kind"] == "attempt_gate"
    # Real evidence from a separate gate run exercises the existing PR stamp.
    report = state / "gate-report.md"
    report.write_text("Review evidence for this head")
    result = state / "review_gates" / "results" / "pr-3181-codex_gate.json"
    result.parent.mkdir()
    result.write_text(json.dumps({
        "gate": "codex_gate", "dispatch_id": "poortrun-3181", "pr_number": 3181,
        "status": "pass", "commit_sha": SHA, "contract_hash": "sha256:3181",
        "report_path": str(report),
    }))
    runner.fulfill_obligation(state, path, record)
    booked = json.loads(path.read_text())
    assert (booked["status"], booked["pr_number"]) == ("fulfilled", 3181)
    assert request.read_bytes() == before


@pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED"])
def test_non_open_request_keeps_existing_retirement(case, pr_state):
    state, path, _, github = case
    github["state"] = pr_state
    runner.fulfill_obligation(state, path, json.loads(path.read_text()))
    booked = json.loads(path.read_text())
    assert (booked["status"], booked["reason"], booked["pr_number"]) == (
        "retired", "no_pr_branch_gone", None,
    )


def test_conflicting_requests_warn_and_keep_existing_retirement(case, caplog):
    state, path, request, _ = case
    payload = json.loads(request.read_text())
    payload["pr_number"] = 3182
    request.with_name("pr-3182-deepseek_gate.json").write_text(json.dumps(payload))
    with caplog.at_level(logging.WARNING):
        runner.fulfill_obligation(state, path, json.loads(path.read_text()))
    assert json.loads(path.read_text())["reason"] == "no_pr_branch_gone"
    assert "3181" in caplog.text and "3182" in caplog.text


@pytest.mark.parametrize("content", [b"{", b"\xff", b"null", b"[]", b'{"builder_vendor": []}',
                                     b'{"builder_vendor": null}'])
def test_malformed_request_is_skipped_with_filename_warning(case, caplog, content):
    state, path, request, _ = case
    request.write_bytes(content)
    with caplog.at_level(logging.WARNING):
        runner.fulfill_obligation(state, path, json.loads(path.read_text()))
    assert json.loads(path.read_text())["reason"] == "no_pr_branch_gone"
    assert str(request) in caplog.text


def test_unreadable_request_is_skipped_with_filename_warning(case, caplog):
    state, path, request, _ = case
    request.rename(request.with_suffix(".saved"))
    request.mkdir()  # IsADirectoryError without patching file I/O.
    with caplog.at_level(logging.WARNING):
        resolution = runner.resolve_pr_number(state, json.loads(path.read_text()))
    assert resolution.status == runner.RESOLUTION_AWAITING
    assert str(request) in caplog.text


@pytest.mark.parametrize("number", [None, True, 0, -1, "3181", 3181.0, {}, "unknown", "missing"])
def test_invalid_pr_number_never_matches(case, number):
    state, path, request, _ = case
    payload = {"builder_vendor": {"dispatch_id": DISPATCH}}
    if number != "missing":
        payload["pr_number"] = number
    request.write_text(json.dumps(payload))
    assert runner.resolve_pr_number(state, json.loads(path.read_text())).status == runner.RESOLUTION_AWAITING


def test_exact_builder_match_and_explicit_store_with_duplicate_requests(case, tmp_path):
    state, path, request, _ = case
    request.with_name("duplicate.json").write_bytes(request.read_bytes())
    wrong = {"builder_vendor": {"dispatch_id": DISPATCH + "-other"}, "pr_number": 3182}
    request.with_name("other-builder.json").write_text(json.dumps(wrong))
    other = tmp_path / "other-project" / "review_gates" / "requests"
    other.mkdir(parents=True)
    wrong["builder_vendor"]["dispatch_id"] = DISPATCH
    (other / request.name).write_text(json.dumps(wrong))
    assert runner.resolve_pr_number(state, json.loads(path.read_text())).pr_number == 3181


@pytest.mark.parametrize("pr_state", [None, "", {}, "unknown", "missing"])
def test_unknown_pr_state_never_resolves_or_retires(case, pr_state):
    state, path, _, github = case
    github["state"] = pr_state
    resolution = runner.resolve_pr_number(state, json.loads(path.read_text()))
    assert resolution.status == runner.RESOLUTION_UNRESOLVABLE
    assert "3181" in resolution.reason


@pytest.mark.parametrize("live_state", ["OPEN", "MERGED", "CLOSED", None])
def test_snapshot_is_confirmed_live_before_accepting_request(case, live_state):
    state, path, _, github = case
    github["snapshot"] = [{"number": 3181, "state": "OPEN"}]
    github["state"] = live_state
    snapshot = runner._GithubSnapshot(REPO)
    resolution = runner.resolve_pr_number(state, json.loads(path.read_text()), snapshot=snapshot)
    expected = {"OPEN": runner.RESOLUTION_RESOLVED, None: runner.RESOLUTION_UNRESOLVABLE}
    assert resolution.status == expected.get(live_state, runner.RESOLUTION_AWAITING)
    assert snapshot.live_confirms.calls == 1


@pytest.mark.parametrize("budget_name", ["fallback", "live_confirms"])
def test_request_lookup_budget_defers_without_writing(case, budget_name):
    state, path, _, github = case
    if budget_name == "live_confirms":
        github["snapshot"] = [{"number": 3181, "state": "OPEN"}]
    snapshot = runner._GithubSnapshot(REPO)
    getattr(snapshot, budget_name).limit = 0
    before = path.read_bytes()
    outcome = runner.fulfill_obligation(state, path, json.loads(before), snapshot=snapshot)
    assert outcome["deferred"] is True
    assert path.read_bytes() == before
    assert all(args[:2] != ["pr", "view"] for args in github["calls"])


def test_registration_keeps_precedence(case):
    state, path, _, github = case
    record = json.loads(path.read_text())
    record["pr_number"] = 3183
    assert runner.resolve_pr_number(state, record).pr_number == 3183
    assert github["calls"] == []
