"""R3b (track review-stopregel-en-lichte-stoelen): the merge door acts on the derived risk class.

Operator decision 2026-10-17, ADR-008 amendment 2026-10-23: one reviewer (the first seat of
the stack) at low and medium, every seat at high and critical. What is pinned here:

- D1: ``review_seat_evaluation.seats_for_risk`` is the one rule, in stack order, fail-closed;
- D2: the door, ``vnx pr-ready`` and ``vnx-gate/review`` read the class of the head they judge
  after fetching it into a checkout that did not have it; a head that cannot be fetched or
  read is high;
- D3: ``kimi_gate,codex_gate`` makes kimi the first seat; a named seat list keeps every seat;
- D5: the merge record carries the class, its reason and source, the stack and the required
  seats under ``first_seat_or_every_seat_by_risk``; a record under the older rule is judged
  by that rule at track closure;
- D6: a caller raise recorded on the head counts, a caller's ``low`` lowers nothing;
- D7: the receipt conversion is high, the dispatch door, lanes and fleet defaults stay medium;
- round 3: the class is read against the PR's own base (``baseRefName``), never ``origin/main``
  by default. A PR for ``release`` that carries merge logic already on ``main`` is high at the
  door, in ``vnx pr-ready`` and in ``vnx-gate/review``; a request record read against another
  base is recomputed; a base nobody read is high.

The git side is real: a bare ``origin`` in ``tmp_path`` with one branch per scenario, and a
door checkout cloned with ``main`` only, so the PR heads reach it only through the fetch.
``tests/conftest.py`` stubs the fetch offline for the rest of the suite; this file puts the
real one back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import closure_verifier
import forge_gate_publisher as fgp
import gate_obligations
import plan_scope
import pr_merge
import pr_readiness
import review_risk_class
import review_seat_evaluation as rse
import smart_router
from review_seat_support import pin_review_stack
from fixtures.closure_track_fixture import CLOSURE_TRACK_ID, seed_done_track

# Captured at import, before the autouse offline stub in tests/conftest.py replaces it.
_REAL_FETCH = review_risk_class.fetch_merge_refs

PR = 3107
PR_ID = str(PR)
STACK = ["codex_gate", "kimi_gate"]
DISPATCH = "20261023-r3b-test"
CLEAN = "# review\n\nThe reviewer read the whole diff and found nothing that holds the merge.\n"
GIT_ENV = {
    **os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "r3b", "GIT_AUTHOR_EMAIL": "r3b@example.invalid",
    "GIT_COMMITTER_NAME": "r3b", "GIT_COMMITTER_EMAIL": "r3b@example.invalid",
}
BASE_FILES = {
    "README.md": "base\n", "docs/guide.md": "guide\n", "scripts/lib/ordinary.py": "X = 1\n",
    "scripts/lib/gate_executor.py": "G = 1\n", "schemas/runtime.sql": "-- base\n",
    ".github/workflows/ci.yml": "name: ci\n", "scripts/lib/report_to_receipt_converter.py": "R = 1\n",
}
# Branch -> the files its one commit changes.
BRANCHES = {
    "pr/medium": {"docs/guide.md": "guide 2\n", "tests/test_new.py": "def test_x():\n    pass\n",
                  "scripts/lib/ordinary.py": "X = 2\n"},
    "pr/gate": {"scripts/lib/gate_executor.py": "G = 2\n"},
    "pr/schema": {"schemas/runtime.sql": "-- changed\n"},
    "pr/security": {".github/workflows/ci.yml": "name: ci2\n"},
    "pr/receipt": {"scripts/lib/report_to_receipt_converter.py": "R = 2\n"},
}
HIGH_CASES = [
    ("pr/gate", "scripts/lib/gate_executor.py", "gate-merge"),
    ("pr/schema", "schemas/runtime.sql", "schema"),
    ("pr/security", ".github/workflows/ci.yml", "security"),
    ("pr/receipt", "scripts/lib/report_to_receipt_converter.py", "gate-merge"),
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=GIT_ENV,
    ).stdout.strip()


def _commit(work: Path, files: Dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def forge(tmp_path_factory) -> Dict[str, Any]:
    """A bare origin with one branch per scenario, and a door checkout that only has main."""
    root = tmp_path_factory.mktemp("r3b-forge")
    origin, work, door = root / "origin.git", root / "work", root / "door"
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(root, "init", "-q", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    _commit(work, BASE_FILES, "base")
    _git(work, "push", "-q", "origin", "main")
    heads: Dict[str, str] = {}
    for branch, files in BRANCHES.items():
        _git(work, "checkout", "-q", "-b", branch, "main")
        heads[branch] = _commit(work, files, branch)
        _git(work, "push", "-q", "origin", branch)
    _git(work, "checkout", "-q", "-b", "pr/two-heads", "main")
    heads["two-heads-first"] = _commit(work, {"docs/guide.md": "first head\n"}, "medium first head")
    heads["two-heads-second"] = _commit(work, {"scripts/pr_merge.py": "M = 1\n"}, "adds a merge path")
    _git(work, "push", "-q", "origin", "pr/two-heads")
    _git(root, "clone", "-q", "--single-branch", "-b", "main", str(origin), str(door))
    return {"door": door, "heads": heads}


@pytest.fixture(autouse=True)
def real_fetch(monkeypatch):
    monkeypatch.setattr(review_risk_class, "fetch_merge_refs", _REAL_FETCH)


@pytest.fixture
def store(tmp_path, monkeypatch) -> Dict[str, Path]:
    data_dir = tmp_path / "data"
    state = data_dir / "state"
    for directory in (state / "review_gates" / "results", state / "review_gates" / "requests",
                      data_dir / "unified_reports", data_dir / "dispatches"):
        directory.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "PROJECT_ROOT": tmp_path, "VNX_HOME": VNX_ROOT, "VNX_DATA_DIR": data_dir,
        "VNX_STATE_DIR": state, "VNX_DISPATCH_DIR": data_dir / "dispatches",
        "VNX_LOGS_DIR": data_dir / "logs", "VNX_PIDS_DIR": data_dir / "pids",
        "VNX_LOCKS_DIR": data_dir / "locks", "VNX_REPORTS_DIR": data_dir / "unified_reports",
        "VNX_DB_DIR": data_dir / "database",
    }.items():
        monkeypatch.setenv(key, str(value))
    pin_review_stack(monkeypatch, ",".join(STACK))
    return {"state": state, "results": state / "review_gates" / "results",
            "requests": state / "review_gates" / "requests", "reports": data_dir / "unified_reports",
            "receipts": state / "t0_receipts.ndjson"}


def _pass(store, gate: str, head: str, branch: str) -> None:
    report = store["reports"] / f"{gate}-{head[:8]}.md"
    report.write_text(CLEAN, encoding="utf-8")
    (store["results"] / f"pr-{PR}-{gate}.json").write_text(json.dumps({
        "gate": gate, "pr_id": PR_ID, "pr_number": PR, "status": "pass", "commit_sha": head,
        "branch": branch, "contract_hash": f"sha256:{gate}", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [], "evidence_source": "live", "test_run": False,
    }), encoding="utf-8")


def _door(store, forge, branch: str, head: str, **kwargs: Any) -> Dict[str, Any]:
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, "codex_gate", store["results"], branch=branch, head_sha=head,
        checkout=kwargs.pop("checkout", forge["door"]), base_branch=kwargs.pop("base_branch", "main"), **kwargs,
    )


# ---------------------------------------------------------------------------
# D1: one rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("risk, expected", [
    ("low", ("codex_gate",)), ("medium", ("codex_gate",)), (" MEDIUM ", ("codex_gate",)),
    ("high", tuple(STACK)), ("critical", tuple(STACK)),
    (None, tuple(STACK)), ("", tuple(STACK)), ("unknown", tuple(STACK)), (7, tuple(STACK)),
])
def test_seats_for_risk_narrows_only_on_a_positive_low_or_medium(risk, expected):
    assert rse.seats_for_risk(STACK, risk) == expected


def test_seats_for_risk_reads_a_risk_class_object():
    medium = review_risk_class.derive_risk_class(review_risk_class.DiffReading(("docs/a.md",)))
    high = review_risk_class.derive_risk_class(review_risk_class.DiffReading(None, error="unreadable"))
    assert (rse.seats_for_risk(STACK, medium), rse.seats_for_risk(STACK, high)) == (("codex_gate",), tuple(STACK))


# ---------------------------------------------------------------------------
# D2: the door, on a head the checkout did not have
# ---------------------------------------------------------------------------


def test_a_medium_head_merges_on_the_first_seat_alone(store, forge):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    gate = _door(store, forge, "pr/medium", head)

    seats = gate["review_seats"]
    assert gate["verdict"] == "GO", gate["message"]
    assert (seats["rule"], seats["risk_class"], seats["required_seats"]) == (
        rse.RISK_SEAT_RULE, "medium", ["codex_gate"])
    assert seats["risk_reason"] == "no high-risk path in 3 file(s)"
    assert "risicoklasse medium" in gate["message"]


@pytest.mark.parametrize("branch, path, category", HIGH_CASES)
def test_a_high_head_needs_a_pass_of_every_seat(store, forge, branch, path, category):
    head = forge["heads"][branch]
    _pass(store, "codex_gate", head, branch)

    refused = _door(store, forge, branch, head)

    assert refused["verdict"] == "NO-GO"
    assert f"risicoklasse high ({category} path {path})" in refused["message"]
    assert "vereiste seats codex_gate, kimi_gate" in refused["message"]
    assert refused["review_seats"]["required_seats"] == STACK

    _pass(store, "kimi_gate", head, branch)
    assert _door(store, forge, branch, head)["verdict"] == "GO"


def test_a_kimi_pass_on_another_head_does_not_fill_the_seat(store, forge):
    head = forge["heads"]["pr/gate"]
    _pass(store, "codex_gate", head, "pr/gate")
    _pass(store, "kimi_gate", forge["heads"]["pr/schema"], "pr/gate")

    assert _door(store, forge, "pr/gate", head)["verdict"] == "NO-GO"


def test_a_later_head_that_adds_a_merge_path_is_high(store, forge):
    first, second = forge["heads"]["two-heads-first"], forge["heads"]["two-heads-second"]
    _pass(store, "codex_gate", first, "pr/two-heads")
    assert _door(store, forge, "pr/two-heads", first)["verdict"] == "GO"

    _pass(store, "codex_gate", second, "pr/two-heads")
    refused = _door(store, forge, "pr/two-heads", second)

    assert refused["verdict"] == "NO-GO"
    assert "gate-merge path scripts/pr_merge.py" in refused["message"]


def test_a_head_that_cannot_be_fetched_is_high(store, forge):
    head = "a" * 40
    _pass(store, "codex_gate", head, "pr/gone")

    refused = _door(store, forge, "pr/gone", head)

    assert refused["verdict"] == "NO-GO"
    assert refused["review_seats"]["risk_class"] == "high"
    assert "could not be fetched" in refused["review_seats"]["risk_reason"]


def test_a_head_whose_diff_cannot_be_read_is_high(store, forge):
    head = "b" * 40  # the branch fetches; this sha is on no branch
    _pass(store, "codex_gate", head, "pr/medium")

    refused = _door(store, forge, "pr/medium", head)

    assert refused["verdict"] == "NO-GO"
    assert "no merge base" in refused["review_seats"]["risk_reason"]


def test_a_store_without_a_checkout_is_high(store, forge):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    refused = _door(store, forge, "pr/medium", head, checkout=None)

    assert refused["verdict"] == "NO-GO"
    assert "checkout of the review store cannot be established" in refused["review_seats"]["risk_reason"]


def test_ready_and_the_summary_check_give_the_door_verdict(store, forge):
    for branch, door_says, summary_says in (
        ("pr/medium", "GO", fgp.CONCLUSION_SUCCESS), ("pr/gate", "NO-GO", fgp.CONCLUSION_ACTION_REQUIRED),
    ):
        head = forge["heads"][branch]
        _pass(store, "codex_gate", head, branch)

        door = _door(store, forge, branch, head)
        evidence = pr_readiness.collect_gate_evidence(
            PR, ["codex_gate"], store["state"], branch=branch, head_sha=head, checkout=forge["door"],
            base_branch="main",
        )
        summary = fgp.review_verdict(
            PR, head, results_dir=store["results"], branch=branch, checkout=forge["door"], base_branch="main",
        )

        ready = "GO" if all(line.verdict == "GO" for line in evidence) else "NO-GO"
        assert (door["verdict"], ready, summary.conclusion) == (door_says, door_says, summary_says), branch
        assert [line.gate for line in evidence] == door["review_seats"]["required_seats"]


# ---------------------------------------------------------------------------
# D3: the first seat is the stack's; a named list keeps every seat
# ---------------------------------------------------------------------------


def test_with_kimi_first_kimi_is_the_one_seat_at_medium(store, forge, monkeypatch):
    pin_review_stack(monkeypatch, "kimi_gate,codex_gate")
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")
    assert _door(store, forge, "pr/medium", head)["verdict"] == "NO-GO"

    _pass(store, "kimi_gate", head, "pr/medium")
    (store["results"] / f"pr-{PR}-codex_gate.json").unlink()
    gate = _door(store, forge, "pr/medium", head)

    assert gate["verdict"] == "GO", gate["message"]
    assert gate["review_seats"]["required_seats"] == ["kimi_gate"]


def test_a_named_seat_list_keeps_every_seat_at_medium(store, forge):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    refused = _door(store, forge, "pr/medium", head, seats=STACK, seats_source=rse.SOURCE_EXPLICIT)

    assert refused["verdict"] == "NO-GO"
    assert refused["review_seats"]["rule"] == rse.SEAT_RULE


# ---------------------------------------------------------------------------
# D6: a caller raise on the head counts; nothing lowers
# ---------------------------------------------------------------------------


def _request_with_risk(store, head: str, branch: str, caller: str, forge, base_ref: str = "origin/main") -> None:
    reading = review_risk_class.read_pr_diff(head, base_ref=base_ref, cwd=forge["door"])
    risk = review_risk_class.derive_risk_class(reading, caller_class=caller, head_sha=head)
    (store["requests"] / f"pr-{PR}-codex_gate.json").write_text(json.dumps({
        "gate": "codex_gate", "pr_number": PR, "branch": branch, "status": "requested",
        "commit_sha": head, "requested_at": "2026-10-23T10:00:00Z", "review_risk": risk.to_record(),
    }), encoding="utf-8")


def test_a_caller_raise_recorded_on_the_head_requires_every_seat(store, forge):
    head = forge["heads"]["pr/medium"]
    _door(store, forge, "pr/medium", head)  # fetches the head into the door checkout
    _request_with_risk(store, head, "pr/medium", "high", forge)
    _pass(store, "codex_gate", head, "pr/medium")

    refused = _door(store, forge, "pr/medium", head)

    assert refused["verdict"] == "NO-GO"
    assert "raised by the caller's --risk-class high" in refused["review_seats"]["risk_reason"]


def test_a_caller_low_on_a_high_head_lowers_nothing(store, forge):
    head = forge["heads"]["pr/gate"]
    _door(store, forge, "pr/gate", head)
    _request_with_risk(store, head, "pr/gate", "low", forge)
    _pass(store, "codex_gate", head, "pr/gate")

    assert _door(store, forge, "pr/gate", head)["review_seats"]["risk_class"] == "high"


# ---------------------------------------------------------------------------
# D5: the merge record, and track closure of older merges
# ---------------------------------------------------------------------------


def _merge_through_door(
    store, door: Path, monkeypatch, branch: str, head: str, base: str = "main",
) -> Tuple[int, List[Dict[str, Any]]]:
    """``pr_merge.main`` on the PR (its base named ``base``), merging into ``door``: exit code and receipts."""
    import file_scope_overlap
    from merge_target import MergeTarget

    go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
    pr_data = {"number": PR, "title": "R3b", "state": "OPEN", "isDraft": False, "headRefName": branch,
               "baseRefName": base, "headRefOid": head, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    monkeypatch.setattr(pr_merge, "resolve_merge_target",
                        lambda engine_root, **_k: MergeTarget(project_root=door, repo="vnx-test/target"))
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (dict(go), dict(pr_data)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: dict(pr_data))
    monkeypatch.setattr(pr_merge, "_resolve_override_reason", lambda reason: reason)
    for name in ("_run_adr_gate", "_run_contract_invalid_gate", "_run_branch_protection_gate"):
        monkeypatch.setattr(pr_merge, name, lambda *a, **k: dict(go))
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
    monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, h="": (True, ""))
    monkeypatch.setattr(file_scope_overlap, "warn_overlaps", lambda *a, **k: [])
    gate_obligations.register_obligation(
        store["state"], dispatch_id=DISPATCH, gate="codex_gate", pr_number=PR, pr_id=PR_ID, branch=branch,
    )

    code = pr_merge.main(["--pr", PR_ID, "--dispatch-id", DISPATCH])
    lines = store["receipts"].read_text(encoding="utf-8").splitlines() if store["receipts"].exists() else []
    return code, [json.loads(line) for line in lines if line.strip()]


def _merge_through_main(store, forge, monkeypatch, branch: str, head: str) -> Dict[str, Any]:
    code, rows = _merge_through_door(store, forge["door"], monkeypatch, branch, head)
    assert code == pr_merge.EXIT_OK
    return [row for row in rows if row.get("event_type") == "pr_merged"][-1]["review_seats"]


def test_the_merge_record_names_the_class_and_the_required_seats(store, forge, monkeypatch):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    seats = _merge_through_main(store, forge, monkeypatch, "pr/medium", head)

    assert seats["rule"] == "first_seat_or_every_seat_by_risk"
    assert (seats["risk_class"], seats["risk_reason"], seats["risk_source"]) == (
        "medium", "no high-risk path in 3 file(s)", review_risk_class.SOURCE_RECOMPUTED)
    assert (seats["stack"], seats["required_seats"], seats["seats"]) == (STACK, ["codex_gate"], ["codex_gate"])
    assert seats["filled_by"] == {"codex_gate": "codex_gate"}


def _contract(branch: str = "pr/medium"):
    from review_contract import Deliverable, ReviewContract

    return ReviewContract(
        pr_id=PR_ID, pr_title="R3b", feature_title="review-stopregel", branch=branch, track="A",
        risk_class="low", merge_policy="standard", closure_stage="ready",
        deliverables=[Deliverable(category="impl", description="seat rule")], review_stack=STACK,
    )


def _closure(store, rule: str, seats: List[str], head: str) -> Dict[str, str]:
    record = {"rule": rule, "pr_id": PR_ID, "head_sha": head, "seats": seats, "source": "config"}
    checks = closure_verifier._validate_review_evidence(
        _contract(), store["results"], branch="pr/medium",
        merge_record={"event_type": "pr_merged", "pr_number": PR, "review_seats": record},
    )
    return {check.name: check.status for check in checks if check.name.startswith("seat_")}


def test_closure_keeps_judging_an_older_merge_by_the_older_rule(store, forge):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    assert _closure(store, rse.SEAT_RULE, STACK, head) == {"seat_codex_gate": "PASS", "seat_kimi_gate": "FAIL"}
    # An overridden merge before R3b recorded no seats: still every seat, no class.
    assert _closure(store, rse.SEAT_RULE, [], head) == {"seat_codex_gate": "PASS", "seat_kimi_gate": "FAIL"}


def test_closure_of_a_merge_under_the_new_rule_reads_its_required_seats(store, forge):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    assert _closure(store, rse.RISK_SEAT_RULE, ["codex_gate"], head) == {"seat_codex_gate": "PASS"}


# ---------------------------------------------------------------------------
# Round 2: the whole track closure follows the seats the merge record required
# ---------------------------------------------------------------------------


FAIL_REPORT = (
    "# review\n\nThe change breaks every caller.\n\n```json\n"
    '{"verdict": "fail", "findings": [{"severity": "blocking", "message": "breaks every caller"}]}\n```\n'
)
def _kimi(store, status: str, head: str, branch: str) -> None:
    """kimi's own record on ``head``: a PASS with a clean report, or a FAIL whose report says so too."""
    fails = status == "fail"
    report = store["reports"] / f"kimi_gate-{status}-{head[:8]}.md"
    report.write_text(FAIL_REPORT if fails else CLEAN, encoding="utf-8")
    blocking = [{"severity": "blocking", "message": "breaks every caller"}] if fails else []
    (store["results"] / f"pr-{PR}-kimi_gate.json").write_text(json.dumps({
        "gate": "kimi_gate", "pr_id": PR_ID, "pr_number": PR, "status": status, "commit_sha": head,
        "branch": branch, "contract_hash": "sha256:kimi_gate", "report_path": str(report),
        "blocking_findings": blocking, "blocking_count": len(blocking), "advisory_findings": [],
        "evidence_source": "live", "test_run": False,
    }), encoding="utf-8")


