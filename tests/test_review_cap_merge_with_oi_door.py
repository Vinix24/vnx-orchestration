"""RG-4 (dispatch dlv-3e700333e5d8): a standing ``mergen-met-OI`` verdict counts at the merge door.

Plan ``claudedocs/2026-10-24-nachtrun-golf-1-3/plannen/review-rondegrens-met-tiebreaker.md``,
row PR-4 and ``## Revisie 1b`` points 3-6: the per-use approval (verdict id, PR
and head), the codex seat only (kimi keeps its own PASS and its own veto), the
aftermath booked complete first, and the refreshed ``vnx-gate/review`` before the
merge. No provider lane and no network: ``gh``, the forge publication and the
merge are replaced at the boundary; the stores, the ledger, the result records,
the open items, the approval ledger and the receipts are the real ones under
``tmp_path``.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

import review_stack_support  # puts scripts/ and scripts/lib on sys.path
from review_seat_support import pin_review_stack
from review_stack_support import BRANCH, HEAD_SHA, PR, build_project_store, isolate_config_registry

PID, HEAD = "proj-rg4", HEAD_SHA
NEW_HEAD = "7e1d2c3b4a5968778695a4b3c2d1e0f918273645"
OLD_HEADS = ["a" * 40, "b" * 40]
CLEAN = "## Findings\n\nBlocking count: 0\nAdvisory count: 0\n"
BLOCKERS = [
    {"severity": "blocking", "title": "cap check", "message": "The cap check reads the wrong ledger.",
     "file_path": "scripts/lib/review_rounds.py", "line": 812},
    {"severity": "warning", "message": "The refusal text names no way out."},
]
_VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS review_cap_verdicts (
    project_id TEXT NOT NULL DEFAULT 'proj-rg4', pr_number INTEGER NOT NULL, verdict_id TEXT NOT NULL,
    head_sha TEXT NOT NULL, cap_rule TEXT NOT NULL CHECK (cap_rule IN ('B3.1', 'B3.2')),
    outcome TEXT NOT NULL CHECK (outcome IN ('verkleinen', 'terug-naar-plan', 'mergen-met-OI',
        'outcome_unavailable_in_r2a')),
    chosen_outcome TEXT NOT NULL, intervention TEXT NOT NULL DEFAULT '', oi_ids TEXT NOT NULL DEFAULT '[]',
    granted_dispatch_id TEXT NOT NULL DEFAULT '', tiebreaker_model TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, pr_number, verdict_id)
);
"""


