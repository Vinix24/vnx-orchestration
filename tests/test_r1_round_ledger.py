"""R1 (track review-stopregel-en-lichte-stoelen): the round ledger counts and refuses.

Plan: ``claudedocs/specs/review-stopregel-en-lichte-stoelen/plan.md``, section 1
("Tellen en weigeren"), dispatch 20261018-rs-r1-rondegrootboek. What is pinned here:

- the red runs, one per decision path, each failing on ``origin/main`` on behaviour
  (the ledger is written as plain NDJSON and every decision is read back through an entry
  point that exists there): a second run on a head with a verdict is refused, in every
  seat; a codex-seat run on a PR at B3.1 and at B3.2 is refused; a PASS that is not in the
  ledger does not count at the merge door; a PR without any ledger line is refused at the
  door with the rebuild command, and passes after the rebuild; the dispatch door refuses a
  build on a capped PR's branch; ``--override-b3`` passes and is recorded; a takeover in
  the codex seat counts as codex; a blocker of the second seat does not count; a failed
  ledger write publishes no verdict;
- the writer: the line lands before the record, carries the key and the verdict, refuses a
  second verdict on the same key, and leaves a rewrite of the same verdict alone;
  ``head_sha`` is in the ``review_gate_result`` receipt;
- the rebuild on the real reports of #2080 and #2089 (``tests/fixtures/review_rounds``, copied
  from the vnx-dev store with only what the rebuild reads: the report headers and findings, the
  PRs' ``review_gate_request``/``review_gate_result`` receipts and their result records): #2080 falls under B3.2
  in round 5 (sum 6), #2089 under B3.1 in round 1; a second rebuild adds nothing.

No provider lane is ever reached: the request round requests and never runs, and where a
call runs its seats, ``execute_gate`` is the edge and is replaced (a refused seat that
reaches it fails the test). Nothing leaves ``tmp_path``.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry
from review_seat_support import no_config_store, pin_review_stack

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review_rounds"
OLD_HEADS = [c * 40 for c in "abcdef0123"]
CLEAN_REPORT = "## Findings\n\nBlocking count: 0\nAdvisory count: 0\n"


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_recorder
    import gate_request_handler
    import review_gate_manager

    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    monkeypatch.setattr(
        gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True,
    )
    receipts: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        review_gate_manager, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event": event, **fields}),
    )
    monkeypatch.setattr(
        gate_recorder, "emit_governance_receipt",
        lambda event, **fields: receipts.append({"event": event, **fields}),
    )
    store["receipts"] = receipts
    store["ledger"] = store["state_dir"] / "review_gates" / "rounds.ndjson"
    store["headless"] = store["reports_dir"] / "headless"
    store["headless"].mkdir(parents=True, exist_ok=True)
    return store


# ---------------------------------------------------------------------------
# Plain helpers: the ledger as NDJSON, results through the real writers
# ---------------------------------------------------------------------------


def _line(
    seat: str, head: str, verdict: str, blockers: int, *, at: int, reviewer: Optional[str] = None,
    takeover_from: str = "", pr: int = PR, branch: str = BRANCH, report: str = "",
) -> Dict[str, Any]:
    return {
        "project_id": "", "pr_number": pr, "head_sha": head, "seat": seat,
        "reviewer": reviewer or seat, "takeover_from": takeover_from, "verdict": verdict,
        "blocking_count": blockers,
        "report_path": report or f"/r/20261017-{at:06d}-HEADLESS-{reviewer or seat}-pr-{pr}-x.md",
        "dispatch_id": f"d-{at}", "recorded_at": f"2026-10-17T{at // 60:02d}:{at % 60:02d}:00Z",
        "branch": branch, "source": "gate",
    }


def _codex_rounds(*blockers: int, pr: int = PR, reviewer: str = "codex_gate") -> List[Dict[str, Any]]:
    """One codex-seat FAIL (or PASS on zero) per old head, in order."""
    return [
        _line("codex_gate", OLD_HEADS[index], "fail" if count else "pass", count, at=index + 1, pr=pr,
              reviewer=reviewer, takeover_from="codex_gate" if reviewer != "codex_gate" else "")
        for index, count in enumerate(blockers)
    ]


def _write_ledger(path: Path, lines: List[Dict[str, Any]], *, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + extra, encoding="utf-8")


def _ledger_lines(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


def _report(directory: Path, gate: str, *, tag: str, pr: int = PR, text: str = CLEAN_REPORT) -> Path:
    path = directory / f"20261018-1000{tag}-HEADLESS-{gate}-pr-{pr}-{tag}ab.md"
    path.write_text(
        f"# {gate} — Headless Gate Report\n\n**PR**: {pr}\n**Branch**: {BRANCH}\n**Gate**: {gate}\n"
        f"**Generated**: 2026-10-18T10:00:{tag}Z\n\n---\n\n{text}",
        encoding="utf-8",
    )
    return path


def _verdict(
    store, gate: str, *, tag: str = "01", head: str = HEAD_SHA, blocking: int = 0, seat: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A decided result payload as gate_artifacts builds it, its report on disk."""
    report = _report(store["headless"], gate, tag=tag)
    payload: Dict[str, Any] = {
        "gate": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed",
        "summary": f"{gate} execution completed successfully", "contract_hash": f"{gate}-{tag}",
        "report_path": str(report), "findings": [],
        "blocking_findings": [{"severity": "blocking", "message": f"defect {n}"} for n in range(blocking)],
        "advisory_findings": [], "commit_sha": head, "branch": BRANCH,
        "dispatch_id": f"20261018-r1-{gate}-{tag}", "recorded_at": f"2026-10-18T10:00:{tag}Z",
    }
    payload["seat"] = seat or gate
    payload.update(extra)
    return payload


