#!/usr/bin/env python3
"""Tests for the SUMMARY check ``vnx-gate/review`` (Golf B, B3).

B2b publishes one check-run per gate (``vnx-gate/glm_gate``, ...). This covers
the one check-run per HEAD that answers the only question branch protection
needs answered: is there a valid review accord on this exact commit?

The rule is not restated here and it is not restated in the code either — it is
``review_seat_evaluation.evaluate_review_seats`` over
``closure_verifier._REVIEW_PEER_GATES``, the same truth the merge door enforces
(OI-2058, R5b: every seat of the project's review stack filled by a distinct
reviewer, and no blocking finding on the head). ``TestTheRuleComesFromTheMergeDoor``
pins that: changing the door's peer set changes this check's answer, which is
only possible if the peer set is being READ rather than copied.

Each test runs in a project whose review stack it names (``pin_review_stack``):
most of them in a one-seat ``glm_gate`` project, so a glm record decides the
whole review. ``tests/test_r5b_merge_seats.py`` covers the two-seat stack.

Mocking boundary: only the client's POST (``forge_check_run.publish_check_run``)
and ``gh pr view`` (``subprocess.run``) are replaced. Every conclusion in this
file is computed by the real ``review_verdict`` against real records on disk,
running the real merge-door invariant chain over real report files.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "forge"))

import apply_branch_protection as apply_bp  # noqa: E402
import closure_verifier  # noqa: E402
import forge_check_run  # noqa: E402
import forge_gate_publisher as fgp  # noqa: E402
import forge_protection_drift as drift  # noqa: E402
from dispatch_spec import Gate  # noqa: E402
from review_seat_support import pin_review_stack  # noqa: E402

pytestmark = pytest.mark.usefixtures("attributed_forge_target")

HEAD = "c" * 40
OTHER_HEAD = "d" * 40
#: The head the cap fell on: a different head than the one a PASS is booked on.
CAP_HEAD = "a" * 40
PR_NUMBER = 4242
PR_ID = str(PR_NUMBER)
BRANCH = "dispatch/20260908-golfb-b3-samenvattende-check"

#: The versioned YAML this repo actually ships, and the registered App read
#: from it — never a literal second copy of either.
YAML_PATH = forge_check_run.DEFAULT_YAML_PATH
REAL_APP_ID = forge_check_run.load_app_config(YAML_PATH).app_id

REVIEW_CHECK = "vnx-gate/review"


# ---------------------------------------------------------------------------
# Records on disk
# ---------------------------------------------------------------------------


def _clean_report(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_text(
        "# poortrapport\n\nDe poort heeft de diff gelezen en niets gevonden dat "
        "merge tegenhoudt.\n",
        encoding="utf-8",
    )
    return str(path)


def _blocking_report(tmp_path: Path, name: str) -> str:
    """A report whose own trailing verdict fence contradicts a ``pass`` record.

    The fence is the authoritative tier in
    ``closure_verifier._count_report_blocking_indicators`` — prose alone would
    make this test about the prose vangnet instead of about the contradiction.
    """
    path = tmp_path / name
    path.write_text(
        "# poortrapport\n\nDe poort heeft de diff gelezen.\n\n"
        "```json\n"
        + json.dumps(
            {
                "verdict": "fail",
                "findings": [{"severity": "blocking", "title": "dit mag niet mergen"}],
            },
            indent=2,
        )
        + "\n```\n",
        encoding="utf-8",
    )
    return str(path)


def _record(
    tmp_path: Path,
    gate: str,
    *,
    status: str = "pass",
    commit_sha: str = HEAD,
    report: Optional[str] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "gate": gate,
        "pr_id": PR_ID,
        "pr_number": PR_NUMBER,
        "status": status,
        "commit_sha": commit_sha,
        "branch": BRANCH,
        "contract_hash": "sha256:deadbeef",
        "report_path": report if report is not None else _clean_report(tmp_path, f"{gate}.md"),
        "blocking_findings": [],
        "advisory_findings": [],
        "dispatch_id": "20260908-golfb-b3-samenvattende-check",
        "evidence_source": "live",
        "test_run": False,
        "summary": f"{gate} {status}",
    }
    record.update(overrides)
    return record


def _write(results_dir: Path, gate: str, record: Dict[str, Any]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{PR_NUMBER}-{gate}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# A capped PR in a canonical store (RG-2)
# ---------------------------------------------------------------------------


def _round_line(head: str, verdict: str, blockers: int, *, at: int, report: str = "") -> Dict[str, Any]:
    return {
        "project_id": "", "pr_number": PR_NUMBER, "head_sha": head, "seat": "codex_gate",
        "reviewer": "codex_gate", "takeover_from": "", "verdict": verdict,
        "blocking_count": blockers,
        "report_path": report or f"/r/20261027-{at:02d}-codex.md",
        "dispatch_id": f"20261027-rg2-{at}", "recorded_at": f"2026-10-27T09:0{at}:00Z",
        "branch": BRANCH, "source": "gate",
    }


def _capped_store(tmp_path: Path, monkeypatch) -> Path:
    """A B3.1-capped PR with a codex PASS on HEAD, in ``.../review_gates/results``.

    The cap fell on CAP_HEAD (three blockers in one round). Only the cap keeps
    the PASS on HEAD from filling the codex seat; an override reason is what the
    door hands over to let it count.
    """
    pin_review_stack(monkeypatch, "codex_gate")
    results = tmp_path / "state" / "review_gates" / "results"
    results.mkdir(parents=True)
    record = _record(tmp_path, "codex_gate")
    _write(results, "codex_gate", record)
    opened = {"kind": "opened", "project_id": "", "pr_number": PR_NUMBER, "basis": "new",
              "evidence": 0, "recorded_at": "2026-10-27T09:00:00Z", "source": "gate"}
    lines = [opened, _round_line(CAP_HEAD, "fail", 3, at=1),
             _round_line(HEAD, "pass", 0, at=2, report=record["report_path"])]
    (results.parent / "rounds.ndjson").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return results


def _stub_publication(monkeypatch, *, current_head: str = HEAD) -> "_Recorder":
    """Replace the client, the App config, auto-merge and the PR's current head."""
    recorder = _Recorder()
    monkeypatch.setattr(fgp, "publish_check_run", recorder)
    monkeypatch.setattr(fgp, "load_app_config", lambda *a, **k: fgp.AppConfig("vnx-gate", 1))
    monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda pr, repo=None: False)
    monkeypatch.setattr(fgp, "_emit_publication_event", lambda **kw: None)
    monkeypatch.setattr(fgp, "_resolve_head_sha", lambda pr, repo: current_head)
    return recorder


