"""OI-1984: a deepseek takeover that took over from kimi signs at the merge door.

The review takeover chain is codex > kimi > deepseek > glm (operator decision
2026-10-04). Before this change deepseek was outside the ``Gate`` enum, so the
closure verifier dropped its takeover record (not in ``_REVIEW_PEER_GATES``) and
the kimi seat stayed unsigned even with a clean deepseek pass on the head.

Every test here drives the public door (``check_review_gate_for_merge``) or the
``vnx-gate/review`` summary (``forge_gate_publisher.review_verdict``) on plain
result files, so the same tests run unmodified on the pre-change code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import closure_verifier  # noqa: E402
import config_registry  # noqa: E402
import forge_gate_publisher as fgp  # noqa: E402
import gate_request_handler  # noqa: E402

PR_NUMBER = 4984
PR_ID = str(PR_NUMBER)
BRANCH = "dispatch/20261004-keten-codex-kimi-deepseek"
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
NEW_CHAIN = "codex_gate,kimi_gate,deepseek_gate,glm_gate"
CLEAN_REPORT = "# Gate report\n\nAll findings reviewed, nothing blocking.\n"


def _write(results_dir: Path, gate: str, record: Dict[str, Any]) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{PR_ID}-{gate}.json"
    path.write_text(json.dumps(record), encoding="utf-8")


def _report(tmp_path: Path) -> str:
    report = tmp_path / "report.md"
    report.write_text(CLEAN_REPORT, encoding="utf-8")
    return str(report)


def _pass_record(gate: str, report: str, **overrides: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "gate": gate,
        "pr_id": PR_ID,
        "pr_number": PR_NUMBER,
        "status": "pass",
        "blocking_count": 0,
        "blocking_findings": [],
        "advisory_findings": [],
        "contract_hash": "15270d8bdfc53bcf",
        "report_path": report,
        "branch": BRANCH,
        "commit_sha": HEAD,
    }
    record.update(overrides)
    return record


def _unavailable(gate: str) -> Dict[str, Any]:
    return {
        "gate": gate,
        "pr_id": PR_ID,
        "status": "unavailable",
        "contract_hash": "",
        "report_path": "",
        "branch": BRANCH,
        "commit_sha": HEAD,
    }


def _deepseek_takeover(report: str, **overrides: Any) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "takeover": True,
        "takeover_from": "kimi_gate",
        "takeover_path": [
            {"gate": "codex_gate", "status": "unavailable"},
            {"gate": "kimi_gate", "status": "unavailable"},
        ],
    }
    fields.update(overrides)
    return _pass_record("deepseek_gate", report, **fields)


def _door(results_dir: Path, gate: str = "kimi_gate") -> Dict[str, Any]:
    return closure_verifier.check_review_gate_for_merge(
        PR_ID, gate, results_dir, branch=BRANCH, head_sha=HEAD
    )


@pytest.fixture()
def seats(tmp_path: Path) -> Path:
    """codex and kimi both unavailable on the head: deepseek is the only reviewer left.

    A codex PASS would sign the kimi seat as a peer by itself, which would hide
    whether the deepseek record counts, so the seat under test is isolated.
    """
    results_dir = tmp_path / "results"
    _write(results_dir, "codex_gate", _unavailable("codex_gate"))
    _write(results_dir, "kimi_gate", _unavailable("kimi_gate"))
    return results_dir


class TestDeepseekTakeoverSignsTheKimiSeat:
    def test_without_deepseek_the_seat_is_open(self, seats):
        assert _door(seats)["verdict"] == "NO-GO"

    def test_deepseek_pass_that_took_over_from_kimi_signs(self, tmp_path, seats):
        _write(seats, "deepseek_gate", _deepseek_takeover(_report(tmp_path)))

        verdict = _door(seats)

        assert verdict["verdict"] == "GO", verdict["message"]
        assert verdict["evidence_gate"] == "deepseek_gate"
        assert "overname" in verdict["message"]

    def test_deepseek_fail_does_not_sign(self, tmp_path, seats):
        _write(
            seats,
            "deepseek_gate",
            _deepseek_takeover(_report(tmp_path), status="fail", blocking_count=1),
        )

        assert _door(seats)["verdict"] == "NO-GO"

    def test_deepseek_pass_on_another_head_does_not_sign(self, tmp_path, seats):
        _write(
            seats,
            "deepseek_gate",
            _deepseek_takeover(_report(tmp_path), commit_sha=OTHER_HEAD),
        )

        assert _door(seats)["verdict"] == "NO-GO"

    def test_plain_deepseek_pass_is_a_peer_signer_like_glm(self, tmp_path, seats):
        record = _deepseek_takeover(_report(tmp_path))
        for key in ("takeover", "takeover_from", "takeover_path"):
            del record[key]
        _write(seats, "deepseek_gate", record)

        assert _door(seats)["verdict"] == "GO"


class TestReviewSummaryCountsDeepseek:
    def test_deepseek_takeover_is_a_peer_in_the_summary(self, tmp_path):
        results_dir = tmp_path / "results"
        report = _report(tmp_path)
        _write(
            results_dir,
            "deepseek_gate",
            _deepseek_takeover(
                report,
                takeover_from="codex_gate",
                takeover_path=[{"gate": "codex_gate", "status": "unavailable"}],
            ),
        )

        verdict = fgp.review_verdict(
            PR_NUMBER, HEAD, results_dir=results_dir, branch=BRANCH
        )

        assert verdict.conclusion == fgp.CONCLUSION_SUCCESS, verdict.reason
        assert "deepseek_gate" in verdict.reason


class TestChainDefault:
    def test_registry_default_is_the_new_chain(self, monkeypatch):
        monkeypatch.delenv("VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)
        monkeypatch.delenv("VNX_OVERRIDE_VNX_REVIEW_GATE_TAKEOVER_CHAIN", raising=False)

        entry = config_registry.CONFIG_REGISTRY["VNX_REVIEW_GATE_TAKEOVER_CHAIN"]

        assert entry.default == NEW_CHAIN

    def test_handler_literal_equals_registry_default(self):
        entry = config_registry.CONFIG_REGISTRY["VNX_REVIEW_GATE_TAKEOVER_CHAIN"]

        assert gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN == entry.default
        assert gate_request_handler._DEFAULT_REVIEW_GATE_TAKEOVER_CHAIN == NEW_CHAIN
