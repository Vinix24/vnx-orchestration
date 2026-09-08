#!/usr/bin/env python3
"""Tests for the contract_invalid merge gate wired into pr_merge.py
(Golf B, B7 / OI-1638 gevolg 3).

``contract_invalid_ledger.is_deliverable_acceptable`` existed with zero
callers outside its own module before this gate: OI-1638 gevolg 3 (see
DISPATCH_RULES.md §14) wrote the read side but never wired a call site.
This is that call site — the merge door (``pr_merge.py::main()``), after the
CI gate, the review gate, and the ADR-number preflight (golf B, B6), before
the merge itself.

Only ``gh`` (via ``_run_ci_gate``/``_run_review_gate``/``_run_adr_gate`` and
``_do_merge``) and the receipts path (via the isolated ``VNX_STATE_DIR``)
are mocked — ``is_deliverable_acceptable``/``_run_contract_invalid_gate``
run for real against a real (tmp) NDJSON receipts file, exercising the
actual read+decision path this gate depends on.
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
import contract_invalid_ledger as cil


SHA = "b" * 40

PR_DATA = {
    "number": 1,
    "title": "test PR",
    "state": "OPEN",
    "headRefName": "feature/x",
    "baseRefName": "main",
    "headRefOid": SHA,
}


@pytest.fixture()
def vnx_env(tmp_path, monkeypatch):
    """Isolated VNX state dir — contract_invalid receipts never touch the real store."""
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


def _write_receipts(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _load_receipts(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _outcome_ci(dispatch_id: str, timestamp: str, **overrides) -> Dict[str, Any]:
    """A worker outcome receipt that failed the report-body contract."""
    rec = {
        "timestamp": timestamp, "event_type": "task_complete",
        "status": "contract_invalid", "dispatch_id": dispatch_id,
        "report_path": f"/reports/{dispatch_id}.md",
    }
    rec.update(overrides)
    return rec


def _outcome_success(dispatch_id: str, timestamp: str, **overrides) -> Dict[str, Any]:
    rec = {
        "timestamp": timestamp, "event_type": "task_complete",
        "status": "success", "dispatch_id": dispatch_id,
    }
    rec.update(overrides)
    return rec


def _gate_request(dispatch_id: str, timestamp: str, **overrides) -> Dict[str, Any]:
    """The gate-plane receipt gate_request_handler.py writes on the SAME
    dispatch_id after the worker's outcome — the receipt that masked the
    check on all 8 measured cases."""
    rec = {
        "timestamp": timestamp, "event_type": "review_gate_request",
        "receipt_kind": "review_gate", "status": "requested",
        "dispatch_id": dispatch_id, "pr_number": 1,
    }
    rec.update(overrides)
    return rec


def _go_gate(**kw):
    gate = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
    gate.update(kw)
    return gate


def _bypass_upstream_gates(monkeypatch):
    """CI/review/ADR gates are GO — out of scope for this dispatch (golf B, B4/B6)."""
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go_gate(), dict(PR_DATA)))
    monkeypatch.setattr(pr_merge, "_run_review_gate", lambda pr, **k: (_go_gate(), dict(PR_DATA)))
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: {"verdict": "GO", "message": "ok"})
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)


def _track_do_merge(monkeypatch):
    calls: List[Any] = []
    monkeypatch.setattr(
        pr_merge, "_do_merge",
        lambda n, m, h="": calls.append((n, m, h)) or (True, ""),
    )
    return calls


def _refusal_record(path: Path, gate_name: str) -> Dict[str, Any]:
    """The named gate's entry inside the one ``pr_merge_refused`` receipt.

    Golf Bx, D3 writes that receipt with all five preflights in
    ``preflight_gates``; this is the record a human or a reader meets when
    asking why the door refused.
    """
    refusals = [
        r for r in _load_receipts(path) if r.get("event_type") == "pr_merge_refused"
    ]
    assert len(refusals) == 1, f"expected exactly one refusal record, got {len(refusals)}"
    gates = {g["gate"]: g for g in refusals[0]["preflight_gates"]}
    return gates[gate_name]


# ---------------------------------------------------------------------------
# Unit tests: _run_contract_invalid_gate
# ---------------------------------------------------------------------------

class TestRunContractInvalidGate:
    def test_empty_dispatch_id_skips_go(self, vnx_env):
        gate = pr_merge._run_contract_invalid_gate("")
        assert gate["verdict"] == "GO"
        assert gate["skipped"] is True
        assert "geen dispatch-id" in gate["message"]

    def test_no_receipt_on_record_is_acceptable(self, vnx_env):
        """Absence is not a rejection (OI-1624 precedent) — nul is een meetfout, so a case
        that DOES have receipts on record is covered separately below."""
        gate = pr_merge._run_contract_invalid_gate(
            "never-ran-dispatch", override_reason=None,
        )
        assert gate["verdict"] == "GO"
        assert gate["overridden"] is False

    def test_latest_contract_invalid_is_no_go(self, vnx_env):
        did = "d-open"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        gate = pr_merge._run_contract_invalid_gate(did)
        assert gate["verdict"] == "NO-GO"
        assert did in gate["message"]
        assert "contract_invalid" in gate["message"]

    def test_latest_success_after_earlier_contract_invalid_is_go(self, vnx_env):
        did = "d-healed"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
            {"timestamp": "2026-09-06T11:00:00Z", "event_type": "task_complete",
             "status": "success", "dispatch_id": did},
        ])
        gate = pr_merge._run_contract_invalid_gate(did)
        assert gate["verdict"] == "GO"

    def test_override_empty_reason_refused(self, vnx_env):
        did = "d-open"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        gate = pr_merge._run_contract_invalid_gate(did, override_reason="   ")
        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is False, (
            "the gate stayed NO-GO, so no override was applied"
        )
        assert "niet-lege reden" in gate["message"]

    def test_override_nonempty_reason_grants_go(self, vnx_env):
        did = "d-open"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        gate = pr_merge._run_contract_invalid_gate(did, override_reason="geverifieerd")
        assert gate["verdict"] == "GO"
        assert gate["overridden"] is True
        assert gate["override_reason"] == "geverifieerd"


# ---------------------------------------------------------------------------
# main() wiring
# ---------------------------------------------------------------------------

class TestMainContractInvalidGateWiring:
    def test_refuses_before_merge_when_latest_receipt_is_contract_invalid(
        self, vnx_env, monkeypatch, capsys,
    ):
        did = "20260907-b7-fixture-nogo"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_ERROR
        err = capsys.readouterr().err
        assert did in err
        assert "contract_invalid" in err
        assert not do_merge_calls, "merge must not run when the contract_invalid gate is NO-GO"

    def test_override_with_reason_allows_merge_and_stamps_receipt(
        self, vnx_env, monkeypatch, capsys,
    ):
        did = "20260907-b7-fixture-override"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", did,
            "--override-contract-invalid", "handmatig geverifieerd",
        ])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls, "merge must run once the gate is overridden"
        out = capsys.readouterr().out
        assert "OVERRIDE" in out

        receipts = _load_receipts(vnx_env["receipts_path"])
        merged = [
            r for r in receipts
            if r.get("event_type") == "pr_merged" and r.get("dispatch_id") == did
        ]
        assert len(merged) == 1
        audit = merged[0].get("contract_invalid_override")
        assert audit is not None, "pr_merged receipt must carry the contract_invalid_override audit field"
        assert audit["reason"] == "handmatig geverifieerd"
        assert audit["flag"] == "--override-contract-invalid"

    def test_override_with_empty_reason_is_refused(self, vnx_env, monkeypatch, capsys):
        did = "20260907-b7-fixture-empty-override"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", did, "--override-contract-invalid", "",
        ])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls
        err = capsys.readouterr().err
        assert "niet-lege reden" in err

        receipts = _load_receipts(vnx_env["receipts_path"])
        assert not any(r.get("event_type") == "pr_merged" for r in receipts)

    def test_no_refusal_when_latest_receipt_is_governed_success(
        self, vnx_env, monkeypatch,
    ):
        did = "20260907-b7-fixture-healed"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
            {"timestamp": "2026-09-06T11:00:00Z", "event_type": "task_complete",
             "status": "success", "dispatch_id": did},
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls, "merge must run once the dispatch has healed past contract_invalid"

    def test_no_dispatch_id_skips_check_loudly(self, vnx_env, monkeypatch, capsys):
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1"])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls, "a dispatch-id-less merge is not itself refused"
        out = capsys.readouterr().out
        assert "contract_invalid-check overgeslagen: geen dispatch-id" in out

    def test_json_no_go_outputs_json(self, vnx_env, monkeypatch, capsys):
        did = "20260907-b7-fixture-json"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "contract_invalid", "dispatch_id": did},
        ])
        _bypass_upstream_gates(monkeypatch)
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did, "--json"])

        assert rc == pr_merge.EXIT_ERROR
        # The upstream (bypassed-as-GO) CI/review/ADR gates print their own
        # plain-text "ok" lines unconditionally before this gate's JSON block
        # (existing behaviour, unrelated to this dispatch) — extract just the
        # JSON object this gate emits.
        stdout = capsys.readouterr().out
        out = json.loads(stdout[stdout.index("{"):])
        assert out["success"] is False
        assert did in out["error"]


# ---------------------------------------------------------------------------
# The gate must fire on the GOVERNED path, not just on a synthetic one-receipt
# chain (07-09 fix-forward). Every case below is measured against the live
# ledger: 8 dispatch-ids merged over a contract_invalid receipt and the gate
# as shipped would have said GO on all 8.
# ---------------------------------------------------------------------------

class TestOutcomeReceiptFiltering:
    def test_gate_request_after_contract_invalid_still_no_go(self, vnx_env):
        """The realistic chain (#1786-shaped): outcome contract_invalid, then
        the review gate writes review_gate_request on the SAME dispatch_id.
        Judging "the latest receipt" reads the gate request and passes."""
        did = "20260906-oi1641-overlap-scan"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
            _gate_request(did, "2026-09-06T11:50:54Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate(did)

        assert gate["verdict"] == "NO-GO"
        assert did in gate["message"]
        assert "contract_invalid" in gate["message"]

    def test_real_outcome_success_after_gate_request_is_go(self, vnx_env):
        did = "20260906-healed-for-real"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
            _gate_request(did, "2026-09-06T11:50:54Z"),
            _outcome_success(did, "2026-09-06T12:05:00Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate(did)

        assert gate["verdict"] == "GO"

    def test_only_gate_plane_receipts_is_go_with_no_outcome_reason(self, vnx_env):
        did = "20260906-gate-plane-only"
        _write_receipts(vnx_env["receipts_path"], [
            _gate_request(did, "2026-09-06T11:50:54Z"),
            {"timestamp": "2026-09-06T11:55:00Z", "event_type": "state_mutation",
             "status": "success", "dispatch_id": did},
        ])

        gate = pr_merge._run_contract_invalid_gate(did)

        assert gate["verdict"] == "GO"
        assert "geen uitkomst-receipt" in gate["message"]

    def test_main_refuses_the_realistic_chain_before_merging(
        self, vnx_env, monkeypatch, capsys,
    ):
        did = "20260906-a1-diff-sandwich"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T17:20:00Z"),
            _gate_request(did, "2026-09-06T17:32:33Z"),
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls, "a gate-plane receipt must not unlock the merge"
        assert did in capsys.readouterr().err


class TestUndecidedOutcomeDoesNotLiftTheRefusal:
    """The plane filter alone still passed 2 of the 8 measured cases. Both had
    a ``task_complete``/``unknown`` written about a minute after the
    contract_invalid, and an ``unknown`` was read as "resolved". Chains below
    reproduce those two field-for-field: the contract_invalid receipt has no
    ``ingested_at`` at all, and the ``unknown`` one carries a raw
    ``timestamp`` EARLIER than the contract_invalid's while its ``ingested_at``
    is later — so it genuinely is the last outcome receipt."""

    D6A2 = [
        {"timestamp": "2026-08-30T12:11:42Z", "event_type": "task_complete",
         "status": "contract_invalid", "dispatch_id": "20260830-140500-d6a2",
         "report_path": "/reports/20260830-140500-d6a2.md"},
        {"timestamp": "2026-08-30T12:11:42.852911+00:00", "ingested_at": "2026-08-30T12:12:59Z",
         "event_type": "task_complete", "status": "unknown",
         "dispatch_id": "20260830-140500-d6a2"},
    ]

    def test_unknown_outcome_after_contract_invalid_is_no_go(self, vnx_env):
        _write_receipts(vnx_env["receipts_path"], self.D6A2)

        gate = pr_merge._run_contract_invalid_gate("20260830-140500-d6a2")

        assert gate["verdict"] == "NO-GO"
        assert "contract_invalid" in gate["message"]
        assert "20260830-140500-d6a2" in gate["message"]

    def test_main_refuses_the_unknown_masked_chain(self, vnx_env, monkeypatch, capsys):
        _write_receipts(vnx_env["receipts_path"], self.D6A2)
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", "20260830-140500-d6a2"])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls, "an undecided outcome must not unlock the merge"
        assert "20260830-140500-d6a2" in capsys.readouterr().err

    def test_success_after_contract_invalid_still_merges(self, vnx_env, monkeypatch):
        """Same chain, decided last outcome: the merge goes through. The filter
        narrows which receipts may be chosen, it does not freeze the verdict."""
        did = "20260830-140500-d6a2-really-healed"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-08-30T12:11:42Z"),
            {"timestamp": "2026-08-30T12:11:42.852911+00:00",
             "ingested_at": "2026-08-30T12:12:59Z", "event_type": "task_complete",
             "status": "success", "dispatch_id": did},
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls

    def test_only_undecided_outcomes_is_go_with_its_own_reason(self, vnx_env):
        """3897 unknown records live in that ledger. Skipped, not refused —
        refusing on them would be a merge blockade, not a fix. The reason must
        say WHY it passed, distinct from "no outcome receipt at all"."""
        did = "20260907-only-unknown"
        _write_receipts(vnx_env["receipts_path"], [
            {"timestamp": "2026-09-06T10:00:00Z", "event_type": "task_complete",
             "status": "unknown", "dispatch_id": did},
        ])

        gate = pr_merge._run_contract_invalid_gate(did)

        assert gate["verdict"] == "GO"
        assert "geen besliste uitkomst" in gate["message"]
        assert "geen uitkomst-receipt" not in gate["message"]

    def test_override_still_forces_the_masked_chain_through(self, vnx_env, monkeypatch):
        """The escape hatch keeps working on the newly-refused chain, and the
        bypass is stamped on the receipt — it is a real bypass now."""
        _write_receipts(vnx_env["receipts_path"], self.D6A2)
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", "20260830-140500-d6a2",
            "--override-contract-invalid", "rapport handmatig nagelezen",
        ])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls
        merged = [
            r for r in _load_receipts(vnx_env["receipts_path"])
            if r.get("event_type") == "pr_merged"
        ]
        assert len(merged) == 1
        assert merged[0]["contract_invalid_override"]["reason"] == "rapport handmatig nagelezen"


class TestUnreadableLedgerFailsClosed:
    def test_unreadable_ledger_is_no_go_with_the_cause(self, vnx_env):
        """A directory where t0_receipts.ndjson should be — the read raises,
        and an I/O failure must never read as "absence, not a rejection"."""
        vnx_env["receipts_path"].mkdir(parents=True)

        gate = pr_merge._run_contract_invalid_gate("d-any")

        assert gate["verdict"] == "NO-GO"
        assert "grootboek onleesbaar" in gate["message"]
        assert "afwezigheid" not in gate["message"]

    def test_main_refuses_on_unreadable_ledger(self, vnx_env, monkeypatch, capsys):
        vnx_env["receipts_path"].mkdir(parents=True)
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", "d-any"])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls
        assert "grootboek onleesbaar" in capsys.readouterr().err


class TestDispatchIdResolvedFromPrNumber:
    """A missing --dispatch-id was a reasonless escape hatch: merge_pr itself
    resolves the id from the PR number to stamp the receipt (pr_merge.py's
    _lookup_dispatch_id_by_pr_number), so the gate can resolve the same chain.
    The real lookup runs here against a real register file in the tmp state
    dir — only the register content is a fixture, not the lookup."""

    PR = 424242

    def _write_register(self, vnx_env, dispatch_id: str, pr_number: int) -> None:
        (vnx_env["state_dir"] / "dispatch_register.ndjson").write_text(
            json.dumps({
                "timestamp": "2026-09-06T11:00:00Z", "event": "dispatch_completed",
                "dispatch_id": dispatch_id, "pr_number": pr_number, "terminal": "T0",
            }) + "\n",
            encoding="utf-8",
        )

    def test_gate_runs_and_refuses_on_resolved_dispatch_id(self, vnx_env, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "")  # no central merge-read
        did = "20260907-b7-resolved-from-pr"
        self._write_register(vnx_env, did, self.PR)
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
            _gate_request(did, "2026-09-06T11:50:54Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate("", pr_number=self.PR)

        assert gate["verdict"] == "NO-GO"
        assert gate["resolved_from_pr"] is True
        assert gate["skipped"] is False
        assert did in gate["message"]

    def test_main_refuses_without_dispatch_id_flag(self, vnx_env, monkeypatch, capsys):
        monkeypatch.setenv("VNX_PROJECT_ID", "")
        did = "20260907-b7-resolved-main"
        self._write_register(vnx_env, did, self.PR)
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
            _gate_request(did, "2026-09-06T11:50:54Z"),
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", str(self.PR)])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls, "omitting --dispatch-id must not bypass the gate"
        assert did in capsys.readouterr().err

    def test_unresolvable_pr_still_skips_loudly(self, vnx_env, monkeypatch):
        monkeypatch.setenv("VNX_PROJECT_ID", "")

        gate = pr_merge._run_contract_invalid_gate("", pr_number=self.PR)

        assert gate["verdict"] == "GO"
        assert gate["skipped"] is True
        assert "geen dispatch-id" in gate["message"]
        assert str(self.PR) in gate["message"]


class TestOverrideOnlyStampsARealBypass:
    """--override-contract-invalid short-circuited BEFORE the ledger read, so
    the flag alone produced overridden=True on a clean chain and main()
    stamped contract_invalid_override onto the receipt — an audit field
    claiming a bypass that never happened."""

    def test_clean_chain_reports_the_flag_as_unnecessary(self, vnx_env):
        did = "20260907-b7-clean-chain"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_success(did, "2026-09-06T11:36:00Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate(did, override_reason="voor de zekerheid")

        assert gate["verdict"] == "GO"
        assert gate["overridden"] is False
        assert gate["override_unnecessary"] is True
        assert gate["override_reason"] is None
        assert "niet nodig" in gate["message"]

    def test_clean_chain_leaves_no_audit_field_on_the_receipt(
        self, vnx_env, monkeypatch, capsys,
    ):
        did = "20260907-b7-clean-chain-main"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_success(did, "2026-09-06T11:36:00Z"),
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", did,
            "--override-contract-invalid", "voor de zekerheid",
        ])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls
        assert "niet nodig" in capsys.readouterr().out

        merged = [
            r for r in _load_receipts(vnx_env["receipts_path"])
            if r.get("event_type") == "pr_merged" and r.get("dispatch_id") == did
        ]
        assert len(merged) == 1
        assert "contract_invalid_override" not in merged[0], (
            "an override field on a clean chain reads in the trail as a "
            "governed failure someone waved through"
        )

    def test_dirty_chain_still_merges_and_stamps(self, vnx_env, monkeypatch):
        did = "20260907-b7-dirty-chain"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
            _gate_request(did, "2026-09-06T11:50:54Z"),
        ])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", did,
            "--override-contract-invalid", "handmatig geverifieerd",
        ])

        assert rc == pr_merge.EXIT_OK
        assert do_merge_calls
        merged = [
            r for r in _load_receipts(vnx_env["receipts_path"])
            if r.get("event_type") == "pr_merged" and r.get("dispatch_id") == did
        ]
        assert len(merged) == 1
        assert merged[0]["contract_invalid_override"] == {
            "flag": "--override-contract-invalid",
            "reason": "handmatig geverifieerd",
        }


class TestOverrideAppliesOnlyToItsOwnReasonCode:
    """Golf Bx, D4 / OI-1666: the flag used to force GO on EVERY non-acceptable
    verdict, because the gate only ever saw ``(False, <Dutch prose>)`` and
    never why. ``is_deliverable_acceptable`` refuses in three distinguishable
    cases — the real contract_invalid, an unreadable ledger (the fail-closed
    I/O refusal), and an empty dispatch_id — and an operator who means "I know
    about that contract_invalid" was silently also waving through "I cannot
    read the ledger", where repairing is the right move, not proceeding.

    The door now decides on the machine-readable code
    (``contract_invalid_ledger.CODE_*``), never on the message text: those
    strings are Dutch prose for a human and break on the first rewording.
    """

    def test_unreadable_ledger_with_the_flag_is_still_no_go(self, vnx_env):
        """The core case. On the pre-D4 door this said GO."""
        vnx_env["receipts_path"].mkdir(parents=True)

        gate = pr_merge._run_contract_invalid_gate(
            "d-any", override_reason="ik weet van die contract_invalid",
        )

        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is False, (
            "no bypass happened, so nothing may be stamped as one"
        )
        assert gate["override_not_applicable"] is True
        assert gate["reason_code"] == cil.CODE_LEDGER_UNREADABLE
        assert "grootboek onleesbaar" in gate["message"]
        assert "geldt alleen" in gate["message"]

    def test_main_refuses_unreadable_ledger_even_with_the_flag(
        self, vnx_env, monkeypatch, capsys,
    ):
        vnx_env["receipts_path"].mkdir(parents=True)
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", "d-any",
            "--override-contract-invalid", "ik weet van die contract_invalid",
        ])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls, (
            "an unreadable ledger must be repaired, not overridden past"
        )
        assert "grootboek onleesbaar" in capsys.readouterr().err

    def test_unreadable_ledger_with_an_empty_reason_is_also_no_go(self, vnx_env):
        """Applicability is judged before the reason is: a flag that does not
        cover this refusal is no override at all, so there is nothing to
        demand a reason for. Both orders refuse; this one says why."""
        vnx_env["receipts_path"].mkdir(parents=True)

        gate = pr_merge._run_contract_invalid_gate("d-any", override_reason="   ")

        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is False
        assert gate["override_not_applicable"] is True
        assert gate["reason_code"] == cil.CODE_LEDGER_UNREADABLE

    def test_real_contract_invalid_with_a_reason_still_grants_go(self, vnx_env):
        """The repair must not take away the escape hatch it was built for."""
        did = "20260908-d4-real-contract-invalid"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
            _gate_request(did, "2026-09-06T11:50:54Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate(
            did, override_reason="rapport handmatig nagelezen",
        )

        assert gate["verdict"] == "GO"
        assert gate["overridden"] is True
        assert gate["override_not_applicable"] is False
        assert gate["override_reason"] == "rapport handmatig nagelezen"
        assert gate["reason_code"] == cil.CODE_CONTRACT_INVALID

    def test_empty_reason_on_a_real_contract_invalid_stays_refused(self, vnx_env):
        """Unchanged behaviour: on the reason the flag DOES cover, an empty
        one is still a refused silent bypass."""
        did = "20260908-d4-empty-reason"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_ci(did, "2026-09-06T11:36:00Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate(did, override_reason="   ")

        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is False, (
            "an override that was refused is not an override that was applied"
        )
        assert gate["override_not_applicable"] is False
        assert "niet-lege reden" in gate["message"]

    def test_clean_chain_with_the_flag_is_unchanged(self, vnx_env):
        """Unchanged behaviour: nothing to override, nothing stamped — and the
        'unnecessary' branch is not the 'not applicable' one."""
        did = "20260908-d4-clean-chain"
        _write_receipts(vnx_env["receipts_path"], [
            _outcome_success(did, "2026-09-06T11:36:00Z"),
        ])

        gate = pr_merge._run_contract_invalid_gate(did, override_reason="voor de zekerheid")

        assert gate["verdict"] == "GO"
        assert gate["overridden"] is False
        assert gate["override_unnecessary"] is True
        assert gate["override_not_applicable"] is False
        assert gate["reason_code"] == cil.CODE_NOT_CONTRACT_INVALID

    @pytest.mark.parametrize("code", [
        cil.CODE_LEDGER_UNREADABLE,
        cil.CODE_EMPTY_DISPATCH_ID,
        "een_code_die_later_wordt_toegevoegd",
    ])
    def test_no_other_refusal_code_can_be_overridden(self, vnx_env, monkeypatch, code):
        """The rule is on the code, not on the cases this door can reach
        today: ``empty_dispatch_id`` is unreachable here (the door skips a
        missing id loudly before the ledger read) and a code added later is
        unknown to this test — both must refuse by construction, not by
        having been enumerated. Only the evaluator is stubbed; the door's own
        decision runs for real."""
        monkeypatch.setattr(
            pr_merge, "evaluate_deliverable_acceptance",
            lambda did, path, **kw: cil.DeliverableAcceptance(False, code, f"reden: {code}"),
        )

        gate = pr_merge._run_contract_invalid_gate("d-any", override_reason="toch maar wel")

        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is False
        assert gate["reason_code"] == code


class TestRefusedOverrideIsNotAnAppliedOne:
    """Golf Bx, D4 fix-forward: the empty-reason branch stamped
    ``overridden: True`` on a gate that stayed NO-GO.

    Two lines above it, the same function refuses to mark an inapplicable
    override as applied, for the stated reason that nothing was bypassed. An
    override that was REFUSED for lacking a reason bypassed exactly as little.
    The field travels: ``_preflight_record`` copies ``overridden`` straight
    into the preflight ledger, so the refusal record claimed a merge door had
    been overridden while it was in the act of refusing.
    """

    def test_the_empty_reason_branch_records_no_override(self, vnx_env):
        did = "20260908-d4ff-empty-reason-field"
        _write_receipts(vnx_env["receipts_path"], [_outcome_ci(did, "2026-09-06T11:36:00Z")])

        gate = pr_merge._run_contract_invalid_gate(did, override_reason="   ")

        assert gate["verdict"] == "NO-GO"
        assert gate["overridden"] is False

    def test_the_refusal_record_does_not_claim_an_override(
        self, vnx_env, monkeypatch,
    ):
        """The field as a reader meets it: inside ``preflight_gates`` on the
        ``pr_merge_refused`` receipt, not just on the gate dict in memory."""
        did = "20260908-d4ff-empty-reason-record"
        _write_receipts(vnx_env["receipts_path"], [_outcome_ci(did, "2026-09-06T11:36:00Z")])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", did, "--override-contract-invalid", "  ",
        ])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls

        record = _refusal_record(vnx_env["receipts_path"], "contract_invalid")
        assert record["verdict"] == "NO-GO"
        assert record["overridden"] is False, (
            "a refused merge may not carry an override in its own record"
        )

    def test_an_applied_override_is_still_recorded_as_one(self, vnx_env, monkeypatch):
        """The other direction, so the fix cannot be "always False": a real
        override with a real reason still reads as overridden on the merged
        receipt."""
        did = "20260908-d4ff-real-override-record"
        _write_receipts(vnx_env["receipts_path"], [_outcome_ci(did, "2026-09-06T11:36:00Z")])
        _bypass_upstream_gates(monkeypatch)
        _track_do_merge(monkeypatch)

        rc = pr_merge.main([
            "--pr", "1", "--dispatch-id", did,
            "--override-contract-invalid", "rapport handmatig nagelezen",
        ])

        assert rc == pr_merge.EXIT_OK
        merged = [
            r for r in _load_receipts(vnx_env["receipts_path"])
            if r.get("event_type") == "pr_merged"
        ]
        assert len(merged) == 1
        gates = {g["gate"]: g for g in merged[0]["preflight_gates"]}
        assert gates["contract_invalid"]["overridden"] is True


class TestRefusalRecordCarriesTheCodeNotOnlyTheProse:
    """Golf Bx, D4 fix-forward: the redencode never reached the record.

    D3 (OI-1665) landed ``_preflight_record`` an hour before D4 (OI-1666)
    landed the codes, and the two never met. ``_preflight_record`` copies five
    fields off the gate result — gate, verdict, message, head_sha, overridden —
    and ``reason_code`` was not one of them.

    So the door DECIDED on the machine-readable code and then recorded only
    the Dutch prose. Establishing afterwards which of the three refusals fired
    was back to matching that prose, which is the exact failure class D4 exists
    to remove, put back one level up: it breaks on the first rewording, and it
    breaks permissively.
    """

    def test_a_real_contract_invalid_refusal_names_its_code(
        self, vnx_env, monkeypatch,
    ):
        """Nothing stubbed but the upstream gates and the merge itself: the
        ledger, the gate decision and the emit path are the real ones."""
        did = "20260908-d4ff-code-in-record"
        _write_receipts(vnx_env["receipts_path"], [_outcome_ci(did, "2026-09-06T11:36:00Z")])
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls

        record = _refusal_record(vnx_env["receipts_path"], "contract_invalid")
        assert record["verdict"] == "NO-GO"
        assert record["reason_code"] == cil.CODE_CONTRACT_INVALID
        assert record["reason_code"] in cil.ACCEPTANCE_CODES

    @pytest.mark.parametrize("code", [
        cil.CODE_LEDGER_UNREADABLE,
        cil.CODE_EMPTY_DISPATCH_ID,
    ])
    def test_the_other_two_refusals_name_their_own_code(
        self, vnx_env, monkeypatch, code,
    ):
        """The two refusals that cannot be produced through the door with a
        WRITABLE ledger, so only the evaluator is stubbed and everything after
        it is real.

        Neither is reachable otherwise. ``empty_dispatch_id`` is caught by the
        door's own loud skip before the ledger is ever read, and a genuinely
        unreadable ledger is also an unwritable one — the refusal record would
        have nowhere to land, which is a separate, pre-existing property
        ``_refuse_merge`` already warns about. Stubbing the evaluator is what
        lets the record itself be tested for all three codes.
        """
        did = "20260908-d4ff-code-" + code
        monkeypatch.setattr(
            pr_merge, "evaluate_deliverable_acceptance",
            lambda d, path, **kw: cil.DeliverableAcceptance(False, code, f"reden: {code}"),
        )
        _bypass_upstream_gates(monkeypatch)
        do_merge_calls = _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_ERROR
        assert not do_merge_calls

        record = _refusal_record(vnx_env["receipts_path"], "contract_invalid")
        assert record["reason_code"] == code
        assert record["reason_code"] != cil.CODE_CONTRACT_INVALID

    def test_the_three_refusals_are_separable_without_reading_the_prose(
        self, vnx_env, monkeypatch,
    ):
        """The blocker itself, stated as one assertion: three refusals, three
        distinct codes in the record, no Dutch text consulted."""
        seen = set()
        for code in (
            cil.CODE_CONTRACT_INVALID,
            cil.CODE_LEDGER_UNREADABLE,
            cil.CODE_EMPTY_DISPATCH_ID,
        ):
            monkeypatch.setattr(
                pr_merge, "evaluate_deliverable_acceptance",
                lambda d, path, _c=code, **kw: cil.DeliverableAcceptance(
                    False, _c, "een en dezelfde nederlandse tekst voor alle drie",
                ),
            )
            _bypass_upstream_gates(monkeypatch)
            _track_do_merge(monkeypatch)

            pr_merge.main(["--pr", "1", "--dispatch-id", f"20260908-d4ff-sep-{code}"])

            records = [
                {g["gate"]: g for g in r["preflight_gates"]}["contract_invalid"]
                for r in _load_receipts(vnx_env["receipts_path"])
                if r.get("event_type") == "pr_merge_refused"
            ]
            seen.add(records[-1]["reason_code"])

        assert seen == {
            cil.CODE_CONTRACT_INVALID,
            cil.CODE_LEDGER_UNREADABLE,
            cil.CODE_EMPTY_DISPATCH_ID,
        }

    def test_an_inapplicable_override_attempt_is_visible_in_the_record(
        self, vnx_env, monkeypatch,
    ):
        """An operator who TRIED to override a refusal this flag does not cover
        is an audit fact of its own. Without the field, that attempt and "no
        flag was passed at all" are the same record, separable only in the
        message."""
        monkeypatch.setattr(
            pr_merge, "evaluate_deliverable_acceptance",
            lambda d, path, **kw: cil.DeliverableAcceptance(
                False, cil.CODE_LEDGER_UNREADABLE, "grootboek onleesbaar: stub",
            ),
        )
        _bypass_upstream_gates(monkeypatch)
        _track_do_merge(monkeypatch)

        pr_merge.main([
            "--pr", "1", "--dispatch-id", "20260908-d4ff-attempt",
            "--override-contract-invalid", "ik weet van die contract_invalid",
        ])

        record = _refusal_record(vnx_env["receipts_path"], "contract_invalid")
        assert record["override_not_applicable"] is True
        assert record["overridden"] is False

    def test_the_merged_receipt_carries_the_accepting_code_too(
        self, vnx_env, monkeypatch,
    ):
        """Not only refusals: a merge records WHICH kind of yes the gate gave,
        so "the chain was clean" and "there was nothing on record" are
        separable on a merged receipt as well."""
        did = "20260908-d4ff-merged-code"
        _write_receipts(vnx_env["receipts_path"], [_outcome_success(did, "2026-09-06T11:36:00Z")])
        _bypass_upstream_gates(monkeypatch)
        _track_do_merge(monkeypatch)

        rc = pr_merge.main(["--pr", "1", "--dispatch-id", did])

        assert rc == pr_merge.EXIT_OK
        merged = [
            r for r in _load_receipts(vnx_env["receipts_path"])
            if r.get("event_type") == "pr_merged"
        ]
        gates = {g["gate"]: g for g in merged[0]["preflight_gates"]}
        assert gates["contract_invalid"]["reason_code"] == cil.CODE_NOT_CONTRACT_INVALID

    def test_a_skipped_gate_says_so_instead_of_leaving_the_field_empty(
        self, vnx_env, monkeypatch, capsys,
    ):
        """The gate's own skip (no dispatch_id, none derivable from the PR)
        never reaches the ledger, so it has no acceptance code — and an empty
        string there would be indistinguishable from a record written before
        this field existed. It gets a door-level literal instead, deliberately
        outside the ledger's closed set."""
        _bypass_upstream_gates(monkeypatch)
        _track_do_merge(monkeypatch)
        monkeypatch.setattr(pr_merge, "_lookup_dispatch_id_by_pr_number", lambda n: "")

        rc = pr_merge.main(["--pr", "1"])

        assert rc == pr_merge.EXIT_OK
        merged = [
            r for r in _load_receipts(vnx_env["receipts_path"])
            if r.get("event_type") == "pr_merged"
        ]
        gates = {g["gate"]: g for g in merged[0]["preflight_gates"]}
        assert gates["contract_invalid"]["reason_code"] == pr_merge.REASON_CODE_GATE_SKIPPED
        assert pr_merge.REASON_CODE_GATE_SKIPPED not in cil.ACCEPTANCE_CODES
