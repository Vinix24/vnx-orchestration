"""R2a (track review-stopregel-en-lichte-stoelen): the tiebreaker at the B3 cap and its one extra round.

Plan: ``claudedocs/specs/review-stopregel-en-lichte-stoelen/plan.md``, section 2
("De tiebreaker bij de cap"), ADR-041. Dispatch 20261020-rs-r2a-tiebreaker. Pinned here:

- the red run: a capped PR with a stored ``verkleinen`` verdict bound to dispatch X gets exactly
  one more codex-seat run, on a head with a commit of X (setup in raw SQL and git, so it reads
  the same store on origin/main, where it fails);
- one test per decision path at the merge door: the granted round open (no PASS past the cap
  counts), used by a PASS (it merges), a FAIL after use (no merge; the tiebreaker can only pick
  the other outcome), a second ``verkleinen`` refused;
- the state contract, one test per rule: concurrent verdicts on the round are serialized and
  the first wins; an interrupted publication fails the run, passes nothing at the door and holds
  the head until ``vnx review rounds open``; the second seat binds first (B3.2) and only a
  codex-seat verdict uses the round;
- ``vnx review cap-verdict``: one model, one outcome, never a findings list, asked once.

No provider lane is reached: the dispatcher is injected and every request round requests and
never runs. Git runs in a throwaway repo under ``tmp_path``.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_stack_support import BRANCH, PR, build_project_store, isolate_config_registry
from review_seat_support import no_config_store, pin_review_stack

X = "20261020-r2a-verkleinen-x"
PID = "proj-r2a"
CLEAN_REPORT = "## Findings\n\nBlocking count: 0\nAdvisory count: 0\n"

# The table as migration 0035 makes it for a store of ``proj-r2a``, in raw SQL so the red run
# reads the same store on origin/main. ``test_the_raw_table_here_is_the_migration_table`` pins it.
_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS review_cap_verdicts (
    project_id          TEXT    NOT NULL DEFAULT 'proj-r2a',
    pr_number           INTEGER NOT NULL,
    verdict_id          TEXT    NOT NULL,
    head_sha            TEXT    NOT NULL,
    cap_rule            TEXT    NOT NULL CHECK (cap_rule IN ('B3.1', 'B3.2')),
    outcome             TEXT    NOT NULL CHECK (outcome IN (
        'verkleinen', 'terug-naar-plan', 'mergen-met-OI', 'outcome_unavailable_in_r2a')),
    chosen_outcome      TEXT    NOT NULL,
    intervention        TEXT    NOT NULL DEFAULT '',
    oi_ids              TEXT    NOT NULL DEFAULT '[]',
    granted_dispatch_id TEXT    NOT NULL DEFAULT '',
    tiebreaker_model    TEXT    NOT NULL DEFAULT '',
    rationale           TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    PRIMARY KEY (project_id, pr_number, verdict_id),
    CHECK (outcome <> 'verkleinen' OR (intervention <> '' AND granted_dispatch_id <> ''))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_cap_verdicts_one_verkleinen
    ON review_cap_verdicts (project_id, pr_number) WHERE outcome = 'verkleinen';
"""


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


# ---------------------------------------------------------------------------
# Store, repo and ledger helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _init_repo(repo: Path) -> str:
    _git(repo, "init", "-q")
    for key, value in (
        ("user.email", "r2a@example.invalid"), ("user.name", "r2a"),
        ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null"),
    ):
        _git(repo, "config", key, value)
    return _commit(repo, "base.txt", "base: the head the cap falls on")


def _create_verdicts_table(state_dir: Path) -> None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.executescript(_VERDICTS_DDL)
    finally:
        conn.close()


def _vid(head: str) -> str:
    return f"cap-pr{PR}-{head}"


