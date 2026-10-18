"""Review gates honor the provider switch (provider-schakelaar C).

The seat walk in ``gate_request_handler`` consults the operator's switch for every seat before
the chain branch and for every candidate the walk reaches. These tests drive the real
``request_reviews`` / ``request_and_execute`` on a tmp store with a tmp switch file
(``VNX_PROVIDER_SWITCH_FILE``, pinned per test by ``tests/conftest.py``). The launch primitive is
patched: the seat dispatch ``_dispatch_one_review`` records which gate would have been
requested, and ``execute_gate`` records which gate would have been run. No test starts kimi,
codex, claude or a harness, and nothing is written outside ``tmp_path``.

Appendix C, Tests:
- kimi off, stack codex_gate,kimi_gate: codex + deepseek (takeover_from kimi_gate, failure reason
  names provider_switched_off), no kimi launch;
- kimi, deepseek and glm off: codex + a chain_exhausted record;
- an existing PASS of a since-disabled gate on this head is not re-dispatched;
- kimi back on: the next request dispatches kimi on the same head with no unreachable hop, and
  its result replaces the provider_switched_off record;
- empty chain, kimi off: codex + a provider_switched_off record for kimi, no kimi launch.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config_registry as cr  # after the sys.path insert above

HEAD = "c" * 40
OTHER_HEAD = "d" * 40
PR = 7311
SWITCHED_OFF = "provider_switched_off"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_config(monkeypatch):
    """Same isolation as tests/test_beta3_e1_review_gate_chain.py: no leaked config resolver,
    no ambient chain or stack override, no ambient project or switch override."""
    import config_runtime as crt
    for key in list(cr.CONFIG_REGISTRY):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{cr._bare(key)}", raising=False)
    for key in [k for k in os.environ if k.startswith("VNX_OVERRIDE_PROVIDER_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)
    yield
    crt._wired_for.clear()
    cr.set_db_resolver(None)
    cr.set_default_project_id(None)


@pytest.fixture
def store(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    for d in (state_dir / "review_gates" / "requests", state_dir / "review_gates" / "results", reports_dir):
        d.mkdir(parents=True, exist_ok=True)
    (project_root / ".vnx-project-id").write_text("switch-test\n", encoding="utf-8")
    for key, value in {
        "VNX_HOME": str(VNX_ROOT), "PROJECT_ROOT": str(project_root),
        "VNX_DATA_DIR": str(data_dir), "VNX_DATA_DIR_EXPLICIT": "1",
        "VNX_STATE_DIR": str(state_dir), "VNX_REPORTS_DIR": str(reports_dir),
        "VNX_DISPATCH_DIR": str(data_dir / "dispatches"), "VNX_LOGS_DIR": str(data_dir / "logs"),
        "VNX_PIDS_DIR": str(data_dir / "pids"), "VNX_LOCKS_DIR": str(data_dir / "locks"),
        "VNX_DB_DIR": str(data_dir / "database"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(project_root)
    # R1: these tests put standing verdicts on disk without a request; the request that
    # produced them opened the PR in the round ledger.
    from review_seat_support import open_pr_in_ledger

    open_pr_in_ledger(state_dir, PR)

    import gate_recorder
    import gate_request_handler
    import review_gate_manager
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: HEAD)
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda pr_number: HEAD)
    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(review_gate_manager, "emit_governance_receipt",
                        lambda event, **fields: receipts.append({"event": event, **fields}))
    monkeypatch.setattr(gate_recorder, "emit_governance_receipt",
                        lambda event, **fields: receipts.append({"event": event, **fields}))
    # A landed result is published as a GitHub check-run; never from a test.
    monkeypatch.setattr(gate_recorder, "publish_forge_check_run", lambda payload, **kw: None)
    return {
        "state_dir": state_dir, "reports_dir": reports_dir,
        "requests_dir": state_dir / "review_gates" / "requests",
        "results_dir": state_dir / "review_gates" / "results",
        "receipts": receipts,
    }


@pytest.fixture
def launches(monkeypatch):
    """Patch the seat dispatch: record the gate a live request would go to, write the request
    record the real per-gate request method writes, launch nothing."""
    import review_gate_manager
    from atomic_io import atomic_write_json
    calls: List[str] = []

    def fake_dispatch_one_review(self, gate, pr_number, branch, risk_class, changed_files, mode, dispatch_id):
        calls.append(gate)
        payload = {"gate": gate, "status": "requested", "branch": branch, "pr_number": pr_number,
                   "commit_sha": HEAD, "dispatch_id": dispatch_id}
        atomic_write_json(self._request_path(gate, pr_number), payload)
        return payload

    monkeypatch.setattr(review_gate_manager.ReviewGateManager, "_dispatch_one_review", fake_dispatch_one_review)
    return calls


def switch_file() -> Path:
    return Path(os.environ["VNX_PROVIDER_SWITCH_FILE"])


def set_switches(**families: bool) -> None:
    """Write the switch file the way the CLI would (schema version 1, global section)."""
    path = switch_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": 1, "global": {
        family: {"enabled": enabled, "changed_at": "2026-10-15T08:00:00Z", "changed_by": "op",
                 "reason": "tegoed sparen"}
        for family, enabled in families.items()
    }, "projects": {}}
    path.write_text(json.dumps(document), encoding="utf-8")


def manager():
    import review_gate_manager
    return review_gate_manager.ReviewGateManager()


def request(stack, *, dispatch_id="20261015-ps-c-test"):
    return manager().request_reviews(
        pr_number=PR, branch="feat/switch", review_stack=stack, risk_class="medium",
        changed_files=["scripts/lib/gate_request_handler.py"], mode="per_pr", dispatch_id=dispatch_id,
    )


def seats(result) -> Dict[str, Dict[str, Any]]:
    return {payload["gate"]: payload for payload in result["requested"]}


def write_result(store, gate: str, payload: Dict[str, Any]) -> Path:
    path = store["results_dir"] / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def decided_result(store, gate: str, status: str, *, head: str = HEAD) -> Dict[str, Any]:
    report = store["reports_dir"] / f"{gate}-pr{PR}.md"
    report.write_text("verdict\n", encoding="utf-8")
    blocking = [] if status == "pass" else [{"severity": "blocking", "title": "real defect"}]
    return {
        "gate": gate, "pr_number": PR, "pr_id": str(PR), "status": status, "branch": "feat/switch",
        "commit_sha": head, "contract_hash": "0123456789abcdef", "report_path": str(report),
        "blocking_findings": blocking, "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dispatch_id": f"{gate.replace('_gate', '')}-gate-pr{PR}-1790000000",
    }


def reachability_record(store, provider: str) -> Path:
    return store["state_dir"] / "provider_reachability" / f"{provider}.json"


# ---------------------------------------------------------------------------
# Appendix C, Tests
# ---------------------------------------------------------------------------

def test_kimi_off_codex_plus_deepseek_takeover_and_no_kimi_launch(store, launches):
    set_switches(kimi=False)

    result = request(["codex_gate", "kimi_gate"])

    assert launches == ["codex_gate", "deepseek_gate"]
    by_gate = seats(result)
    assert set(by_gate) == {"codex_gate", "deepseek_gate"}
    deepseek = by_gate["deepseek_gate"]
    assert deepseek["takeover_from"] == "kimi_gate"
    assert deepseek["takeover_reason"] == SWITCHED_OFF
    assert SWITCHED_OFF in deepseek["failure_reason"]
    assert "source=global" in deepseek["failure_reason"] and "tegoed sparen" in deepseek["failure_reason"]
    [hop] = deepseek["takeover_path"]
    assert hop["gate"] == "kimi_gate" and hop["reason"] == SWITCHED_OFF and hop["status"] == "disabled"
    on_disk = json.loads((store["requests_dir"] / f"pr-{PR}-deepseek_gate.json").read_text(encoding="utf-8"))
    assert on_disk["takeover_from"] == "kimi_gate"
    # Passing a seat over books nothing for the passed-over gate and no reachability fact.
    assert not (store["results_dir"] / f"pr-{PR}-kimi_gate.json").exists()
    assert not reachability_record(store, "kimi").exists()
    request_receipt = next(r for r in store["receipts"] if r.get("gate") == "deepseek_gate")
    assert request_receipt["takeover_from"] == "kimi_gate" and SWITCHED_OFF in request_receipt["failure_reason"]


def test_kimi_deepseek_off_codex_plus_chain_exhausted_on_the_default_chain(store, launches, monkeypatch):
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
    set_switches(kimi=False, deepseek=False)

    result = request(["codex_gate", "kimi_gate"])

    assert launches == ["codex_gate"]
    exhausted = seats(result)["kimi_gate"]
    assert exhausted["status"] == "chain_exhausted"
    assert [hop["gate"] for hop in exhausted["takeover_path"]] == ["kimi_gate", "deepseek_gate"]


def test_kimi_deepseek_glm_off_codex_plus_chain_exhausted(store, launches, monkeypatch):
    # glm_gate left the default chain (R4, operator decision 2026-10-17): name it explicitly.
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate,deepseek_gate,glm_gate")
    set_switches(kimi=False, deepseek=False, glm=False)

    result = request(["codex_gate", "kimi_gate"])

    assert launches == ["codex_gate"]
    by_gate = seats(result)
    assert by_gate["codex_gate"]["status"] == "requested"
    exhausted = by_gate["kimi_gate"]
    assert exhausted["status"] == "chain_exhausted" and exhausted["reason"] == "takeover_chain_exhausted"
    assert [hop["gate"] for hop in exhausted["takeover_path"]] == ["kimi_gate", "deepseek_gate", "glm_gate"]
    assert {hop["reason"] for hop in exhausted["takeover_path"]} == {SWITCHED_OFF}
    for gate in ("kimi_gate", "deepseek_gate", "glm_gate"):
        assert f"{gate} unavailable ({SWITCHED_OFF})" in exhausted["failure_reason"]
    marker = store["results_dir"] / f"pr-{PR}-kimi_gate-chain-exhausted.json"
    assert json.loads(marker.read_text(encoding="utf-8"))["failure_reason"] == exhausted["failure_reason"]
    for provider in ("kimi", "deepseek-harness", "glm-harness"):
        assert not reachability_record(store, provider).exists()


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_existing_verdict_of_a_disabled_gate_on_this_head_is_not_redispatched(store, launches, verdict):
    kimi_record = write_result(store, "kimi_gate", decided_result(store, "kimi_gate", verdict))
    before = kimi_record.read_bytes()
    set_switches(kimi=False)

    result = request(["codex_gate", "kimi_gate"])

    # Neither kimi re-dispatched nor its seat handed to deepseek: the verdict stands, and a
    # standing FAIL is never laundered into a successor's PASS by switching kimi off.
    assert launches == ["codex_gate"]
    kimi_seat = seats(result)["kimi_gate"]
    assert kimi_seat["status"] == verdict
    assert SWITCHED_OFF in kimi_seat["provider_switch"]
    assert "takeover" not in kimi_seat
    assert kimi_record.read_bytes() == before
    assert not (store["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()
    assert not (store["requests_dir"] / f"pr-{PR}-deepseek_gate.json").exists()


def test_a_verdict_about_another_head_does_not_hold_a_disabled_seat(store, launches):
    write_result(store, "kimi_gate", decided_result(store, "kimi_gate", "pass", head=OTHER_HEAD))
    set_switches(kimi=False)

    result = request(["kimi_gate"])

    assert launches == ["deepseek_gate"]
    assert seats(result)["deepseek_gate"]["takeover_from"] == "kimi_gate"


def test_kimi_back_on_dispatches_kimi_and_its_result_replaces_the_switch_record(store, launches, monkeypatch):
    import gate_recorder

    # Round 1: kimi is the chain tail (no successor) and switched off.
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate")
    set_switches(kimi=False)
    first = request(["kimi_gate"])
    assert launches == []
    assert seats(first)["kimi_gate"]["reason"] == SWITCHED_OFF
    kimi_result = store["results_dir"] / f"pr-{PR}-kimi_gate.json"
    assert json.loads(kimi_result.read_text(encoding="utf-8"))["reason"] == SWITCHED_OFF
    assert not reachability_record(store, "kimi").exists()

    # Round 2: kimi back on, the standard chain (kimi now a chain key).
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN")
    set_switches(kimi=True)
    second = request(["kimi_gate"])
    assert launches == ["kimi_gate"]
    kimi_seat = seats(second)["kimi_gate"]
    assert kimi_seat["status"] == "requested"
    assert "takeover" not in kimi_seat and "takeover_path" not in kimi_seat

    # The kimi run's own result -- here a less-decided outage -- replaces the switch record.
    request_payload = json.loads((store["requests_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
    gate_recorder.record_failure(
        gate="kimi_gate", pr_number=PR, pr_id="",
        result={"reason": "harness_lane_dispatch_error", "reason_detail": "lane crashed",
                "duration_seconds": 1.0, "partial_output_lines": 0, "runner_pid": 1},
        request_payload=request_payload,
        requests_dir=store["requests_dir"], results_dir=store["results_dir"],
    )
    replaced = json.loads(kimi_result.read_text(encoding="utf-8"))
    assert replaced["status"] == "unavailable" and replaced["reason"] == "harness_lane_dispatch_error"


def test_empty_chain_kimi_off_books_codex_and_a_switch_record_for_kimi(store, launches, monkeypatch):
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    set_switches(kimi=False)

    result = request(["codex_gate", "kimi_gate"])

    assert launches == ["codex_gate"]
    kimi_seat = seats(result)["kimi_gate"]
    assert kimi_seat["status"] == "not_executable" and kimi_seat["reason"] == SWITCHED_OFF
    assert SWITCHED_OFF in kimi_seat["reason_detail"] and "family=kimi" in kimi_seat["reason_detail"]
    record = json.loads((store["results_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
    assert record["status"] == "not_executable" and record["reason"] == SWITCHED_OFF
    assert record["commit_sha"] == HEAD
    on_disk_request = json.loads((store["requests_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
    assert on_disk_request["status"] == "not_executable" and on_disk_request["reason"] == SWITCHED_OFF
    assert not reachability_record(store, "kimi").exists()


# ---------------------------------------------------------------------------
# Appendix C, Behavior: the remaining bullets
# ---------------------------------------------------------------------------

def test_a_profile_stack_seat_outside_the_chain_is_covered_the_same_way(store, launches, monkeypatch):
    """A seat that is not a chain key (here: a chain that does not name glm at all)."""
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "codex_gate,kimi_gate")
    set_switches(glm=False)

    result = request(["glm_gate"])

    assert launches == []
    assert seats(result)["glm_gate"]["reason"] == SWITCHED_OFF


def test_request_and_execute_runs_no_switched_off_seat(store, monkeypatch):
    """The executor runs only requests that went out: a switch record is never executed."""
    import review_gate_manager
    executed: List[str] = []

    def fake_execute_gate(self, *, gate, pr_number=None, pr_id=""):
        executed.append(gate)
        return {"gate": gate, "status": "completed", "commit_sha": HEAD, "blocking_findings": [],
                "contract_hash": "0123456789abcdef", "report_path": "r.md"}

    monkeypatch.setattr(review_gate_manager.ReviewGateManager, "execute_gate", fake_execute_gate)
    monkeypatch.setattr(review_gate_manager.ReviewGateManager, "_codex_headless_available", lambda self: True)
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    set_switches(kimi=False)

    outcome = manager().request_and_execute(
        pr_number=PR, branch="feat/switch", review_stack=["codex_gate", "kimi_gate"], risk_class="medium",
        changed_files=["scripts/x.py"], mode="per_pr", dispatch_id="20261015-ps-c-test",
    )

    assert executed == ["codex_gate"]
    kimi_entry = next(g for g in outcome["gates"] if g["gate"] == "kimi_gate")
    assert kimi_entry["reason"] == SWITCHED_OFF and kimi_entry["passed"] is False
    assert outcome["has_required_failure"] is True


# ---------------------------------------------------------------------------
# Fix round 2 (PR #2058, codex finding 1): a standing verdict passes the same checks a fresh
# result passes, against the head the executor observes
# ---------------------------------------------------------------------------

@pytest.fixture
def executor(store, launches, monkeypatch):
    """``request_and_execute`` on kimi alone, with the executor's run patched: it records the
    gate it would have run and launches nothing. ``lookups`` counts every PR-head lookup, per
    module, so a test can pin that the call looked the head up once."""
    import gate_recorder
    import gate_request_handler
    import review_gate_manager
    executed: List[str] = []
    lookups = {"executor": 0, "handler": 0}

    def fake_execute_gate(self, *, gate, pr_number=None, pr_id=""):
        executed.append(gate)
        return {"gate": gate, "status": "completed", "commit_sha": HEAD, "blocking_findings": []}

    def executor_head(pr_number):
        lookups["executor"] += 1
        return HEAD

    def handler_head(pr_number):
        lookups["handler"] += 1
        return HEAD

    monkeypatch.setattr(review_gate_manager.ReviewGateManager, "execute_gate", fake_execute_gate)
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", executor_head)
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", handler_head)
    monkeypatch.setenv("VNX_CODEX_HEADLESS_ENABLED", "1")
    set_switches(kimi=False)

    def run():
        outcome = manager().request_and_execute(
            pr_number=PR, branch="feat/switch", review_stack=["kimi_gate"], risk_class="medium",
            changed_files=["scripts/x.py"], mode="per_pr", dispatch_id="20261015-ps-c-r2-test",
        )
        [entry] = [g for g in outcome["gates"] if g["gate"] == "kimi_gate"]
        return outcome, entry

    return {"run": run, "executed": executed, "lookups": lookups, "launches": launches}


def assert_booked_switched_off(store, outcome, entry, refused: str) -> Dict[str, Any]:
    """The seat did not pass and is booked not_executable/provider_switched_off."""
    assert entry["passed"] is False
    assert entry["execution_status"] == "not_executable"
    assert entry["reason"] == SWITCHED_OFF
    assert outcome["has_required_failure"] is True
    request_record = json.loads((store["requests_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
    assert request_record["status"] == "not_executable" and request_record["reason"] == SWITCHED_OFF
    assert request_record["commit_sha"] == HEAD
    assert refused in request_record["standing_verdict_refused"]
    return request_record


@pytest.mark.parametrize("missing", ["contract_hash", "report_path"])
def test_a_standing_pass_without_evidence_does_not_pass(store, executor, monkeypatch, missing):
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    record = decided_result(store, "kimi_gate", "pass")
    record[missing] = ""
    on_disk = write_result(store, "kimi_gate", record)
    before = on_disk.read_bytes()

    outcome, entry = executor["run"]()

    assert_booked_switched_off(store, outcome, entry, "evidence incomplete")
    assert executor["executed"] == [] and executor["launches"] == []
    # The guard leaves the seat's own record standing on disk; only the request is booked.
    assert on_disk.read_bytes() == before


def test_a_standing_pass_whose_report_file_is_gone_does_not_pass(store, executor, monkeypatch):
    """The result writer's own check (gate_result_parser.evidence_error): the report is on disk."""
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    record = decided_result(store, "kimi_gate", "pass")
    Path(record["report_path"]).unlink()
    write_result(store, "kimi_gate", record)

    outcome, entry = executor["run"]()

    assert_booked_switched_off(store, outcome, entry, "report_path file does not exist")