def _write_result(store, gate: str, payload: Dict[str, Any]):
    import gate_recorder

    return gate_recorder.write_result_guarded(
        store["results_dir"] / f"pr-{PR}-{gate}.json", payload, gate=gate, pr_ref=str(PR),
    )


def _request(store, stack: List[str], **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    import review_gate_manager

    result = review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=stack, risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261018-rs-r1-test", **kwargs,
    )
    return {str(payload.get("seat") or payload["gate"]): payload for payload in result["requested"]}


def _rc(main, argv: List[str]) -> int:
    """A CLI's exit code, also when argparse exits (an unknown flag on old code is a code, not a crash)."""
    try:
        return main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)


# ---------------------------------------------------------------------------
# Red runs: where a run starts (gate_request_handler)
# ---------------------------------------------------------------------------


class TestRefusalWhereARunStarts:
    def test_a_second_run_on_a_head_with_a_verdict_is_refused_in_every_seat(self, env):
        _write_ledger(env["ledger"], [
            _line("codex_gate", HEAD_SHA, "fail", 1, at=1),
            _line("kimi_gate", HEAD_SHA, "pass", 0, at=2),
        ])

        seats = _request(env, ["codex_gate", "kimi_gate"])

        for seat in ("codex_gate", "kimi_gate"):
            assert seats[seat]["status"] == "blocked", seats[seat]
            assert seats[seat]["reason"] == "verdict_on_head"
            assert "second run on a head with a verdict is refused" in seats[seat]["reason_detail"]
        assert not (env["requests_dir"] / f"pr-{PR}-codex_gate.json").exists(), "nothing was requested"

    def test_a_head_without_a_verdict_may_run_again(self, env):
        _write_ledger(env["ledger"], _codex_rounds(1, 1))

        seats = _request(env, ["codex_gate"])

        assert seats["codex_gate"]["status"] == "requested"
        assert seats["codex_gate"]["commit_sha"] == HEAD_SHA

    def test_a_standing_pass_is_reported_and_never_run_again(self, env, monkeypatch):
        import gate_executor
        import review_gate_manager

        payload = _verdict(env, "codex_gate")
        _write_result(env, "codex_gate", payload)

        def no_run(self, **kwargs):
            raise AssertionError(f"the lane was reached for {kwargs.get('gate')}")

        monkeypatch.setattr(gate_executor.GateExecutorMixin, "execute_gate", no_run)
        result = review_gate_manager.ReviewGateManager().request_and_execute(
            pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
            changed_files=["x.py"], mode="per_pr",
        )

        entry = result["gates"][0]
        assert entry["passed"] is True and result["has_required_failure"] is False
        assert "second run on a head with a verdict is refused" in entry["detail"]["redispatch_refused"]

    def test_a_codex_seat_run_on_a_pr_at_b3_1_is_refused(self, env):
        _write_ledger(env["ledger"], _codex_rounds(3))

        seats = _request(env, ["codex_gate"])

        assert seats["codex_gate"]["status"] == "blocked", seats["codex_gate"]
        assert seats["codex_gate"]["reason"] == "b3_cap"
        detail = seats["codex_gate"]["reason_detail"]
        assert "B3.1" in detail and "round 1 had 3 blockers" in detail and '--override-b3 --reason "..."' in detail

    def test_a_codex_seat_run_on_a_pr_at_b3_2_is_refused_on_every_head(self, env):
        _write_ledger(env["ledger"], _codex_rounds(2, 1, 1, 1, 1))

        seats = _request(env, ["codex_gate"])

        assert seats["codex_gate"]["reason"] == "b3_cap", seats["codex_gate"]
        detail = seats["codex_gate"]["reason_detail"]
        assert "B3.2" in detail and "sum over 5 rounds is 6" in detail and "(2, 1, 1, 1, 1)" in detail

    def test_b3_1_goes_first_when_both_rules_hold_in_one_round(self, env):
        _write_ledger(env["ledger"], _codex_rounds(1, 1, 1, 3))

        detail = _request(env, ["codex_gate"])["codex_gate"]["reason_detail"]

        assert "under B3.1" in detail and "round 4 had 3 blockers" in detail

    def test_a_takeover_in_the_codex_seat_counts_as_codex(self, env):
        _write_ledger(env["ledger"], _codex_rounds(3, reviewer="deepseek_gate"))

        seats = _request(env, ["codex_gate"])

        assert seats["codex_gate"]["reason"] == "b3_cap", seats["codex_gate"]

    def test_a_blocker_of_the_second_seat_does_not_count(self, env):
        _write_ledger(env["ledger"], _codex_rounds(1, 1) + [
            _line("kimi_gate", OLD_HEADS[0], "fail", 3, at=10, reviewer="deepseek_gate", takeover_from="kimi_gate"),
            _line("kimi_gate", OLD_HEADS[1], "fail", 3, at=11, reviewer="deepseek_gate", takeover_from="kimi_gate"),
        ])

        seats = _request(env, ["codex_gate"])
        assert seats["codex_gate"]["status"] == "requested", seats["codex_gate"]
        written, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", blocking=1))

        assert landed is True
        codex = [line for line in _ledger_lines(env["ledger"]) if line["seat"] == "codex_gate"]
        assert [line["blocking_count"] for line in codex] == [1, 1, 1], "the codex verdict was counted"

    def test_an_unreadable_ledger_refuses_the_round_until_it_is_rebuilt(self, env):
        _write_ledger(env["ledger"], _codex_rounds(1), extra='{"project_id":"","pr_number":%d,"head_sh' % PR)

        seats = _request(env, ["codex_gate", "kimi_gate"])

        for seat in ("codex_gate", "kimi_gate"):
            assert seats[seat]["reason"] == "round_ledger_unreadable", seats[seat]
            assert f"vnx review rounds rebuild --pr {PR}" in seats[seat]["reason_detail"]

    def test_the_override_passes_and_is_recorded_on_the_request_the_line_and_the_receipt(self, env):
        import gate_recorder
        import review_gate_manager

        _write_ledger(env["ledger"], _codex_rounds(2, 1, 1, 1, 1))

        rc = _rc(review_gate_manager.main, [
            "request", "--pr", str(PR), "--branch", BRANCH, "--review-stack", "codex_gate",
            "--changed-files", "x.py", "--override-b3", "--reason", "operator: last fix is one line",
        ])

        assert rc == 0
        request = json.loads((env["requests_dir"] / f"pr-{PR}-codex_gate.json").read_text())
        assert request["status"] == "requested"
        assert request["override_b3"]["reason"] == "operator: last fix is one line"
        assert request["override_b3"]["rule"] == "B3.2"
        result = gate_recorder.stamp_request_identity(_verdict(env, "codex_gate", seat=None), request)
        _write_result(env, "codex_gate", result)
        line = _ledger_lines(env["ledger"])[-1]
        assert line["head_sha"] == HEAD_SHA and line["override"]["reason"] == "operator: last fix is one line"
        receipt = [r for r in env["receipts"] if r["event"] == "review_gate_result"][-1]
        assert receipt["override_b3"]["reason"] == "operator: last fix is one line"
        assert receipt["head_sha"] == HEAD_SHA

    def test_an_override_without_a_reason_is_refused(self, env):
        import review_gate_manager

        rc = _rc(review_gate_manager.main, [
            "request", "--pr", str(PR), "--branch", BRANCH, "--review-stack", "codex_gate",
            "--changed-files", "x.py", "--override-b3",
        ])

        assert rc == 2
        assert not (env["requests_dir"] / f"pr-{PR}-codex_gate.json").exists()