@pytest.fixture(autouse=True)
def _one_seat_glm_project(monkeypatch):
    pin_review_stack(monkeypatch, "glm_gate")


@pytest.fixture()
def results_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "results"
    directory.mkdir()
    return directory


def _verdict(results_dir: Path, head: str = HEAD) -> fgp.ForgeVerdict:
    return fgp.review_verdict(PR_NUMBER, head, results_dir=results_dir, branch=BRANCH)


# ---------------------------------------------------------------------------
# The five behaviours the dispatch names
# ---------------------------------------------------------------------------


class TestSummaryConclusion:
    def test_ci_gate_pass_alone_never_fills_a_seat(self, tmp_path, results_dir):
        """OI-1645 on the check-run surface: CI is not a review signer.

        A head whose only fully-evidenced pass is ``ci_gate`` has never been
        reviewed. Since R5b the summary says so per seat: the required seat has
        not spoken on this head, which is ``action_required`` (blocking on a
        required check), never ``success``.
        """
        _write(results_dir, "ci_gate", _record(tmp_path, "ci_gate"))

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_ACTION_REQUIRED
        assert "glm_gate" in verdict.reason

    def test_glm_pass_with_full_evidence_is_success(self, tmp_path, results_dir):
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate"))

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_SUCCESS
        assert "glm_gate" in verdict.reason

    def test_pass_bound_to_another_sha_is_action_required(self, tmp_path, results_dir):
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate", commit_sha=OTHER_HEAD))

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_ACTION_REQUIRED
        assert verdict.conclusion != fgp.CONCLUSION_SUCCESS

    def test_no_review_verdict_at_all_is_action_required(self, results_dir):
        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_ACTION_REQUIRED

    def test_takeover_successor_signs_for_the_declared_gate(self, tmp_path, results_dir, monkeypatch):
        """OI-1576: the chain slid forward and the successor named the gate.

        A codex-only project: glm's record answers for codex's seat
        (``takeover_path[0]``), and fills nothing else.
        """
        pin_review_stack(monkeypatch, "codex_gate")
        _write(
            results_dir,
            "glm_gate",
            _record(
                tmp_path,
                "glm_gate",
                takeover=True,
                takeover_path=[{"gate": "codex_gate", "status": "unavailable"}],
            ),
        )

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_SUCCESS
        assert "glm_gate" in verdict.reason
        assert "overname" in verdict.reason


