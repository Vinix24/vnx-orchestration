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
- D7: the receipt conversion is high, the dispatch door, lanes and fleet defaults stay medium.

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
from typing import Any, Dict, List

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
        checkout=kwargs.pop("checkout", forge["door"]), **kwargs,
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
        )
        summary = fgp.review_verdict(PR, head, results_dir=store["results"], branch=branch, checkout=forge["door"])

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


def _request_with_risk(store, head: str, branch: str, caller: str, forge) -> None:
    reading = review_risk_class.read_pr_diff(head, cwd=forge["door"])
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


def _merge_through_main(store, forge, monkeypatch, branch: str, head: str) -> Dict[str, Any]:
    import file_scope_overlap
    from merge_target import MergeTarget

    go = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
    pr_data = {"number": PR, "title": "R3b", "state": "OPEN", "isDraft": False, "headRefName": branch,
               "baseRefName": "main", "headRefOid": head, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    monkeypatch.setattr(pr_merge, "resolve_merge_target",
                        lambda engine_root, **_k: MergeTarget(project_root=forge["door"], repo="vnx-test/target"))
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

    assert pr_merge.main(["--pr", PR_ID, "--dispatch-id", DISPATCH]) == pr_merge.EXIT_OK
    rows = [json.loads(line) for line in store["receipts"].read_text(encoding="utf-8").splitlines() if line.strip()]
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


def _contract():
    from review_contract import Deliverable, ReviewContract

    return ReviewContract(
        pr_id=PR_ID, pr_title="R3b", feature_title="review-stopregel", branch="pr/medium", track="A",
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
