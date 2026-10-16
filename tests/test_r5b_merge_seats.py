"""OI-2058 (merge side), OI-2064, OI-2107 — reparatiegolf R5b: every evaluator counts seats one way.

Plan: ``claudedocs/specs/reparatiegolf/plan.md``, "R5 in detail" (the R5b parts). What is
pinned here:

- the five red runs of the plan table and the dispatch: (a) a codex PASS without a kimi
  record passes the door (``check_review_gate_for_merge`` and the ``pr_merge`` dry-run);
  (b) a codex rejection next to a kimi PASS publishes ``vnx-gate/review`` = ``success``;
  (c) kimi read codex's seat and nobody read kimi's, and the door merges; (d) ``vnx pr-ready``
  says READY for (a); (e) two explicit single-seat requests with only codex passed merge;
- which seats are required: the config stack a request on the head recorded, else a fresh
  resolution; an explicit request never narrows it; the recorded one wins and says so;
- one reviewer, one seat: ``takeover_path[0]`` names the seat, a stated seat wins, and the
  booking and the peer route only take a reviewer no other seat counts;
- blocking always blocks; a switched-off seat is never a pass and its refusal names the way out;
- one test per evidence route (direct, takeover, OI-1719 booking, OI-1624 peer, OI-1642
  out-of-scope absence, OI-1851 partial review);
- the merge record carries the seat list, and track closure applies the seat rule to a merge
  under it while a merge from before R5b keeps the rule it was merged under;
- the governance enforcer credits a seat the same way (OI-2107), and the enforcement verifier
  resolves its default seats fresh.

Round 2 (codex FAIL on ``389036f6``): a PASS JSON that its own report contradicts is a
rejection, for every review peer on the head, inside or outside the seats; closure checks the
hash and the report of every record that fills a seat; and for every head of this file
``vnx pr-ready`` says READY and ``vnx-gate/review`` says ``success`` exactly when the door
says GO.

No real provider, store or GitHub: tmp stores, ``gh`` replaced at the edge (``_query_pr``,
``_run_ci_gate``, ``fetch_pr_facts``, ``ci_contexts.evaluate_commit``), and every config value
resolved with no store behind it (``review_seat_support``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(VNX_ROOT / "scripts"), str(VNX_ROOT / "scripts" / "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ci_contexts
import closure_verifier
import forge_gate_publisher as fgp
import gate_obligations
import pr_merge
import pr_ready
import pr_readiness
from review_seat_support import no_config_store, pin_review_stack

PR = 2074
PR_ID = str(PR)
BRANCH = "dispatch/20261016-rg-r5b-merge-seats"
HEAD = "e" * 40
OTHER_HEAD = "f" * 40
DISPATCH = "20261016-rg-r5b-test"
STACK = ["codex_gate", "kimi_gate"]
CLEAN = "# review\n\nThe reviewer read the whole diff and found nothing that holds the merge.\n"


# ---------------------------------------------------------------------------
# A throwaway project store
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch) -> Dict[str, Path]:
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
    return {
        "tmp": tmp_path,
        "state": state,
        "results": state / "review_gates" / "results",
        "requests": state / "review_gates" / "requests",
        "reports": data_dir / "unified_reports",
        "receipts": state / "t0_receipts.ndjson",
    }


def _report(store, name: str, text: str = CLEAN) -> str:
    path = store["reports"] / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _record(store, gate: str, *, status: str = "pass", blocking: bool = False, **extra: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "gate": gate, "pr_id": PR_ID, "pr_number": PR, "status": status,
        "commit_sha": HEAD, "branch": BRANCH, "contract_hash": f"sha256:{gate}",
        "report_path": _report(store, f"{gate}.md"), "blocking_findings": [],
        "advisory_findings": [], "evidence_source": "live", "test_run": False,
    }
    if blocking:
        record["blocking_findings"] = [{"severity": "blocking", "message": "breaks every caller"}]
        record["blocking_count"] = 1
    record.update(extra)
    return record


def _absent(gate: str, *, status: str = "unavailable", reason: str = "lane_exhausted", **extra: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "gate": gate, "pr_id": PR_ID, "pr_number": PR, "status": status, "reason": reason,
        "reason_detail": f"{gate}: {reason}", "commit_sha": HEAD, "branch": BRANCH,
        "contract_hash": "", "report_path": "", "blocking_findings": [],
    }
    record.update(extra)
    return record


def _write(store, gate: str, record: Dict[str, Any]) -> Path:
    path = store["results"] / f"pr-{PR}-{gate}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def _request(
    store, gate: str, *, seat: Optional[str] = None, seats: Optional[List[str]] = None,
    source: str = "config", commit_sha: str = HEAD, requested_at: str = "2026-10-16T10:00:00Z",
    **extra: Any,
) -> None:
    record: Dict[str, Any] = {
        "gate": gate, "pr_number": PR, "branch": BRANCH, "status": "requested",
        "commit_sha": commit_sha, "requested_at": requested_at,
    }
    if seat:
        record["seat"] = seat
    if seats is not None:
        record["review_seats"] = {
            "seats": list(seats), "extra_gates": [], "source": source,
            "key": "VNX_DEFAULT_REVIEW_STACK" if source == "config" else "",
            "value": ",".join(seats), "profile_stack": None,
        }
    record.update(extra)
    (store["requests"] / f"pr-{PR}-{gate}.json").write_text(json.dumps(record), encoding="utf-8")


def _took_over(seat: str, *passed_over: str) -> Dict[str, Any]:
    hops = [{"gate": gate, "reason": "lane_exhausted", "status": "unavailable", "detail": "weekly limit"}
            for gate in (seat, *passed_over)]
    return {"takeover": True, "takeover_from": hops[-1]["gate"], "takeover_path": hops}


def _door(store, gate: str = "codex_gate", **kwargs: Any) -> Dict[str, Any]:
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, gate, store["results"], branch=BRANCH, head_sha=HEAD, **kwargs,
    )


def _summary(store) -> fgp.ForgeVerdict:
    return fgp.review_verdict(PR, HEAD, results_dir=store["results"], branch=BRANCH)


def _go(message: str) -> Dict[str, Any]:
    return {"verdict": "GO", "message": message, "overridden": False, "override_reason": None}


_PR_DATA = {
    "number": PR, "title": "R5b merge seats", "state": "OPEN", "isDraft": False,
    "headRefName": BRANCH, "baseRefName": "main", "headRefOid": HEAD,
    "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
}


def _stub_door_edges(monkeypatch) -> None:
    """Everything of the door that talks to GitHub, plus the three preflights after review."""
    import file_scope_overlap

    monkeypatch.setattr(pr_merge, "_run_ci_gate", lambda pr, **k: (_go("CI groen"), dict(_PR_DATA)))
    monkeypatch.setattr(pr_merge, "_query_pr", lambda n: dict(_PR_DATA))
    monkeypatch.setattr(pr_merge, "_resolve_override_reason", lambda reason: reason)
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: _go("adr"))
    monkeypatch.setattr(pr_merge, "_run_contract_invalid_gate", lambda *a, **k: _go("contract"))
    monkeypatch.setattr(pr_merge, "_run_branch_protection_gate", lambda *a, **k: _go("protection"))
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: True)
    monkeypatch.setattr(file_scope_overlap, "warn_overlaps", lambda *a, **k: [])


def _obligation(store, gate: str = "codex_gate") -> None:
    gate_obligations.register_obligation(
        store["state"], dispatch_id=DISPATCH, gate=gate, pr_number=PR, pr_id=PR_ID, branch=BRANCH,
    )


def _json_out(capsys) -> Dict[str, Any]:
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])


# ---------------------------------------------------------------------------
# The red runs (plan table R5b, dispatch (a)-(e)): every one fails on 35609d56 on behaviour
# ---------------------------------------------------------------------------


class TestRedRuns:
    def test_a_codex_pass_without_a_kimi_record_does_not_pass_the_door(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "kimi_gate" in verdict["message"]

    def test_a_codex_pass_without_a_kimi_record_is_not_merged_by_the_door(self, store, monkeypatch, capsys):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _obligation(store)
        _stub_door_edges(monkeypatch)

        rc = pr_merge.main(["--pr", PR_ID, "--dry-run", "--json"])
        payload = _json_out(capsys)

        assert rc == pr_merge.EXIT_ERROR, payload
        assert payload["refused_by"] == "review"
        assert "kimi_gate" in payload["error"]

    def test_b_a_codex_rejection_next_to_a_kimi_pass_is_never_success(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate", status="completed", blocking=True))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))

        verdict = _summary(store)

        assert verdict.conclusion == fgp.CONCLUSION_FAILURE, verdict.reason
        assert "codex_gate" in verdict.reason

    def test_c_kimi_reading_codexs_seat_leaves_kimis_own_seat_open(self, store):
        _write(store, "codex_gate", _absent("codex_gate"))
        _request(store, "codex_gate", seat="codex_gate", seats=STACK)
        _request(store, "kimi_gate", seat="codex_gate", seats=STACK, **_took_over("codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", **_took_over("codex_gate")))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "seat kimi_gate" in verdict["message"]

    def test_d_pr_ready_does_not_say_ready_for_a_codex_pass_alone(self, store, monkeypatch):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _obligation(store)
        monkeypatch.setattr(pr_readiness, "fetch_pr_facts", lambda *a, **k: dict(_PR_DATA))
        monkeypatch.setattr(
            ci_contexts, "evaluate_commit",
            lambda *a, **k: [ci_contexts.ContextState(context="VNX CI", state=ci_contexts.STATE_PASSED, detail="ok")],
        )

        report = pr_readiness.assess(PR, store["tmp"], store["state"])

        assert report.verdict != pr_readiness.VERDICT_READY, report.to_dict()
        assert any(gate.gate == "kimi_gate" and not gate.satisfied for gate in report.gates)

    def test_e_two_single_seat_requests_with_only_codex_passed_are_refused(self, store):
        _request(store, "codex_gate", seat="codex_gate", seats=["codex_gate"], source="explicit")
        _request(store, "kimi_gate", seat="kimi_gate", seats=["kimi_gate"], source="explicit",
                 requested_at="2026-10-16T10:05:00Z")
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "kimi_gate" in verdict["message"]


# ---------------------------------------------------------------------------
# Which seats are required (the T0's decision, dispatch point 2)
# ---------------------------------------------------------------------------


class TestWhichSeatsAreRequired:
    def test_the_config_stack_recorded_on_the_head_wins_over_a_fresh_resolution(self, store):
        _request(store, "codex_gate", seat="codex_gate", seats=["codex_gate"], source="config")
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        seats = verdict["review_seats"]
        assert (seats["seats"], seats["source"]) == (["codex_gate"], "config")
        assert seats["fresh_seats"] == STACK
        assert "geboekte stack" in seats["note"]

    def test_an_explicit_request_never_narrows_the_config_stack(self, store):
        _request(store, "codex_gate", seat="codex_gate", seats=["codex_gate"], source="explicit")
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert verdict["review_seats"]["seats"] == STACK
        assert verdict["review_seats"]["source"] == "config_fresh"

    def test_without_a_recorded_stack_the_seats_are_resolved_fresh_on_every_call(self, store, monkeypatch):
        _write(store, "codex_gate", _record(store, "codex_gate"))

        pin_review_stack(monkeypatch, "codex_gate")
        assert _door(store)["verdict"] == "GO"
        pin_review_stack(monkeypatch, "codex_gate,kimi_gate")
        assert _door(store)["verdict"] == "NO-GO"

    def test_a_request_about_another_head_records_nothing_for_this_one(self, store):
        _request(store, "codex_gate", seat="codex_gate", seats=["codex_gate"], commit_sha=OTHER_HEAD)
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _door(store)

        assert verdict["review_seats"]["source"] == "config_fresh"
        assert verdict["verdict"] == "NO-GO"

    def test_an_unknown_name_in_the_stack_refuses_by_name(self, store, monkeypatch):
        pin_review_stack(monkeypatch, "codex_gate,nonsense_gate")
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert "nonsense_gate" in verdict["message"]


# ---------------------------------------------------------------------------
# One reviewer, one seat
# ---------------------------------------------------------------------------


class TestOneReviewerOneSeat:
    def test_the_first_hop_names_the_seat_never_a_later_one(self, store):
        """deepseek walked past codex, then kimi: it read codex's seat, not kimi's."""
        _write(store, "codex_gate", _absent("codex_gate"))
        _write(store, "kimi_gate", _absent("kimi_gate"))
        _write(store, "deepseek_gate", _record(store, "deepseek_gate", **_took_over("codex_gate", "kimi_gate")))

        verdict = _door(store)
        successors, _notes = closure_verifier._find_takeover_successor_results(
            "kimi_gate", PR_ID, store["results"], branch=BRANCH, head_sha=HEAD,
        )

        assert successors == []
        assert verdict["verdict"] == "NO-GO"
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "deepseek_gate"}
        assert "deepseek_gate telt al voor een andere seat" in verdict["message"]

    def test_a_stated_seat_beats_an_earlier_takeover_path(self, store):
        """A standing kimi PASS from a round where it read codex's seat; this round asked it for its own."""
        _request(store, "codex_gate", seat="codex_gate", seats=STACK)
        _request(store, "kimi_gate", seat="kimi_gate", seats=STACK)
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", **_took_over("codex_gate")))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "codex_gate", "kimi_gate": "kimi_gate"}

    def test_one_independent_peer_fills_one_absent_seat_only(self, store):
        _write(store, "codex_gate", _absent("codex_gate"))
        _write(store, "kimi_gate", _absent("kimi_gate"))
        _write(store, "glm_gate", _record(store, "glm_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "glm_gate"}
        assert "glm_gate telt al voor een andere seat" in verdict["message"]

    def test_two_distinct_peers_fill_two_absent_seats(self, store):
        _write(store, "codex_gate", _absent("codex_gate"))
        _write(store, "kimi_gate", _absent("kimi_gate"))
        _write(store, "glm_gate", _record(store, "glm_gate"))
        _write(store, "deepseek_gate", _record(store, "deepseek_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        filled = verdict["review_seats"]["filled_by"]
        assert set(filled) == set(STACK) and len(set(filled.values())) == 2


# ---------------------------------------------------------------------------
# Blocking always blocks; a switched-off seat is never a pass
# ---------------------------------------------------------------------------


class TestBlockingAndSwitchedOff:
    def test_a_rejection_by_a_reviewer_outside_the_stack_blocks_a_full_house(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        _write(store, "glm_gate", _record(store, "glm_gate", status="fail", blocking=True))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "codex_gate", "kimi_gate": "kimi_gate"}
        assert verdict["review_seats"]["blocking"] == ["glm_gate"]
        assert _summary(store).conclusion == fgp.CONCLUSION_FAILURE

    def test_a_switched_off_seat_without_a_successor_names_the_switch_and_both_ways_out(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        detail = "provider_switched_off: kimi_gate family=kimi source=project scope=vnx-dev consumer=review_gate"
        _write(store, "kimi_gate", _absent(
            "kimi_gate", status="not_executable", reason="provider_switched_off", reason_detail=detail,
        ))
        (store["results"] / f"pr-{PR}-kimi_gate-chain-exhausted.json").write_text(json.dumps({
            "gate": "kimi_gate", "pr_number": PR, "status": "chain_exhausted",
            "reason": "takeover_chain_exhausted", "reviewers_tried": ["kimi_gate", "deepseek_gate", "glm_gate"],
            "takeover": True, "takeover_path": [
                {"gate": "kimi_gate", "reason": "provider_switched_off", "status": "disabled", "detail": detail},
                {"gate": "deepseek_gate", "reason": "credit_exhausted", "status": "unreachable", "detail": "402"},
                {"gate": "glm_gate", "reason": "credit_exhausted", "status": "unreachable", "detail": "402"},
            ],
        }), encoding="utf-8")

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert "kimi_gate" not in verdict["review_seats"]["filled_by"]
        message = verdict["message"]
        assert "seat kimi_gate" in message and "provider-schakelaar" in message
        assert "`vnx providers on kimi`" in message and "VNX_DEFAULT_REVIEW_STACK" in message
        assert "geprobeerd: kimi_gate, deepseek_gate, glm_gate" in message


# ---------------------------------------------------------------------------
# Every evidence route fills at most one seat (the report's route table)
# ---------------------------------------------------------------------------


class TestEvidenceRoutes:
    def test_direct_each_seat_its_own_record(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["routes"] == {"codex_gate": "direct", "kimi_gate": "direct"}

    def test_takeover_every_seat_its_own_successor(self, store):
        """R5a's shape: codex exhausted, kimi reads codex's seat, deepseek reads kimi's."""
        _write(store, "codex_gate", _absent("codex_gate"))
        _request(store, "codex_gate", seat="codex_gate", seats=STACK)
        _request(store, "kimi_gate", seat="codex_gate", seats=STACK, **_took_over("codex_gate"))
        _request(store, "deepseek_gate", seat="kimi_gate", seats=STACK, **_took_over("kimi_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", **_took_over("codex_gate")))
        _write(store, "deepseek_gate", _record(store, "deepseek_gate", **_took_over("kimi_gate")))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        seats = verdict["review_seats"]
        assert seats["filled_by"] == {"codex_gate": "kimi_gate", "kimi_gate": "deepseek_gate"}
        assert seats["routes"] == {"codex_gate": "takeover", "kimi_gate": "takeover"}

    def _booking(self, store, pointed_gate: str) -> None:
        root = store["state"] / "review_gates" / "obligations"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{DISPATCH}.json").write_text(json.dumps({
            "schema_version": 1, "kind": "review_gate_obligation", "dispatch_id": DISPATCH,
            "gate": "codex_gate", "pr_number": PR, "status": "fulfilled",
            "reason": "fulfilled_by_takeover_evidence",
            "result_path": str(store["results"] / f"pr-{PR}-{pointed_gate}.json"),
        }), encoding="utf-8")

    def test_booking_fills_the_seat_it_was_booked_for(self, store):
        _write(store, "codex_gate", _absent("codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        _write(store, "glm_gate", _record(store, "glm_gate"))
        self._booking(store, "glm_gate")

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["routes"]["codex_gate"] == "takeover_booking"
        assert verdict["review_seats"]["filled_by"]["codex_gate"] == "glm_gate"

    def test_booking_never_points_at_a_reviewer_another_seat_counts(self, store):
        _write(store, "codex_gate", _absent("codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        self._booking(store, "kimi_gate")

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert "kimi_gate, dat al voor een andere seat telt" in verdict["message"]

    def test_independent_peer_fills_only_the_absent_seat(self, store):
        _write(store, "codex_gate", _absent("codex_gate", status="not_executable", reason="quota_exceeded"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        _write(store, "glm_gate", _record(store, "glm_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "glm_gate", "kimi_gate": "kimi_gate"}
        assert verdict["review_seats"]["routes"]["codex_gate"] == "independent_peer"

    def test_out_of_scope_absence_opens_the_same_peer_route(self, store):
        record = _absent("codex_gate", status="not_executable", reason="quota_exceeded")
        for key in ("commit_sha", "branch"):
            del record[key]
        _write(store, "codex_gate", record)
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        _write(store, "glm_gate", _record(store, "glm_gate"))

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["filled_by"]["codex_gate"] == "glm_gate"

    def test_a_partial_review_fills_no_seat_and_names_the_cut(self, store, monkeypatch):
        pin_review_stack(monkeypatch, "kimi_gate,codex_gate")
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(
            store, "kimi_gate", status="partial_review", reason_detail="diff cut at 800000 of 912345 chars",
        ))

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert "kimi_gate" not in verdict["review_seats"]["filled_by"]
        assert "zag maar een deel van de diff" in verdict["message"]
        assert "800000" in verdict["message"]

        _write(store, "glm_gate", _record(store, "glm_gate"))
        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["filled_by"]["kimi_gate"] == "glm_gate"


# ---------------------------------------------------------------------------
# vnx-gate/review follows the door
# ---------------------------------------------------------------------------


class TestReviewSummary:
    def test_a_seat_that_has_not_spoken_is_action_required(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))

        verdict = _summary(store)

        assert verdict.conclusion == fgp.CONCLUSION_ACTION_REQUIRED
        assert "kimi_gate" in verdict.reason

    def test_a_seat_that_spoke_and_did_not_hold_is_failure(self, store):
        blocking_report = _report(store, "kimi-contradicted.md", (
            "# kimi\n\n```json\n" + json.dumps({"verdict": "fail", "findings": [
                {"severity": "blocking", "title": "dit mag niet mergen"}]}) + "\n```\n"
        ))
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", report_path=blocking_report))

        assert _summary(store).conclusion == fgp.CONCLUSION_FAILURE

    def test_every_seat_filled_by_a_distinct_reviewer_is_success(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))

        verdict = _summary(store)

        assert verdict.conclusion == fgp.CONCLUSION_SUCCESS, verdict.reason
        assert "codex_gate=codex_gate" in verdict.reason and "kimi_gate=kimi_gate" in verdict.reason


# ---------------------------------------------------------------------------
# The merge record, and track closure
# ---------------------------------------------------------------------------


def _receipts(store, event_type: str) -> List[Dict[str, Any]]:
    path = store["receipts"]
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("event_type") == event_type]


class TestMergeRecord:
    def test_the_pr_merged_receipt_carries_the_seat_list_and_its_source(self, store, monkeypatch):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        _obligation(store)
        _stub_door_edges(monkeypatch)
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, h="": (True, ""))

        rc = pr_merge.main(["--pr", PR_ID, "--dispatch-id", DISPATCH])

        assert rc == pr_merge.EXIT_OK
        seats = _receipts(store, "pr_merged")[-1]["review_seats"]
        assert seats["rule"] == "every_seat_distinct_reviewer"
        assert (seats["seats"], seats["source"], seats["head_sha"]) == (STACK, "config_fresh", HEAD)
        assert seats["filled_by"] == {"codex_gate": "codex_gate", "kimi_gate": "kimi_gate"}

    def test_an_overridden_review_records_that_no_seat_was_judged(self, store, monkeypatch):
        _obligation(store)
        _stub_door_edges(monkeypatch)
        monkeypatch.setattr(pr_merge, "_do_merge", lambda n, m, h="": (True, ""))

        rc = pr_merge.main(["--pr", PR_ID, "--dispatch-id", DISPATCH, "--override-reason", "codex down, T0 read it"])

        assert rc == pr_merge.EXIT_OK
        seats = _receipts(store, "pr_merged")[-1]["review_seats"]
        assert (seats["verdict"], seats["seats"], seats["head_sha"]) == ("overridden", [], HEAD)


def _contract(review_stack: List[str], pr_id: str = PR_ID, content_hash: str = ""):
    from review_contract import Deliverable, ReviewContract

    return ReviewContract(
        pr_id=pr_id, pr_title="R5b", feature_title="reparatiegolf", branch=BRANCH, track="A",
        risk_class="low", merge_policy="standard", closure_stage="ready",
        deliverables=[Deliverable(category="impl", description="seat rule")],
        review_stack=review_stack, content_hash=content_hash,
    )


def _merged(review_seats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {"event_type": "pr_merged", "pr_number": PR, "conclusion": "merged"}
    if review_seats is not None:
        receipt["review_seats"] = review_seats
    return receipt


def _seat_rule(seats: List[str]) -> Dict[str, Any]:
    return {"rule": "every_seat_distinct_reviewer", "pr_id": PR_ID, "head_sha": HEAD, "seats": seats,
            "source": "config"}


class TestTrackClosure:
    def test_a_pr_merged_before_r5b_keeps_the_one_seat_rule(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))

        checks = closure_verifier._validate_review_evidence(
            _contract(["codex_gate"]), store["results"], branch=BRANCH, merge_record=_merged(),
        )

        assert all(check.status == "PASS" for check in checks), [c.__dict__ for c in checks]
        assert not any(check.name.startswith("seat_") for check in checks)

    def test_the_same_evidence_under_the_seat_rule_fails_on_the_open_seat(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate"))

        checks = closure_verifier._validate_review_evidence(
            _contract(["codex_gate"]), store["results"], branch=BRANCH,
            merge_record=_merged(_seat_rule(STACK)),
        )
        by_name = {check.name: check for check in checks}

        assert by_name["seat_codex_gate"].status == "PASS"
        assert by_name["seat_kimi_gate"].status == "FAIL"

    def test_a_takeover_merge_closes_without_the_old_per_gate_reading(self, store):
        _write(store, "codex_gate", _absent("codex_gate", status="not_executable", reason="quota_exceeded"))
        _request(store, "kimi_gate", seat="codex_gate", seats=STACK, **_took_over("codex_gate"))
        _request(store, "deepseek_gate", seat="kimi_gate", seats=STACK, **_took_over("kimi_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", **_took_over("codex_gate")))
        _write(store, "deepseek_gate", _record(store, "deepseek_gate", **_took_over("kimi_gate")))

        checks = closure_verifier._validate_review_evidence(
            _contract(STACK), store["results"], branch=BRANCH, merge_record=_merged(_seat_rule(STACK)),
        )

        assert all(check.status == "PASS" for check in checks), [c.__dict__ for c in checks if c.status != "PASS"]
        assert {"seat_codex_gate", "seat_kimi_gate"} <= {check.name for check in checks}

    def test_the_merge_record_is_found_by_number_and_the_latest_wins(self, store):
        lines = [
            _merged(),
            {"event_type": "pr_merged", "pr_number": 3, "pr_id": "PR-HYG-9", "conclusion": "merged"},
            _merged(_seat_rule(STACK)),
        ]
        store["receipts"].write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

        found = closure_verifier._find_merge_record(store["receipts"], PR_ID)

        assert found["review_seats"]["seats"] == STACK
        assert closure_verifier._find_merge_record(store["receipts"], "PR-HYG-9")["pr_number"] == 3
        assert closure_verifier._find_merge_record(store["receipts"], "3")["pr_id"] == "PR-HYG-9"
        # An internal label is never a GitHub number: label PR-3 is not PR #3.
        assert closure_verifier._find_merge_record(store["receipts"], "PR-3") is None
        assert closure_verifier._find_merge_record(store["tmp"] / "absent.ndjson", PR_ID) is None


# ---------------------------------------------------------------------------
# The governance enforcer (OI-2107) and the enforcement verifier read the same seat
# ---------------------------------------------------------------------------

_ENFORCER_CONFIG = """\
version: 1
mode: standard
checks:
  codex_gate_required:
    level: 2
    description: "codex seat"
  kimi_gate_required:
    level: 2
    description: "kimi seat"
presets:
  standard: {}
"""


class TestEveryReaderUsesTheSameSeat:
    def _enforcer(self, store):
        from governance_enforcer import GovernanceEnforcer

        config = store["tmp"] / "governance_enforcement.yaml"
        config.write_text(_ENFORCER_CONFIG, encoding="utf-8")
        enforcer = GovernanceEnforcer()
        enforcer.load_config(config)
        return enforcer

    def test_a_kimi_pass_for_codexs_seat_satisfies_codex_and_not_kimi(self, store, monkeypatch):
        import governance_enforcer

        monkeypatch.setattr(governance_enforcer, "GATE_RESULTS_DIR", store["results"])
        _write(store, "codex_gate", _absent("codex_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", **_took_over("codex_gate")))
        enforcer = self._enforcer(store)
        ctx = {"pr_number": PR, "head_sha": HEAD}

        assert enforcer.check("codex_gate_required", ctx).passed is True
        kimi = enforcer.check("kimi_gate_required", ctx)
        assert kimi.passed is False
        assert "read seat codex_gate" in kimi.message

    def test_a_successor_that_only_walked_past_a_seat_does_not_satisfy_it(self, store, monkeypatch):
        import governance_enforcer

        monkeypatch.setattr(governance_enforcer, "GATE_RESULTS_DIR", store["results"])
        _write(store, "deepseek_gate", _record(store, "deepseek_gate", **_took_over("codex_gate", "kimi_gate")))
        enforcer = self._enforcer(store)
        ctx = {"pr_number": PR, "head_sha": HEAD}

        assert enforcer.check("codex_gate_required", ctx).passed is True
        assert enforcer.check("kimi_gate_required", ctx).passed is False

    def test_the_verifier_resolves_its_default_seats_fresh(self, store, monkeypatch):
        import gate_enforcement_verify
        import review_gate_manager

        monkeypatch.setattr(review_gate_manager, "DEFAULT_REVIEW_STACK", ["glm_gate"])
        pin_review_stack(monkeypatch, "codex_gate,kimi_gate")

        assert list(gate_enforcement_verify._default_stack()) == STACK

    def test_the_verifier_holds_a_run_to_the_seats_its_report_requested(self):
        import gate_enforcement_verify

        report = {"gates": [], "review_seats": {"seats": ["codex_gate"], "source": "config"}}

        stack = gate_enforcement_verify.requested_stack(report, lambda: ["never_used"])

        assert list(stack()) == ["codex_gate"]
        assert list(gate_enforcement_verify.requested_stack({"gates": []}, lambda: STACK)()) == STACK


# ---------------------------------------------------------------------------
# Round 2: a rejection blocks, whichever record says it (codex blocking 2, warning 3)
# ---------------------------------------------------------------------------


def _rejecting_report(store, name: str) -> str:
    """A report whose own verdict fence says FAIL with a blocking finding (invariant 7)."""
    return _report(store, name, (
        "# review\n\n```json\n" + json.dumps({"verdict": "fail", "findings": [
            {"severity": "blocking", "title": "breekt elke aanroeper"}]}) + "\n```\n"
    ))


def _contradicted(store, gate: str, **extra: Any) -> Dict[str, Any]:
    """A PASS JSON whose report on disk carries a FAIL verdict and a blocking finding."""
    return _record(store, gate, report_path=_rejecting_report(store, f"{gate}-contradicted.md"), **extra)


def _ready(store, monkeypatch) -> "pr_readiness.Readiness":
    monkeypatch.setattr(pr_readiness, "fetch_pr_facts", lambda *a, **k: dict(_PR_DATA))
    monkeypatch.setattr(
        ci_contexts, "evaluate_commit",
        lambda *a, **k: [ci_contexts.ContextState(context="VNX CI", state=ci_contexts.STATE_PASSED, detail="ok")],
    )
    return pr_readiness.assess(PR, store["tmp"], store["state"])


def _full_house(store) -> None:
    _write(store, "codex_gate", _record(store, "codex_gate"))
    _write(store, "kimi_gate", _record(store, "kimi_gate"))


def _rejection_beside_a_filled_seat(store) -> None:
    """codex reads its own seat, kimi rejects while reading codex's seat, deepseek reads kimi's."""
    _request(store, "codex_gate", seat="codex_gate", seats=STACK)
    _request(store, "kimi_gate", seat="codex_gate", seats=STACK)
    _request(store, "deepseek_gate", seat="kimi_gate", seats=STACK, **_took_over("kimi_gate"))
    _write(store, "codex_gate", _record(store, "codex_gate"))
    _write(store, "kimi_gate", _record(store, "kimi_gate", status="completed", blocking=True))
    _write(store, "deepseek_gate", _record(store, "deepseek_gate", **_took_over("kimi_gate")))


class TestARejectionInTheReportBlocks:
    def test_a_reviewer_outside_the_seats_whose_report_rejects_its_pass_blocks(self, store, monkeypatch):
        """codex's reproduction: both seats filled, glm's PASS JSON contradicted by its FAIL report."""
        _full_house(store)
        _write(store, "glm_gate", _contradicted(store, "glm_gate"))
        _obligation(store)

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "codex_gate", "kimi_gate": "kimi_gate"}
        assert verdict["review_seats"]["blocking"] == ["glm_gate"]
        assert "glm_gate (reviewer voor seat glm_gate)" in verdict["message"]
        assert "spreekt de JSON tegen" in verdict["message"]
        summary = _summary(store)
        assert summary.conclusion == fgp.CONCLUSION_FAILURE, summary.reason
        assert "glm_gate" in summary.reason
        report = _ready(store, monkeypatch)
        assert report.verdict != pr_readiness.VERDICT_READY, report.to_dict()
        assert any(gate.gate == "glm_gate" and gate.verdict == "NO-GO" for gate in report.gates)

    def test_the_same_rejection_blocks_when_glm_is_the_obligations_declared_gate(self, store, monkeypatch, capsys):
        _full_house(store)
        _write(store, "glm_gate", _contradicted(store, "glm_gate"))
        _obligation(store, "glm_gate")
        _stub_door_edges(monkeypatch)

        assert _door(store, "glm_gate")["verdict"] == "NO-GO"
        rc = pr_merge.main(["--pr", PR_ID, "--dry-run", "--json"])
        payload = _json_out(capsys)

        assert rc == pr_merge.EXIT_ERROR, payload
        assert payload["refused_by"] == "review"
        assert "glm_gate" in payload["error"] and "spreekt de JSON tegen" in payload["error"]
        assert _summary(store).conclusion == fgp.CONCLUSION_FAILURE
        assert _ready(store, monkeypatch).verdict != pr_readiness.VERDICT_READY

    def test_a_seats_own_reviewer_whose_report_contradicts_its_pass_blocks_and_fills_nothing(self, store, monkeypatch):
        _write(store, "codex_gate", _record(store, "codex_gate"))
        _write(store, "kimi_gate", _contradicted(store, "kimi_gate"))
        _obligation(store)

        verdict = _door(store)

        assert verdict["verdict"] == "NO-GO"
        assert "kimi_gate" not in verdict["review_seats"]["filled_by"]
        assert verdict["review_seats"]["blocking"] == ["kimi_gate"]
        assert "kimi_gate (reviewer voor seat kimi_gate)" in verdict["message"]
        assert _summary(store).conclusion == fgp.CONCLUSION_FAILURE
        report = _ready(store, monkeypatch)
        assert report.verdict != pr_readiness.VERDICT_READY
        assert [gate.gate for gate in report.gates].count("kimi_gate") == 1, "the seat line already names kimi"

    @pytest.mark.parametrize("report_path", ["", "missing"], ids=["empty_report_path", "no_report_on_disk"])
    def test_an_incomplete_stray_record_is_no_rejection(self, store, monkeypatch, report_path):
        """Incomplete evidence fills no seat, and on its own it never blocks either."""
        _full_house(store)
        path = str(store["reports"] / "glm-never-written.md") if report_path else ""
        _write(store, "glm_gate", _record(store, "glm_gate", status="completed", report_path=path))
        _obligation(store)

        verdict = _door(store)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["review_seats"]["blocking"] == []
        assert _summary(store).conclusion == fgp.CONCLUSION_SUCCESS
        assert _ready(store, monkeypatch).verdict == pr_readiness.VERDICT_READY

    def test_a_rejection_for_a_seat_another_reviewer_filled_still_blocks_readiness(self, store, monkeypatch):
        """codex's warning 3: kimi rejects while reading codex's seat, deepseek fills kimi's."""
        _rejection_beside_a_filled_seat(store)
        _obligation(store)

        verdict = _door(store)
        report = _ready(store, monkeypatch)

        assert verdict["verdict"] == "NO-GO"
        assert verdict["review_seats"]["filled_by"] == {"codex_gate": "codex_gate", "kimi_gate": "deepseek_gate"}
        assert verdict["review_seats"]["blocking"] == ["kimi_gate"]
        assert report.verdict != pr_readiness.VERDICT_READY, report.to_dict()
        blocking_lines = [g for g in report.gates if g.gate == "kimi_gate" and g.verdict == "NO-GO"]
        assert len(blocking_lines) == 1
        assert blocking_lines[0].rejected is True
        assert "reviewer voor seat codex_gate" in blocking_lines[0].message
        assert "kimi_gate REJECTED on head" in pr_ready._gate_line(report)
        assert _summary(store).conclusion == fgp.CONCLUSION_FAILURE

    def test_a_seat_rejected_by_its_own_reviewer_gets_one_readiness_line(self, store, monkeypatch):
        _write(store, "codex_gate", _record(store, "codex_gate", status="completed", blocking=True))
        _write(store, "kimi_gate", _record(store, "kimi_gate"))
        _obligation(store)

        report = _ready(store, monkeypatch)

        assert report.verdict != pr_readiness.VERDICT_READY
        assert [gate.gate for gate in report.gates] == ["codex_gate", "kimi_gate"]


# ---------------------------------------------------------------------------
# Round 2: closure checks the hash and the report of every seat's filling record (codex blocking 1)
# ---------------------------------------------------------------------------

CONTRACT_HASH = "c0ffee01" + "0" * 56
STALE_HASH = "deadbeef" + "0" * 56


class TestClosureChecksEverySeatsEvidence:
    def _checks(self, store, merge_record: Dict[str, Any], stack: Optional[List[str]] = None):
        checks = closure_verifier._validate_review_evidence(
            _contract(stack or STACK, content_hash=CONTRACT_HASH), store["results"], branch=BRANCH,
            merge_record=merge_record,
        )
        return {check.name: check for check in checks}, checks

    def test_a_stale_hash_on_a_seats_own_record_fails_that_seat(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate", contract_hash=STALE_HASH))
        _write(store, "kimi_gate", _record(store, "kimi_gate", contract_hash=CONTRACT_HASH))

        by_name, _checks = self._checks(store, _merged(_seat_rule(STACK)))

        assert by_name["seat_codex_gate"].status == "PASS"
        hash_check = by_name["hash_codex_gate"]
        assert hash_check.status == "FAIL"
        assert "codex_gate" in hash_check.detail
        assert "c0ffee01" in hash_check.detail and "deadbeef" in hash_check.detail
        assert "hash_kimi_gate" not in by_name

    def test_a_stale_hash_names_the_reviewer_that_filled_the_seat(self, store):
        _write(store, "codex_gate", _absent("codex_gate", status="not_executable", reason="quota_exceeded"))
        _request(store, "kimi_gate", seat="codex_gate", seats=STACK, **_took_over("codex_gate"))
        _request(store, "deepseek_gate", seat="kimi_gate", seats=STACK, **_took_over("kimi_gate"))
        _write(store, "kimi_gate", _record(store, "kimi_gate", contract_hash=STALE_HASH, **_took_over("codex_gate")))
        _write(store, "deepseek_gate", _record(
            store, "deepseek_gate", contract_hash=CONTRACT_HASH, **_took_over("kimi_gate"),
        ))

        by_name, _checks = self._checks(store, _merged(_seat_rule(STACK)))

        assert by_name["seat_codex_gate"].status == "PASS"
        assert by_name["hash_codex_gate"].status == "FAIL"
        assert "kimi_gate" in by_name["hash_codex_gate"].detail
        assert "hash_kimi_gate" not in by_name

    def test_matching_hashes_pass(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate", contract_hash=CONTRACT_HASH))
        _write(store, "kimi_gate", _record(store, "kimi_gate", contract_hash=CONTRACT_HASH))

        by_name, checks = self._checks(store, _merged(_seat_rule(STACK)))

        assert all(check.status == "PASS" for check in checks), [c.__dict__ for c in checks if c.status != "PASS"]
        assert by_name["report_codex_gate"].status == "PASS" and by_name["report_kimi_gate"].status == "PASS"

    def test_a_grandfathered_merge_keeps_the_old_hash_check(self, store):
        _write(store, "codex_gate", _record(store, "codex_gate", contract_hash=STALE_HASH))

        by_name, checks = self._checks(store, _merged(), stack=["codex_gate"])

        assert by_name["hash_codex_gate"].status == "FAIL"
        assert "codex_gate receipt hash mismatch" in by_name["hash_codex_gate"].detail
        assert not any(check.name.startswith("seat_") for check in checks)

    def test_a_filling_record_that_lost_its_report_fails_its_seats_report_check(self, store):
        """The report existed when the door merged; at closure it is gone."""
        _write(store, "codex_gate", _record(store, "codex_gate", contract_hash=CONTRACT_HASH))
        _write(store, "kimi_gate", _record(store, "kimi_gate", contract_hash=CONTRACT_HASH))
        (store["reports"] / "codex_gate.md").unlink()

        by_name, _checks = self._checks(store, _merged(_seat_rule(STACK)))

        assert by_name["seat_codex_gate"].status == "FAIL"
        report_check = by_name["report_codex_gate"]
        assert report_check.status == "FAIL"
        assert "does not exist" in report_check.detail and "codex_gate.md" in report_check.detail
        assert by_name["report_kimi_gate"].status == "PASS"


# ---------------------------------------------------------------------------
# Round 2: readiness and vnx-gate/review answer what the door answers, on every head
# ---------------------------------------------------------------------------


def _head_a_and_d(store) -> str:
    """Round 1 (a), and (d) which reads the same head through readiness."""
    _write(store, "codex_gate", _record(store, "codex_gate"))
    return "codex_gate"


def _head_b(store) -> str:
    _write(store, "codex_gate", _record(store, "codex_gate", status="completed", blocking=True))
    _write(store, "kimi_gate", _record(store, "kimi_gate"))
    return "codex_gate"


def _head_c(store) -> str:
    _write(store, "codex_gate", _absent("codex_gate"))
    _request(store, "codex_gate", seat="codex_gate", seats=STACK)
    _request(store, "kimi_gate", seat="codex_gate", seats=STACK, **_took_over("codex_gate"))
    _write(store, "kimi_gate", _record(store, "kimi_gate", **_took_over("codex_gate")))
    return "codex_gate"


def _head_e(store) -> str:
    _request(store, "codex_gate", seat="codex_gate", seats=["codex_gate"], source="explicit")
    _request(store, "kimi_gate", seat="kimi_gate", seats=["kimi_gate"], source="explicit",
             requested_at="2026-10-16T10:05:00Z")
    _write(store, "codex_gate", _record(store, "codex_gate"))
    return "codex_gate"


def _head_full_house(store) -> str:
    _full_house(store)
    return "codex_gate"


def _head_stray_contradicted(store) -> str:
    _full_house(store)
    _write(store, "glm_gate", _contradicted(store, "glm_gate"))
    return "codex_gate"


def _head_declared_contradicted(store) -> str:
    _head_stray_contradicted(store)
    return "glm_gate"


def _head_seat_contradicted(store) -> str:
    _write(store, "codex_gate", _record(store, "codex_gate"))
    _write(store, "kimi_gate", _contradicted(store, "kimi_gate"))
    return "codex_gate"


def _head_incomplete_stray(store) -> str:
    _full_house(store)
    _write(store, "glm_gate", _record(store, "glm_gate", status="completed", report_path=""))
    return "codex_gate"


def _head_rejection_beside_a_filled_seat(store) -> str:
    _rejection_beside_a_filled_seat(store)
    return "codex_gate"


_HEADS = [
    pytest.param(_head_a_and_d, "NO-GO", id="round1_a_and_d"),
    pytest.param(_head_b, "NO-GO", id="round1_b"),
    pytest.param(_head_c, "NO-GO", id="round1_c"),
    pytest.param(_head_e, "NO-GO", id="round1_e"),
    pytest.param(_head_full_house, "GO", id="full_house"),
    pytest.param(_head_stray_contradicted, "NO-GO", id="stray_report_contradicts_pass"),
    pytest.param(_head_declared_contradicted, "NO-GO", id="declared_report_contradicts_pass"),
    pytest.param(_head_seat_contradicted, "NO-GO", id="seat_report_contradicts_pass"),
    pytest.param(_head_incomplete_stray, "GO", id="incomplete_stray_record"),
    pytest.param(_head_rejection_beside_a_filled_seat, "NO-GO", id="rejection_beside_a_filled_seat"),
]


@pytest.mark.parametrize("build, door_says", _HEADS)
def test_readiness_and_the_summary_say_go_exactly_when_the_door_does(store, monkeypatch, build, door_says):
    gate = build(store)
    _obligation(store, gate)

    door = _door(store, gate)
    ready = _ready(store, monkeypatch)
    summary = _summary(store)

    assert door["verdict"] == door_says, door["message"]
    assert (ready.verdict == pr_readiness.VERDICT_READY) == (door["verdict"] == "GO"), ready.to_dict()
    assert (summary.conclusion == fgp.CONCLUSION_SUCCESS) == (door["verdict"] == "GO"), summary.reason