class TestSummaryConclusionEdges:
    def test_review_gate_rejection_on_this_head_is_failure(self, tmp_path, results_dir):
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate", status="fail"))

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_FAILURE
        assert "glm_gate" in verdict.reason

    def test_provider_outage_is_action_required_never_failure(self, tmp_path, results_dir):
        """Uitval is afwezigheid van bewijs, geen afkeuring (OI-1624)."""
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate", status="unavailable"))

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_ACTION_REQUIRED

    def test_pass_contradicted_by_its_own_report_is_failure(self, tmp_path, results_dir):
        _write(
            results_dir,
            "glm_gate",
            _record(tmp_path, "glm_gate", report=_blocking_report(tmp_path, "glm-blocking.md")),
        )

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_FAILURE

    def test_reanchored_evidence_never_signs(self, tmp_path, results_dir):
        """A verdict not produced by a run against this head cannot sign it.

        The same rule ``forge_gate_publisher._evidence_source_reason`` applies
        per gate: the merge door does not read ``evidence_source``, so without
        this the summary check would be MORE permissive than the per-gate one.
        """
        _write(
            results_dir,
            "glm_gate",
            _record(tmp_path, "glm_gate", evidence_source="reanchored"),
        )

        verdict = _verdict(results_dir)

        assert verdict.conclusion != fgp.CONCLUSION_SUCCESS

    def test_offline_test_run_never_signs(self, tmp_path, results_dir):
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate", test_run=True))

        verdict = _verdict(results_dir)

        assert verdict.conclusion == fgp.CONCLUSION_ACTION_REQUIRED

    def test_an_empty_head_is_refused(self, results_dir):
        with pytest.raises(fgp.ForgeCheckRunError):
            fgp.review_verdict(PR_NUMBER, "", results_dir=results_dir)

    @pytest.mark.parametrize("gate,status", [
        ("ci_gate", "pass"),
        ("glm_gate", "pass"),
        ("glm_gate", "fail"),
        ("glm_gate", "unavailable"),
        ("glm_gate", "pending"),
    ])
    def test_conclusion_is_never_neutral_or_skipped(self, tmp_path, results_dir, gate, status):
        """``neutral``/``skipped`` read as SATISFIED on a required check."""
        _write(results_dir, gate, _record(tmp_path, gate, status=status))

        verdict = _verdict(results_dir)

        assert verdict.conclusion in fgp.FORGE_CONCLUSIONS


class TestTheRuleComesFromTheMergeDoor:
    """The peer set is READ from closure_verifier, never copied into B3.

    A copy would pass every test above and then drift the first time OI-1645's
    exclusion list changes — the summary check would keep signing on a gate the
    door had already stopped accepting.
    """

    def test_ci_gate_signs_only_if_the_door_says_it_may(
        self, tmp_path, results_dir, monkeypatch
    ):
        """A stack naming ``ci_gate`` names no review seat while the door says CI
        is no reviewer, so nothing can sign; widen the door's peer set and the
        same stack has a seat that the CI pass fills."""
        pin_review_stack(monkeypatch, "ci_gate")
        _write(results_dir, "ci_gate", _record(tmp_path, "ci_gate"))
        assert _verdict(results_dir).conclusion == fgp.CONCLUSION_ACTION_REQUIRED

        monkeypatch.setattr(
            closure_verifier,
            "_REVIEW_PEER_GATES",
            frozenset(closure_verifier._REVIEW_PEER_GATES | {"ci_gate"}),
        )

        assert _verdict(results_dir).conclusion == fgp.CONCLUSION_SUCCESS

    def test_no_gate_in_the_enum_is_named_review(self):
        """``vnx-gate/review`` must never collide with ``vnx-gate/<gate>``."""
        assert fgp.REVIEW_SUMMARY_SLUG not in {g.value for g in Gate}

    def test_check_run_name_refuses_the_reserved_review_slug(self):
        with pytest.raises(fgp.ForgeCheckRunError):
            fgp.check_run_name(fgp.REVIEW_SUMMARY_SLUG)

    def test_the_summary_check_name_is_the_contract(self):
        assert fgp.REVIEW_SUMMARY_CHECK_NAME == REVIEW_CHECK


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, head_sha, name, conclusion, summary, **kwargs):
        self.calls.append(
            {"head_sha": head_sha, "name": name, "conclusion": conclusion, "summary": summary}
        )
        return {"id": 99}