# ---------------------------------------------------------------------------
# Red runs and pins: the writer books the line before the record
# ---------------------------------------------------------------------------


class TestTheWriter:
    def test_a_verdict_lands_in_the_ledger_with_its_key_and_head_in_the_receipt(self, env):
        payload = _verdict(env, "codex_gate", blocking=2)

        _write_result(env, "codex_gate", payload)

        (line,) = _ledger_lines(env["ledger"])
        assert (line["pr_number"], line["head_sha"], line["seat"], line["reviewer"]) == (PR, HEAD_SHA, "codex_gate", "codex_gate")
        assert (line["verdict"], line["blocking_count"]) == ("fail", 2)
        assert line["report_path"] == payload["report_path"] and line["dispatch_id"] == payload["dispatch_id"]
        receipt = [r for r in env["receipts"] if r["event"] == "review_gate_result"][-1]
        assert receipt["head_sha"] == HEAD_SHA and receipt["seat"] == "codex_gate"

    def test_a_takeover_verdict_is_booked_in_the_seat_it_read(self, env):
        payload = _verdict(
            env, "deepseek_gate", seat="codex_gate", takeover=True, takeover_from="codex_gate",
            takeover_path=[{"gate": "codex_gate", "reason": "lane_exhausted", "status": "unavailable"}],
        )

        _write_result(env, "deepseek_gate", payload)

        (line,) = _ledger_lines(env["ledger"])
        assert (line["seat"], line["reviewer"], line["takeover_from"]) == ("codex_gate", "deepseek_gate", "codex_gate")

    def test_a_failed_ledger_write_publishes_no_verdict(self, env):
        env["ledger"].mkdir(parents=True)

        with pytest.raises(OSError):
            _write_result(env, "codex_gate", _verdict(env, "codex_gate"))

        assert not (env["results_dir"] / f"pr-{PR}-codex_gate.json").exists()
        assert not [r for r in env["receipts"] if r["event"] == "review_gate_result"]

    def test_a_failed_ledger_write_fails_the_raising_writer_too(self, env):
        import gate_depth
        import gate_recorder

        env["ledger"].mkdir(parents=True)
        path = env["results_dir"] / f"pr-{PR}-codex_gate.json"

        with pytest.raises(OSError):
            gate_recorder.record_terminal_result(
                gate="codex_gate", pr_id=str(PR), result_path=path, payload=_verdict(env, "codex_gate"),
                execution_depth=gate_depth.single_shot_depth(400, False),
            )

        assert not path.exists()

    def test_a_second_verdict_on_the_same_head_and_seat_is_refused_at_the_write(self, env):
        first = _verdict(env, "codex_gate", tag="01", blocking=1)
        _write_result(env, "codex_gate", first)

        on_disk, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="02"))

        assert landed is False
        assert on_disk["report_path"] == first["report_path"], "the first verdict stands"
        assert len(_ledger_lines(env["ledger"])) == 1

    def test_a_rewrite_of_the_same_verdict_is_no_second_verdict(self, env):
        payload = _verdict(env, "kimi_gate")
        _write_result(env, "kimi_gate", payload)

        _on_disk, landed = _write_result(env, "kimi_gate", dict(payload, failure_reason="annotated", takeover=False))

        assert landed is True
        assert len(_ledger_lines(env["ledger"])) == 1

    def test_a_codex_verdict_on_a_pr_at_the_cap_is_not_booked_without_an_override(self, env):
        _write_ledger(env["ledger"], _codex_rounds(3))

        _on_disk, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate"))

        assert landed is False
        assert len(_ledger_lines(env["ledger"])) == 1

    def test_no_verdict_writes_no_line(self, env):
        payload = dict(_verdict(env, "codex_gate"), status="unavailable", reason="lane_exhausted", contract_hash="")

        _write_result(env, "codex_gate", payload)

        assert _ledger_lines(env["ledger"]) == []


