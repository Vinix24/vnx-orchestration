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
        assert gate["overridden"] is True
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