class TestReviewPublication:
    def test_dry_run_posts_nothing_and_needs_no_app(self, tmp_path, results_dir, monkeypatch):
        recorder = _Recorder()
        monkeypatch.setattr(fgp, "publish_check_run", recorder)
        monkeypatch.setattr(
            fgp, "load_app_config", lambda *a, **k: pytest.fail("dry-run mag geen App nodig hebben")
        )
        # The armed-auto-merge refusal DOES run under --dry-run, exactly as it
        # does for the per-gate publisher: a rehearsal that hides a refusal is
        # rehearsing the wrong run.
        monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda pr, repo=None: False)
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate"))

        payload = fgp.publish_review_summary(
            PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH, dry_run=True
        )

        assert payload["name"] == REVIEW_CHECK
        assert payload["conclusion"] == fgp.CONCLUSION_SUCCESS
        assert recorder.calls == []

    def test_publication_sends_the_summary_name_and_conclusion(
        self, tmp_path, results_dir, monkeypatch
    ):
        recorder = _Recorder()
        monkeypatch.setattr(fgp, "publish_check_run", recorder)
        monkeypatch.setattr(fgp, "load_app_config", lambda *a, **k: fgp.AppConfig("vnx-gate", 1))
        monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda pr, repo=None: False)
        monkeypatch.setattr(fgp, "_emit_publication_event", lambda **kw: None)
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate"))

        fgp.publish_review_summary(PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH)

        assert len(recorder.calls) == 1
        assert recorder.calls[0]["name"] == REVIEW_CHECK
        assert recorder.calls[0]["conclusion"] == fgp.CONCLUSION_SUCCESS
        assert recorder.calls[0]["head_sha"] == HEAD

    def test_success_is_refused_while_auto_merge_is_armed(
        self, tmp_path, results_dir, monkeypatch
    ):
        recorder = _Recorder()
        monkeypatch.setattr(fgp, "publish_check_run", recorder)
        monkeypatch.setattr(fgp, "load_app_config", lambda *a, **k: fgp.AppConfig("vnx-gate", 1))
        monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda pr, repo=None: True)
        monkeypatch.setattr(fgp, "_emit_publication_event", lambda **kw: None)
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate"))

        with pytest.raises(fgp.ForgePublishRefused):
            fgp.publish_review_summary(PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH)

        assert recorder.calls == []

    def test_dry_run_still_shows_the_armed_auto_merge_refusal(
        self, tmp_path, results_dir, monkeypatch
    ):
        """A rehearsal that hides a refusal is rehearsing the wrong run."""
        monkeypatch.setattr(
            fgp, "publish_check_run", lambda *a, **k: pytest.fail("dry-run mag niet posten")
        )
        monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda pr, repo=None: True)
        monkeypatch.setattr(fgp, "_emit_publication_event", lambda **kw: None)
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate"))

        with pytest.raises(fgp.ForgePublishRefused):
            fgp.publish_review_summary(
                PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH, dry_run=True
            )

    def test_red_is_published_even_with_auto_merge_armed(
        self, tmp_path, results_dir, monkeypatch
    ):
        """The red check IS the brake; withholding it removes the brake."""
        recorder = _Recorder()
        monkeypatch.setattr(fgp, "publish_check_run", recorder)
        monkeypatch.setattr(fgp, "load_app_config", lambda *a, **k: fgp.AppConfig("vnx-gate", 1))
        monkeypatch.setattr(fgp, "auto_merge_is_armed", lambda pr, repo=None: True)
        monkeypatch.setattr(fgp, "_emit_publication_event", lambda **kw: None)
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate", status="fail"))

        fgp.publish_review_summary(PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH)

        assert recorder.calls[0]["conclusion"] == fgp.CONCLUSION_FAILURE


# ---------------------------------------------------------------------------
# The cap and the override on the required check (RG-2, OI-2143)
# ---------------------------------------------------------------------------