# ---------------------------------------------------------------------------
# Red runs: the merge door (review_seat_evaluation via closure_verifier and pr_merge)
# ---------------------------------------------------------------------------


@pytest.fixture
def door(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state = data_dir / "state"
    for directory in (state / "review_gates" / "results", state / "review_gates" / "requests",
                      data_dir / "unified_reports" / "headless", data_dir / "dispatches"):
        directory.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "PROJECT_ROOT": tmp_path, "VNX_HOME": review_stack_support.VNX_ROOT, "VNX_DATA_DIR": data_dir,
        "VNX_STATE_DIR": state, "VNX_DISPATCH_DIR": data_dir / "dispatches", "VNX_LOGS_DIR": data_dir / "logs",
        "VNX_PIDS_DIR": data_dir / "pids", "VNX_LOCKS_DIR": data_dir / "locks",
        "VNX_REPORTS_DIR": data_dir / "unified_reports", "VNX_DB_DIR": data_dir / "database",
    }.items():
        monkeypatch.setenv(key, str(value))
    no_config_store(monkeypatch)
    pin_review_stack(monkeypatch, "codex_gate,kimi_gate")
    return {
        "data": data_dir, "state": state, "results": state / "review_gates" / "results",
        "ledger": state / "review_gates" / "rounds.ndjson", "headless": data_dir / "unified_reports" / "headless",
        "receipts": state / "t0_receipts.ndjson",
    }


