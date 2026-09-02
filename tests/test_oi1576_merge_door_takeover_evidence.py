#!/usr/bin/env python3
"""OI-1576: the merge door must read takeover provenance from the successor's
OWN record.

Measured on main 0faeccce against the vnx-dev store (PR #1729): the gate chain
slid forward — codex_gate unavailable, kimi_gate not executable, glm_gate took
over and wrote a valid PASS under its OWN name, carrying
``takeover: true`` + a ``takeover_path`` whose first element is the declared
gate (``codex_gate``). ``gate_obligation_runner`` walks that chain since #1740,
but the merge door (``pr_merge`` ->
``closure_verifier.check_review_gate_for_merge`` -> ``_find_gate_result``) only
ever looked for a record NAMED after the declared gate — NO-GO while glm had
in fact reviewed and passed the head commit.

The fix is read-side only: a successor record counts as evidence for declared
gate X when, and only when:

  1. the record carries ``takeover: true`` AND a non-empty ``takeover_path``
  2. an element of ``takeover_path`` has ``gate == X``
  3. the record's OWN status is a pass — never a chain step's status
  4. every invariant the door already enforces still holds (terminal, complete
     evidence, report on disk, gate_is_pass, no blocking indicators, the
     head-sha binding from #1740)

A record with ``takeover: true`` but an empty/absent ``takeover_path`` is a
THIRD outcome — not "no takeover", not "proven provenance" — and is refused
with a message that the provenance could not be determined.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier

# Mirrored from the live vnx-dev store records for PR #1729.
PR_ID = "1729"
BRANCH = "dispatch/20260830-143000-oi1532-levende-dispatch-niet-afboeken"
HEAD_SHA = "aa1b8ebf151bf87a3f98c647d0c97d63e59aa197"
STALE_SHA = "e9c9e3d579473569d5204c10d5bf4d346bef1ba2"

CLEAN_REPORT = "# Gate report\n\nAll findings reviewed, nothing blocking.\n"


def _write_result(results_dir: Path, gate: str, data: dict) -> Path:
    """Write a result record under the writer's real naming (pr-<n>-<gate>.json)."""
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{PR_ID}-{gate}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _report_file(tmp_path: Path, content: str = CLEAN_REPORT) -> Path:
    report = tmp_path / "report.md"
    report.write_text(content, encoding="utf-8")
    return report


def _declared_unavailable() -> dict:
    """pr-1729-codex_gate.json as measured: unavailable, recorded on a stale sha."""
    return {
        "gate": "codex_gate",
        "pr_id": PR_ID,
        "status": "unavailable",
        "contract_hash": "",
        "report_path": "",
        "branch": BRANCH,
        "commit_sha": STALE_SHA,
    }


def _successor(report: Path, **overrides) -> dict:
    """pr-1729-glm_gate.json as measured before the later direct run overwrote it:
    a pass under the successor's own name, naming its own provenance."""
    data = {
        "gate": "glm_gate",
        "pr_id": PR_ID,
        "status": "pass",
        "blocking_count": 0,
        "blocking_findings": [],
        "contract_hash": "15270d8bdfc53bcf",
        "report_path": str(report),
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
        "takeover": True,
        "takeover_from": "kimi_gate",
        "takeover_path": [
            {"gate": "codex_gate", "status": "unavailable"},
            {"gate": "kimi_gate", "status": "not_executable"},
        ],
    }
    data.update(overrides)
    return data


def _check(results_dir: Path, gate: str = "codex_gate", head_sha: str = HEAD_SHA) -> dict:
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, gate, results_dir, branch=BRANCH, head_sha=head_sha
    )