def test_a_standing_pass_about_another_head_than_the_executor_observes_does_not_pass(
    store, executor, monkeypatch,
):
    """Codex's reproduction: the handler's own lookup named the record's head, the executor's
    named another. One lookup now decides both, and it is the executor's."""
    import gate_request_handler
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda pr_number: OTHER_HEAD)
    write_result(store, "kimi_gate", decided_result(store, "kimi_gate", "pass", head=OTHER_HEAD))

    outcome, entry = executor["run"]()

    assert_booked_switched_off(store, outcome, entry, "head binding mismatch")
    assert executor["lookups"]["executor"] == 1


def test_a_complete_standing_pass_on_the_observed_head_stands(store, executor, monkeypatch):
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    on_disk = write_result(store, "kimi_gate", decided_result(store, "kimi_gate", "pass"))
    before = on_disk.read_bytes()

    outcome, entry = executor["run"]()

    assert entry["passed"] is True
    assert entry["execution_status"] == "pass"
    assert outcome["has_required_failure"] is False
    assert SWITCHED_OFF in entry["detail"]["provider_switch"]
    assert executor["executed"] == [] and executor["launches"] == []
    assert on_disk.read_bytes() == before
    assert not (store["requests_dir"] / f"pr-{PR}-kimi_gate.json").exists()
    # One head lookup for the whole call, shared by the request round and the executor.
    assert executor["lookups"] == {"executor": 1, "handler": 0}