def _door_pass(door, gate: str, *, tag: str) -> Dict[str, Any]:
    report = _report(door["headless"], gate, tag=tag)
    record = {
        "gate": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed", "commit_sha": HEAD_SHA,
        "branch": BRANCH, "contract_hash": f"sha256:{gate}", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [], "evidence_source": "live", "test_run": False,
        "dispatch_id": f"20261018-{gate}", "recorded_at": f"2026-10-18T10:00:{tag}Z",
    }
    (door["results"] / f"pr-{PR}-{gate}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _line_for(record: Dict[str, Any], *, at: int) -> Dict[str, Any]:
    return _line(record["gate"], HEAD_SHA, "pass", 0, at=at, report=record["report_path"])


def _check(door, **kwargs: Any) -> Dict[str, Any]:
    import closure_verifier

    return closure_verifier.check_review_gate_for_merge(
        str(PR), "codex_gate", door["results"], branch=BRANCH, head_sha=HEAD_SHA, **kwargs,
    )


_PR_DATA = {
    "number": PR, "title": "R1", "state": "OPEN", "isDraft": False, "headRefName": BRANCH,
    "baseRefName": "main", "headRefOid": HEAD_SHA, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
}


def _stub_door_edges(monkeypatch, door) -> None:
    import file_scope_overlap
    import gate_obligations
    import pr_merge

    def go(message: str) -> Dict[str, Any]:
        return {"verdict": "GO", "message": message, "overridden": False, "override_reason": None}

    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (go("CI groen"), dict(_PR_DATA)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: dict(_PR_DATA))
    monkeypatch.setattr(pr_merge, "_resolve_override_reason", lambda reason: reason)
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: go("adr"))
    monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: go("contract"))
    monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", lambda *a, **k: go("protection"))
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
    monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, h="": (True, ""))
    monkeypatch.setattr(file_scope_overlap, "warn_overlaps", lambda *a, **k: [])
    gate_obligations.register_obligation(
        door["state"], dispatch_id="20261018-rs-r1-door", gate="codex_gate", pr_number=PR, pr_id=str(PR), branch=BRANCH,
    )


