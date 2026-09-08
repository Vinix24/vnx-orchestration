#!/usr/bin/env python3
"""Golf Bx / D7: a takeover successor that rendered NO verdict must not
displace the independent signer.

Measured live on main 58330dd4 while merging PR #1818 (head ``3074b642``), in
the vnx-dev store ``state/review_gates/results/``:

  - ``pr-1818-glm_gate.json``  status=pass, contract_hash + report_path filled,
    same branch + commit_sha, no takeover claim.
  - ``pr-1818-codex_gate.json`` status=unavailable (usage limit), empty
    contract_hash/report_path.
  - ``pr-1818-kimi_gate.json``  status=not_executable,
    reason='gate_not_subprocess_routable', empty contract_hash/report_path,
    ``takeover: true`` with ``takeover_path`` naming ``codex_gate``.

The merge door refused with::

    overname-route voor codex_gate: kimi_gate resultaat mist contract_hash
    en/of report_path: bewijs onvolledig

A fully proven pass existed on the exact head, and a record that only says
something about the RUNNER's routing ("this gate is not subprocess routable")
held it back. The OI-1576 takeover loop runs before the OI-1624/OI-1642
independent-signer branch, and the loop's ``first_failure`` was being set by a
record that never rendered a verdict at all — the same failure family as the
rest of golf Bx: a record that is not a judgement works as a judgement.

The fix is read-side and one-sided: a successor whose canonical status is not
in ``_DECIDED_VERDICT_STATES`` (pass ∪ fail) — the SAME discriminator the
declared-gate branch directly above already applies, and the same one
``_find_peer_gate_results`` uses to decide who may sign — is skipped instead of
recorded as the first failure. A successor that DID render a verdict is judged
by the unchanged invariant chain and still blocks.

The ordering itself is deliberately NOT changed: the takeover loop stays in
front of the independent-signer branch, because a successor that explicitly
claims the declared gate is stronger evidence than a lane that does not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import closure_verifier

# Mirrored from the live vnx-dev store records for PR #1818.
PR_ID = "1818"
BRANCH = "dispatch/20260908-bx-d2-fence-mededeling"
HEAD_SHA = "3074b6429b1f8ae2a0a353d3a027acab2c200639"

CLEAN_REPORT = "# glm gate report\n\nglm gate: pass (0 blocking finding(s))\n"


def _write_result(results_dir: Path, gate: str, data: dict) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{PR_ID}-{gate}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _report_file(tmp_path: Path, content: str = CLEAN_REPORT) -> Path:
    report = tmp_path / "glm-gate-pr1818.md"
    report.write_text(content, encoding="utf-8")
    return report


def _declared_unavailable(**overrides) -> dict:
    """pr-1818-codex_gate.json as measured: unavailable after exit_nonzero."""
    data = {
        "gate": "codex_gate",
        "pr_id": PR_ID,
        "status": "unavailable",
        "reason": "exit_nonzero",
        "contract_hash": "",
        "report_path": "",
        "blocking_findings": [],
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
    }
    data.update(overrides)
    return data


def _kimi_not_executable(**overrides) -> dict:
    """pr-1818-kimi_gate.json as measured: a takeover claim carrying no verdict.

    ``not_executable`` is terminal (the chain finally classified this lane as
    unable to run) but is neither pass nor fail — the record says something
    about the runner's routing, never about the code.
    """
    data = {
        "gate": "kimi_gate",
        "pr_id": PR_ID,
        "status": "not_executable",
        "reason": "gate_not_subprocess_routable",
        "contract_hash": "",
        "report_path": "",
        "blocking_findings": [],
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
        "takeover": True,
        "takeover_from": "codex_gate",
        "takeover_reason": "exit_nonzero",
        "takeover_source_status": "unavailable",
        "takeover_path": [
            {
                "gate": "codex_gate",
                "reason": "exit_nonzero",
                "status": "unavailable",
            }
        ],
    }
    data.update(overrides)
    return data


def _glm_pass(report: Path, **overrides) -> dict:
    """pr-1818-glm_gate.json as measured: a fully evidenced independent pass on
    the same head, making no takeover claim of its own."""
    data = {
        "gate": "glm_gate",
        "pr_id": PR_ID,
        "status": "pass",
        "reason": "verdict",
        "blocking_findings": [],
        "contract_hash": "991f53dcca1f4c3f",
        "report_path": str(report),
        "branch": BRANCH,
        "commit_sha": HEAD_SHA,
        "test_run": False,
    }
    data.update(overrides)
    return data


def _check(results_dir: Path, gate: str = "codex_gate") -> dict:
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, gate, results_dir, branch=BRANCH, head_sha=HEAD_SHA
    )


class TestSuccessorWithoutVerdictDoesNotDisplaceSigner:
    def test_live_1818_shape_is_go_via_independent_signer(self, tmp_path):
        """The exact live #1818 constellation: declared gate unavailable, a
        takeover successor that rendered no verdict, and a fully evidenced
        independent pass on the same head. The pass must sign."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "kimi_gate", _kimi_not_executable())
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["gate"] == "codex_gate"
        assert verdict["evidence_gate"] == "glm_gate"
        # Signed via the independent-signer branch, not via the takeover route:
        # kimi never claimed anything about the code.
        assert "zelfstandig" in verdict["message"]
        assert "bewijs onvolledig" not in verdict["message"]

    def test_unavailable_successor_also_does_not_displace(self, tmp_path):
        """``not_executable`` is not a hand-picked exception: an ``unavailable``
        successor carries no verdict either and must step aside the same way."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(
            results_dir,
            "kimi_gate",
            _kimi_not_executable(status="unavailable", reason="exit_nonzero"),
        )
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["evidence_gate"] == "glm_gate"

    def test_no_signer_reports_absence_not_incomplete_evidence(self, tmp_path):
        """With the verdictless successor skipped and no independent signer
        either, the refusal must be the honest OI-1624 absence message — never
        'resultaat mist contract_hash', which describes a botched attempt."""
        results_dir = tmp_path / "results"
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(results_dir, "kimi_gate", _kimi_not_executable())

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO"
        assert "nul geldige ondertekenaars" in verdict["message"]
        assert "mist contract_hash" not in verdict["message"]


class TestRealRejectionStillBlocks:
    def test_fully_evidenced_failing_successor_still_blocks(self, tmp_path):
        """A successor that DID render a verdict and rejected the head keeps
        blocking, even next to a fully evidenced independent pass. The repair
        may not remove a real rejection."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        kimi_report = tmp_path / "kimi-gate-pr1818.md"
        kimi_report.write_text(
            "# kimi gate report\n\nkimi gate: fail (1 blocking finding)\n",
            encoding="utf-8",
        )
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(
            results_dir,
            "kimi_gate",
            _kimi_not_executable(
                status="fail",
                reason="verdict",
                contract_hash="991f53dcca1f4c3f",
                report_path=str(kimi_report),
                blocking_findings=[{"title": "unsafe path join"}],
            ),
        )
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "overname-route voor codex_gate" in verdict["message"]
        assert "kimi_gate" in verdict["message"]

    def test_decided_pass_successor_still_takes_over(self, tmp_path):
        """The OI-1576 route itself is untouched: a successor that rendered a
        fully evidenced pass still signs for the declared gate, and does so
        BEFORE the independent-signer branch is consulted."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(
            results_dir,
            "kimi_gate",
            _kimi_not_executable(
                status="pass",
                reason="verdict",
                contract_hash="991f53dcca1f4c3f",
                report_path=str(report),
            ),
        )
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["evidence_gate"] == "kimi_gate"
        assert "bewezen via overname" in verdict["message"]

    def test_decided_pass_without_evidence_still_blocks(self, tmp_path):
        """A successor claiming ``pass`` but carrying no contract_hash/report_path
        DID render a verdict — it is judged by the unchanged invariant chain and
        refused, rather than silently stepping aside."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        _write_result(results_dir, "codex_gate", _declared_unavailable())
        _write_result(
            results_dir, "kimi_gate", _kimi_not_executable(status="pass", reason="verdict")
        )
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "mist contract_hash" in verdict["message"]