def test_a_standing_verdict_on_an_unknown_head_does_not_stand(store, executor, monkeypatch):
    """The executor lets an unknown binding through for a result it just produced; a verdict
    reused instead of a run stands only when it provably names the observed head."""
    import gate_recorder
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    monkeypatch.setattr(gate_recorder, "get_pr_head_sha", lambda pr_number: "")
    write_result(store, "kimi_gate", decided_result(store, "kimi_gate", "pass"))

    outcome, entry = executor["run"]()

    assert entry["passed"] is False and entry["reason"] == SWITCHED_OFF
    assert outcome["has_required_failure"] is True


def test_a_chain_seat_whose_standing_pass_lacks_evidence_is_booked_not_passed_over(
    store, executor, monkeypatch,
):
    """With the default chain kimi is a chain key. Its own decided verdict on this head keeps the
    seat (no successor reads in its place); a verdict that does not stand is booked, never a
    pass and never handed to deepseek."""
    record = decided_result(store, "kimi_gate", "pass")
    record["contract_hash"] = ""
    write_result(store, "kimi_gate", record)

    outcome, entry = executor["run"]()

    assert executor["launches"] == [] and executor["executed"] == []
    assert_booked_switched_off(store, outcome, entry, "evidence incomplete")
    assert not (store["requests_dir"] / f"pr-{PR}-deepseek_gate.json").exists()