class TestTheMergeDoor:
    def test_two_passes_in_the_ledger_pass_the_door(self, door):
        codex, kimi = _door_pass(door, "codex_gate", tag="01"), _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], [_line_for(codex, at=1), _line_for(kimi, at=2)])

        verdict = _check(door)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["round_ledger"]["lines"] == 2

    def test_a_pass_that_is_not_in_the_ledger_does_not_count(self, door):
        _door_pass(door, "codex_gate", tag="01")
        kimi = _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], [_line_for(kimi, at=2)])

        verdict = _check(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "de PASS van codex_gate (seat codex_gate)" in verdict["message"]
        assert "staat niet in het rondegrootboek" in verdict["message"]

    def test_a_pr_without_ledger_lines_is_refused_with_the_rebuild_and_passes_after_it(self, door):
        _door_pass(door, "codex_gate", tag="01")
        _door_pass(door, "kimi_gate", tag="02")
        door["ledger"].write_text("", encoding="utf-8")

        refused = _check(door)

        assert refused["verdict"] == "NO-GO", refused["message"]
        assert f"vnx review rounds rebuild --pr {PR}" in refused["message"]
        import review_rounds_rebuild

        rebuilt = review_rounds_rebuild.rebuild(PR, data_dir=door["data"])
        assert len(rebuilt.added) == 2
        assert _check(door)["verdict"] == "GO"

    def test_the_codex_seat_of_a_pr_at_the_cap_is_not_filled(self, door):
        codex, kimi = _door_pass(door, "codex_gate", tag="01"), _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], _codex_rounds(2, 1, 1, 1, 1) + [_line_for(codex, at=20), _line_for(kimi, at=21)])

        verdict = _check(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "B3-cap" in verdict["message"] and "B3.2" in verdict["message"]
        assert "kimi_gate" in verdict["review_seats"]["filled_by"], "the second seat is not held by the cap"

    def test_vnx_gate_review_agrees_with_the_door(self, door):
        import forge_gate_publisher

        _door_pass(door, "codex_gate", tag="01")
        kimi = _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], [_line_for(kimi, at=2)])

        summary = forge_gate_publisher.review_verdict(PR, HEAD_SHA, results_dir=door["results"], branch=BRANCH)

        assert summary.conclusion != forge_gate_publisher.CONCLUSION_SUCCESS

    def test_the_door_override_passes_the_cap_and_lands_in_the_merge_receipt(self, door, monkeypatch):
        import pr_merge

        codex, kimi = _door_pass(door, "codex_gate", tag="01"), _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], _codex_rounds(3) + [_line_for(codex, at=20), _line_for(kimi, at=21)])
        _stub_door_edges(monkeypatch, door)

        refused = _rc(pr_merge.main, ["--pr", str(PR), "--dispatch-id", "20261018-rs-r1-door"])
        merged = _rc(pr_merge.main, [
            "--pr", str(PR), "--dispatch-id", "20261018-rs-r1-door",
            "--override-b3", "--reason", "B3.1 op ronde 1, fix is klein",
        ])

        assert (refused, merged) == (pr_merge.EXIT_ERROR, pr_merge.EXIT_OK)
        receipts = [json.loads(raw) for raw in door["receipts"].read_text().splitlines() if raw.strip()]
        merged_receipt = [r for r in receipts if r.get("event_type") == "pr_merged"][-1]
        override = merged_receipt["review_seats"]["round_ledger"]["override"]
        assert override == {"flag": "--override-b3", "reason": "B3.1 op ronde 1, fix is klein", "applied": True}

    def test_a_door_override_without_a_reason_is_refused(self, door, monkeypatch):
        import pr_merge

        _stub_door_edges(monkeypatch, door)

        assert _rc(pr_merge.main, ["--pr", str(PR), "--dispatch-id", "20261018-rs-r1-door", "--override-b3"]) == pr_merge.EXIT_ERROR

    def test_track_closure_keeps_the_rule_the_merge_was_judged_under(self, door):
        import closure_verifier
        from review_contract import Deliverable, ReviewContract

        _door_pass(door, "codex_gate", tag="01")
        _door_pass(door, "kimi_gate", tag="02")
        door["ledger"].write_text("", encoding="utf-8")
        contract = ReviewContract(
            pr_id=str(PR), pr_title="R1", feature_title="stopregel", branch=BRANCH, track="A", risk_class="low",
            merge_policy="standard", closure_stage="ready", deliverables=[Deliverable(category="impl", description="r1")],
            review_stack=["codex_gate", "kimi_gate"],
        )
        merged = {"event_type": "pr_merged", "pr_number": PR, "conclusion": "merged", "review_seats": {
            "rule": "every_seat_distinct_reviewer", "pr_id": str(PR), "head_sha": HEAD_SHA,
            "seats": ["codex_gate", "kimi_gate"], "source": "config"}}

        checks = closure_verifier._validate_review_evidence(contract, door["results"], branch=BRANCH, merge_record=merged)

        assert all(check.status == "PASS" for check in checks), [c.__dict__ for c in checks if c.status != "PASS"]


# ---------------------------------------------------------------------------
# Red run: the dispatch door's early warning
# ---------------------------------------------------------------------------


def _build_snapshot(tmp_path, *, work_ref: str):
    from dispatch_cli import build_runtime_snapshot, load_spec
    from dispatch_spec import Reject, validate

    data_dir = tmp_path / "vnx-data"
    bundle = data_dir / "dispatches" / "pending" / "20261018-staging-r1"
    bundle.mkdir(parents=True)
    instruction = bundle / "instruction.md"
    instruction.write_text("# Fix the last blocker\n\nRole: backend-developer\n\nFix it.\n", encoding="utf-8")
    spec = {
        "schema_version": 1, "project_id": "vnx-dev", "dispatch_id": "20261018-r1-build",
        "staging_id": "20261018-staging-r1", "instruction_file": str(instruction), "role": "backend-developer",
        "target_slot": "T1", "gate": "codex_gate", "dispatch_paths": [], "provider": "claude", "model": None,
        "deadline_seconds": 3600, "isolation": "worktree", "work_ref": work_ref,
    }
    spec_file = bundle / "dispatch-spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    vspec = validate(load_spec(spec_file), project_id="vnx-dev", repo_root=review_stack_support.VNX_ROOT)
    assert not isinstance(vspec, Reject), vspec
    return data_dir, vspec, spec_file, build_runtime_snapshot


