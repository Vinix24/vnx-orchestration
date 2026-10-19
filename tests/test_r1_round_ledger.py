"""R1 (track review-stopregel-en-lichte-stoelen): the round ledger counts and refuses.

Plan: ``claudedocs/specs/review-stopregel-en-lichte-stoelen/plan.md``, section 1
("Tellen en weigeren", revision 4: no rebuild). Dispatches 20261018-rs-r1-rondegrootboek,
-r2b and -r3. What is pinned here:

- the red runs, one per decision path: a second run on a head with a verdict is refused, in
  every seat; a codex-seat run on a PR at B3.1 and at B3.2 is refused; a PASS that is not in the
  ledger does not count at the merge door; a PR that is not opened is refused at the door; the
  dispatch door refuses a build on a capped PR's branch; ``--override-b3`` passes and is
  recorded; a takeover in the codex seat counts as codex; a blocker of the second seat does not
  count; a failed ledger write publishes no verdict;
- the writer: the line lands before the record, carries the key and the verdict, refuses a
  second verdict on the same key, and leaves a rewrite of the same verdict alone;
  ``head_sha`` is in the ``review_gate_result`` receipt;
- round 2: the ledger is a regular file or absent at every reader and writer, and every append
  starts on a record boundary; a PR counts only once an ``opened`` line says the ledger holds
  its history;
- round 3 (the three codex blockers of round 2, all in the rebuild, which is gone): earlier
  history in any one source (a headless report, a result record under either name, a
  ``review_gate_result`` receipt) refuses the PR until the operator opens it with
  ``vnx review rounds open --pr N --reason``, a source that cannot be read is evidence, and a
  bad ledger line stops every PR it can touch while nothing removes it.

No provider lane is ever reached: the request round requests and never runs, and where a
call runs its seats, ``execute_gate`` is the edge and is replaced (a refused seat that
reaches it fails the test). Nothing leaves ``tmp_path``.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry
from review_seat_support import no_config_store, pin_review_stack

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


def _opened(pr: int = PR, *, project_id: str = "") -> Dict[str, Any]:
    """The line the gate writes for a PR without earlier reviews: the ledger holds its history from here."""
    return {
        "kind": "opened", "project_id": project_id, "pr_number": pr, "basis": "new", "evidence": 0,
        "recorded_at": "2026-10-17T00:00:00Z", "source": "gate",
    }


def _write_ledger(path: Path, lines: List[Dict[str, Any]], *, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + extra, encoding="utf-8")


def _all_lines(path: Path) -> List[Dict[str, Any]]:
    """Every line that parses (a torn fragment does not)."""
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    return [line for line in lines if isinstance(line, dict)]


def _ledger_lines(path: Path) -> List[Dict[str, Any]]:
    """The verdict lines (no ``kind``)."""
    return [line for line in _all_lines(path) if "kind" not in line]


def _opened_lines(path: Path) -> List[Dict[str, Any]]:
    return [line for line in _all_lines(path) if line.get("kind") == "opened"]


def _override_lines(path: Path) -> List[Dict[str, Any]]:
    return [line for line in _all_lines(path) if line.get("kind") == "override"]


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


def _request(store, stack: List[str], *, pr: int = PR, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    import review_gate_manager

    result = review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=pr, branch=BRANCH, review_stack=stack, risk_class="medium",
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

        assert _request(env, ["codex_gate"])["codex_gate"]["status"] == "requested", "the request opens the PR"
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

    def test_an_unreadable_ledger_refuses_the_round_and_names_the_way_out(self, env):
        _write_ledger(env["ledger"], _codex_rounds(1), extra='{"project_id":"","pr_number":%d,"head_sh' % PR)

        seats = _request(env, ["codex_gate", "kimi_gate"])

        for seat in ("codex_gate", "kimi_gate"):
            assert seats[seat]["reason"] == "round_ledger_unreadable", seats[seat]
            assert f"set {env['ledger']} aside, then reopen every open PR" in seats[seat]["reason_detail"]
            assert 'vnx review rounds open --pr N --reason "..."' in seats[seat]["reason_detail"]

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


def _door_pass(door, gate: str, *, tag: str, pr: int = PR) -> Dict[str, Any]:
    report = _report(door["headless"], gate, tag=tag, pr=pr)
    record = {
        "gate": gate, "pr_id": str(pr), "pr_number": pr, "status": "completed", "commit_sha": HEAD_SHA,
        "branch": BRANCH, "contract_hash": f"sha256:{gate}", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [], "evidence_source": "live", "test_run": False,
        "dispatch_id": f"20261018-{gate}", "recorded_at": f"2026-10-18T10:00:{tag}Z",
    }
    (door["results"] / f"pr-{pr}-{gate}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _line_for(record: Dict[str, Any], *, at: int) -> Dict[str, Any]:
    return _line(record["gate"], HEAD_SHA, "pass", 0, at=at, report=record["report_path"], pr=record["pr_number"])


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
        _write_ledger(door["ledger"], [_opened(), _line_for(codex, at=1), _line_for(kimi, at=2)])

        verdict = _check(door)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["round_ledger"]["lines"] == 2

    def test_a_pass_that_is_not_in_the_ledger_does_not_count(self, door):
        _door_pass(door, "codex_gate", tag="01")
        kimi = _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], [_opened(), _line_for(kimi, at=2)])

        verdict = _check(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "de PASS van codex_gate (seat codex_gate)" in verdict["message"]
        assert "staat niet in het rondegrootboek" in verdict["message"]

    def test_a_pr_that_is_not_opened_is_refused_and_an_open_reconstructs_nothing(self, door):
        import review_rounds_cli

        _door_pass(door, "codex_gate", tag="01")
        _door_pass(door, "kimi_gate", tag="02")
        door["ledger"].write_text("", encoding="utf-8")

        refused = _check(door)
        opened = review_rounds_cli.main(["rounds", "open", "--pr", str(PR), "--reason", "pre-R1", "--data-dir", str(door["data"])])
        after = _check(door)

        assert refused["verdict"] == "NO-GO", refused["message"]
        assert f'vnx review rounds open --pr {PR} --reason "..."' in refused["message"]
        assert opened == 0
        assert after["verdict"] == "NO-GO", "the PASSes on disk never had a ledger line"
        assert "staat niet in het rondegrootboek" in after["message"]

    def test_the_codex_seat_of_a_pr_at_the_cap_is_not_filled(self, door):
        codex, kimi = _door_pass(door, "codex_gate", tag="01"), _door_pass(door, "kimi_gate", tag="02")
        _write_ledger(door["ledger"], [_opened()] + _codex_rounds(2, 1, 1, 1, 1) + [_line_for(codex, at=20), _line_for(kimi, at=21)])

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
        _write_ledger(door["ledger"], [_opened()] + _codex_rounds(3) + [_line_for(codex, at=20), _line_for(kimi, at=21)])
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


def _door_env(monkeypatch, tmp_path, data_dir) -> None:
    """Point the real door (run_dispatch) at the snapshot's throwaway store."""
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
    monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")