def test_request_time_glm_and_deepseek_are_classified_by_name(store):
    """The direct request methods (reached when the switch flips between the seat decision and
    the request) book provider_switched_off, never gate_runner_missing."""
    set_switches(glm=False, deepseek=False)
    m = manager()
    for gate, method in (("glm_gate", m._request_glm), ("deepseek_gate", m._request_deepseek)):
        payload = method(PR, "feat/switch", "medium", ["scripts/x.py"], "per_pr", "20261015-ps-c-test")
        assert payload["status"] == "not_executable", gate
        assert payload["reason"] == SWITCHED_OFF, gate
        record = json.loads((store["results_dir"] / f"pr-{PR}-{gate}.json").read_text(encoding="utf-8"))
        assert record["reason"] == SWITCHED_OFF, gate


def test_request_time_kimi_goes_through_the_availability_helper(store):
    set_switches(kimi=False)
    payload = manager()._request_kimi(PR, "feat/switch", "medium", ["scripts/x.py"], "per_pr", "")
    assert payload["status"] == "not_executable" and payload["reason"] == SWITCHED_OFF


def test_classify_unavailable_names_the_switch_never_gate_runner_missing(store):
    set_switches(kimi=False, glm=False, deepseek=False)
    m = manager()
    for gate in ("kimi_gate", "glm_gate", "deepseek_gate"):
        reason, detail = m._classify_unavailable(gate)
        assert reason == SWITCHED_OFF, gate
        assert detail.startswith(f"{SWITCHED_OFF}: {gate}"), detail
    assert m._classify_unavailable("not_a_registered_gate")[0] == "unsupported_gate_type"
    set_switches()
    assert m._classify_unavailable("glm_gate")[0] == "gate_runner_missing"