@pytest.fixture(autouse=True)
def _clean_config_registry(monkeypatch):
    yield from isolate_config_registry(monkeypatch)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = build_project_store(tmp_path, monkeypatch)
    import gate_request_handler
    import gate_recorder
    import gate_result_parser
    import review_risk_class

    pin_review_stack(monkeypatch, "codex_gate,kimi_gate")
    for key, value in (
        ("VNX_OVERRIDE_CI_GATE_REQUIRED", "0"), ("VNX_OVERRIDE_REVIEW_ROUND_CAP", "999"),
        ("VNX_PROJECT_ID", PID), ("VNX_PROJECT_ROOT", str(store["project_root"])),
    ):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        gate_request_handler.GateRequestHandlerMixin, "_codex_headless_available", lambda self: True)
    head: List[str] = [HEAD]
    for module in (gate_request_handler, gate_recorder, gate_result_parser):
        monkeypatch.setattr(module, "get_pr_head_sha", lambda _pr, **_kw: head[0])
    # Nothing here fetches from origin: the risk class stays the fail-closed `high`.
    monkeypatch.setattr(review_risk_class, "fetch_merge_refs", lambda *a, **k: "no fetch in this test")
    (store["project_root"] / ".vnx-project-id").write_text(PID + "\n", encoding="utf-8")
    _git(store["project_root"], "init", "-q")
    for key, value in (("user.email", "rg4@example.invalid"), ("user.name", "rg4"),
                       ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
        _git(store["project_root"], "config", key, value)
    (store["project_root"] / "base.txt").write_text("base\n", encoding="utf-8")
    _git(store["project_root"], "add", "base.txt")
    _git(store["project_root"], "commit", "-q", "-m", "base: the head the cap fell on")
    _git(store["project_root"], "remote", "add", "origin", "https://github.com/acme/project-rg4.git")
    conn = sqlite3.connect(str(store["state_dir"] / "runtime_coordination.db"))
    try:
        conn.executescript(_VERDICTS_DDL)
        conn.commit()
    finally:
        conn.close()
    store.update(head=head, ledger=store["state_dir"] / "review_gates" / "rounds.ndjson",
                 headless=store["reports_dir"] / "headless")
    store["headless"].mkdir(parents=True, exist_ok=True)
    return store


# ---------------------------------------------------------------------------
# The world: B3.2 (2 + 2 + 2 blockers), the counted codex round on the PR head
# ---------------------------------------------------------------------------


def _line(seat: str, head: str, verdict: str, blockers: int, *, at: int, report: str = "") -> Dict[str, Any]:
    return {
        "project_id": PID, "pr_number": PR, "head_sha": head, "seat": seat, "reviewer": seat,
        "takeover_from": "", "verdict": verdict, "blocking_count": blockers,
        "report_path": report or f"/r/20261028-{at:06d}-HEADLESS-{seat}-pr-{PR}-x.md",
        "dispatch_id": f"d-{at}", "recorded_at": f"2026-10-28T08:{at:02d}:00Z", "branch": BRANCH, "source": "gate",
    }


def _write_ledger(path: Path, lines: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def _record(env, gate: str, *, tag: str, blocking: int = 0) -> Dict[str, Any]:
    import gate_recorder

    report = env["headless"] / f"20261028-1000{tag}-HEADLESS-{gate}-pr-{PR}-{tag}.md"
    text = CLEAN if not blocking else f"## Findings\n\nFAIL\n\nBlocking count: {blocking}\nAdvisory count: 0\n"
    report.write_text(f"# {gate} — headless gate report\n\n**PR**: {PR}\n\n---\n\n{text}", encoding="utf-8")
    payload = {
        "gate": gate, "seat": gate, "pr_id": str(PR), "pr_number": PR, "status": "completed",
        "summary": f"{gate} execution completed", "contract_hash": f"sha256:{gate}-{tag}", "findings": [],
        "report_path": str(report), "blocking_findings": [dict(i) for i in BLOCKERS[:blocking]],
        "advisory_findings": [], "commit_sha": HEAD, "branch": BRANCH,
        "dispatch_id": f"20261028-rg4-{gate}-{tag}", "recorded_at": f"2026-10-28T10:00:{tag}Z",
    }
    gate_recorder.write_result_guarded(env["results_dir"] / f"pr-{PR}-{gate}.json", payload, gate=gate, pr_ref=str(PR))
    return payload


def _capped_world(env, *, kimi: str = "pass") -> None:
    codex, kimi_record = _record(env, "codex_gate", tag="01", blocking=2), _record(env, "kimi_gate", tag="02")
    _write_ledger(env["ledger"], [
        {"kind": "opened", "project_id": PID, "pr_number": PR, "basis": "new", "evidence": 0,
         "recorded_at": "2026-10-28T08:00:00Z", "source": "gate"},
        _line("codex_gate", OLD_HEADS[0], "fail", 2, at=1),
        _line("codex_gate", OLD_HEADS[1], "fail", 2, at=2),
        _line("codex_gate", HEAD, "fail", 2, at=3, report=codex["report_path"]),
        _line("kimi_gate", HEAD, kimi, 0, at=4, report=kimi_record["report_path"]),
    ])


def _book(env, *, approve: bool = True, aftermath: bool = True) -> Any:
    """The verdict on the PR head, its per-use approval, and the carried-out aftermath."""
    import review_cap_aftermath as rca
    import review_cap_verdicts as rcv

    rcv.record_verdict(env["state_dir"], rcv.CapVerdict(
        project_id=PID, pr_number=PR, verdict_id=rcv.verdict_id_for(PR, HEAD), head_sha=HEAD, cap_rule="B3.2",
        outcome=rcv.MERGEN_MET_OI, chosen_outcome=rcv.MERGEN_MET_OI, tiebreaker_model="deepseek-v4-pro",
        rationale="the findings did not converge", created_at="2026-10-28T09:00:00Z"))
    (stored,) = rcv.read_verdicts(env["state_dir"], PR)
    if approve:
        rcv.record_merge_approval(env["state_dir"], stored, "Vincent", note="merge met open items")
    if aftermath:
        assert rca.carry_out_merge_with_oi(env["state_dir"], stored).complete
    (standing,) = rcv.read_verdicts(env["state_dir"], PR)
    return standing


def _evaluation(env, head: str = HEAD):
    import review_seat_evaluation as rse

    return rse.evaluate_review_seats(str(PR), env["results_dir"], branch=BRANCH, head_sha=head)


_PR_DATA = {"number": PR, "title": "RG-4", "state": "OPEN", "isDraft": False, "headRefName": BRANCH,
            "baseRefName": "main", "headRefOid": HEAD, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}


def _door_edges(monkeypatch, env, *, head: str = HEAD, moved: bool = False) -> None:
    """Every gh/forge edge of the door, nothing beyond it: the seat rule and the ledgers run for real."""
    import file_scope_overlap
    import forge_gate_publisher
    import gate_obligations
    import pr_merge

    env["head"][0] = head
    go = {"verdict": "GO", "message": "groen", "overridden": False, "override_reason": None}
    pr_data = dict(_PR_DATA, headRefOid=head)
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), dict(pr_data)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: dict(pr_data))
    monkeypatch.setattr(pr_merge, "_resolve_override_reason", lambda reason: reason)
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: dict(go))
    monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: dict(go))
    monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", lambda *a, **k: dict(go))
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
    monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, h="": (True, ""))
    monkeypatch.setattr(file_scope_overlap, "warn_overlaps", lambda *a, **k: [])
    # RG-2's refresh reads the PR's current head at the forge: a moved head publishes nothing.
    monkeypatch.setattr(forge_gate_publisher, "_resolve_head_sha", lambda pr, repo: NEW_HEAD if moved else head)
    gate_obligations.register_obligation(
        env["state_dir"], dispatch_id="dlv-rg4", gate="codex_gate", pr_number=PR, pr_id=str(PR), branch=BRANCH)