class TestSuccessorEvidenceAccepted:
    def test_declared_unavailable_successor_pass_is_go(self, tmp_path):
        """The live #1729 shape: declared codex_gate unavailable on a stale sha,
        glm_gate pass on the head sha with codex_gate in its takeover_path."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "glm_gate", _successor(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO"
        assert verdict["gate"] == "codex_gate"
        assert verdict["evidence_gate"] == "glm_gate"
        assert "overname" in verdict["message"]

    def test_successor_without_own_pass_is_no_go(self, tmp_path):
        """Requirement 3: the successor's OWN status decides — a successor that
        is not itself a pass is NO-GO even with a perfect takeover_path."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(
            results_dir, "glm_gate", _successor(report, status="fail", blocking_count=1)
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "glm_gate" in verdict["message"]

    def test_successor_sha_mismatch_is_no_go(self, tmp_path):
        """The sha-binding from #1740 applies to takeover evidence too: a
        successor pass recorded against a different commit is stale evidence."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(
            results_dir, "glm_gate", _successor(report, commit_sha=STALE_SHA)
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"

    def test_successor_with_blocking_report_is_no_go(self, tmp_path):
        """Invariant 7 applies to takeover evidence too: a passing record whose
        report carries blocking indicators contradicts itself."""
        results_dir = tmp_path / "results"
        report = _report_file(
            tmp_path,
            content='# Gate report\n\n```json\n{"verdict": "fail", "findings": []}\n```\n',
        )
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "glm_gate", _successor(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "blocking" in verdict["message"]

    def test_successor_missing_contract_hash_is_no_go(self, tmp_path):
        """Complete-evidence invariant applies to takeover evidence too."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "glm_gate", _successor(report, contract_hash=""))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "contract_hash" in verdict["message"]


class TestThirdBranchProvenanceNotDeterminable:
    def test_takeover_true_empty_path_is_no_go(self, tmp_path):
        """takeover: true with an EMPTY takeover_path can prove nothing — refused
        with 'herkomst niet vast te stellen', a third outcome, not a third value."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "glm_gate", _successor(report, takeover_path=[]))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "herkomst niet vast te stellen" in verdict["message"]

    def test_takeover_true_missing_path_is_no_go(self, tmp_path):
        """Same third branch when takeover_path is absent from the record."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        record = _successor(report)
        del record["takeover_path"]
        _write_result(results_dir, "glm_gate", record)

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "herkomst niet vast te stellen" in verdict["message"]


class TestUnrelatedRecordsNeverTakeOver:
    def test_plain_pass_for_other_gate_is_no_go(self, tmp_path):
        """A passing record that makes no takeover claim is not evidence for
        the declared gate — no random pass may take over."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        record = _successor(report)
        del record["takeover"]
        del record["takeover_from"]
        del record["takeover_path"]
        _write_result(results_dir, "glm_gate", record)

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"

    def test_takeover_path_without_declared_gate_is_no_go(self, tmp_path):
        """A takeover record whose path never passes through the declared gate
        proves a DIFFERENT takeover, not this one."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(
            results_dir,
            "glm_gate",
            _successor(
                report,
                takeover_path=[{"gate": "kimi_gate", "status": "not_executable"}],
            ),
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"