def test_gate_is_available_false_for_a_disabled_harness_gate(store):
    import gate_recorder
    set_switches(kimi=False)
    assert gate_recorder.gate_is_available("kimi_gate", state_dir=store["state_dir"]) is False
    assert gate_recorder.gate_is_available("glm_gate", state_dir=store["state_dir"]) is True
    m = manager()
    assert m._kimi_gate_available() is False
    assert m._glm_gate_available() is True and m._deepseek_gate_available() is True
    set_switches(kimi=True)
    assert m._kimi_gate_available() is True


def test_an_unreadable_switch_file_makes_every_harness_gate_unavailable(store):
    switch_file().parent.mkdir(parents=True, exist_ok=True)
    switch_file().write_text("{broken", encoding="utf-8")
    m = manager()
    assert (m._kimi_gate_available(), m._glm_gate_available(), m._deepseek_gate_available()) == (False, False, False)
    reason, detail = m._classify_unavailable("kimi_gate")
    assert reason == SWITCHED_OFF and "source=unreadable" in detail


def test_an_override_brake_refuses_the_seat(store, launches, monkeypatch):
    monkeypatch.setenv("VNX_OVERRIDE_PROVIDER_KIMI", "off")
    result = request(["kimi_gate"])
    assert launches == ["deepseek_gate"]
    assert "source=override" in seats(result)["deepseek_gate"]["failure_reason"]