class TestTheCapAndTheOverride:
    def test_a_capped_head_without_an_override_keeps_the_check_red(self, tmp_path, monkeypatch):
        """Acceptance 2: today's rule, pinned — the cap alone keeps the check red."""
        results = _capped_store(tmp_path, monkeypatch)
        recorder = _stub_publication(monkeypatch)

        payload = fgp.publish_review_summary(PR_NUMBER, HEAD, results_dir=results, branch=BRANCH)

        assert payload["conclusion"] == fgp.CONCLUSION_FAILURE
        assert recorder.calls[0]["conclusion"] == fgp.CONCLUSION_FAILURE
        assert "B3.1" in payload["summary"] and "B3 override" not in payload["summary"]

    def test_an_override_reason_turns_the_same_head_green_and_is_on_the_summary(
        self, tmp_path, monkeypatch
    ):
        """Acceptance 1: ``override_b3`` on a capped head publishes ``success``."""
        results = _capped_store(tmp_path, monkeypatch)
        recorder = _stub_publication(monkeypatch)
        reason = "operator approved the remaining blockers as open items"

        payload = fgp.publish_review_summary(
            PR_NUMBER, HEAD, results_dir=results, branch=BRANCH, b3_override=reason
        )

        assert payload["conclusion"] == fgp.CONCLUSION_SUCCESS
        assert recorder.calls[0]["conclusion"] == fgp.CONCLUSION_SUCCESS
        assert reason in payload["summary"] and "B3 override" in payload["summary"]
        assert "B3.1" in payload["summary"]

    def test_a_record_with_an_override_reason_is_published_green_from_the_recorder(
        self, tmp_path, monkeypatch
    ):
        """Acceptance 1, end to end: the record's own ``override_b3`` reaches the check."""
        import gate_recorder

        results = _capped_store(tmp_path, monkeypatch)
        recorder = _stub_publication(monkeypatch)
        reason = "operator approved two remaining blockers"
        payload = {
            "gate": "codex_gate", "pr_id": PR_ID, "pr_number": PR_NUMBER, "status": "pass",
            "commit_sha": HEAD, "branch": BRANCH, "report_path": str(tmp_path / "codex_gate.md"),
            "override_b3": {"reason": reason, "rule": "B3.1"},
        }

        gate_recorder.publish_forge_review_summary(
            payload, pr_number=PR_NUMBER, head_sha=HEAD,
            result_path=results / f"pr-{PR_NUMBER}-codex_gate.json",
        )

        assert [call["conclusion"] for call in recorder.calls] == [fgp.CONCLUSION_SUCCESS]
        assert "B3 override" in recorder.calls[0]["summary"]
        assert reason in recorder.calls[0]["summary"]

    def test_the_threshold_is_named_when_the_cap_record_carries_one(self, tmp_path, monkeypatch):
        """RG-1's ``threshold`` in the cap record reaches the summary when present."""
        import review_rounds

        monkeypatch.setattr(review_rounds.CapState, "to_record", lambda self: {
            "rule": self.rule, "cap_round": self.cap_round, "rounds": len(self.rounds),
            "blockers": list(self.blockers), "total": self.total, "threshold": "rounds",
        })
        results = _capped_store(tmp_path, monkeypatch)
        _stub_publication(monkeypatch)

        payload = fgp.publish_review_summary(
            PR_NUMBER, HEAD, results_dir=results, branch=BRANCH, b3_override="approved"
        )

        assert "drempel: rounds" in payload["summary"]

    def test_an_override_outside_a_cap_says_so_and_changes_nothing(self, tmp_path, results_dir):
        """An override on a PR the cap never touched fills no seat by itself."""
        _write(results_dir, "glm_gate", _record(tmp_path, "glm_gate", status="fail"))

        verdict = fgp.review_verdict(
            PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH, b3_override="noted"
        )

        assert verdict.conclusion == fgp.CONCLUSION_FAILURE
        assert "B3 override: noted" in verdict.reason and "niet op de B3-cap" in verdict.reason


