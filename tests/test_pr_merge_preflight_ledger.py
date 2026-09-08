#!/usr/bin/env python3
"""Golf Bx, D3 (OI-1665): the five preflight verdicts land in the ledger — on
the merge AND on a refusal.

Measured on main before this change: ``pr_merge.py`` printed five preflight
verdicts (CI, review, ADR, contract_invalid, branch-protection) to the
terminal and the ``pr_merged`` receipt knew none of them. A refused merge left
no record at all: the five ``return EXIT_ERROR`` branches in ``main()`` write
nothing, so "this door refused PR #N and this gate is why" existed only in a
terminal that is gone the moment the pane closes.

The door SHORT-CIRCUITS on the first NO-GO, and that is deliberate
(fail-closed, golf A/B). So "all five verdicts after a refusal" is not
achievable and is not what these tests demand. What they demand is that a
reader can tell three states apart:

  GO             — this gate ran and approved
  NO-GO          — this gate ran and refused
  not_evaluated  — this gate never ran, because an earlier one refused

The third value is the deliverable: without it, a gate that never ran is
absent from the record, and absence reads as silent approval.

Three test groups, matching the dispatch's three red tests:

1. ``TestRefusalRecord`` — a NO-GO leaves a readable record whose boundary
   MOVES with the failing gate's position in the chain (three different
   failing positions covered: 1, 2 and 4).
2. ``TestMergedReceiptCarriesAllFive`` — a merge stamps all five verdicts on
   the existing ``pr_merged`` receipt (no second evidence place beside it).
3. ``TestShortCircuitUnchanged`` — after a NO-GO on gate two, gate three is
   provably never called. This test exists to stop a later hand from making
   the refusal record "more complete" by removing the short-circuit.

Only the two gh-driven gates (``_run_ci_gate``/``_run_review_gate``) and
``_do_merge`` are stubbed. The ADR gate, the contract_invalid gate and the
branch-protection gate run for real against the conftest network stubs and an
isolated tmp ``VNX_STATE_DIR``, so the ledger records come out of the real
gate functions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import pr_merge


SHA = "c" * 40

PR_DATA = {
    "number": 7,
    "title": "PR-D3 preflight ledger",
    "state": "OPEN",
    "headRefName": "feature/d3",
    "baseRefName": "main",
    "headRefOid": SHA,
}


@pytest.fixture()
def vnx_env(tmp_path, monkeypatch):
    """Isolated VNX state dir — no receipt from this file touches the real store."""
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("VNX_DISPATCH_DIR", str(data_dir / "dispatches"))
    monkeypatch.setenv("VNX_LOGS_DIR", str(data_dir / "logs"))
    monkeypatch.setenv("VNX_PIDS_DIR", str(data_dir / "pids"))
    monkeypatch.setenv("VNX_LOCKS_DIR", str(data_dir / "locks"))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(data_dir / "unified_reports"))
    monkeypatch.setenv("VNX_DB_DIR", str(data_dir / "database"))
    (data_dir / "dispatches").mkdir(parents=True, exist_ok=True)
    return {
        "state_dir": state_dir,
        "data_dir": data_dir,
        "receipts_path": state_dir / "t0_receipts.ndjson",
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_receipts(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _load_receipts(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _receipts_of(path: Path, event_type: str) -> List[Dict[str, Any]]:
    return [r for r in _load_receipts(path) if r.get("event_type") == event_type]


def _go(**kw) -> Dict[str, Any]:
    gate = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
    gate.update(kw)
    return gate


def _no_go(message: str, **kw) -> Dict[str, Any]:
    gate = {"verdict": "NO-GO", "message": message, "overridden": False, "override_reason": None}
    gate.update(kw)
    return gate


_DEFAULT_PR_DATA = object()


def _stub_gh_gates(monkeypatch, *, ci=None, review=None, pr_data=_DEFAULT_PR_DATA):
    """CI + review gates are the two that need gh; everything else runs for real.

    ``pr_data=None`` is a real case (``gh pr view`` failed), so it is passed
    through as None rather than treated as "use the default".
    """
    data = dict(PR_DATA) if pr_data is _DEFAULT_PR_DATA else pr_data
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (ci or _go(), data))
    monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (review or _go(), data))
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)


def _track_do_merge(monkeypatch):
    calls: List[Any] = []
    monkeypatch.setattr(
        pr_merge, "_do_merge",
        lambda n, m, h="": calls.append((n, m, h)) or (True, ""),
    )
    return calls


def _by_gate(gates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {g["gate"]: g for g in gates}


def _refusal(receipts_path: Path) -> Dict[str, Any]:
    refusals = _receipts_of(receipts_path, "pr_merge_refused")
    assert len(refusals) == 1, f"expected exactly one refusal record, got {len(refusals)}"
    return refusals[0]


# ---------------------------------------------------------------------------
# 1. A refusal leaves a readable record; the boundary moves with the position
# ---------------------------------------------------------------------------

class TestRefusalRecord:
    def test_no_go_on_gate_one_marks_the_other_four_not_evaluated(
        self, vnx_env, monkeypatch,
    ):
        _stub_gh_gates(monkeypatch, ci=_no_go("CI-run niet groen op deze head"))
        merges = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-ci-nogo"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merges

        rec = _refusal(vnx_env["receipts_path"])
        assert rec["refused_by"] == "ci"
        assert rec["conclusion"] == "refused"
        assert rec["pr_number"] == 7
        assert rec["dispatch_id"] == "20260908-d3-ci-nogo"

        gates = _by_gate(rec["preflight_gates"])
        assert list(gates) == list(pr_merge.PREFLIGHT_ORDER)
        assert gates["ci"]["verdict"] == "NO-GO"
        assert "CI-run niet groen" in gates["ci"]["message"]
        for later in ("review", "adr", "contract_invalid", "branch_protection"):
            assert gates[later]["verdict"] == pr_merge.VERDICT_NOT_EVALUATED, later

    def test_no_go_on_gate_two_keeps_gate_one_go_and_marks_three_onwards(
        self, vnx_env, monkeypatch,
    ):
        _stub_gh_gates(
            monkeypatch,
            ci=_go(message=f"VNX CI groen op {SHA[:7]}"),
            review=_no_go("geen review-gate-verplichting gevonden voor PR #7"),
        )
        merges = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-review-nogo"])

        assert rc == pr_merge.EXIT_ERROR
        assert not merges

        gates = _by_gate(_refusal(vnx_env["receipts_path"])["preflight_gates"])
        assert gates["ci"]["verdict"] == "GO"
        assert gates["review"]["verdict"] == "NO-GO"
        assert "review-gate-verplichting" in gates["review"]["message"]
        for later in ("adr", "contract_invalid", "branch_protection"):
            assert gates[later]["verdict"] == pr_merge.VERDICT_NOT_EVALUATED, later

    def test_no_go_on_gate_four_leaves_only_gate_five_not_evaluated(
        self, vnx_env, monkeypatch,
    ):
        """The fourth gate refuses for real (a contract_invalid outcome receipt
        on the chain) — only branch-protection is left unevaluated."""
        did = "20260908-d3-contract-nogo"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-07T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        _stub_gh_gates(monkeypatch)
        merges = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_ERROR
        assert not merges

        rec = _refusal(vnx_env["receipts_path"])
        assert rec["refused_by"] == "contract_invalid"
        gates = _by_gate(rec["preflight_gates"])
        for earlier in ("ci", "review", "adr"):
            assert gates[earlier]["verdict"] == "GO", earlier
        assert gates["contract_invalid"]["verdict"] == "NO-GO"
        assert did in gates["contract_invalid"]["message"]
        assert gates["branch_protection"]["verdict"] == pr_merge.VERDICT_NOT_EVALUATED

    def test_not_evaluated_names_the_gate_that_stopped_the_door(
        self, vnx_env, monkeypatch,
    ):
        """A reader must not have to reconstruct WHY a gate did not run."""
        _stub_gh_gates(monkeypatch, review=_no_go("geen gate-verdict"))
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-why"])

        gates = _by_gate(_refusal(vnx_env["receipts_path"])["preflight_gates"])
        assert "review" in gates["adr"]["message"]

    def test_refusal_is_its_own_event_type_and_writes_no_pr_merged(
        self, vnx_env, monkeypatch,
    ):
        """Design choice under test: a refusal is ``pr_merge_refused``, NEVER a
        ``pr_merged`` carrying conclusion=refused. Every merged-PR reader in
        the tree filters on the literal string ``pr_merged``
        (track_reconciler.py:320, build_feature_plan.py:212,
        build_t0_state.py:999, pr_queue_state.py:128, traceability_audit.py:610,
        digest/collectors/progress.py:63) and none of them reads
        ``conclusion`` — so reusing the event type would make every one of them
        count a refused merge as a merge."""
        _stub_gh_gates(monkeypatch, ci=_no_go("niet toetsbaar"))
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-eventtype"])

        assert _receipts_of(vnx_env["receipts_path"], "pr_merged") == []
        assert len(_receipts_of(vnx_env["receipts_path"], "pr_merge_refused")) == 1

    def test_refusal_record_carries_the_head_it_judged(self, vnx_env, monkeypatch):
        _stub_gh_gates(monkeypatch, review=_no_go("geen gate-verdict"))
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-head"])

        rec = _refusal(vnx_env["receipts_path"])
        assert rec["head_sha"] == SHA
        gates = _by_gate(rec["preflight_gates"])
        assert gates["ci"]["head_sha"] == SHA
        assert gates["review"]["head_sha"] == SHA
        # A gate that never ran judged no commit at all.
        assert gates["adr"]["head_sha"] == ""

    def test_unresolvable_head_still_leaves_a_record(self, vnx_env, monkeypatch):
        """Negative path: the CI gate's own "head not determinable" refusal has
        no sha to record, and must still produce a readable refusal."""
        _stub_gh_gates(
            monkeypatch,
            ci=_no_go("PR-head (sha/branch) kon niet worden bepaald voor #7"),
            pr_data=None,
        )
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-nohead"])

        assert rc == pr_merge.EXIT_ERROR
        rec = _refusal(vnx_env["receipts_path"])
        assert rec["head_sha"] == ""
        assert rec["refused_by"] == "ci"

    def test_dry_run_refusal_writes_nothing(self, vnx_env, monkeypatch):
        """``--dry-run`` is documented as "no merge, no write" — a refusal in
        dry-run mode must not append a governance record either."""
        _stub_gh_gates(monkeypatch, ci=_no_go("CI niet groen"))
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-dry", "--dry-run"])

        assert rc == pr_merge.EXIT_ERROR
        assert _receipts_of(vnx_env["receipts_path"], "pr_merge_refused") == []

    def test_json_output_carries_the_ledger(self, vnx_env, monkeypatch, capsys):
        _stub_gh_gates(monkeypatch, review=_no_go("geen gate-verdict"))
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-json", "--json"])

        # The door prints its per-gate progress lines to stdout before the JSON
        # payload (pre-existing behaviour, unchanged here), so parse from the
        # first brace rather than the first byte.
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{"):])
        assert payload["refused_by"] == "review"
        assert _by_gate(payload["preflight_gates"])["adr"]["verdict"] == (
            pr_merge.VERDICT_NOT_EVALUATED
        )


# ---------------------------------------------------------------------------
# 2. The merge branch: all five verdicts on the existing pr_merged receipt
# ---------------------------------------------------------------------------

class TestMergedReceiptCarriesAllFive:
    def test_all_five_verdicts_land_on_the_pr_merged_receipt(
        self, vnx_env, monkeypatch,
    ):
        _stub_gh_gates(monkeypatch)
        merges = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-merge"])

        assert rc == pr_merge.EXIT_OK
        assert merges

        merged = _receipts_of(vnx_env["receipts_path"], "pr_merged")
        assert len(merged) == 1
        gates = merged[0]["preflight_gates"]
        assert [g["gate"] for g in gates] == list(pr_merge.PREFLIGHT_ORDER)
        for g in gates:
            assert g["verdict"] == "GO", g
            assert g["message"], f"{g['gate']} has no message"
            assert g["head_sha"] == SHA, g

    def test_no_separate_gate_registration_beside_the_merged_receipt(
        self, vnx_env, monkeypatch,
    ):
        """One evidence place, not two: the merge branch writes the ``pr_merged``
        receipt and nothing else."""
        _stub_gh_gates(monkeypatch)
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-one-place"])

        kinds = {r.get("event_type") for r in _load_receipts(vnx_env["receipts_path"])}
        assert kinds == {"pr_merged"}

    def test_an_overridden_gate_is_visible_as_overridden(self, vnx_env, monkeypatch):
        _stub_gh_gates(
            monkeypatch,
            ci=_go(message="OVERRIDE: CI-check overgeslagen (gate flaked)",
                   overridden=True, override_reason="gate flaked"),
        )
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-override"])

        gates = _by_gate(_receipts_of(vnx_env["receipts_path"], "pr_merged")[0]["preflight_gates"])
        assert gates["ci"]["overridden"] is True
        assert gates["review"]["overridden"] is False

    def test_bootstrap_branch_protection_go_is_recorded_as_go(
        self, vnx_env, monkeypatch,
    ):
        """The branch-protection gate's bootstrap no-op is a GO with its own
        message — the record must carry that message, not an empty verdict."""
        _stub_gh_gates(monkeypatch)
        _track_do_merge(monkeypatch)

        pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-bootstrap"])

        gates = _by_gate(_receipts_of(vnx_env["receipts_path"], "pr_merged")[0]["preflight_gates"])
        assert gates["branch_protection"]["verdict"] == "GO"
        assert "preflight overgeslagen" in gates["branch_protection"]["message"]


# ---------------------------------------------------------------------------
# 3. The short-circuit is UNCHANGED
# ---------------------------------------------------------------------------

class TestShortCircuitUnchanged:
    def _count_downstream(self, monkeypatch):
        counts = {"adr": 0, "contract_invalid": 0, "branch_protection": 0}
        real_adr = pr_merge._run_adr_gate
        real_contract = pr_merge._run_contract_invalid_gate
        real_protection = pr_merge._run_branch_protection_gate

        def adr(*a, **k):
            counts["adr"] += 1
            return real_adr(*a, **k)

        def contract(*a, **k):
            counts["contract_invalid"] += 1
            return real_contract(*a, **k)

        def protection(*a, **k):
            counts["branch_protection"] += 1
            return real_protection(*a, **k)

        monkeypatch.setattr(pr_merge, "_run_adr_gate", adr)
        monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", contract)
        monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", protection)
        return counts

    def test_no_go_on_gate_two_never_calls_gate_three(self, vnx_env, monkeypatch):
        """The refusal record must NOT be bought by running the remaining gates
        anyway. Gate three (ADR) is provably never called."""
        _stub_gh_gates(monkeypatch, review=_no_go("geen gate-verdict"))
        counts = self._count_downstream(monkeypatch)
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-shortcircuit"])

        assert rc == pr_merge.EXIT_ERROR
        assert counts == {"adr": 0, "contract_invalid": 0, "branch_protection": 0}

    def test_no_go_on_gate_one_never_calls_gate_two(self, vnx_env, monkeypatch):
        calls: List[int] = []

        def review(pr, **k):
            calls.append(pr)
            return _go(), dict(PR_DATA)

        monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_no_go("CI rood"), dict(PR_DATA)))
        monkeypatch.setattr(pr_merge, "_run_review_gate", review)
        monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
        counts = self._count_downstream(monkeypatch)
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-sc1"])

        assert rc == pr_merge.EXIT_ERROR
        assert calls == [], "review gate must not run after a CI NO-GO"
        assert counts == {"adr": 0, "contract_invalid": 0, "branch_protection": 0}

    def test_every_gate_runs_exactly_once_on_a_clean_merge(self, vnx_env, monkeypatch):
        """The ledger is built from the gate results the door already has — it
        must not re-run a gate to fill a field."""
        _stub_gh_gates(monkeypatch)
        counts = self._count_downstream(monkeypatch)
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "7", "--dispatch-id", "20260908-d3-once"])

        assert rc == pr_merge.EXIT_OK
        assert counts == {"adr": 1, "contract_invalid": 1, "branch_protection": 1}