class TestContradictionTrap:
    def test_path_step_status_never_disqualifies_declared_gates_own_pass(self, tmp_path):
        """The walking-reader trap (live in the #1729 store):
        pr-1729-deepseek_gate.json's takeover_path names glm_gate as
        not_executable, while pr-1729-glm_gate.json itself says pass. Glm ran
        twice — once as a chain step the runner could not drive, once as a
        direct script run. A record's OWN status decides; the status of a step
        in someone else's path must never disqualify it."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        glm_record = {
            "gate": "glm_gate",
            "pr_id": PR_ID,
            "status": "pass",
            "blocking_count": 0,
            "blocking_findings": [],
            "contract_hash": "15270d8bdfc53bcf",
            "report_path": str(report),
            "branch": BRANCH,
            "commit_sha": HEAD_SHA,
        }
        _write_result(results_dir, "glm_gate", glm_record)
        deepseek_record = _successor(
            report,
            gate="deepseek_gate",
            status="not_executable",
            takeover_from="glm_gate",
            takeover_path=[
                {"gate": "codex_gate", "status": "unavailable"},
                {"gate": "kimi_gate", "status": "not_executable"},
                {"gate": "glm_gate", "status": "not_executable"},
            ],
        )
        _write_result(results_dir, "deepseek_gate", deepseek_record)

        verdict = _check(results_dir, gate="glm_gate")

        assert verdict["verdict"] == "GO"
        assert "evidence_gate" not in verdict


class TestFilenameFilterPreservesEvidence:
    """OI-1599 advisory: ``_find_takeover_successor_results`` now pre-filters
    the results_dir glob by filename (``_pr_scoped_json_candidates``) before
    opening/parsing a single record — the glm_gate advisory on cd0e3112 flags
    the unconditional ``*.json`` scan as a merge-time cost that grows with the
    (monotonically growing) results directory.

    The only real risk of that change is a filename the filter skips that
    still carries valid evidence. Every test below drives the PUBLIC
    ``check_review_gate_for_merge`` path (never the private filter helper
    directly), so the SAME test runs unmodified against the pre-change code
    (which never filters by filename — it always scans everything) and the
    post-change code. Both must pass identically; a test that only passes
    post-change would prove the filter self-consistent, not that it preserved
    what the unfiltered scan used to find.
    """

    def test_successor_found_regardless_of_filename_convention(self, tmp_path):
        """The two real writer conventions — legacy ``pr-<n>-<gate>.json``
        (``_write_result``) and contract-style ``<slug>-<gate>-contract.json``
        (``gate_recorder.result_file_path`` when ``pr_id`` is set) — must both
        still be found as takeover evidence, not just whichever one a test
        helper happens to default to."""
        results_dir = tmp_path / "results"
        results_dir.mkdir(parents=True)
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        # Contract-style filename instead of the legacy pr-<n>-<gate>.json.
        contract_path = results_dir / f"{PR_ID}-glm_gate-contract.json"
        contract_path.write_text(json.dumps(_successor(report)), encoding="utf-8")

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO"
        assert verdict["evidence_gate"] == "glm_gate"

    def test_successor_found_among_many_other_pr_noise_files(self, tmp_path):
        """Hundreds of OTHER PRs' records sit in the same results_dir (the
        live vnx-dev store measured 522 files across 419 distinct pr_ids on
        2026-09-02). None of that noise — written under both naming
        conventions — may hide this PR's own successor evidence."""
        results_dir = tmp_path / "results"
        results_dir.mkdir(parents=True)
        report = _report_file(tmp_path)
        for other_pr in range(1000, 1050):
            noise = _declared_unavailable()
            noise["pr_id"] = str(other_pr)
            noise["gate"] = "glm_gate"
            (results_dir / f"pr-{other_pr}-glm_gate.json").write_text(
                json.dumps(noise), encoding="utf-8"
            )
            (results_dir / f"{other_pr}-glm_gate-contract.json").write_text(
                json.dumps(noise), encoding="utf-8"
            )
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "glm_gate", _successor(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO"
        assert verdict["evidence_gate"] == "glm_gate"

    def test_pr_id_with_glob_metacharacter_falls_back_to_full_scan(self, tmp_path):
        """A pr_id containing a glob metacharacter is never produced by any
        real caller (every one passes ``str(pr_number)``), but the filter
        must still resolve correctly rather than silently mis-globbing and
        losing evidence for it."""
        results_dir = tmp_path / "results"
        results_dir.mkdir(parents=True)
        report = _report_file(tmp_path)
        weird_pr_id = "17[29]"
        declared = _declared_unavailable()
        declared["pr_id"] = weird_pr_id
        (results_dir / "weird-codex_gate.json").write_text(
            json.dumps(declared), encoding="utf-8"
        )
        successor = _successor(report)
        successor["pr_id"] = weird_pr_id
        (results_dir / "weird-glm_gate.json").write_text(
            json.dumps(successor), encoding="utf-8"
        )

        verdict = closure_verifier.check_review_gate_for_merge(
            weird_pr_id, "codex_gate", results_dir, branch=BRANCH, head_sha=HEAD_SHA
        )

        assert verdict["verdict"] == "GO"
        assert verdict["evidence_gate"] == "glm_gate"