def _fire(data_dir, spec_file, monkeypatch):
    """Run the real door with the headless lane stubbed; returns (rc, calls)."""
    import dispatch_cli

    calls: List[Any] = []
    monkeypatch.setattr(dispatch_cli, "_execute_claude_headless", lambda *a, **kw: calls.append(a) or 0)
    return dispatch_cli.run_dispatch(spec_file), calls


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

    def test_the_cli_refuses_a_whitespace_only_override_reason(self):
        import dispatch_cli

        assert _rc(dispatch_cli.main, [
            "--spec-file", "/nonexistent.json", "--override-b3", "--reason", "   ",
        ]) == 2

    def test_the_override_books_a_ledger_record_with_all_fields(self, tmp_path, monkeypatch):
        """R1d (OI-2180 point 3): an applied --override-b3 books one ``override`` line per capped PR at fire time."""
        import dispatch_cli

        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(2, 1, 1, 1, 1))

        snapshot = build(vspec, data_dir=data_dir, spec_file=spec_file)

        (applied,) = [v for v in snapshot.constraint_verdicts if v.code == "b3-cap-override-applied"]
        assert applied.override_applied
        assert _override_lines(ledger) == [], "snapshot build writes nothing durable"
        assert dispatch_cli._book_b3_override_at_fire(snapshot, state_dir=data_dir / "state") is None
        (override,) = _override_lines(ledger)
        assert override["project_id"] == ""
        assert override["pr_number"] == PR
        assert override["head_sha"] == OLD_HEADS[4], "B3.2 fell on round 5"
        assert override["reason"] == "operator: one-line fix"
        assert override["set_by"] == dispatch_cli.B3_OVERRIDE_REASON_ENV
        assert override["total"] == 6
        assert override["rule"] == "B3.2"

    def test_a_ledger_write_failure_refuses_the_override(self, tmp_path, monkeypatch):
        """A failure to book the override line at fire time refuses the override (fail-closed)."""
        import dispatch_cli
        import review_rounds

        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(3))

        snapshot = build(vspec, data_dir=data_dir, spec_file=spec_file)

        # The decision is made at snapshot time (an applied override), never booked yet.
        assert [v for v in snapshot.constraint_verdicts if v.code == "b3-cap-override-applied"]

        def refuse_write(*_args, **_kwargs):
            raise review_rounds.RoundLedgerError("round ledger could not be written: io")

        monkeypatch.setattr(review_rounds, "book_b3_override", refuse_write)

        refused = dispatch_cli._book_b3_override_at_fire(snapshot, state_dir=data_dir / "state")

        assert refused is not None and refused.code == "b3-override-ledger-error"
        assert refused.severity == "blocking"
        assert "could not be booked" in refused.message
        assert _override_lines(ledger) == []

    def test_the_receipt_field_is_present_with_an_override_and_absent_without(self, tmp_path, monkeypatch):
        """The dispatch receipt carries the override only when it was applied; never a stale field."""
        import dispatch_cli
        import dispatch_register
        from dispatch_internal import issue_permit
        from dispatch_plan import compile_plan

        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=BRANCH)
        _write_ledger(data_dir / "state" / "review_gates" / "rounds.ndjson", _codex_rounds(2, 1, 1, 1, 1))

        monkeypatch.delenv("VNX_OVERRIDE_B3_REASON", raising=False)
        without = build(vspec, data_dir=data_dir, spec_file=spec_file)
        assert dispatch_cli._b3_override_for_plan(compile_plan(vspec, without)) is None

        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        with_override = build(vspec, data_dir=data_dir, spec_file=spec_file)
        plan = compile_plan(vspec, with_override)
        assert dispatch_cli._b3_override_for_plan(plan) == {
            "flag": "--override-b3", "reason": "operator: one-line fix", "applied": True,
        }

        captured = {}
        monkeypatch.setattr(dispatch_register, "append_event_idempotent", lambda event, **fields: captured.update(fields))
        dispatch_cli._register_dispatch_created(plan, issue_permit(plan), vspec.spec, state_dir=data_dir / "state")
        assert captured["extra"]["override_b3"] == {
            "flag": "--override-b3", "reason": "operator: one-line fix", "applied": True,
        }

    def test_the_read_side_shows_the_override_record(self, tmp_path, monkeypatch, capsys):
        """``vnx review rounds show`` lists a build-door override record for the PR."""
        import dispatch_cli
        import review_rounds_cli

        monkeypatch.setenv("VNX_PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, build = _build_snapshot(tmp_path, work_ref=BRANCH)
        _write_ledger(data_dir / "state" / "review_gates" / "rounds.ndjson", _codex_rounds(2, 1, 1, 1, 1))
        snapshot = build(vspec, data_dir=data_dir, spec_file=spec_file)
        assert dispatch_cli._book_b3_override_at_fire(snapshot, state_dir=data_dir / "state") is None

        rc = _rc(review_rounds_cli.main, ["rounds", "show", "--pr", str(PR), "--data-dir", str(data_dir)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "override" in out
        assert "operator: one-line fix" in out
        assert "VNX_OVERRIDE_B3_REASON" in out

    # ------------------------------------------------------------------
    # R1d fix round: the override line is booked only when the door fires
    # ------------------------------------------------------------------

    def test_a_dry_run_with_override_books_zero_ledger_lines(self, tmp_path, monkeypatch, capsys):
        """Red run on d8e570c0: --dry-run --override-b3 appended an override line; it must not."""
        import dispatch_cli

        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, _build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(2, 1, 1, 1, 1))
        _door_env(monkeypatch, tmp_path, data_dir)

        rc = dispatch_cli.run_dispatch(spec_file, dry_run=True)

        assert rc == 0
        assert _override_lines(ledger) == [], "a dry-run must produce zero durable side effects"
        out = capsys.readouterr().out
        assert "b3-cap-override-applied" in out, "the dry-run plan still shows the override would apply"
        assert f"#{PR}" in out, "and names the PR it would apply to"

    def test_a_stop_condition_refusal_after_the_override_books_zero_lines(self, tmp_path, monkeypatch):
        """A fire refused by a stop-condition after the override check books zero override lines."""
        import dispatch_cli
        from dispatch_plan import ConstraintVerdict

        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, _build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(2, 1, 1, 1, 1))
        _door_env(monkeypatch, tmp_path, data_dir)
        monkeypatch.setattr(
            dispatch_cli, "_check_stop_conditions_verdict",
            lambda **kw: ConstraintVerdict(
                code="stop-conditions-triggered", severity="blocking",
                message="provider_exhausted: claude",
            ),
        )

        rc, calls = _fire(data_dir, spec_file, monkeypatch)

        assert rc == 1
        assert calls == [], "nothing is dispatched"
        assert _override_lines(ledger) == []

    def test_a_lane_reachability_refusal_after_the_override_books_zero_lines(self, tmp_path, monkeypatch):
        """A fire refused by lane reachability after the override check books zero override lines."""
        import dispatch_cli
        from dispatch_cli import ReachabilityVerdict

        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, _build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(2, 1, 1, 1, 1))
        _door_env(monkeypatch, tmp_path, data_dir)
        monkeypatch.setattr(
            dispatch_cli, "_check_reachability",
            lambda *a, **kw: ReachabilityVerdict(block=True, reason="provider exhausted"),
        )

        rc, calls = _fire(data_dir, spec_file, monkeypatch)

        assert rc == 1
        assert calls == [], "nothing is dispatched"
        assert _override_lines(ledger) == []

    def test_a_real_fire_books_one_override_line_and_the_receipt_carries_it(self, tmp_path, monkeypatch):
        """A real fire books exactly one override line per capped PR, and the receipt carries override_b3."""
        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, _build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(2, 1, 1, 1, 1))
        _door_env(monkeypatch, tmp_path, data_dir)

        rc, calls = _fire(data_dir, spec_file, monkeypatch)

        assert rc == 0
        assert calls, "the lane was invoked"
        (override,) = _override_lines(ledger)
        assert override["pr_number"] == PR
        assert override["reason"] == "operator: one-line fix"

        route_decisions = data_dir / "state" / "route_decisions.ndjson"
        assert route_decisions.exists(), "a real fire persists its route decision"
        records = [json.loads(line) for line in route_decisions.read_text(encoding="utf-8").splitlines() if line.strip()]
        (record,) = records
        assert record["override_b3"] == {
            "flag": "--override-b3", "reason": "operator: one-line fix", "applied": True,
        }

    def test_a_ledger_write_failure_at_fire_refuses_with_nothing_dispatched(self, tmp_path, monkeypatch):
        """Fail-closed at the new booking point: a ledger write failure refuses, nothing is dispatched."""
        import dispatch_cli
        import review_rounds

        monkeypatch.setenv("VNX_OVERRIDE_B3_REASON", "operator: one-line fix")
        data_dir, vspec, spec_file, _build = _build_snapshot(tmp_path, work_ref=BRANCH)
        ledger = data_dir / "state" / "review_gates" / "rounds.ndjson"
        _write_ledger(ledger, _codex_rounds(3))
        _door_env(monkeypatch, tmp_path, data_dir)

        def refuse_write(*_args, **_kwargs):
            raise review_rounds.RoundLedgerError("round ledger could not be written: io")

        monkeypatch.setattr(review_rounds, "book_b3_override", refuse_write)

        rc, calls = _fire(data_dir, spec_file, monkeypatch)

        assert rc == 1
        assert calls == [], "nothing is dispatched"
        assert _override_lines(ledger) == []
        assert not (data_dir / "state" / "route_decisions.ndjson").exists(), "no receipt field is written"