def _store_verkleinen(state_dir: Path, head: str, *, dispatch: str = X, rule: str = "B3.1") -> str:
    """A stored ``verkleinen`` verdict on ``head``, as the tiebreaker records it (raw SQL)."""
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    try:
        conn.execute(
            "INSERT INTO review_cap_verdicts (project_id, pr_number, verdict_id, head_sha, cap_rule, outcome, "
            "chosen_outcome, intervention, granted_dispatch_id, tiebreaker_model, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'verkleinen', 'verkleinen', 'split the parser out', ?, 'deepseek-v4-pro', ?)",
            (PID, PR, _vid(head), head, rule, dispatch, "2026-10-20T09:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    return _vid(head)


def _line(
    seat: str, head: str, verdict: str, blockers: int, *, at: int, report: str = "", granted_by: str = "",
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    line = {
        "project_id": "", "pr_number": PR, "head_sha": head, "seat": seat, "reviewer": reviewer or seat,
        "takeover_from": "", "verdict": verdict, "blocking_count": blockers,
        "report_path": report or f"/r/20261020-{at:06d}-HEADLESS-{seat}-pr-{PR}-x.md",
        "dispatch_id": f"d-{at}", "recorded_at": f"2026-10-20T08:{at:02d}:00Z", "branch": BRANCH, "source": "gate",
    }
    if granted_by:
        line["granted_by"] = granted_by
    return line


def _opened() -> Dict[str, Any]:
    return {
        "kind": "opened", "project_id": "", "pr_number": PR, "basis": "new", "evidence": 0,
        "recorded_at": "2026-10-20T08:00:00Z", "source": "gate",
    }


def _write_ledger(path: Path, lines: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _ledger_lines(path: Path) -> List[Dict[str, Any]]:
    lines = [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
    return [line for line in lines if "kind" not in line]


def _granted(path: Path) -> List[tuple]:
    return [(line["head_sha"], line["verdict"], line["granted_by"]) for line in _ledger_lines(path) if line.get("granted_by")]


# ---------------------------------------------------------------------------
# The gate's world: a project store, a git repo, the real writers
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_recorder
    import gate_request_handler
    import gate_result_parser
    import review_gate_manager

    monkeypatch.setenv("VNX_OVERRIDE_CI_GATE_REQUIRED", "0")
    monkeypatch.setenv("VNX_PROJECT_ID", PID)
    # The repo the PR's commits live in: ensure_env()["PROJECT_ROOT"] honours this override.
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(store["project_root"]))
    monkeypatch.setattr(
        gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True,
    )
    receipts: List[Dict[str, Any]] = []
    for module in (review_gate_manager, gate_recorder):
        monkeypatch.setattr(module, "emit_governance_receipt", lambda event, **fields: receipts.append({"event": event, **fields}))
    head: List[str] = [""]
    for module in (gate_request_handler, gate_recorder, gate_result_parser):
        monkeypatch.setattr(module, "get_pr_head_sha", lambda _pr: head[0])
    repo = store["project_root"]
    store.update(
        receipts=receipts, head=head, repo=repo, cap_head=_init_repo(repo),
        ledger=store["state_dir"] / "review_gates" / "rounds.ndjson", headless=store["reports_dir"] / "headless",
    )
    store["headless"].mkdir(parents=True, exist_ok=True)
    _create_verdicts_table(store["state_dir"])
    return store


def _capped(store, *extra: Dict[str, Any]) -> None:
    """PR at B3.1: three codex-seat blockers in round 1, on the repo's base commit."""
    _write_ledger(store["ledger"], [_opened(), _line("codex_gate", store["cap_head"], "fail", 3, at=1), *extra])


def _verdict(store, gate: str, *, tag: str, head: str, blocking: int = 0) -> Dict[str, Any]:
    """A decided result payload as gate_artifacts builds it, its report on disk."""
    report = store["headless"] / f"20261020-1000{tag}-HEADLESS-{gate}-pr-{PR}-{tag}ab.md"
    report.write_text(f"# {gate}\n\n**PR**: {PR}\n\n---\n\n{CLEAN_REPORT}", encoding="utf-8")
    return {
        "gate": gate, "seat": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed",
        "summary": f"{gate} execution completed successfully", "contract_hash": f"{gate}-{tag}",
        "report_path": str(report), "findings": [],
        "blocking_findings": [{"severity": "blocking", "message": f"defect {n}"} for n in range(blocking)],
        "advisory_findings": [], "commit_sha": head, "branch": BRANCH,
        "dispatch_id": f"20261020-r2a-{gate}-{tag}", "recorded_at": f"2026-10-20T10:00:{tag}Z",
    }


def _write_result(store, gate: str, payload: Dict[str, Any]):
    import gate_recorder

    return gate_recorder.write_result_guarded(
        store["results_dir"] / f"pr-{PR}-{gate}.json", payload, gate=gate, pr_ref=str(PR),
    )


def _codex_request(store, head: str) -> Dict[str, Any]:
    import review_gate_manager

    store["head"][0] = head
    result = review_gate_manager.ReviewGateManager().request_reviews(
        pr_number=PR, branch=BRANCH, review_stack=["codex_gate"], risk_class="medium",
        changed_files=["scripts/lib/example.py"], mode="per_pr", dispatch_id="20261020-rs-r2a-test",
    )
    return {str(p.get("seat") or p["gate"]): p for p in result["requested"]}["codex_gate"]


# ---------------------------------------------------------------------------
# Red run: the granted round at the gate and at the writer
# ---------------------------------------------------------------------------


class TestTheGrantedRound:
    def test_a_stored_verkleinen_grants_exactly_one_more_codex_run_on_a_head_with_a_commit_of_its_dispatch(self, env):
        x1 = _commit(env["repo"], "fix1.txt", f"fix: split the parser out\n\nDispatch-ID: {X}")
        x2 = _commit(env["repo"], "fix2.txt", f"fix: follow-up\n\nDispatch-ID: {X}")
        _capped(env)
        vid = _store_verkleinen(env["state_dir"], env["cap_head"])

        first = _codex_request(env, x1)
        assert first["status"] == "requested", first
        _on_disk, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="11", head=x1))
        second = _codex_request(env, x2)

        assert landed is True
        assert _granted(env["ledger"]) == [(x1, "pass", vid)]
        assert second["status"] == "blocked" and second["reason"] == "b3_cap", second
        assert f"was used by the codex-seat PASS on head {x1[:12]}" in second["reason_detail"]

    def test_a_head_without_a_commit_of_the_dispatch_is_refused_and_told_why(self, env):
        other = _commit(env["repo"], "other.txt", "fix: something else\n\nDispatch-ID: 20261020-someone-else")
        _capped(env)
        _store_verkleinen(env["state_dir"], env["cap_head"])

        refused = _codex_request(env, other)
        _on_disk, landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="12", head=other))

        assert refused["reason"] == "b3_cap", refused
        assert f"open for a head with a commit of dispatch {X}" in refused["reason_detail"]
        assert landed is False, "the writer refuses it too: it is the backstop for a run without a request"
        assert _granted(env["ledger"]) == []

    def test_a_reanchored_verdict_never_uses_the_granted_round(self, tmp_path):
        import review_rounds

        ledger = tmp_path / "rounds.ndjson"
        _write_ledger(ledger, [_opened(), _line("codex_gate", CAP, "fail", 3, at=1)])
        reanchor = review_rounds.RoundLine.from_dict(
            {**_line("codex_gate", HEAD, "fail", 3, at=1), "reanchored_from": CAP},
        )

        def qualified(_cap, _lines, _since):
            return review_rounds.Grant(_vid(CAP), X, CAP, qualified_head=HEAD)

        with pytest.raises(review_rounds.RoundRefusal) as refused:
            review_rounds.append_verdict(ledger, reanchor, grant_for=qualified)

        assert refused.value.reason == "b3_cap"
        assert [line for line in _ledger_lines(ledger) if line.get("granted_by")] == []

    def test_a_verdict_row_that_does_not_parse_grants_nothing(self, tmp_path):
        import review_cap_verdicts
        import review_rounds

        state = tmp_path / ".vnx-data" / PID / "state"
        state.mkdir(parents=True)
        _create_verdicts_table(state)
        _store_verkleinen(state, CAP)
        conn = sqlite3.connect(str(state / "runtime_coordination.db"))
        conn.execute("UPDATE review_cap_verdicts SET oi_ids = 'not json'")
        conn.commit()
        conn.close()
        cap = review_rounds.cap_state([review_rounds.RoundLine.from_dict(_line("codex_gate", CAP, "fail", 3, at=1))], PR)

        with pytest.raises(review_cap_verdicts.CapVerdictError):
            review_cap_verdicts.read_verdicts(state, PR)
        assert review_cap_verdicts.door_grant(state, cap) is None

    def test_the_raw_table_here_is_the_migration_table(self, tmp_path):
        from migrations import apply_0035

        def shape(conn: sqlite3.Connection) -> tuple:
            info = conn.execute("PRAGMA table_info(review_cap_verdicts)").fetchall()
            index = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'ux_review_cap_verdicts_one_verkleinen'")
            return tuple(info), " ".join(index.fetchone()[0].split())

        raw, migrated = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
        raw.executescript(_VERDICTS_DDL)
        apply_0035.create_table(migrated, PID)

        assert shape(raw) == shape(migrated)