def _record_merge(store, review_seats: Any) -> None:
    """Append a ``pr_merged`` receipt for the PR; track closure reads the latest one."""
    line = {"event_type": "pr_merged", "pr_number": PR, "conclusion": "merged"}
    if review_seats is not None:
        line["review_seats"] = review_seats
    with open(store["receipts"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _track_closure(store, tmp_path, monkeypatch, branch: str) -> Dict[str, Any]:
    """The complete ``verify_closure`` verdict of the merged PR, git and GitHub probes answered offline.

    The track is done in the tracks DB; the merge evidence is the test's own ``pr_merged`` receipt.
    """
    if not (store["state"] / "runtime_coordination.db").exists():
        seed_done_track(store["state"], monkeypatch, pr_numbers=(PR,), merged=False)
    claim = tmp_path / "closure_claim.json"
    claim.write_text(json.dumps({"test_files": ["closure_claim.json"], "test_command": "pytest -q"}), encoding="utf-8")
    merged = {"number": PR, "state": "MERGED", "mergeCommit": {"oid": "c" * 40}}
    monkeypatch.setattr(closure_verifier, "_remote_branch_exists", lambda name, root: True)
    monkeypatch.setattr(closure_verifier, "_find_branch_pr", lambda name: dict(merged))
    monkeypatch.setattr(closure_verifier, "_merge_commit_on_main", lambda oid, root: True)
    return closure_verifier.verify_closure(
        project_root=tmp_path, track_id=CLOSURE_TRACK_ID,
        branch=branch, mode="post_merge", claim_file=claim, review_contract=_contract(branch),
        gate_results_dir=store["results"],
    )


def _failed(closure: Dict[str, Any]) -> set:
    return {check["name"] for check in closure["checks"] if check["status"] != "PASS"}


def test_a_medium_merge_on_codex_alone_passes_track_closure(store, forge, tmp_path, monkeypatch):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")
    seats = _merge_through_main(store, forge, monkeypatch, "pr/medium", head)
    assert (seats["risk_class"], seats["required_seats"], seats["stack"]) == ("medium", ["codex_gate"], STACK)

    closure = _track_closure(store, tmp_path, monkeypatch, "pr/medium")

    assert closure["verdict"] == "pass", [c for c in closure["checks"] if c["status"] != "PASS"]
    not_required = next(c for c in closure["checks"] if c["name"] == "review_seats_not_required")
    assert "kimi_gate" in not_required["detail"] and "risicoklasse medium" in not_required["detail"]


# The door's verdict on a non-required seat that did run (measured, not assumed): a FAIL on the
# merged head blocks (``review_seat_evaluation.blocking_records`` reads every review peer on the
# head); a PASS is not asked for; a record on another head is out of scope.
NOT_REQUIRED_SEAT_CASES = [
    ("fail", "merged", "NO-GO", "fail"),
    ("pass", "merged", "GO", "pass"),
    ("fail", "other", "GO", "pass"),
    ("pass", "other", "GO", "pass"),
]


@pytest.mark.parametrize("status, on, door_says, closure_says", NOT_REQUIRED_SEAT_CASES)
def test_closure_gives_the_door_verdict_on_a_seat_the_class_did_not_require(
    store, forge, tmp_path, monkeypatch, status, on, door_says, closure_says,
):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")
    _merge_through_main(store, forge, monkeypatch, "pr/medium", head)
    _kimi(store, status, head if on == "merged" else forge["heads"]["pr/schema"], "pr/medium")

    door = _door(store, forge, "pr/medium", head)
    closure = _track_closure(store, tmp_path, monkeypatch, "pr/medium")

    assert (door["verdict"], closure["verdict"]) == (door_says, closure_says), door["message"]
    assert _failed(closure) == ({"review_seats_blocking"} if closure_says == "fail" else set())


def test_a_high_merge_still_needs_every_seat_at_track_closure(store, forge, tmp_path, monkeypatch):
    head = forge["heads"]["pr/gate"]
    _pass(store, "codex_gate", head, "pr/gate")
    _pass(store, "kimi_gate", head, "pr/gate")
    seats = _merge_through_main(store, forge, monkeypatch, "pr/gate", head)
    assert (seats["risk_class"], seats["required_seats"]) == ("high", STACK)

    assert _track_closure(store, tmp_path, monkeypatch, "pr/gate")["verdict"] == "pass"

    (store["results"] / f"pr-{PR}-kimi_gate.json").unlink()
    closure = _track_closure(store, tmp_path, monkeypatch, "pr/gate")
    assert (closure["verdict"], _failed(closure)) == ("fail", {"seat_kimi_gate"})


@pytest.mark.parametrize("review_seats, failing", [
    ({"rule": rse.SEAT_RULE, "pr_id": PR_ID, "head_sha": "", "seats": STACK, "source": "config"}, "seat_kimi_gate"),
    (None, "gate_kimi_gate"),
], ids=["older-seat-rule", "no-seat-record"])
def test_an_older_merge_keeps_demanding_every_gate_at_track_closure(
    store, forge, tmp_path, monkeypatch, review_seats, failing,
):
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")
    if review_seats is not None:
        review_seats = dict(review_seats, head_sha=head)
    _record_merge(store, review_seats)

    closure = _track_closure(store, tmp_path, monkeypatch, "pr/medium")

    assert (closure["verdict"], _failed(closure)) == ("fail", {failing})


def _without(key: str):
    return lambda record: {name: value for name, value in record.items() if name != key}


# A record narrows the per-gate checks only when it says so positively; every other shape
# leaves kimi_gate demanded, as on the head before this round (fail-closed). With its seats
# kept, kimi is demanded by the per-gate check; without them closure resolves the seats the
# way the door does, and a store whose checkout cannot be established reads high (every seat).
MALFORMED_RECORDS = {
    "stack-missing": (_without("stack"), "gate_kimi_gate"),
    "stack-none": (lambda r: dict(r, stack=None), "gate_kimi_gate"),
    "stack-a-string": (lambda r: dict(r, stack="codex_gate,kimi_gate"), "gate_kimi_gate"),
    "risk-class-missing": (_without("risk_class"), "gate_kimi_gate"),
    "risk-class-none": (lambda r: dict(r, risk_class=None), "gate_kimi_gate"),
    "risk-class-unknown": (lambda r: dict(r, risk_class="moderate"), "gate_kimi_gate"),
    "risk-class-high-with-one-seat": (lambda r: dict(r, risk_class="high"), "gate_kimi_gate"),
    "required-seats-missing": (_without("required_seats"), "gate_kimi_gate"),
    "required-seats-none": (lambda r: dict(r, required_seats=None), "gate_kimi_gate"),
    "required-seats-differ": (lambda r: dict(r, required_seats=STACK), "gate_kimi_gate"),
    "seats-missing": (_without("seats"), "seat_kimi_gate"),
    "seats-none": (lambda r: dict(r, seats=None), "seat_kimi_gate"),
    "seats-empty": (lambda r: dict(r, seats=[], required_seats=[]), "seat_kimi_gate"),
}


@pytest.mark.parametrize("shape", sorted(MALFORMED_RECORDS))
def test_a_record_that_does_not_say_which_seats_it_left_out_narrows_nothing(
    store, forge, tmp_path, monkeypatch, shape,
):
    mutate, failing = MALFORMED_RECORDS[shape]
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")
    seats = _merge_through_main(store, forge, monkeypatch, "pr/medium", head)
    _record_merge(store, mutate(seats))

    closure = _track_closure(store, tmp_path, monkeypatch, "pr/medium")

    assert (closure["verdict"], _failed(closure)) == ("fail", {failing})
    assert "review_seats_not_required" not in {check["name"] for check in closure["checks"]}


# ---------------------------------------------------------------------------
# D7: the receipt conversion is high; door, lanes and fleet defaults stay medium
# ---------------------------------------------------------------------------


RECEIPT_PATHS = [
    "scripts/lib/report_to_receipt_converter.py", "scripts/lib/append_receipt_internals/validation.py",
    "scripts/append_receipt.py", "scripts/lib/report_body_contract.py", "scripts/lib/receipt_outcome.py",
    "scripts/lib/receipt_verdict.py", "scripts/lib/receipt_processor/rp_append.sh", "scripts/receipt_processor.sh",
]
MEDIUM_PATHS = [
    "scripts/lib/dispatch_cli.py", "scripts/lib/providers/x.py", "agents/x/CLAUDE.md",
    "scripts/lib/subprocess_dispatch.py", "scripts/lib/provider_dispatch.py", "scripts/lib/subprocess_adapter.py",
    "skills/x/SKILL.md", ".claude/skills/x/SKILL.md", "scripts/lib/receipt_schema.py",
    "scripts/lib/governance_receipts.py", "scripts/lib/receipt_provenance.py",
]


@pytest.mark.parametrize("path", RECEIPT_PATHS)
def test_a_receipt_conversion_path_is_high_and_needs_a_plan_gate(path):
    risk = review_risk_class.derive_risk_class(review_risk_class.DiffReading((path,)))
    assert (risk.risk_class, smart_router.plan_scope_path_category(path)) == ("high", "gate-merge")
    assert plan_scope.dispatch_rule([path]) == f"gate-merge path {path}"


@pytest.mark.parametrize("path", MEDIUM_PATHS)
def test_the_dispatch_door_lanes_and_fleet_defaults_stay_medium(path):
    risk = review_risk_class.derive_risk_class(review_risk_class.DiffReading((path,)))
    assert (risk.risk_class, smart_router.plan_scope_path_category(path)) == ("medium", None)


# ---------------------------------------------------------------------------
# vnx gate names the seats the door requires
# ---------------------------------------------------------------------------


def test_vnx_gate_names_the_class_and_the_seats_the_door_requires(store, capsys):
    import review_gate_manager

    base = {"review_seats": {"seats": STACK, "source": "config"}}
    review_gate_manager._print_risk_class({**base, "review_risk": {"risk_class": "medium", "reason": "no high-risk path"}})
    review_gate_manager._print_risk_class({**base, "review_risk": {"risk_class": "high", "reason": "schema path x"}})

    err = capsys.readouterr().err.splitlines()
    assert err == [
        "RISK CLASS medium: no high-risk path; the merge door requires codex_gate of stack codex_gate, kimi_gate",
        "RISK CLASS high: schema path x; the merge door requires codex_gate, kimi_gate of stack codex_gate, kimi_gate",
    ]


# ---------------------------------------------------------------------------
# Round 3: the class is read against the PR's own base, not origin/main
# ---------------------------------------------------------------------------


RELEASE_PR_BRANCH = "pr/release-docs"


@pytest.fixture(scope="module")
def release_forge(tmp_path_factory) -> Dict[str, Any]:
    """``release`` one merge-logic commit behind ``main``; a PR for ``release``, off ``main``, adds a doc.

    Against ``origin/main`` the PR is the doc alone (medium). Against ``origin/release``, the base
    the PR names, it also carries ``scripts/pr_merge.py`` (high). The door checkout has ``main``
    only: the PR head and ``release`` reach it only through the fetch.
    """
    root = tmp_path_factory.mktemp("r3b-release-forge")
    origin, work, door = root / "origin.git", root / "work", root / "door"
    _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(root, "init", "-q", "-b", "main", str(work))
    _git(work, "remote", "add", "origin", str(origin))
    release = _commit(work, BASE_FILES, "base")
    _git(work, "push", "-q", "origin", "main", "main:release")
    main = _commit(work, {"scripts/pr_merge.py": "M = 1\n"}, "merge logic lands on main")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", RELEASE_PR_BRANCH)
    head = _commit(work, {"docs/guide.md": "release notes\n"}, "a doc change for release")
    _git(work, "push", "-q", "origin", RELEASE_PR_BRANCH)
    _git(root, "clone", "-q", "--single-branch", "-b", "main", str(origin), str(door))
    return {"door": door, "head": head, "release": release, "main": main}


def _release_class(store, release_forge, base: Any) -> review_risk_class.RiskClass:
    return review_risk_class.merge_head_risk_class(
        store["requests"], PR_ID, release_forge["head"], base_branch=base, branch=RELEASE_PR_BRANCH,
        checkout=release_forge["door"],
    )


def test_the_release_pr_is_medium_against_main_and_high_against_its_own_base(store, release_forge):
    against_main = _release_class(store, release_forge, "main")
    against_release = _release_class(store, release_forge, "release")

    assert (against_main.risk_class, against_main.base_ref, against_main.merge_base) == (
        "medium", "origin/main", release_forge["main"])
    assert (against_release.risk_class, against_release.base_ref, against_release.merge_base) == (
        "high", "origin/release", release_forge["release"])
    assert against_release.deciding == (("scripts/pr_merge.py", "gate-merge"),)


@pytest.mark.parametrize("base, why", [
    (None, "is not known"), ("", "is not known"), (["release"], "is not known"),
    ("release:refs/heads/main", "no plain branch name"), ("-release", "no plain branch name"),
    ("gone", "could not be fetched"),
], ids=["none", "empty", "malformed", "refspec", "option", "not-on-origin"])
def test_a_base_that_is_missing_malformed_or_cannot_be_fetched_is_high(store, release_forge, base, why):
    risk = _release_class(store, release_forge, base)

    assert risk.risk_class == "high"
    assert why in risk.reason
    assert risk.reason.startswith("fail-closed:")


def test_the_door_refuses_a_release_pr_on_its_first_seat_alone(store, release_forge, monkeypatch):
    """Codex's reproduction on #2193: the door read origin/main and merged a high PR on codex alone."""
    head = release_forge["head"]
    _pass(store, "codex_gate", head, RELEASE_PR_BRANCH)

    code, rows = _merge_through_door(store, release_forge["door"], monkeypatch, RELEASE_PR_BRANCH, head, base="release")

    assert code != pr_merge.EXIT_OK
    assert not [row for row in rows if row.get("event_type") == "pr_merged"]
    refused = [row for row in rows if row.get("event_type") == "pr_merge_refused"][-1]
    review = next(gate for gate in refused["preflight_gates"] if gate["gate"] == "review")
    assert review["verdict"] == "NO-GO"
    assert "risicoklasse high (gate-merge path scripts/pr_merge.py)" in review["message"]
    assert "vereiste seats codex_gate, kimi_gate" in review["message"]


def test_a_release_pr_with_every_seat_merges_and_the_record_names_its_base(store, release_forge, monkeypatch):
    head = release_forge["head"]
    _pass(store, "codex_gate", head, RELEASE_PR_BRANCH)
    _pass(store, "kimi_gate", head, RELEASE_PR_BRANCH)

    code, rows = _merge_through_door(store, release_forge["door"], monkeypatch, RELEASE_PR_BRANCH, head, base="release")

    assert code == pr_merge.EXIT_OK
    seats = [row for row in rows if row.get("event_type") == "pr_merged"][-1]["review_seats"]
    assert (seats["risk_class"], seats["required_seats"], seats["base_branch"]) == ("high", STACK, "release")
    assert (seats["risk"]["base_ref"], seats["risk"]["merge_base"]) == ("origin/release", release_forge["release"])


def test_a_request_record_read_against_main_is_recomputed_at_the_door(store, release_forge):
    head = release_forge["head"]
    _release_class(store, release_forge, "release")  # fetches the head into the door checkout
    _request_with_risk(store, head, RELEASE_PR_BRANCH, "low", release_forge, base_ref="origin/main")
    _pass(store, "codex_gate", head, RELEASE_PR_BRANCH)

    refused = _door(store, release_forge, RELEASE_PR_BRANCH, head, base_branch="release")

    seats = refused["review_seats"]
    assert refused["verdict"] == "NO-GO"
    assert (seats["risk_class"], seats["risk_source"]) == ("high", review_risk_class.SOURCE_RECOMPUTED)
    assert "against origin/main" in seats["risk"]["note"] and "against origin/release" in seats["risk"]["note"]


def test_a_door_call_without_a_base_reads_high_not_main(store, forge):
    """The PR on main, but the caller read no base: high (every seat), never the class against main."""
    head = forge["heads"]["pr/medium"]
    _pass(store, "codex_gate", head, "pr/medium")

    refused = _door(store, forge, "pr/medium", head, base_branch=None)

    assert refused["verdict"] == "NO-GO"
    assert refused["review_seats"]["risk_class"] == "high"
    assert "the PR's base branch is not known" in refused["review_seats"]["risk_reason"]


def _ready(store, release_forge, monkeypatch, base: str) -> str:
    """``vnx pr-ready`` (``pr_readiness.assess``) on the release PR, its facts naming ``base``."""
    import ci_contexts

    head = release_forge["head"]
    facts = {"number": PR, "state": "OPEN", "isDraft": False, "headRefName": RELEASE_PR_BRANCH,
             "headRefOid": head, "baseRefName": base}

    def offline(*_a, **_k):
        raise ci_contexts.CIContextsError("offline in the unit test")

    monkeypatch.setattr(pr_readiness, "fetch_pr_facts", lambda *_a, **_k: dict(facts))
    monkeypatch.setattr(pr_readiness.ci_contexts, "evaluate_commit", offline)
    report = pr_readiness.assess(PR, release_forge["door"], store["state"])
    assert not report.gates_error, report.gates_error
    return "GO" if report.gates and all(line.verdict == "GO" for line in report.gates) else "NO-GO"


def _summary_check(store, release_forge, monkeypatch, base: str) -> Tuple[str, List[str]]:
    """``vnx-gate/review`` as the recorder publishes it (dry run): the base read from the PR in the store's repo."""
    import forge_project_target
    from forge_project_target import ForgeTarget

    target = ForgeTarget(project_id="r3b", project_root=release_forge["door"], owner_repo="vnx-test/release")
    asked: List[str] = []

    def gh_pr_json(pr_number, field, repo):
        asked.append(f"{repo}:{field}")
        return {field: base if field == "baseRefName" else None}

    monkeypatch.setattr(fgp, "resolve_forge_target", lambda _results_dir: target)
    monkeypatch.setattr(forge_project_target, "resolve_forge_target", lambda _results_dir: target)
    monkeypatch.setattr(fgp, "_gh_pr_json", gh_pr_json)
    payload = fgp.publish_review_summary(
        PR, release_forge["head"], results_dir=store["results"], branch=RELEASE_PR_BRANCH, dry_run=True,
    )
    return payload["conclusion"], asked


@pytest.mark.parametrize("base, door_says, summary_says", [
    ("release", "NO-GO", fgp.CONCLUSION_ACTION_REQUIRED),
    ("main", "GO", fgp.CONCLUSION_SUCCESS),
])
def test_ready_and_the_summary_check_read_the_base_the_pr_names(
    store, release_forge, monkeypatch, base, door_says, summary_says,
):
    head = release_forge["head"]
    _pass(store, "codex_gate", head, RELEASE_PR_BRANCH)
    gate_obligations.register_obligation(
        store["state"], dispatch_id=DISPATCH, gate="codex_gate", pr_number=PR, pr_id=PR_ID, branch=RELEASE_PR_BRANCH,
    )

    door = _door(store, release_forge, RELEASE_PR_BRANCH, head, base_branch=base)
    ready = _ready(store, release_forge, monkeypatch, base)
    summary, asked = _summary_check(store, release_forge, monkeypatch, base)

    assert (door["verdict"], ready, summary) == (door_says, door_says, summary_says), door["message"]
    assert asked[0] == "vnx-test/release:baseRefName"
