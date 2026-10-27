"""GE-4c (dlv-a4644be8f2ee): the merge door reads an obligation the runner books, verdict unchanged.

``pr_merge --dry-run`` resolves the PR's declared gate from the door's
obligation record (``gate_obligations.declared_gates_for_pr``). GE-4c moves
the OUTCOME booking from a standalone ``update_obligation`` into
``commit_gate_outcome`` (and its obligation-only sibling), so the same
obligation is now written by a different writer in a different critical
section. The door must read it identically.

The golden was captured on the head BEFORE this dispatch (base ``d80d748f``)
from the old-way booking and is committed first; the second test books the
same obligation through ``commit_gate_outcome`` and demands the same payload.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gate_obligations
import pr_merge
from review_seat_support import no_config_store

PR, PR_ID, BRANCH, HEAD = 4409, "4409", "dispatch/ge-4c-golden", "e" * 40
DISPATCH = "codex-gate-pr4409-1"
GOLDEN = Path(__file__).resolve().parent / "goldens" / "ge4c_pr_merge_dry_run.json"
STACK = ["codex_gate", "kimi_gate"]
CLEAN = "# review\n\nThe reviewer read the whole diff and found nothing that holds the merge.\n"

_PR_DATA = {
    "number": PR, "title": "GE-4c golden", "state": "OPEN", "isDraft": False,
    "headRefName": BRANCH, "baseRefName": "main", "headRefOid": HEAD,
    "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
}


def _store(tmp_path, monkeypatch) -> dict:
    data_dir = tmp_path / "data"
    state = data_dir / "state"
    for directory in (
        state / "review_gates" / "results",
        state / "review_gates" / "requests",
        data_dir / "unified_reports",
        data_dir / "dispatches",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for key, value in {
        "PROJECT_ROOT": tmp_path, "VNX_HOME": VNX_ROOT, "VNX_DATA_DIR": data_dir,
        "VNX_STATE_DIR": state, "VNX_DISPATCH_DIR": data_dir / "dispatches",
        "VNX_LOGS_DIR": data_dir / "logs", "VNX_PIDS_DIR": data_dir / "pids",
        "VNX_LOCKS_DIR": data_dir / "locks", "VNX_REPORTS_DIR": data_dir / "unified_reports",
        "VNX_DB_DIR": data_dir / "database",
    }.items():
        monkeypatch.setenv(key, str(value))
    no_config_store(monkeypatch)
    return {"state": state, "results": state / "review_gates" / "results", "reports": data_dir / "unified_reports"}


def _write_pass(store, gate: str) -> None:
    report = store["reports"] / f"{gate}.md"
    report.write_text(CLEAN, encoding="utf-8")
    record = {
        "gate": gate, "pr_id": PR_ID, "pr_number": PR, "status": "pass", "commit_sha": HEAD,
        "branch": BRANCH, "contract_hash": f"sha256:{gate}", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [], "evidence_source": "live", "test_run": False,
        # The engine stamps it; the booking names the attempt it belongs to.
        "attempt_seq": 1,
    }
    (store["results"] / f"pr-{PR}-{gate}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")


def _go(message: str) -> dict:
    return {"verdict": "GO", "message": message, "overridden": False, "override_reason": None}


def _stub_the_network(monkeypatch) -> None:
    import file_scope_overlap
    from merge_target import MergeTarget

    # The target the door judges is part of the golden: pin it, so the payload does
    # not depend on which checkout (or which conftest stub) the suite runs under.
    monkeypatch.setattr(pr_merge, "resolve_merge_target",
                        lambda engine_root, **_kwargs: MergeTarget(project_root=Path(engine_root), repo="vnx-test/target"))
    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go("CI groen"), dict(_PR_DATA)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: dict(_PR_DATA))
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: _go("adr"))
    monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: _go("contract"))
    monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", lambda *a, **k: _go("protection"))
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
    monkeypatch.setattr(pr_merge, "_do_merge", lambda *a, **k: (True, ""))
    monkeypatch.setattr(file_scope_overlap, "warn_overlaps", lambda *a, **k: [])


FULFILLED = {
    "status": gate_obligations.STATUS_FULFILLED,
    "reason": "fulfilled_by_existing_evidence",
    "reason_detail": "codex_gate already passed on this head",
    "resolved_at": "2026-10-27T10:00:00Z",
    "result_path": "",  # filled per run: it names the store's own directory
}


def _register(store) -> Path:
    path = gate_obligations.register_obligation(
        store["state"], dispatch_id=DISPATCH, gate="codex_gate", pr_number=PR, pr_id=PR_ID, branch=BRANCH,
    )
    assert path is not None
    return path


def _dry_run(tmp_path, monkeypatch, *, booked_by: str) -> tuple[dict, dict]:
    """The dry-run payload and the obligation record, with the obligation fulfilled ``booked_by``."""
    store = _store(tmp_path, monkeypatch)
    _write_pass(store, "codex_gate")
    _write_pass(store, "kimi_gate")
    path = _register(store)
    fields = {**FULFILLED, "result_path": str(store["results"] / f"pr-{PR}-codex_gate.json")}
    if booked_by == "old_way":
        # What the runner did before GE-4c: a standalone merge, outside the record's slot lock.
        gate_obligations.update_obligation(path, **fields)
    else:
        from gate_outcome import GateObligation, commit_obligation_outcome

        commit_obligation_outcome(
            GateObligation(path=path, fields=fields),
            result_path=store["results"] / f"pr-{PR}-codex_gate.json",
        )
    _stub_the_network(monkeypatch)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = pr_merge.main(["--pr", PR_ID, "--dry-run", "--json"])
    text = out.getvalue()
    assert store["results"].joinpath(f"pr-{PR}-codex_gate.json").exists()
    payload = json.loads(text[text.index("{"):])
    payload["exit_code"] = rc
    return payload, json.loads(path.read_text(encoding="utf-8"))


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_dry_run_on_the_old_way_booking_is_the_committed_golden(tmp_path, monkeypatch):
    payload, _obligation = _dry_run(tmp_path, monkeypatch, booked_by="old_way")

    assert payload == _golden(), "the golden is the door's own output on the head before GE-4c"
    assert payload["exit_code"] == pr_merge.EXIT_OK


def test_the_dry_run_on_a_booking_through_the_commit_gives_the_same_verdict(tmp_path, monkeypatch):
    committed, obligation = _dry_run(tmp_path, monkeypatch, booked_by="commit")
    payload, old_obligation = _dry_run(tmp_path / "old", monkeypatch, booked_by="old_way")

    assert committed == _golden(), "the door reads a booking by the commit exactly as before"
    # The same booking, bar the store it names (each run has its own tmp store).
    booked = ("status", "reason", "reason_detail", "resolved_at")
    assert {k: obligation[k] for k in booked} == {k: old_obligation[k] for k in booked}
    assert Path(obligation["result_path"]).name == Path(old_obligation["result_path"]).name
    assert obligation["booked_for"] == {
        "gate": "codex_gate", "pr_number": PR, "commit_sha": HEAD, "attempt_seq": 1,
    }
    assert "booked_for" not in old_obligation, "the old writer stamped no attempt identity"