class TestTheDispatchDoor:
    def test_a_build_on_the_branch_of_a_capped_pr_is_refused(self, tmp_path, monkeypatch):
        from dispatch_plan import compile_plan

        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        monkeypatch.delenv("VNX_OVERRIDE_B3_REASON", raising=False)
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=f"origin/{BRANCH}")
        _write_ledger(data_dir / "state" / "review_gates" / "rounds.ndjson", _codex_rounds(2, 1, 1, 1, 1))

        snapshot = build(vspec, data_dir=data_dir, spec_file=spec_file)

        (cap,) = [v for v in snapshot.constraint_verdicts if v.code == "b3-cap-reached"]
        assert cap.severity == "blocking"
        assert "B3.2" in cap.message and "(2, 1, 1, 1, 1)" in cap.message and "--override-b3" in cap.message
        plan = compile_plan(vspec, snapshot)
        assert getattr(plan, "code", "") == "b3-cap-reached"

    def test_the_override_turns_the_refusal_into_an_audited_warning(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=BRANCH)
        _write_ledger(data_dir / "state" / "review_gates" / "rounds.ndjson", _codex_rounds(3))

        snapshot = build(vspec, data_dir=data_dir, spec_file=spec_file)

        (applied,) = [v for v in snapshot.constraint_verdicts if v.code == "b3-cap-override-applied"]
        assert applied.severity == "warn" and applied.override_applied and "operator: one-line fix" in applied.message

    def test_a_branch_below_the_cap_is_not_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=BRANCH)
        _write_ledger(data_dir / "state" / "review_gates" / "rounds.ndjson", _codex_rounds(2, 1, 1))

        snapshot = build(vspec, data_dir=data_dir, spec_file=spec_file)

        assert not [v for v in snapshot.constraint_verdicts if v.code.startswith("b3-")]

    def test_the_cli_refuses_an_override_without_a_reason(self):
        import dispatch_cli

        assert _rc(dispatch_cli.main, ["--spec-file", "/nonexistent.json", "--override-b3"]) == 2


# ---------------------------------------------------------------------------
# The rebuild on the real reports of #2080 and #2089
# ---------------------------------------------------------------------------


@pytest.fixture
def real_store(tmp_path):
    """The fixture laid out as a project store (the repo ignores ``state/`` and ``unified_reports/``)."""
    store = tmp_path / "store"
    shutil.copytree(FIXTURE / "headless", store / "unified_reports" / "headless")
    shutil.copytree(FIXTURE / "results", store / "state" / "review_gates" / "results")
    shutil.copy(FIXTURE / "review_gate_receipts.ndjson", store / "state" / "t0_receipts.ndjson")
    shutil.copy(FIXTURE / "vnx-project-id", store / ".vnx-project-id")
    return store