def _review_gate(env, monkeypatch, *, head: str = HEAD, moved: bool = False) -> Dict[str, Any]:
    import pr_merge

    _door_edges(monkeypatch, env, head=head, moved=moved)
    return pr_merge._run_review_gate(PR)[0]


def _door(env, monkeypatch, *, head: str = HEAD, moved: bool = False) -> int:
    import pr_merge

    _door_edges(monkeypatch, env, head=head, moved=moved)
    try:
        return pr_merge.main(["--pr", str(PR), "--dispatch-id", "2026-10-28-rg4", "--dry-run"])
    except SystemExit as exc:
        return int(exc.code or 0)


# ---------------------------------------------------------------------------
# Point 3: the approval is per use — one verdict, one PR, one head
# ---------------------------------------------------------------------------


class TestCountsOnThatHeadOnly:
    def test_a_full_approval_lets_the_codex_seat_count_and_the_door_goes(self, env, monkeypatch):
        _capped_world(env)
        verdict = _book(env)

        gate = _review_gate(env, monkeypatch)

        assert gate["verdict"] == "GO", gate["message"]
        standing = gate["review_seats"]["round_ledger"]["merge_with_oi"]
        assert (standing["verdict_id"], standing["head_sha"], standing["approved"]) == (verdict.verdict_id, HEAD, True)
        assert standing["oi_ids"] == list(verdict.oi_ids)
        assert gate["review_seats"]["routes"]["codex_gate"] == "merge_with_oi"
        assert _door(env, monkeypatch) == 0, "the door's own dry run goes GO on this head"

    def test_a_new_commit_is_a_new_head_and_nothing_counts_there(self, env, monkeypatch):
        _capped_world(env)
        _book(env)

        stale, fresh = _evaluation(env, HEAD), _evaluation(env, NEW_HEAD)

        assert stale.go is True, stale.message()
        assert fresh.go is False and fresh.rounds.merge_with_oi is None
        gate = _review_gate(env, monkeypatch, head=NEW_HEAD)
        assert gate["verdict"] == "NO-GO", gate["message"]
        assert "merge_with_oi" not in gate["review_seats"]["round_ledger"]
        assert _door(env, monkeypatch, head=NEW_HEAD) != 0

    @pytest.mark.parametrize("how", ["no-approval", "no-verdict-id", "another-pr", "another-head"])
    def test_an_approval_that_does_not_name_this_use_lets_nothing_count(self, env, how):
        from decision_binding import record_operator_approval

        _capped_world(env)
        verdict = _book(env, approve=False)
        if how != "no-approval":
            fields: Dict[str, Any] = {"verdict_id": verdict.verdict_id, "pr_number": PR, "head_sha": HEAD}
            if how == "no-verdict-id":
                fields.pop("verdict_id")
            elif how == "another-pr":
                fields["pr_number"] = PR + 1
            else:
                fields["head_sha"] = NEW_HEAD
            record_operator_approval(
                verdict.verdict_id, "merge_pr", "Vincent", state_dir=env["state_dir"], fields=fields)

        evaluation = _evaluation(env)

        assert evaluation.go is False
        assert evaluation.rounds.merge_with_oi is None
        message = evaluation.message()
        assert "mergen-met-OI" in message and "operator_approval" in message
        assert verdict.verdict_id in message and f"PR #{PR}" in message


# ---------------------------------------------------------------------------
# Point 5: the open items come first, the merge second
# ---------------------------------------------------------------------------