def test_codex_and_tooling_gates_are_never_refused(store):
    """Decision 2b: codex is untouched, also when the switch file is unreadable."""
    import gate_recorder
    switch_file().parent.mkdir(parents=True, exist_ok=True)
    switch_file().write_text("{broken", encoding="utf-8")
    for gate in ("codex_gate", "ci_gate", "wiring_gate", "claude_github_optional"):
        assert gate_recorder.gate_switch_refusal(gate, state_dir=store["state_dir"]) is None, gate
    assert gate_recorder.gate_switch_refusal("not_a_registered_gate") is None


def test_switch_record_is_classified_by_name_never_lane_exhausted(store):
    """An operator reason that reads like a quota message must not make the walk treat the
    record as an exhausted lane."""
    m = manager()
    record = {
        "gate": "kimi_gate", "pr_number": PR, "status": "not_executable", "reason": SWITCHED_OFF,
        "reason_detail": "provider_switched_off: kimi_gate family=kimi source=global reason='insufficient balance'",
        "residual_risk": "You've reached your usage limit for this billing cycle",
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    assert m._classify_review_seat_failure(record) == "switched_off"


def test_a_switch_record_does_not_hold_the_seat_once_the_family_is_back_on(store, launches):
    write_result(store, "kimi_gate", {
        "gate": "kimi_gate", "pr_number": PR, "status": "not_executable", "reason": SWITCHED_OFF,
        "reason_detail": "provider_switched_off: kimi_gate family=kimi", "commit_sha": HEAD,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    result = request(["kimi_gate"])
    assert launches == ["kimi_gate"]
    assert "takeover" not in seats(result)["kimi_gate"]


def test_a_switch_refusal_never_reaches_the_reachability_record(store, launches, monkeypatch):
    """Switch off, refused call, switch on: the next request proceeds with no cooldown and no
    unreachable fact, because the refusal was never booked as one."""
    import provider_reachability
    monkeypatch.setenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", "")
    set_switches(kimi=False)
    request(["kimi_gate"])
    assert provider_reachability.get("kimi", state_dir=store["state_dir"]).state.value == "unmeasured"
    assert not (store["state_dir"] / "provider_reachability").exists()
    monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN")
    set_switches(kimi=True)
    request(["kimi_gate"])
    assert launches == ["kimi_gate"]