class TestTheRebuildOnRealReports:
    def test_2080_falls_under_b3_2_in_round_5(self, real_store):
        import review_rounds_rebuild

        result = review_rounds_rebuild.rebuild(2080, data_dir=real_store)

        assert (result.cap.rule, result.cap.cap_round) == ("B3.2", 5)
        assert result.cap.blockers == (2, 1, 1, 1, 1, 1, 0)
        assert sum(result.cap.blockers[:5]) == 6
        assert len(result.added) == 14
        kimi_seat = [line for line in result.added if line.seat == "kimi_gate"]
        assert {(line.reviewer, line.takeover_from) for line in kimi_seat} == {("deepseek_gate", "kimi_gate")}
        assert [item.report for item in result.skipped] == ["20261016-173732-HEADLESS-codex_gate-pr-2080-8a9678.md"]

    def test_2089_falls_under_b3_1_in_round_1(self, real_store):
        import review_rounds_rebuild

        result = review_rounds_rebuild.rebuild(2089, data_dir=real_store)

        assert (result.cap.rule, result.cap.cap_round) == ("B3.1", 1)
        assert result.cap.blockers == (3, 1, 1, 1)
        second_seat = [line for line in result.added if line.seat == "kimi_gate"]
        assert [(line.verdict, line.blocking_count) for line in second_seat] == [("fail", 1), ("pass", 0)]

    def test_a_rebuild_never_duplicates_a_line(self, real_store):
        import review_rounds_rebuild

        first = review_rounds_rebuild.rebuild(2080, data_dir=real_store)
        ledger = real_store / "state" / "review_gates" / "rounds.ndjson"
        before = ledger.read_text(encoding="utf-8")
        second = review_rounds_rebuild.rebuild(2080, data_dir=real_store)

        assert len(first.added) == 14 and second.added == () and second.kept == 14
        assert ledger.read_text(encoding="utf-8") == before

    def test_the_lines_carry_the_store_s_project_id(self, real_store):
        import review_rounds_rebuild

        result = review_rounds_rebuild.rebuild(2089, data_dir=real_store)

        assert {line.project_id for line in result.added} == {"vnx-dev"}

    def test_the_cli_rebuilds_and_prints_the_count(self, real_store, capsys):
        import review_rounds_cli

        rc = review_rounds_cli.main(["rounds", "rebuild", "--pr", "2089", "--data-dir", str(real_store), "--json"])

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0 and payload["cap"]["rule"] == "B3.1" and len(payload["added"]) == 6

    def test_unreadable_lines_are_dropped_or_quarantined_and_the_pr_is_counted_again(self, real_store):
        import review_rounds
        import review_rounds_rebuild

        ledger = real_store / "state" / "review_gates" / "rounds.ndjson"
        ledger.write_text('{"project_id":"vnx-dev","pr_number":2089,"head_sh\nnot json at all\n', encoding="utf-8")

        result = review_rounds_rebuild.rebuild(2089, data_dir=real_store)

        assert (result.dropped, result.quarantined) == (1, 1)
        assert (ledger.parent / "rounds.ndjson.quarantine").read_text(encoding="utf-8") == "not json at all\n"
        assert review_rounds.read_ledger(ledger).bad == ()


# ---------------------------------------------------------------------------
# The count and the line, on their own
# ---------------------------------------------------------------------------


class TestTheCount:
    @pytest.mark.parametrize("blockers,rule,cap_round", [
        ((2, 1, 1, 1, 1), "B3.2", 5),
        ((2, 2, 2), "B3.2", 3),
        ((1, 1, 1, 3), "B3.1", 4),
        ((3,), "B3.1", 1),
        ((2, 2, 1, 0), "", 0),
        ((1, 1, 1, 1, 1, 0, 0), "", 0),
        ((1, 1, 1, 1, 1, 0, 1), "B3.2", 7),
    ])
    def test_b3_walks_the_rounds_in_order(self, blockers, rule, cap_round):
        import review_rounds

        lines = [review_rounds.RoundLine.from_dict(line) for line in _codex_rounds(*blockers)]
        cap = review_rounds.cap_state(lines, PR)

        assert (cap.rule, cap.cap_round) == (rule, cap_round)

    def test_the_cap_is_sticky_a_later_pass_does_not_lift_it(self):
        import review_rounds

        lines = [review_rounds.RoundLine.from_dict(line) for line in _codex_rounds(2, 1, 1, 1, 1, 0)]

        assert review_rounds.cap_state(lines, PR).rule == "B3.2"

    def test_a_returning_head_counts_once(self):
        import review_rounds

        lines = [review_rounds.RoundLine.from_dict(line) for line in _codex_rounds(2, 2)]
        lines.append(review_rounds.RoundLine.from_dict(_line("codex_gate", OLD_HEADS[0], "fail", 3, at=9)))

        assert review_rounds.cap_state(lines, PR).blockers == (2, 2)

    @pytest.mark.parametrize("broken", [
        {"pr_number": "2080"}, {"blocking_count": -1}, {"verdict": "maybe"}, {"head_sha": ""}, {"seat": 3},
        {"override": "yes"},
    ])
    def test_a_line_with_a_wrong_field_is_no_line(self, broken):
        import review_rounds

        with pytest.raises(ValueError):
            review_rounds.RoundLine.from_dict({**_line("codex_gate", HEAD_SHA, "pass", 0, at=1), **broken})

    def test_the_tiebreaker_seam_grants_nothing_in_r1(self):
        import review_rounds

        cap = review_rounds.cap_state([review_rounds.RoundLine.from_dict(line) for line in _codex_rounds(3)], PR)

        assert review_rounds.round_granted_past_cap(cap, head_sha=HEAD_SHA) is False