class TestTheAftermathComesFirst:
    def test_a_blocker_without_its_open_item_is_a_no_go_with_the_reason(self, env):
        _capped_world(env)
        _book(env)
        path = env["state_dir"] / "open_items.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "items": data["items"][1:]}, indent=2), encoding="utf-8")

        evaluation = _evaluation(env)

        assert evaluation.go is False and evaluation.rounds.merge_with_oi is None
        assert "nasleep" in evaluation.message() and "dedup_key" in evaluation.message()
        assert f"review-cap:{PID}:" in evaluation.message()

    def test_a_missing_merge_ready_record_is_a_no_go_too(self, env):
        _capped_world(env)
        _book(env)
        path = env["state_dir"] / "t0_receipts.ndjson"
        kept = [raw for raw in path.read_text(encoding="utf-8").splitlines()
                if json.loads(raw).get("step") != "merge_ready"]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

        evaluation = _evaluation(env)

        assert evaluation.go is False
        assert "merge_ready" in evaluation.message()


# ---------------------------------------------------------------------------
# Point 4: the codex seat only; the other seat keeps its own veto
# ---------------------------------------------------------------------------


class TestTheSecondSeatKeepsItsVeto:
    def test_a_high_pr_still_needs_its_kimi_pass(self, env, monkeypatch):
        _capped_world(env)
        _book(env)
        (env["results_dir"] / f"pr-{PR}-kimi_gate.json").unlink()
        lines = [json.loads(raw) for raw in env["ledger"].read_text(encoding="utf-8").splitlines()]
        _write_ledger(env["ledger"], [line for line in lines if line.get("seat") != "kimi_gate"])

        evaluation = _evaluation(env)

        assert evaluation.go is False, "the kimi seat is required at high and has no PASS"
        assert evaluation.fill_for("kimi_gate").filled is False
        assert evaluation.fill_for("codex_gate").filled is True
        assert "kimi_gate" in evaluation.message()
        assert _review_gate(env, monkeypatch)["verdict"] == "NO-GO"

    def test_a_kimi_fail_stays_a_veto(self, env):
        _capped_world(env, kimi="fail")
        _record(env, "kimi_gate", tag="02", blocking=1)
        _book(env)

        evaluation = _evaluation(env)

        assert evaluation.go is False
        assert evaluation.fill_for("codex_gate").filled is True, "the verdict lifts the codex seat only"
        assert [rejection.gate for rejection in evaluation.blocking] == ["kimi_gate"]
        assert "kimi_gate" in evaluation.message() and "keurde deze kop af" in evaluation.message()


# ---------------------------------------------------------------------------
# Point 6: the required check is refreshed before the merge
# ---------------------------------------------------------------------------


class TestTheCheckIsRefreshedBeforeTheMerge:
    def test_a_moved_head_publishes_nothing_and_the_door_refuses_naming_the_check(self, env, monkeypatch):
        import forge_gate_publisher

        _capped_world(env)
        _book(env)

        gate = _review_gate(env, monkeypatch, moved=True)

        assert gate["verdict"] == "NO-GO", gate["message"]
        assert forge_gate_publisher.REVIEW_SUMMARY_CHECK_NAME in gate["message"] and HEAD[:12] in gate["message"]
        assert _door(env, monkeypatch, moved=True) != 0

    def test_a_publication_that_raises_is_a_refusal_too(self, env, monkeypatch):
        import forge_gate_publisher
        import pr_merge

        _capped_world(env)
        _book(env)
        _door_edges(monkeypatch, env)

        def boom(*args, **kwargs):
            raise RuntimeError("forge down")

        monkeypatch.setattr(forge_gate_publisher, "refresh_review_summary", boom)
        gate, _pr_data = pr_merge._run_review_gate(PR)

        assert gate["verdict"] == "NO-GO", gate["message"]
        assert forge_gate_publisher.REVIEW_SUMMARY_CHECK_NAME in gate["message"] and "forge down" in gate["message"]

    def test_the_refresh_carries_the_verdict_as_its_evidence(self, env, monkeypatch):
        import forge_gate_publisher
        import pr_merge

        _capped_world(env)
        verdict = _book(env)
        _door_edges(monkeypatch, env)
        seen: List[str] = []

        def record(pr_number, head_sha, *, b3_override, **kwargs):
            seen.append(b3_override)
            return True

        monkeypatch.setattr(forge_gate_publisher, "refresh_review_summary", record)
        gate, _pr_data = pr_merge._run_review_gate(PR)

        assert gate["verdict"] == "GO", gate["message"]
        (reason,) = seen
        assert verdict.verdict_id in reason and HEAD[:12] in reason