# ---------------------------------------------------------------------------
# Round 2, G1: the ledger is one regular file, and every record is framed
# ---------------------------------------------------------------------------

NEW_HEAD = "9" * 40
H1 = OLD_HEADS[0]


def _env_door(env, monkeypatch, *, head: str = HEAD_SHA, pr: int = PR, **kwargs: Any) -> Dict[str, Any]:
    """The merge door's review-seat verdict on the env store (codex and kimi seats)."""
    import closure_verifier

    pin_review_stack(monkeypatch, "codex_gate,kimi_gate")
    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    return closure_verifier.check_review_gate_for_merge(
        str(pr), "codex_gate", env["results_dir"], branch=BRANCH, head_sha=head, **kwargs,
    )


def _not_a_file(ledger: Path, kind: str, target_lines: List[Dict[str, Any]]) -> None:
    """Put a directory or a symlink (to a ledger the old reader accepts) at the ledger path."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        ledger.mkdir()
        return
    target = ledger.parent.parent / "elsewhere.ndjson"
    _write_ledger(target, target_lines)
    ledger.symlink_to(target)


class TestLedgerIntegrity:
    def test_a_torn_line_of_another_pr_does_not_swallow_the_next_verdict(self, env, monkeypatch):
        """Codex blocker 1: a FAIL(3) appended onto a torn PR-101 fragment vanished from its count."""
        assert _request(env, ["codex_gate"])["codex_gate"]["status"] == "requested"
        with env["ledger"].open("a", encoding="utf-8") as handle:
            handle.write('{"project_id":"","pr_number":101,"head_sha":"' + "1" * 20)

        _fail, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="01", blocking=3))
        assert landed is True
        _on_disk, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="02", head=NEW_HEAD))

        assert landed is False, "a PASS on a new head of a PR at B3.1 is not booked"
        codex = [(line["head_sha"], line["blocking_count"]) for line in _ledger_lines(env["ledger"])]
        assert codex == [(HEAD_SHA, 3)], "the FAIL kept a line of its own"
        verdict = _env_door(env, monkeypatch, head=NEW_HEAD)
        assert verdict["verdict"] == "NO-GO"
        assert verdict["review_seats"]["round_ledger"].get("cap", {}).get("rule") == "B3.1", verdict["review_seats"]
        raw = env["ledger"].read_text(encoding="utf-8").splitlines()
        assert raw[-2] == '{"project_id":"","pr_number":101,"head_sha":"' + "1" * 20, "the fragment is a line of its own"

    @pytest.mark.parametrize("kind", ["directory", "symlink"])
    def test_a_ledger_path_that_is_no_file_refuses_the_door(self, door, kind):
        """Codex blocker 3: a non-file ledger was read as absent, and two PASSes merged."""
        codex, kimi = _door_pass(door, "codex_gate", tag="01"), _door_pass(door, "kimi_gate", tag="02")
        _not_a_file(door["ledger"], kind, [_line_for(codex, at=1), _line_for(kimi, at=2)])

        verdict = _check(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        refusal = verdict["review_seats"].get("round_ledger", {}).get("refusal", "")
        assert f"is not a regular file ({kind})" in refusal and "reopen every open PR" in refusal

    @pytest.mark.parametrize("kind", ["directory", "symlink"])
    def test_a_ledger_path_that_is_no_file_refuses_every_seat(self, env, kind):
        _not_a_file(env["ledger"], kind, _codex_rounds(1))

        seats = _request(env, ["codex_gate", "kimi_gate"])

        for seat in ("codex_gate", "kimi_gate"):
            assert seats[seat]["status"] == "blocked", seats[seat]
            assert seats[seat]["reason"] == "round_ledger_unreadable"
            assert f"is not a regular file ({kind})" in seats[seat]["reason_detail"]
            assert not (env["requests_dir"] / f"pr-{PR}-{seat}.json").exists(), "no lane is reached"
        assert _open_cli(env) == 2, "the operator line is refused on the same ledger"


# ---------------------------------------------------------------------------
# Round 2, G2: a PR's review history is recorded (the ``opened`` line)
# ---------------------------------------------------------------------------


def _append_receipt(state_dir: Path, receipt: Dict[str, Any]) -> None:
    with (state_dir / "t0_receipts.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(receipt) + "\n")


def _legacy_codex_fail(env, *, head: str = H1, blockers: int = 3, tag: str = "00") -> Path:
    """A codex FAIL from before R1: its report, its record and its two receipts, no ledger line."""
    text = f"## Findings\n\nBlocking count: {blockers}\nAdvisory count: 0\n"
    report = _report(env["headless"], "codex_gate", tag=tag, text=text)
    record = {
        "gate": "codex_gate", "pr_id": str(PR), "pr_number": PR, "status": "completed", "commit_sha": head,
        "branch": BRANCH, "contract_hash": "sha256:legacy", "report_path": str(report),
        "blocking_findings": [{"severity": "blocking", "message": f"defect {n}"} for n in range(blockers)],
        "advisory_findings": [], "dispatch_id": "20261017-legacy", "recorded_at": "2026-10-17T09:00:00Z",
    }
    (env["results_dir"] / f"pr-{PR}-codex_gate.json").write_text(json.dumps(record), encoding="utf-8")
    _append_receipt(env["state_dir"], {
        "event_type": "review_gate_request", "timestamp": "2026-10-17T08:55:00Z", "gate": "codex_gate",
        "status": "requested", "pr_number": PR, "request": {
            "commit_sha": head, "seat": "codex_gate", "report_path": str(report), "branch": BRANCH, "gate": "codex_gate",
        },
    })
    _append_receipt(env["state_dir"], {
        "event_type": "review_gate_result", "timestamp": "2026-10-17T09:00:00Z", "gate": "codex_gate",
        "status": "failed", "gate_status": "completed", "blocking_count": blockers, "report_path": str(report),
        "pr_number": PR,
    })
    return report


def _backstop_passes(env) -> None:
    """PASSes on the current head written by the writer alone: the path without a request."""
    for gate, tag in (("codex_gate", "02"), ("kimi_gate", "03")):
        _payload, landed = _write_result(env, gate, _verdict(env, gate, tag=tag))
        assert landed is True, gate


class TestTheOpenedLine:
    def test_a_legacy_pr_is_refused_in_every_seat_until_the_operator_opens_it(self, env):
        """Codex blocker 2 (a): a request on a new head admitted a PR whose pre-R1 FAIL was never counted."""
        report = _legacy_codex_fail(env)
        env["ledger"].write_text("", encoding="utf-8")

        seats = _request(env, ["codex_gate", "kimi_gate"])
        overridden = _request(env, ["codex_gate"], override_b3="operator: try anyway")

        for payload in (seats["codex_gate"], seats["kimi_gate"], overridden["codex_gate"]):
            assert payload["status"] == "blocked", payload
            assert payload["reason"] == "round_history_missing", payload
            assert f"first: {report.name}" in payload["reason_detail"]
            assert f'vnx review rounds open --pr {PR} --reason "..."' in payload["reason_detail"]
        for seat in ("codex_gate", "kimi_gate"):
            assert not (env["requests_dir"] / f"pr-{PR}-{seat}.json").exists(), "no lane is reached"
        assert _opened_lines(env["ledger"]) == []

    def test_backstop_passes_of_a_legacy_pr_land_but_do_not_pass_the_door(self, env, monkeypatch):
        """Codex blocker 2 (b): fresh PASSes booked without a request merged past the pre-R1 FAIL."""
        _legacy_codex_fail(env)
        env["ledger"].write_text("", encoding="utf-8")

        _backstop_passes(env)
        verdict = _env_door(env, monkeypatch)

        assert [line["verdict"] for line in _ledger_lines(env["ledger"])] == ["pass", "pass"], "no verdict is lost"
        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "niet geopend in het rondegrootboek" in verdict["message"]
        assert f'vnx review rounds open --pr {PR} --reason "..."' in verdict["message"]

    def test_a_new_pr_is_opened_by_its_first_request_and_its_pass_counts(self, env, monkeypatch):
        """Codex blocker 2 (d): a PR without earlier reviews runs, and the door counts its PASS."""
        seats = _request(env, ["codex_gate", "kimi_gate"])
        assert {seat: payload["status"] for seat, payload in seats.items()} == {
            "codex_gate": "requested", "kimi_gate": "requested",
        }
        _backstop_passes(env)

        verdict = _env_door(env, monkeypatch)

        assert [(line["basis"], line["evidence"], line["source"]) for line in _opened_lines(env["ledger"])] == [
            ("new", 0, "gate"),
        ]
        assert verdict["verdict"] == "GO", verdict["message"]

    def test_a_second_request_does_not_open_the_pr_twice(self, env):
        _request(env, ["codex_gate"])
        _request(env, ["codex_gate"])

        assert len(_opened_lines(env["ledger"])) == 1

    @pytest.mark.parametrize("broken", [{"basis": "maybe"}, {"evidence": -1}, {"pr_number": "1926"}, {"recorded_at": ""}])
    def test_a_bad_opened_line_refuses_its_own_pr(self, broken):
        """A pin: an ``opened`` line is parsed as strictly as a verdict line, never half-trusted."""
        import review_rounds

        ledger = review_rounds.parse_ledger(Path("rounds.ndjson"), json.dumps({**_opened(), **broken}) + "\n")

        assert f"unreadable for PR #{PR}" in review_rounds.unreadable_text(ledger, PR)
        assert ledger.lines == ()

    def test_an_unknown_line_kind_is_a_bad_line(self):
        import review_rounds

        ledger = review_rounds.parse_ledger(Path("rounds.ndjson"), json.dumps({**_opened(), "kind": "closed"}) + "\n")

        assert [(bad.pr_number, bad.error) for bad in ledger.bad] == [(PR, "unknown line kind 'closed'")]


# ---------------------------------------------------------------------------
# Round 3: no rebuild. Earlier history fails closed, a legacy PR opens by an operator line
# ---------------------------------------------------------------------------

OPEN_REASON = "operator: reviewed before R1, history read by hand"


def _open_cli(env, pr: int = PR, *, reason: Optional[str] = OPEN_REASON, extra: Optional[List[str]] = None) -> int:
    import review_rounds_cli

    argv = ["rounds", "open", "--pr", str(pr), "--data-dir", str(env["data_dir"])]
    if reason is not None:
        argv += ["--reason", reason]
    return _rc(review_rounds_cli.main, argv + (extra or []))


def _history_only_in_a_receipt(env, *, blockers: int = 3) -> str:
    """A codex FAIL that left only its ``review_gate_result`` receipt: no report on disk, no record."""
    report = f"/gone/20261016-090000-HEADLESS-codex_gate-pr-{PR}-0a0a0a.md"
    _append_receipt(env["state_dir"], {
        "timestamp": "2026-10-16T09:05:00Z", "event_type": "review_gate_result", "receipt_kind": "review_gate",
        "status": "failed", "gate": "codex_gate", "gate_status": "completed", "contract_hash": "c0dexc0dex",
        "report_path": report, "blocking_count": blockers, "pr_id": str(PR), "pr_number": PR,
    })
    return f"receipt:codex_gate:{Path(report).name}"


def _history_only_in_a_contract_record(env, *, blockers: int = 3) -> str:
    """A codex FAIL that left only its record under the contract name ``N-<gate>-contract.json``."""
    name = f"{PR}-codex_gate-contract.json"
    (env["results_dir"] / name).write_text(json.dumps({
        "gate": "codex_gate", "pr_id": str(PR), "pr_number": PR, "status": "completed", "commit_sha": H1,
        "contract_hash": "sha256:legacy", "report_path": f"/gone/20261016-HEADLESS-codex_gate-pr-{PR}-c0.md",
        "blocking_findings": [{"severity": "blocking", "message": f"defect {n}"} for n in range(blockers)],
        "advisory_findings": [], "recorded_at": "2026-10-16T09:00:00Z",
    }), encoding="utf-8")
    return name


_HISTORY_SOURCES = {"receipt-only": _history_only_in_a_receipt, "contract-record-only": _history_only_in_a_contract_record}


class TestEarlierHistoryFailsClosed:
    @pytest.mark.parametrize("source", sorted(_HISTORY_SOURCES))
    def test_a_legacy_fail_in_one_source_refuses_until_the_operator_opens_the_pr(self, env, monkeypatch, source):
        """Codex round-2 blockers 1 and 2: a FAIL(3) only in a receipt, or only in a contract-named
        record, then PASSes on a new head. Without its history the PR was opened as new and merged."""
        import review_rounds

        evidence = _HISTORY_SOURCES[source](env)
        seats = _request(env, ["codex_gate", "kimi_gate"], head_sha=NEW_HEAD)

        for seat in ("codex_gate", "kimi_gate"):
            assert seats[seat]["status"] == "blocked", seats[seat]
            assert seats[seat]["reason"] == "round_history_missing", seats[seat]
            assert evidence in seats[seat]["reason_detail"]
            assert f'vnx review rounds open --pr {PR} --reason "..."' in seats[seat]["reason_detail"]
        assert _opened_lines(env["ledger"]) == [], "no opened line for a PR with history"
        for gate, tag in (("codex_gate", "02"), ("kimi_gate", "03")):
            assert _write_result(env, gate, _verdict(env, gate, tag=tag, head=NEW_HEAD))[1] is True, gate
        assert _env_door(env, monkeypatch, head=NEW_HEAD)["verdict"] == "NO-GO", "two PASSes do not merge past it"
        assert evidence in review_rounds.prior_evidence(env["data_dir"], PR)

        assert _open_cli(env) == 0
        reopened_head = "8" * 40
        reopened = _request(env, ["codex_gate", "kimi_gate"], head_sha=reopened_head)

        assert {seat: payload["status"] for seat, payload in reopened.items()} == {
            "codex_gate": "requested", "kimi_gate": "requested",
        }
        assert [(line["basis"], line["source"], line["reason"]) for line in _opened_lines(env["ledger"])] == [
            ("attested", "operator", OPEN_REASON),
        ]
        # R1c: what was booked before the attested line counts for nothing, the PASSes included.
        assert _env_door(env, monkeypatch, head=NEW_HEAD)["verdict"] == "NO-GO"
        for gate, tag in (("codex_gate", "04"), ("kimi_gate", "05")):
            assert _write_result(env, gate, _verdict(env, gate, tag=tag, head=reopened_head))[1] is True, gate
        assert _env_door(env, monkeypatch, head=reopened_head)["verdict"] == "GO", "the cap starts at zero from that line"

    @pytest.mark.parametrize("break_it", ["record-does-not-parse", "receipt-ledger-cannot-be-opened"])
    def test_a_source_that_cannot_be_read_is_evidence(self, env, break_it):
        if break_it == "record-does-not-parse":
            broken = env["results_dir"] / f"pr-{PR}-codex_gate.json"
            broken.write_text('{"gate": "codex_gate", "status": "compl', encoding="utf-8")
        else:
            broken = env["state_dir"] / "t0_receipts.ndjson"
            broken.mkdir()

        seats = _request(env, ["codex_gate"])

        assert (seats["codex_gate"]["status"], seats["codex_gate"].get("reason")) == ("blocked", "round_history_missing")
        assert f"unreadable:{broken}" in seats["codex_gate"]["reason_detail"]
        assert _opened_lines(env["ledger"]) == []

    def test_a_bad_line_that_names_no_pr_stops_every_pr_and_nothing_removes_it(self, env, monkeypatch):
        """Codex round-2 blocker 3: a rebuild of an unrelated PR quarantined the line of a capped PR."""
        import review_rounds_cli

        other = PR + 1
        paths = {"headless": env["headless"], "results": env["results_dir"]}
        lines: List[Dict[str, Any]] = [_opened(PR), _opened(other)]
        for pr in (PR, other):
            for at, (gate, tag) in enumerate((("codex_gate", "01"), ("kimi_gate", "02")), start=1):
                lines.append(_line_for(_door_pass(paths, gate, tag=tag, pr=pr), at=at))
        _write_ledger(env["ledger"], lines, extra="not json at all\n")
        before = env["ledger"].read_bytes()

        seats = _request(env, ["codex_gate", "kimi_gate"], pr=other)
        doors = [_env_door(env, monkeypatch, pr=pr) for pr in (PR, other)]
        opened = _open_cli(env, other)
        rebuilt = _rc(review_rounds_cli.main, ["rounds", "rebuild", "--pr", str(other), "--data-dir", str(env["data_dir"])])

        assert env["ledger"].read_bytes() == before, "no line was removed, moved or rewritten"
        assert not (env["ledger"].parent / "rounds.ndjson.quarantine").exists()
        assert (opened, rebuilt) == (2, 2), "neither verb opens or rebuilds past an unreadable ledger"
        for seat in ("codex_gate", "kimi_gate"):
            assert seats[seat]["reason"] == "round_ledger_unreadable", seats[seat]
        for door_verdict in doors:
            assert door_verdict["verdict"] == "NO-GO", door_verdict["message"]
            assert door_verdict["review_seats"]["round_ledger"]["refusal"], door_verdict["review_seats"]


class TestRoundsOpen:
    def test_an_empty_reason_is_refused(self, env):
        assert _open_cli(env, reason="   ") == 2
        assert _open_cli(env, reason=None) == 2
        assert _opened_lines(env["ledger"]) == []

    def test_open_writes_the_line_and_one_receipt_and_a_second_call_writes_neither(self, env, capsys):
        import getpass

        evidence = _history_only_in_a_receipt(env)
        first = _open_cli(env)
        second = _open_cli(env)

        out = capsys.readouterr().out
        assert (first, second) == (0, 0)
        (line,) = _opened_lines(env["ledger"])
        assert (line["basis"], line["source"], line["actor"], line["reason"]) == (
            "attested", "operator", getpass.getuser(), OPEN_REASON,
        )
        assert line["recorded_at"] and line["evidence"] == 1
        assert f"already opened (basis attested, recorded_at {line['recorded_at']})" in out
        receipts = [
            json.loads(raw) for raw in (env["state_dir"] / "t0_receipts.ndjson").read_text().splitlines()
            if '"review_round_opened"' in raw
        ]
        assert len(receipts) == 1, receipts
        receipt = receipts[0]
        assert (receipt["pr_number"], receipt["basis"], receipt["actor"], receipt["reason"]) == (
            PR, "attested", getpass.getuser(), OPEN_REASON,
        )
        assert receipt["ledger"] == str(env["ledger"]) and receipt["evidence"] == [evidence]

    def test_a_receipt_failure_is_reported_and_the_line_stands(self, env, monkeypatch, capsys):
        import governance_receipts

        def refuse(*_args, **_kwargs):
            raise governance_receipts.AppendReceiptError("io", 1, "receipt ledger is read-only")

        monkeypatch.setattr(governance_receipts, "emit_governance_receipt", refuse)

        rc = _open_cli(env)

        assert rc == 1
        err = capsys.readouterr().err
        assert "the ledger line stands" in err and "receipt ledger is read-only" in err
        assert [line["basis"] for line in _opened_lines(env["ledger"])] == ["attested"]

    @pytest.mark.parametrize("broken", [
        {"basis": "rebuild"}, {"basis": "attested", "reason": "", "actor": "vincent", "source": "operator"},
        {"basis": "attested", "reason": "why", "actor": "", "source": "operator"},
    ])
    def test_the_parser_takes_new_and_attested_only(self, broken):
        import review_rounds

        ledger = review_rounds.parse_ledger(Path("rounds.ndjson"), json.dumps({**_opened(), **broken}) + "\n")

        assert ledger.opened == () and [bad.pr_number for bad in ledger.bad] == [PR]

    def test_rounds_open_follows_vnx_state_dir_outside_the_data_dir(self, tmp_path, monkeypatch):
        """OI-2181 (1): ``rounds open`` writes the ledger the gate reads, honoring ``VNX_STATE_DIR``.

        With a state dir outside ``<VNX_DATA_DIR>/state`` the old command wrote the
        ``opened`` line and its receipt into a ledger nobody read and reported success.
        """
        import gate_request_handler
        import review_rounds_cli

        data_dir = tmp_path / "data"
        state_dir = tmp_path / "external" / "state"
        reports_dir = data_dir / "unified_reports"
        for directory in (
            state_dir / "review_gates" / "results",
            state_dir / "review_gates" / "requests",
            reports_dir / "headless",
            # The old store (``<VNX_DATA_DIR>/state``) exists too, so the old command
            # writes there and reports success — exactly the bug this test pins.
            data_dir / "state" / "review_gates",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("VNX_HOME", str(review_stack_support.VNX_ROOT))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
        monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
        monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
        monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
        monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
        monkeypatch.chdir(tmp_path)

        rc = review_rounds_cli.main(["rounds", "open", "--pr", str(PR), "--reason", OPEN_REASON])

        assert rc == 0
        ledger = state_dir / "review_gates" / "rounds.ndjson"
        assert [(line["basis"], line["pr_number"]) for line in _opened_lines(ledger)] == [("attested", PR)]
        # The gate's reader resolves the same ledger: the PR is opened, not refused.
        rounds = gate_request_handler._PrRounds.read(state_dir / "review_gates", PR)
        assert not rounds.unreadable and rounds.history_missing == ""
        assert rounds.cap.pr_number == PR
        # The receipt lands in the same store the gate reads, not under <VNX_DATA_DIR>/state.
        assert '"review_round_opened"' in (state_dir / "t0_receipts.ndjson").read_text(encoding="utf-8")
        assert not (data_dir / "state" / "review_gates" / "rounds.ndjson").exists()


class TestReviewCliSpawnFailure:
    def test_a_spawn_failure_is_a_defined_error(self, tmp_path, monkeypatch, capsys):
        """OI-2180 (4): an ``OSError`` from process creation is caught, named and returned as an error code."""
        import vnx_cli.commands.review as review_cli

        script = tmp_path / "review_rounds_cli.py"
        script.write_text("", encoding="utf-8")
        monkeypatch.setattr(review_cli, "resolve_review_script", lambda root=None: script)

        def broken(*_args, **_kwargs):
            raise OSError("spawn failed")

        monkeypatch.setattr(review_cli.subprocess, "run", broken)

        rc = review_cli.vnx_review(Namespace(review_args=["rounds", "open", "--pr", str(PR), "--reason", "x"]))

        assert rc == review_cli._EXIT_ERROR
        assert f"could not start {script}" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# R1c (OI-2180, OI-2185): a re-anchor, the opened boundary, one store per command
# ---------------------------------------------------------------------------

KIMI_ID = f"kimi-gate-pr{PR}-20261019100002"


def _reanchor(env, monkeypatch, gate: str, *, new_head: str) -> int:
    """``gate_reanchor_cli --apply`` onto ``new_head``. gh, the contract hash and the symbol analysis are
    the edge and allow it; the write goes through the real guarded recorder and the round ledger."""
    import gate_reanchor
    import gate_reanchor_cli

    record = json.loads((env["results_dir"] / f"pr-{PR}-{gate}.json").read_text(encoding="utf-8"))
    view = {"headRefOid": new_head, "headRefName": BRANCH, "files": [{"path": "scripts/lib/example.py"}]}
    monkeypatch.setattr(gate_reanchor_cli, "_gh", lambda args, timeout=30: json.dumps(view))
    monkeypatch.setattr(gate_reanchor_cli, "current_contract_hash", lambda _gate, _pr: record["contract_hash"])
    monkeypatch.setattr(gate_reanchor, "can_reanchor", lambda _root, **kw: gate_reanchor.ReanchorDecision(
        allowed=True, reason="identical diff on a moved base", contract_hash_matches=True,
        old_sha=kw["old_sha"], new_sha=kw["new_sha"],
    ))
    return gate_reanchor_cli.main(["--pr", str(PR), "--gate", gate, "--apply", "--project-root", str(env["project_root"])])


def _seat_lines(ledger: Path, seat: str) -> List[Any]:
    return [(line["head_sha"], line.get("reanchored_from", "")) for line in _ledger_lines(ledger) if line["seat"] == seat]


class TestAReanchorIsBooked:
    def test_a_reanchored_pass_gets_a_line_for_its_new_head_and_counts_at_the_door(self, env, monkeypatch):
        """OI-2180 (1). Red on 2b9da5c5: the re-anchored record was taken for a rewrite of the record
        it replaced, no line was booked for the new head, and two valid PASSes on it were NO-GO."""
        _request(env, ["codex_gate", "kimi_gate"])
        assert _write_result(env, "kimi_gate", _verdict(env, "kimi_gate", tag="02", dispatch_id=KIMI_ID))[1] is True
        assert _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="03", head=NEW_HEAD))[1] is True

        rc = _reanchor(env, monkeypatch, "kimi_gate", new_head=NEW_HEAD)
        verdict = _env_door(env, monkeypatch, head=NEW_HEAD)

        assert rc == 0
        assert verdict["verdict"] == "GO", verdict["message"]
        assert _seat_lines(env["ledger"], "kimi_gate") == [(HEAD_SHA, ""), (NEW_HEAD, HEAD_SHA)]

    def test_a_reanchored_codex_seat_verdict_is_no_second_round(self, env, monkeypatch):
        """The relocated verdict is the one already counted on its own head: its blockers count once."""
        import review_rounds

        _request(env, ["codex_gate"])
        takeover = {"takeover": True, "takeover_from": "codex_gate", "dispatch_id": KIMI_ID}
        fail = _verdict(env, "kimi_gate", tag="02", blocking=2, seat="codex_gate", **takeover)
        assert _write_result(env, "kimi_gate", fail)[1] is True

        assert _reanchor(env, monkeypatch, "kimi_gate", new_head=NEW_HEAD) == 0

        assert _seat_lines(env["ledger"], "codex_gate") == [(HEAD_SHA, ""), (NEW_HEAD, HEAD_SHA)]
        ledger = review_rounds.read_ledger(env["ledger"])
        assert review_rounds.cap_state(ledger.for_pr("", PR), PR).blockers == (2,)

    def test_a_reanchor_of_a_verdict_the_ledger_does_not_count_is_refused(self, env, monkeypatch):
        """A verdict booked before an attested ``opened`` line is not counted; carrying it to a new head
        would make it count there. The write is refused and the record stays on its own head."""
        assert _write_result(env, "kimi_gate", _verdict(env, "kimi_gate", tag="02", dispatch_id=KIMI_ID))[1] is True
        assert _open_cli(env) == 0

        rc = _reanchor(env, monkeypatch, "kimi_gate", new_head=NEW_HEAD)

        assert rc == 20, "the guarded write refused"
        assert _seat_lines(env["ledger"], "kimi_gate") == [(HEAD_SHA, "")]
        record = json.loads((env["results_dir"] / f"pr-{PR}-kimi_gate.json").read_text(encoding="utf-8"))
        assert record["commit_sha"] == HEAD_SHA


class TestTheOpenedBoundary:
    def test_the_cap_starts_at_zero_at_an_attested_opened_line(self, env, monkeypatch):
        """OI-2180 (2). Red on 2b9da5c5: a FAIL(3) booked before ``rounds open`` still counted. The open
        message said the cap starts at zero there; the next request refused the codex seat anyway."""
        import dispatch_cli

        assert _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="01", blocking=3))[1] is True
        assert _open_cli(env) == 0

        seats = _request(env, ["codex_gate", "kimi_gate"])

        assert {seat: payload["status"] for seat, payload in seats.items()} == {
            "codex_gate": "requested", "kimi_gate": "requested",
        }, "neither the cap nor the FAIL on this head is counted from before the line"
        for gate, tag in (("codex_gate", "02"), ("kimi_gate", "03")):
            assert _write_result(env, gate, _verdict(env, gate, tag=tag))[1] is True, gate
        verdict = _env_door(env, monkeypatch)
        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["round_ledger"]["cap"]["blockers"] == [0]
        assert dispatch_cli._capped_prs_on_branch(env["state_dir"], BRANCH) == []

    def test_a_new_opened_line_draws_no_boundary(self, env):
        """Only ``basis`` ``attested`` draws the line: a ``new`` line has nothing before it to leave out."""
        import review_rounds

        _write_ledger(env["ledger"], _codex_rounds(3) + [_opened()])

        ledger = review_rounds.read_ledger(env["ledger"])

        assert review_rounds.cap_state(ledger.for_pr("", PR), PR).rule == "B3.1"


class TestTheGateReadsItsOwnStore:
    def test_the_history_check_reads_the_store_of_the_ledger_it_opens(self, env, tmp_path):
        """OI-2185 (2). Red on 2b9da5c5: the history check read the store VNX_STATE_DIR names, not the
        one the ledger lives in, and opened a PR with an earlier FAIL as new."""
        import gate_request_handler

        review_gates = tmp_path / "other" / "state" / "review_gates"
        (review_gates / "results").mkdir(parents=True)
        legacy = _verdict(env, "codex_gate", tag="01", blocking=3)
        Path(legacy["report_path"]).unlink()  # the earlier review is in the other store only
        (review_gates / "results" / f"pr-{PR}-codex_gate.json").write_text(json.dumps(legacy), encoding="utf-8")

        rounds = gate_request_handler._PrRounds.read(review_gates, PR)

        assert f"first: pr-{PR}-codex_gate.json" in rounds.history_missing, rounds.history_missing
        assert _opened_lines(review_gates / "rounds.ndjson") == []


class TestRoundsOpenNamesOneStore:
    def test_data_dir_and_a_state_dir_outside_it_are_refused_naming_both(self, env, tmp_path, monkeypatch, capsys):
        """OI-2185 (1). Red on 2b9da5c5: ``--data-dir`` won, the opened line went into
        ``<data-dir>/state`` while the gate reads VNX_STATE_DIR, and the command exited 0."""
        state_dir = tmp_path / "external" / "state"
        (state_dir / "review_gates").mkdir(parents=True)
        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

        rc = _open_cli(env)

        err = capsys.readouterr().err
        assert rc == 2, err
        assert str(env["state_dir"]) in err and str(state_dir) in err
        assert _opened_lines(env["ledger"]) == []
        assert _opened_lines(state_dir / "review_gates" / "rounds.ndjson") == []

    def test_data_dir_alone_opens_the_pr_in_its_own_store(self, env, monkeypatch):
        monkeypatch.delenv("VNX_STATE_DIR")

        assert _open_cli(env) == 0
        assert [line["basis"] for line in _opened_lines(env["ledger"])] == ["attested"]

    def test_the_derived_state_dir_of_another_project_does_not_overrule_data_dir(self, env, tmp_path):
        """``bin/vnx`` exports VNX_STATE_DIR as ``<VNX_DATA_DIR>/state`` of the project it runs in; that
        derived value says nothing about the store ``--data-dir`` names."""
        import review_rounds_cli

        other = tmp_path / "other" / ".vnx-data"
        (other / "state" / "review_gates").mkdir(parents=True)

        rc = _rc(review_rounds_cli.main, ["rounds", "open", "--pr", str(PR), "--reason", OPEN_REASON, "--data-dir", str(other)])

        assert rc == 0
        assert [line["basis"] for line in _opened_lines(other / "state" / "review_gates" / "rounds.ndjson")] == ["attested"]
        assert _opened_lines(env["ledger"]) == []


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