class TestTheRefreshEntryPoint:
    """RG-4 wires this into the door; it republishes without writing a gate record."""

    def test_it_republishes_green_and_writes_no_gate_record(self, tmp_path, monkeypatch):
        """Acceptance 4: green for the capped head, and the results dir is untouched."""
        results = _capped_store(tmp_path, monkeypatch)
        recorder = _stub_publication(monkeypatch)
        before = sorted(path.name for path in results.iterdir())

        published = fgp.refresh_review_summary(
            PR_NUMBER, HEAD, b3_override="door override: two open items booked",
            results_dir=results, branch=BRANCH,
        )

        assert published["conclusion"] == fgp.CONCLUSION_SUCCESS
        assert [call["head_sha"] for call in recorder.calls] == [HEAD]
        assert recorder.calls[0]["conclusion"] == fgp.CONCLUSION_SUCCESS
        assert "B3 override" in recorder.calls[0]["summary"]
        assert "two open items booked" in recorder.calls[0]["summary"]
        assert sorted(path.name for path in results.iterdir()) == before

    def test_a_moved_head_publishes_nothing_and_says_why(self, tmp_path, monkeypatch, caplog):
        """Acceptance 3: the PR points at another commit, so nothing goes out."""
        results = _capped_store(tmp_path, monkeypatch)
        recorder = _stub_publication(monkeypatch, current_head=OTHER_HEAD)

        with caplog.at_level(logging.WARNING, logger="forge_gate_publisher"):
            published = fgp.refresh_review_summary(
                PR_NUMBER, HEAD, b3_override="door override", results_dir=results, branch=BRANCH
            )

        assert published is None and recorder.calls == []
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert HEAD[:12] in logged and OTHER_HEAD[:12] in logged and "verschoven" in logged

    def test_a_failing_forge_call_returns_nothing_published(self, tmp_path, monkeypatch):
        """Acceptance 4: the door gets a refusal, not a traceback (``None`` in RG-4 round 2)."""
        results = _capped_store(tmp_path, monkeypatch)
        _stub_publication(monkeypatch)

        def exploding(*_a, **_k):
            raise fgp.ForgeAPIError("GitHub gaf 500", status=500, body="boom")

        monkeypatch.setattr(fgp, "publish_check_run", exploding)

        assert fgp.refresh_review_summary(
            PR_NUMBER, HEAD, b3_override="door override", results_dir=results, branch=BRANCH
        ) is None

    def test_an_override_without_a_reason_is_refused(self, tmp_path, monkeypatch):
        results = _capped_store(tmp_path, monkeypatch)

        with pytest.raises(fgp.ForgeCheckRunError):
            fgp.refresh_review_summary(PR_NUMBER, HEAD, b3_override="   ", results_dir=results)


# ---------------------------------------------------------------------------
# The YAML: required, and bound to the App that signs
# ---------------------------------------------------------------------------


def _real_yaml_doc() -> Dict[str, Any]:
    return yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))


#: A context that exists NOWHERE — not in ``checks[]``, not in
#: ``pending_checks``, not on the branch. The parking-slot invariant needs a
#: pending entry to observe, and after OP-B3 the YAML no longer ships one; a
#: synthetic name keeps the invariant testable without re-parking the real
#: check to test it.
SYNTHETIC_PENDING_CHECK = "vnx-gate/nooit-vereist-alleen-voor-deze-test"