class TestDeclaredGateWithOwnVerdictUnchanged:
    def test_declared_pass_stands_on_its_own(self, tmp_path):
        """A declared gate carrying its own decided verdict never reaches the
        takeover loop — not even when a failing successor claims it."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        fail_report = tmp_path / "kimi-fail.md"
        fail_report.write_text("# kimi\n\nkimi gate: fail\n", encoding="utf-8")
        _write_result(
            results_dir,
            "codex_gate",
            _declared_unavailable(
                status="pass",
                reason="verdict",
                contract_hash="991f53dcca1f4c3f",
                report_path=str(report),
            ),
        )
        _write_result(
            results_dir,
            "kimi_gate",
            _kimi_not_executable(
                status="fail",
                reason="verdict",
                contract_hash="991f53dcca1f4c3f",
                report_path=str(fail_report),
                blocking_findings=[{"title": "still blocking"}],
            ),
        )

        verdict = _check(results_dir)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert "evidence_gate" not in verdict
        assert verdict["message"].startswith("codex_gate resultaat aanwezig en passing")

    def test_declared_fail_stands_on_its_own(self, tmp_path):
        """Symmetric: a declared gate that rejected the head is never rescued by
        a verdictless successor or by an independent pass."""
        results_dir = tmp_path / "results"
        report = _report_file(tmp_path)
        fail_report = tmp_path / "codex-fail.md"
        fail_report.write_text("# codex\n\ncodex gate: fail\n", encoding="utf-8")
        _write_result(
            results_dir,
            "codex_gate",
            _declared_unavailable(
                status="fail",
                reason="verdict",
                contract_hash="991f53dcca1f4c3f",
                report_path=str(fail_report),
                blocking_findings=[{"title": "real rejection"}],
            ),
        )
        _write_result(results_dir, "kimi_gate", _kimi_not_executable())
        _write_result(results_dir, "glm_gate", _glm_pass(report))

        verdict = _check(results_dir)

        assert verdict["verdict"] == "NO-GO", verdict["message"]
        assert "codex_gate" in verdict["message"]