# ---------------------------------------------------------------------------
# The merge door, one test per decision path
# ---------------------------------------------------------------------------


HEAD = "c" * 40
CAP = "a" * 40


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
        "VNX_REPORTS_DIR": data_dir / "unified_reports", "VNX_DB_DIR": data_dir / "database", "VNX_PROJECT_ID": PID,
    }.items():
        monkeypatch.setenv(key, str(value))
    no_config_store(monkeypatch)
    pin_review_stack(monkeypatch, "codex_gate,kimi_gate")
    _create_verdicts_table(state)
    return {
        "state": state, "results": state / "review_gates" / "results",
        "ledger": state / "review_gates" / "rounds.ndjson", "headless": data_dir / "unified_reports" / "headless",
    }


def _door_record(door, gate: str, *, tag: str, verdict: str = "pass") -> Dict[str, Any]:
    report = door["headless"] / f"20261020-1000{tag}-HEADLESS-{gate}-pr-{PR}-{tag}ab.md"
    report.write_text(f"# {gate}\n\n---\n\n{CLEAN_REPORT}", encoding="utf-8")
    blocking = [] if verdict == "pass" else [{"severity": "blocking", "message": "defect"}]
    record = {
        "gate": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed", "commit_sha": HEAD,
        "branch": BRANCH, "contract_hash": f"sha256:{gate}", "report_path": str(report),
        "blocking_findings": blocking, "advisory_findings": [], "evidence_source": "live", "test_run": False,
        "dispatch_id": f"20261020-{gate}", "recorded_at": f"2026-10-20T10:00:{tag}Z",
    }
    (door["results"] / f"pr-{PR}-{gate}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _door_ledger(door, codex_verdict: str, *, granted_by: str) -> None:
    codex = _door_record(door, "codex_gate", tag="01", verdict=codex_verdict)
    kimi = _door_record(door, "kimi_gate", tag="02")
    _write_ledger(door["ledger"], [
        _opened(), _line("codex_gate", CAP, "fail", 3, at=1),
        _line("codex_gate", HEAD, codex_verdict, 0 if codex_verdict == "pass" else 1, at=2,
              report=codex["report_path"], granted_by=granted_by),
        _line("kimi_gate", HEAD, "pass", 0, at=3, report=kimi["report_path"]),
    ])


def _check(door) -> Dict[str, Any]:
    import closure_verifier

    return closure_verifier.check_review_gate_for_merge(
        str(PR), "codex_gate", door["results"], branch=BRANCH, head_sha=HEAD,
    )


class TestTheMergeDoor:
    def test_while_the_round_is_open_no_codex_pass_past_the_cap_counts(self, door):
        _store_verkleinen(door["state"], CAP)
        _door_ledger(door, "pass", granted_by="")

        verdict = _check(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "B3-cap" in verdict["message"] and "vnx review cap-verdict" in verdict["message"]

    def test_the_pass_that_used_the_round_merges_and_the_record_names_the_round(self, door):
        vid = _store_verkleinen(door["state"], CAP)
        _door_ledger(door, "pass", granted_by=vid)

        verdict = _check(door)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["round_ledger"]["granted_round"] == {"verdict_id": vid, "dispatch_id": X}

    def test_a_granted_line_of_a_verdict_the_store_does_not_hold_counts_nothing(self, door):
        _door_ledger(door, "pass", granted_by=_vid(CAP))

        verdict = _check(door)

        assert verdict["verdict"] == "NO-GO", verdict["message"]

    def test_a_fail_in_the_extra_round_does_not_merge_and_leaves_only_the_other_outcome(self, door, monkeypatch):
        import review_cap_tiebreaker
        import review_cap_verdicts

        vid = _store_verkleinen(door["state"], CAP)
        _door_ledger(door, "fail", granted_by=vid)
        monkeypatch.setattr(review_cap_tiebreaker, "_default_dispatcher", _no_model)

        before = _check(door)
        decision = review_cap_tiebreaker.decide(door["state"], PR, for_dispatch=X)
        after = _check(door)

        assert before["verdict"] == after["verdict"] == "NO-GO"
        verdict = decision.verdict
        assert decision.status == "recorded" and verdict.verdict_id == _vid(HEAD)
        assert (verdict.cap_rule, verdict.outcome, verdict.chosen_outcome) == (
            "B3.1", review_cap_verdicts.UNAVAILABLE, review_cap_verdicts.TERUG_NAAR_PLAN,
        )
        assert verdict.tiebreaker_model == "", "one outcome left: the rule decides, no model is asked"

    def test_a_second_verkleinen_on_one_pr_is_refused(self, door):
        import review_cap_verdicts

        _store_verkleinen(door["state"], CAP)
        second = review_cap_verdicts.CapVerdict(
            project_id="", pr_number=PR, verdict_id=_vid(HEAD), head_sha=HEAD, cap_rule="B3.2",
            outcome=review_cap_verdicts.VERKLEINEN, chosen_outcome=review_cap_verdicts.VERKLEINEN,
            intervention="split again", granted_dispatch_id="20261020-r2a-second", created_at="2026-10-20T11:00:00Z",
        )

        with pytest.raises(review_cap_verdicts.CapVerdictRefused, match="one verkleinen"):
            review_cap_verdicts.record_verdict(door["state"], second)

        assert [v.verdict_id for v in review_cap_verdicts.read_verdicts(door["state"], PR)] == [_vid(CAP)]


def _no_model(*_args, **_kwargs):
    def refuse(*_a):
        raise AssertionError("no model may be asked here")
    return refuse


# ---------------------------------------------------------------------------
# The state contract of cap and gate, one test per rule
# ---------------------------------------------------------------------------


class TestTheStateContract:
    @pytest.mark.parametrize("same_head", [True, False], ids=["one-head", "two-heads"])
    def test_concurrent_verdicts_on_the_round_are_serialized_and_the_first_wins(self, env, same_head):
        x1 = _commit(env["repo"], "fix1.txt", f"fix: one\n\nDispatch-ID: {X}")
        x2 = x1 if same_head else _commit(env["repo"], "fix2.txt", f"fix: two\n\nDispatch-ID: {X}")
        _capped(env)
        vid = _store_verkleinen(env["state_dir"], env["cap_head"])
        payloads = [_verdict(env, "codex_gate", tag="21", head=x1), _verdict(env, "codex_gate", tag="22", head=x2)]
        start, landed = threading.Barrier(2), []

        def run(payload):
            start.wait()
            landed.append(_write_result(env, "codex_gate", payload)[1])

        threads = [threading.Thread(target=run, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(landed) == [False, True]
        granted = _granted(env["ledger"])
        assert len(granted) == 1 and granted[0][2] == vid
        assert len([line for line in _ledger_lines(env["ledger"]) if line["seat"] == "codex_gate"]) == 2

    def test_an_interrupted_publication_passes_nothing_and_holds_the_head_until_the_operator_releases_it(
        self, env, monkeypatch,
    ):
        import closure_verifier
        import gate_recorder
        import review_rounds_cli

        x1 = _commit(env["repo"], "fix1.txt", f"fix: one\n\nDispatch-ID: {X}")
        _capped(env)
        _store_verkleinen(env["state_dir"], env["cap_head"])
        assert _codex_request(env, x1)["status"] == "requested"

        def disk_full(*_args, **_kwargs):
            raise OSError("disk full")

        with monkeypatch.context() as patch:
            patch.setattr(gate_recorder, "_write_result_atomic", disk_full)
            with pytest.raises(OSError, match="disk full"):
                _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="31", head=x1))
        pin_review_stack(monkeypatch, "codex_gate")
        door = closure_verifier.check_review_gate_for_merge(
            str(PR), "codex_gate", env["results_dir"], branch=BRANCH, head_sha=x1,
        )
        held = _codex_request(env, x1)
        released = review_rounds_cli.main(["rounds", "open", "--pr", str(PR), "--reason", "publication on x1 cut off"])
        after = _codex_request(env, x1)

        assert [line[0] for line in _granted(env["ledger"])] == [x1], "the verdict was booked before the record"
        assert door["verdict"] == "NO-GO", door["message"]
        assert held["status"] == "blocked" and held["reason"] == "verdict_on_head", held
        assert "interrupted publication" in held["reason_detail"] and "vnx review rounds open" in held["reason_detail"]
        assert released == 0
        assert after["status"] == "requested", after

    def test_the_second_seat_binds_first_and_only_a_codex_verdict_uses_the_round(self, env, monkeypatch):
        import review_cap_tiebreaker
        import review_cap_verdicts

        x1 = _commit(env["repo"], "fix1.txt", f"fix: one\n\nDispatch-ID: {X}")
        _capped(env)
        vid = _store_verkleinen(env["state_dir"], env["cap_head"])

        _on_disk, kimi_landed = _write_result(env, "kimi_gate", _verdict(env, "kimi_gate", tag="41", head=x1, blocking=2))
        still_open = _codex_request(env, x1)
        _on_disk, codex_landed = _write_result(env, "codex_gate", _verdict(env, "codex_gate", tag="42", head=x1, blocking=1))
        monkeypatch.setattr(review_cap_tiebreaker, "_default_dispatcher", _no_model)
        decision = review_cap_tiebreaker.decide(env["state_dir"], PR, for_dispatch=X)

        assert kimi_landed is True and still_open["status"] == "requested", still_open
        assert codex_landed is True and _granted(env["ledger"]) == [(x1, "fail", vid)]
        verdict = decision.verdict
        assert (verdict.cap_rule, verdict.outcome, verdict.chosen_outcome) == (
            "B3.2", review_cap_verdicts.UNAVAILABLE, review_cap_verdicts.MERGEN_MET_OI,
        )


# ---------------------------------------------------------------------------
# vnx review cap-verdict
# ---------------------------------------------------------------------------


def _answer(outcome: str, intervention: str = "", **extra: Any) -> str:
    body = {"outcome": outcome, "intervention": intervention, "rationale": "one round settles it", **extra}
    return f"My decision.\n```vnx-review-cap-tiebreak\n{json.dumps(body)}\n```\n"


@pytest.fixture
def model(monkeypatch):
    """The tiebreaker's lane, injected: answers in order, every instruction it was given."""
    import review_cap_tiebreaker

    state: Dict[str, Any] = {"answers": [], "instructions": []}

    def dispatcher(_provider, _model, instruction, _did):
        state["instructions"].append(instruction)
        return state["answers"].pop(0)

    monkeypatch.setattr(review_cap_tiebreaker, "_default_dispatcher", lambda _data_dir, _timeout: dispatcher)
    monkeypatch.setattr(review_cap_tiebreaker, "pr_diff_stat", lambda _pr: "2 files changed, +40 -3\n  a.py +30 -2")
    monkeypatch.setattr(
        review_cap_tiebreaker, "resolve_model",
        lambda config=None: ("deepseek-harness", "deepseek-v4-pro", "deepseek-v4-pro"),
    )
    return state


def _cli(*argv: str) -> int:
    import review_rounds_cli

    return review_rounds_cli.main(["cap-verdict", "--pr", str(PR), *argv])


class TestTheCapVerdictCli:
    def test_a_capped_pr_gets_one_verkleinen_and_a_repeat_asks_no_model(self, env, model):
        import review_cap_verdicts

        _capped(env)
        model["answers"].append(_answer("verkleinen", "split the parser out of the gate"))

        assert (_cli("--for-dispatch", X), _cli("--for-dispatch", X)) == (0, 0)

        (instruction,) = model["instructions"]
        assert "B3.1" in instruction and "3 blocker(s)" in instruction and "2 files changed" in instruction
        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert (verdict.project_id, verdict.head_sha, verdict.outcome) == (PID, env["cap_head"], "verkleinen")
        assert (verdict.granted_dispatch_id, verdict.intervention) == (X, "split the parser out of the gate")
        assert verdict.tiebreaker_model == "deepseek-v4-pro"
        receipts = (env["state_dir"] / "t0_receipts.ndjson").read_text(encoding="utf-8")
        assert receipts.count('"review_cap_verdict"') == 1

    def test_a_findings_list_or_two_interventions_records_nothing(self, env, model):
        import review_cap_verdicts

        _capped(env)
        model["answers"] += [
            _answer("verkleinen", "x", findings=["a", "b"]),
            _answer("verkleinen", ["split", "rename"]),
        ]

        assert (_cli("--for-dispatch", X), _cli("--for-dispatch", X)) == (2, 2)
        assert review_cap_verdicts.read_verdicts(env["state_dir"], PR) == ()

    def test_the_other_outcome_is_recorded_unavailable_and_the_cap_stays_closed(self, env, model):
        import review_cap_verdicts

        x1 = _commit(env["repo"], "fix1.txt", f"fix: one\n\nDispatch-ID: {X}")
        _capped(env)
        model["answers"].append(_answer("terug-naar-plan"))

        assert _cli("--for-dispatch", X) == 0
        (verdict,) = review_cap_verdicts.read_verdicts(env["state_dir"], PR)
        assert (verdict.outcome, verdict.chosen_outcome, verdict.granted_dispatch_id) == (
            review_cap_verdicts.UNAVAILABLE, "terug-naar-plan", "",
        )
        assert _codex_request(env, x1)["reason"] == "b3_cap"

    def test_nothing_is_decided_below_the_cap_or_without_the_dispatch_to_bind(self, env, model):
        _write_ledger(env["ledger"], [_opened(), _line("codex_gate", env["cap_head"], "fail", 2, at=1)])
        below = _cli("--for-dispatch", X)
        _capped(env)
        unbound = _cli()

        assert (below, unbound) == (2, 2)
        assert model["instructions"] == []

    def test_the_dispatch_door_lets_only_the_granted_dispatch_out_and_only_while_the_round_is_open(self, env):
        import dispatch_cli

        _capped(env)
        vid = _store_verkleinen(env["state_dir"], env["cap_head"])
        open_for_x = dispatch_cli._capped_prs_on_branch(env["state_dir"], BRANCH, X)
        open_for_other = dispatch_cli._capped_prs_on_branch(env["state_dir"], BRANCH, "20261020-someone-else")
        _capped(env, _line("codex_gate", "d" * 40, "fail", 1, at=5, granted_by=vid))
        used = dispatch_cli._capped_prs_on_branch(env["state_dir"], BRANCH, X)

        assert open_for_x == []
        assert [cap.pr_number for cap in open_for_other] == [PR]
        assert [cap.pr_number for cap in used] == [PR]

    def test_the_own_config_key_resolves_through_the_registry(self):
        import review_cap_tiebreaker

        config = review_cap_tiebreaker.load_config()
        provider, model_key, _cli_arg = review_cap_tiebreaker.resolve_model(config)

        assert config == {"provider": "deepseek_harness", "model": "deepseek-v4-pro"}
        assert (provider, model_key) == ("deepseek-harness", "deepseek-v4-pro")