class TestBranchProtectionYaml:
    """OP-B3 flipped this class. Before it, ``vnx-gate/review`` sat in
    ``pending_checks`` and the assertions here pinned that it reached neither
    the apply nor the drift check. That was true of the YAML on ``main`` and is
    the exact state OP-B3 ends: the entry now lives in
    ``required_status_checks.checks`` bound to ``app.app_id``, so every
    assertion about WHERE it sits is inverted on purpose.

    What did NOT change is the parking-slot invariant itself — an entry in
    ``pending_checks`` still reaches neither apply nor drift. It lost its last
    real user, not its meaning, so it is pinned below on a synthetic entry
    (:data:`SYNTHETIC_PENDING_CHECK`) instead of being deleted along with the
    promotion.
    """

    def test_review_check_is_no_longer_parked(self):
        config = drift.load_protection_config(YAML_PATH)

        assert REVIEW_CHECK not in config.pending_checks
        assert config.pending_checks == ()

    def test_review_check_is_required_and_bound_to_the_app(self):
        """Required AND bound. The binding is half the requirement: an unbound
        ``vnx-gate/*`` context could be satisfied by any app that knows the
        name, which is why the schema reader refuses one (see
        :meth:`test_promoting_it_unbound_is_refused`)."""
        config = drift.load_protection_config(YAML_PATH)

        assert (REVIEW_CHECK, REAL_APP_ID) in {(c.context, c.app_id) for c in config.checks}

    def test_apply_and_drift_now_see_it(self):
        """The inversion that has teeth: it is in the PUT body the apply sends
        and in the object the drift check compares against live state."""
        config = drift.load_protection_config(YAML_PATH)

        normalized = drift.to_normalized_dict(config)
        put_body = apply_bp.build_put_payload(config)

        assert {"context": REVIEW_CHECK, "app_id": REAL_APP_ID} in (
            normalized["required_status_checks"]["checks"]
        )
        assert {"context": REVIEW_CHECK, "app_id": REAL_APP_ID} in (
            put_body["required_status_checks"]["checks"]
        )

    def test_apply_and_drift_never_see_a_pending_entry(self):
        """The B1 parking-slot invariant, on a synthetic entry.

        ``pending_checks`` remains a slot that requires nothing of ``main``:
        ``apply_branch_protection.py`` leaves it out of the PUT and
        ``forge_protection_drift.py`` never compares it. OP-B3 emptied the list
        but did not remove the mechanism, and the next check to be parked there
        must inherit the same guarantee.
        """
        doc = _real_yaml_doc()
        doc["pending_checks"] = [SYNTHETIC_PENDING_CHECK]

        config = drift.parse_protection_config(yaml.safe_dump(doc))

        normalized = drift.to_normalized_dict(config)
        put_body = apply_bp.build_put_payload(config)

        assert SYNTHETIC_PENDING_CHECK in config.pending_checks
        assert "pending_checks" not in normalized
        assert SYNTHETIC_PENDING_CHECK not in json.dumps(normalized)
        assert SYNTHETIC_PENDING_CHECK not in json.dumps(put_body)

    def test_the_real_yaml_with_the_bound_entry_is_accepted(self):
        """What the old ``test_promoting_it_with_the_real_app_id_is_accepted``
        meant, now that the promotion is on disk.

        It used to build the promoted document by appending the entry to a copy.
        Appending it now produces a DUPLICATE context, which the schema reader
        refuses for its own reason — the test would still be red-free but it
        would be measuring the duplicate guard instead of acceptance. So it
        reads the shipped file as-is: the real YAML, through the real parser,
        with the entry it really carries.
        """
        config = drift.parse_protection_config(YAML_PATH.read_text(encoding="utf-8"))

        contexts = [c.context for c in config.checks]
        assert contexts.count(REVIEW_CHECK) == 1
        assert dict((c.context, c.app_id) for c in config.checks)[REVIEW_CHECK] == REAL_APP_ID

    @pytest.mark.parametrize("app_id", [None, drift.ANY_APP_ID])
    def test_promoting_it_unbound_is_refused(self, app_id):
        """Unbinding the entry that now ships must still be refused.

        Same guarantee as before, reached from the other side: the entry no
        longer has to be added, it has to be BROKEN. Rewriting the app_id of the
        real entry to ``null`` / the "any app" sentinel is exactly the edit a
        future hand could make to the shipped file.
        """
        doc = _real_yaml_doc()
        entries = [
            c for c in doc["required_status_checks"]["checks"] if c["context"] == REVIEW_CHECK
        ]
        assert len(entries) == 1, "de promotie hoort precies een entry op te leveren"
        entries[0]["app_id"] = app_id

        with pytest.raises(drift.ProtectionConfigError):
            drift.parse_protection_config(yaml.safe_dump(doc))


# ---------------------------------------------------------------------------
# The rehearsal: what the apply WOULD send once the entry is promoted
# ---------------------------------------------------------------------------


class TestPendingPreview:
    def test_the_put_object_carries_the_review_check_bound_to_the_app(self):
        payload = fgp.pending_promotion_put_payload(YAML_PATH)

        checks = payload["required_status_checks"]["checks"]
        assert {"context": REVIEW_CHECK, "app_id": REAL_APP_ID} in checks

    def test_every_currently_required_check_survives_the_promotion(self):
        """The shipped file, where ``pending_checks`` is empty since OP-B3.

        Kept as the no-op control, and it is honest about being one: with
        nothing parked, the promotion loop runs zero iterations and this
        asserts ``X <= X``, which cannot fail. It pins that an EMPTY
        ``pending_checks`` neither drops nor invents a requirement — nothing
        more. The test that actually exercises the loop is
        :meth:`test_a_parked_entry_is_promoted_bound_to_the_signing_app`.
        """
        config = drift.load_protection_config(YAML_PATH)

        payload = fgp.pending_promotion_put_payload(YAML_PATH)

        promoted = {c["context"] for c in payload["required_status_checks"]["checks"]}
        assert config.pending_checks == (), (
            "dit is de nul-iteratie-controle; met een geparkeerde entry meet hij "
            "iets anders dan hij zegt"
        )
        assert {c.context for c in config.checks} <= promoted

    def test_a_parked_entry_is_promoted_bound_to_the_signing_app(self, tmp_path):
        """The promotion loop itself, on a synthetic parked entry.

        Without this the loop is dead code behind a green suite: OP-B3 emptied
        ``pending_checks``, so every other test here drives
        :func:`pending_promotion_put_payload` through zero iterations. The two
        refusal branches stayed covered; the branch that actually promotes did
        not. Same technique as
        :meth:`TestBranchProtectionYaml.test_apply_and_drift_never_see_a_pending_entry`
        — a synthetic entry on a copy of the real document, so the shipped file
        is never re-parked to test the mechanism that unparks it.
        """
        doc = _real_yaml_doc()
        doc["pending_checks"] = [SYNTHETIC_PENDING_CHECK]
        path = tmp_path / "branch_protection.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        before = drift.load_protection_config(path)
        assert SYNTHETIC_PENDING_CHECK not in {c.context for c in before.checks}

        payload = fgp.pending_promotion_put_payload(path)

        promoted = payload["required_status_checks"]["checks"]
        by_context = {c["context"]: c["app_id"] for c in promoted}
        assert {c.context for c in before.checks} <= set(by_context), (
            "een promotie mag geen bestaande eis laten vallen"
        )
        assert by_context[SYNTHETIC_PENDING_CHECK] == REAL_APP_ID, (
            "de geparkeerde entry komt gebonden aan de tekenende App uit de lus, "
            "niet als kale context"
        )
        assert len(promoted) == len(before.checks) + 1
        assert "pending_checks" not in payload

    def test_the_rehearsal_never_talks_to_github(self, monkeypatch, capsys):
        """No PUT, no POST, no ``gh`` — a rehearsal that writes is not one."""
        import subprocess as _subprocess

        monkeypatch.setattr(
            _subprocess, "run", lambda *a, **k: pytest.fail(f"subprocess gestart: {a}")
        )
        monkeypatch.setattr(
            fgp, "publish_check_run", lambda *a, **k: pytest.fail("check-run gepubliceerd")
        )

        rc = fgp.main(["pending-preview"])

        printed = capsys.readouterr().out
        assert rc == fgp.EXIT_OK
        assert REVIEW_CHECK in printed
        assert str(REAL_APP_ID) in printed
        assert json.loads(printed[printed.index("{"):])["required_status_checks"]["strict"] is False

    def test_a_pending_entry_that_is_not_a_vnx_gate_check_is_refused(self, tmp_path):
        doc = _real_yaml_doc()
        doc["pending_checks"] = ["Profile Z (something else)"]
        path = tmp_path / "branch_protection.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")

        with pytest.raises(fgp.ForgePublishRefused):
            fgp.pending_promotion_put_payload(path)

    @pytest.mark.parametrize("content", [None, "branch: main\n"])
    def test_an_unreadable_yaml_refuses_instead_of_tracebacking(self, tmp_path, content):
        """A missing file and a malformed one both come back as a refusal.

        ``load_protection_config`` raises ``OSError`` / ``ProtectionConfigError``
        (a ``ValueError``); neither is a ``ForgeCheckRunError``, so without the
        translation at this boundary the CLI's handler misses both and prints a
        traceback — for the one command whose entire purpose is a readable
        rehearsal.
        """
        path = tmp_path / "branch_protection.yaml"
        if content is not None:
            path.write_text(content, encoding="utf-8")

        with pytest.raises(fgp.ForgePublishRefused):
            fgp.pending_promotion_put_payload(path)

    def test_an_unreadable_yaml_exits_non_zero_without_a_traceback(self, tmp_path, capsys):
        rc = fgp.main(["pending-preview", "--yaml", str(tmp_path / "afwezig.yaml")])

        captured = capsys.readouterr()
        assert rc == fgp.EXIT_ERROR
        assert "pending-preview:" in captured.err
        assert "Traceback" not in captured.err

    def test_a_pending_entry_already_required_is_refused(self, tmp_path):
        """A context that is parked AND required refuses in the rehearsal.

        Before OP-B3 this test appended the entry to ``checks[]`` itself. After
        it, ``checks[]`` already carries ``vnx-gate/review``, so appending would
        make the document hold the context TWICE — and the schema reader refuses
        that for its own, earlier reason ("bevat 'vnx-gate/review' dubbel",
        measured). The test would stay green while
        :func:`pending_promotion_put_payload`'s own ``context in required``
        refusal went unexercised. Re-parking the already-required entry is what
        reaches that branch now.
        """
        doc = _real_yaml_doc()
        doc["pending_checks"] = [REVIEW_CHECK]
        assert REVIEW_CHECK in {
            c["context"] for c in doc["required_status_checks"]["checks"]
        }, "checks[] hoort de entry na OP-B3 al te dragen; anders test dit iets anders"
        path = tmp_path / "branch_protection.yaml"
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")

        with pytest.raises(fgp.ForgePublishRefused, match="zowel in pending_checks als in"):
            fgp.pending_promotion_put_payload(path)
