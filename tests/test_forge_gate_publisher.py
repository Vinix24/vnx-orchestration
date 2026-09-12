#!/usr/bin/env python3
"""Tests for scripts/lib/forge_gate_publisher.py (Golf B, B2b).

Where ``test_forge_check_run_client.py`` covers the transport (B2a), this
covers the judgment: which gate-result record may become which GitHub
``conclusion``, which records are refused outright, and the fact that a
failing publication never touches the record on disk.

Mocking boundary, per dispatch: only the client's POST
(``forge_check_run.publish_check_run``, imported here) and ``gh pr view``
(``subprocess.run``) are mocked. The mapping itself is NEVER
mocked — every conclusion in this file is computed by the real
``conclusion_for``/``classify_record`` against a real record on disk, and the
proven-pass path runs the real merge-door invariant chain over a real report
file.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import forge_gate_publisher as fcr  # noqa: E402
import gate_recorder  # noqa: E402
import gate_status  # noqa: E402

HEAD = "a" * 40
OTHER_HEAD = "b" * 40

#: What ``gh pr view --json autoMergeRequest`` returns on a PR that will merge
#: itself the moment its last required check goes green.
ARMED_AUTO_MERGE = {"autoMergeRequest": {"enabledAt": "2026-09-08T00:00:00Z"}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_report(tmp_path: Path, name: str = "report.md") -> Path:
    """A gate report with no blocking indicators at all."""
    path = tmp_path / name
    path.write_text(
        "# glm_gate\n\nDe poort heeft de diff gelezen en niets gevonden dat merge tegenhoudt.\n",
        encoding="utf-8",
    )
    return path


def _proven_pass(tmp_path: Path, **overrides: Any) -> Dict[str, Any]:
    """A record that satisfies every condition for ``success``."""
    record: Dict[str, Any] = {
        "gate": "glm_gate",
        "pr_id": "",
        "pr_number": 1811,
        "status": "pass",
        "commit_sha": HEAD,
        "branch": "dispatch/x",
        "contract_hash": "sha256:deadbeef",
        "report_path": str(_clean_report(tmp_path)),
        "blocking_findings": [],
        "advisory_findings": [],
        "dispatch_id": "20260908-golfb-b2b-forge-integratie",
        "evidence_source": "live",
        "test_run": False,
        "summary": "glm_gate PASS",
    }
    record.update(overrides)
    return record


def _write_record(results_dir: Path, pr_number: int, gate: str, record: Dict[str, Any]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"pr-{pr_number}-{gate}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_returning(
    monkeypatch: pytest.MonkeyPatch,
    payload: Optional[Dict[str, Any]],
    *,
    returncode: int = 0,
) -> None:
    """Make every ``gh pr view --json ...`` call answer ``payload``."""

    def fake_run(argv: List[str], **_kwargs: Any) -> _FakeCompleted:
        assert argv[0] == "gh", f"only gh may be shelled out to here, got {argv!r}"
        body = "" if payload is None else json.dumps(payload)
        return _FakeCompleted(returncode, stdout=body)

    monkeypatch.setattr(fcr.subprocess, "run", fake_run)
    monkeypatch.setattr(fcr.shutil, "which", lambda _name: "/usr/bin/gh")


def _capture_publish(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Replace the client POST; return the list it appends each call to."""
    calls: List[Dict[str, Any]] = []

    def fake_publish(
        head_sha: str,
        name: str,
        conclusion: str,
        summary: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        calls.append(
            {
                "head_sha": head_sha,
                "name": name,
                "conclusion": conclusion,
                "summary": summary,
                **kwargs,
            }
        )
        return {"id": 1, "conclusion": conclusion}

    monkeypatch.setattr(fcr, "publish_check_run", fake_publish)
    return calls


@pytest.fixture(autouse=True)
def _summary_publication_captured(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Capture the recorder's SUMMARY publication instead of performing it.

    Since the OP-B3 fix-forward the recorder publishes TWO check-runs per
    result write: ``vnx-gate/<gate>`` and ``vnx-gate/review``. Most tests in
    this file replace only the first (``publish_for_record``), so without this
    the second would run for real — ``load_app_config`` against the shipped
    YAML (which carries a live ``app_id``), then the keychain, then a POST to
    api.github.com from a unit test.

    Autouse and function-scoped, so it shares this test's ``monkeypatch``
    instance: a test that wants the real summary path, or a different stub,
    sets its own and the later call wins — the same convention
    ``conftest.py``'s offline stubs document.

    Returns the list the stub appends to, so a test can request the fixture by
    name and assert on WHAT was published.
    """
    calls: List[Dict[str, Any]] = []

    def fake_summary(pr_number: int, head_sha: str, **kwargs: Any) -> Dict[str, Any]:
        calls.append({"pr_number": pr_number, "head_sha": head_sha, **kwargs})
        return {"conclusion": "action_required", "name": fcr.REVIEW_SUMMARY_CHECK_NAME}

    monkeypatch.setattr(fcr, "publish_review_summary", fake_summary)
    return calls


# ---------------------------------------------------------------------------
# The mapping — statuses
# ---------------------------------------------------------------------------


def test_proven_pass_on_this_head_is_success(tmp_path: Path) -> None:
    """The positive control: without it every negative below is vacuous."""
    assert fcr.conclusion_for(_proven_pass(tmp_path), HEAD) == "success"


def test_unavailable_is_action_required(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, status="unavailable")
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "action_required"
    assert "unavailable" in verdict.reason


def test_not_executable_is_action_required(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, status="not_executable")
    assert fcr.conclusion_for(record, HEAD) == "action_required"


def test_pass_without_contract_hash_is_failure(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, contract_hash="")
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "failure"
    assert "contract_hash" in verdict.reason


def test_pass_whose_report_is_gone_is_failure(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, report_path=str(tmp_path / "nope.md"))
    assert fcr.conclusion_for(record, HEAD) == "failure"


def test_fail_status_is_failure(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, status="fail")
    assert fcr.conclusion_for(record, HEAD) == "failure"


def test_blocked_status_is_failure(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, status="blocked")
    assert fcr.conclusion_for(record, HEAD) == "failure"


def test_pass_with_blocking_findings_is_failure(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, blocking_findings=[{"message": "kapot"}])
    assert fcr.conclusion_for(record, HEAD) == "failure"


def test_every_status_the_recorder_can_write_lands_in_one_of_three(tmp_path: Path) -> None:
    """Read the vocabulary from the recorder's own status module, never a list
    written out here — a list of my own would agree with itself forever."""
    vocabulary = gate_status.ALL_KNOWN_STATES
    assert vocabulary, "nul is eerst een meetfout: ALL_KNOWN_STATES is leeg"
    landed = {}
    for status in sorted(vocabulary):
        record = _proven_pass(tmp_path, status=status)
        landed[status] = fcr.conclusion_for(record, HEAD)
    assert set(landed.values()) <= {"success", "failure", "action_required"}
    # And the buckets are actually distinct: a mapping that answered
    # action_required for everything would satisfy the assertion above.
    assert set(landed.values()) == {"success", "failure", "action_required"}


def test_record_without_any_status_is_action_required(tmp_path: Path) -> None:
    """147 claude_github_optional records on disk carry no status at all
    (measured 2026-09-08). That is an absent verdict, not an unknown one."""
    record = _proven_pass(tmp_path)
    del record["status"]
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "action_required"
    assert "uitspraak" in verdict.reason


def test_invented_status_raises(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, status="approve_with_notes")
    with pytest.raises(fcr.ForgeStatusUnmapped) as excinfo:
        fcr.conclusion_for(record, HEAD)
    assert "approve_with_notes" in str(excinfo.value)


def test_unmapped_status_never_degrades_to_a_default(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, status="probably-fine")
    with pytest.raises(fcr.ForgeStatusUnmapped):
        fcr.conclusion_for(record, HEAD)


# ---------------------------------------------------------------------------
# The mapping — head binding
# ---------------------------------------------------------------------------


def test_record_for_another_sha_is_action_required_never_success(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, commit_sha=OTHER_HEAD)
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "action_required"
    assert "poort niet gedraaid op deze kop" in verdict.reason


def test_record_with_empty_sha_is_action_required(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, commit_sha="")
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "action_required"
    assert "poort niet gedraaid op deze kop" in verdict.reason


def test_absent_record_is_action_required() -> None:
    verdict = fcr.classify_record(None, HEAD)
    assert verdict.conclusion == "action_required"
    assert "poort niet gedraaid op deze kop" in verdict.reason


def test_empty_head_sha_raises_rather_than_leniently_matching(tmp_path: Path) -> None:
    """``gate_recorder.result_is_for_head`` returns True on an empty head — the
    right call for the merge door, which must not reclassify evidence on a gh
    outage. Publishing has no such head to be lenient about."""
    with pytest.raises(fcr.ForgeCheckRunError):
        fcr.conclusion_for(_proven_pass(tmp_path), "")


def test_test_run_record_is_never_success(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, test_run=True)
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "action_required"
    assert "test_run" in verdict.reason


def test_reprocessed_evidence_is_never_success(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, evidence_source="reprocessed")
    verdict = fcr.classify_record(record, HEAD)
    assert verdict.conclusion == "action_required"
    assert "reprocessed" in verdict.reason


def test_reanchored_evidence_is_never_success(tmp_path: Path) -> None:
    record = _proven_pass(tmp_path, evidence_source="reanchored")
    assert fcr.conclusion_for(record, HEAD) == "action_required"


def test_absent_evidence_source_still_passes(tmp_path: Path) -> None:
    """Measured 2026-09-08 over 1148 result records in ~/.vnx-data: only
    glm_gate and kimi_gate write ``evidence_source`` at all. Requiring the
    literal string would make ``success`` unreachable for codex_gate (460
    ``completed`` records), i.e. a check that can never go green."""
    record = _proven_pass(tmp_path)
    del record["evidence_source"]
    assert fcr.conclusion_for(record, HEAD) == "success"


# ---------------------------------------------------------------------------
# The publisher
# ---------------------------------------------------------------------------


@pytest.fixture
def _registered_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the operator has done runbook §1/§3.

    The shipped YAML carries ``app_id: null`` on purpose until then, and
    ``publish_for_record`` short-circuits on it before touching gh or the
    keychain — see ``test_publish_is_a_cheap_no_op_until_the_app_exists``.
    """
    monkeypatch.setattr(fcr, "load_app_config", lambda *_a, **_k: fcr.AppConfig("vnx-gate", 42))


def test_publish_is_a_cheap_no_op_until_the_app_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No App, no gh round-trip: the recorder calls this after every write."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    shelled: List[Any] = []
    monkeypatch.setattr(fcr.subprocess, "run", lambda *a, **k: shelled.append(a))
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(
        fcr,
        "load_app_config",
        lambda *_a, **_k: (_ for _ in ()).throw(fcr.ForgeAppConfigError("app_id ontbreekt")),
    )

    with pytest.raises(fcr.ForgeAppConfigError):
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert shelled == [], "geen gh-aanroep zolang de App niet bestaat"
    assert calls == []


def test_dry_run_needs_no_registered_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rehearsal an operator wants BEFORE registering the App."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    monkeypatch.setattr(
        fcr,
        "load_app_config",
        lambda *_a, **_k: (_ for _ in ()).throw(fcr.ForgeAppConfigError("app_id ontbreekt")),
    )

    payload = fcr.publish_for_record(
        1811, "glm_gate", HEAD, results_dir=results_dir, dry_run=True
    )
    assert payload["conclusion"] == "success"


def test_publish_calls_the_client_with_the_vnx_gate_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert len(calls) == 1
    assert calls[0]["name"] == "vnx-gate/glm_gate"
    assert calls[0]["conclusion"] == "success"
    assert calls[0]["head_sha"] == HEAD


def test_publish_refuses_success_for_a_record_that_is_not_a_proven_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """The publisher does not take the mapping's word for it: even if
    ``classify_record`` were changed to hand back ``success``, the publisher
    re-asks the proven-pass predicate before a green check leaves the machine."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, contract_hash=""))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(
        fcr, "classify_record", lambda *_a, **_k: fcr.ForgeVerdict("success", "gelogen")
    )

    with pytest.raises(fcr.ForgePublishRefused) as excinfo:
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert "bewezen pass" in str(excinfo.value)
    assert calls == []


def test_publish_refuses_a_pr_with_an_active_auto_merge_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": {"enabledAt": "2026-09-08T00:00:00Z"}})
    calls = _capture_publish(monkeypatch)

    with pytest.raises(fcr.ForgePublishRefused) as excinfo:
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert "auto-merge" in str(excinfo.value)
    assert calls == [], "geen enkele publicatie op een PR die zichzelf kan mergen"


def test_publish_refuses_when_the_auto_merge_lookup_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """Fail CLOSED: a gh outage must not be read as "no auto-merge armed"."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, None, returncode=1)
    calls = _capture_publish(monkeypatch)

    with pytest.raises(fcr.ForgePublishRefused):
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)
    assert calls == []


# ---------------------------------------------------------------------------
# The auto-merge refusal covers ``success`` and nothing else
# ---------------------------------------------------------------------------


def test_a_failure_is_published_even_when_auto_merge_is_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """Refusing the RED update is what would be dangerous here.

    The refusal exists so that ``publish --pr N`` cannot finish a merge after a
    gate went red. That argument is about ``success`` only. Withholding a
    ``failure`` on a PR with auto-merge queued leaves whatever check-run the
    head already carries — a stale ``success`` included — as the last word
    branch protection sees, and the merge proceeds on evidence that has since
    been contradicted. Publishing the red one is the thing that STOPS it.
    """
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, status="fail"))
    _gh_returning(monkeypatch, ARMED_AUTO_MERGE)
    calls = _capture_publish(monkeypatch)

    payload = fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert payload["conclusion"] == "failure"
    assert [c["conclusion"] for c in calls] == ["failure"], (
        "een rode uitkomst houdt een armed auto-merge tegen en moet dus juist wél landen"
    )


def test_action_required_is_published_even_when_auto_merge_is_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """Same rule for the blocking non-verdict: a record for another head."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, commit_sha=OTHER_HEAD))
    _gh_returning(monkeypatch, ARMED_AUTO_MERGE)
    calls = _capture_publish(monkeypatch)

    payload = fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert payload["conclusion"] == "action_required"
    assert [c["conclusion"] for c in calls] == ["action_required"]


def test_a_proven_pass_is_still_refused_when_auto_merge_is_armed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """The narrowing changes the SCOPE of the refusal, never its content: the
    one conclusion that could perform the merge is refused with the same reason
    and the same repair instruction as before."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, ARMED_AUTO_MERGE)
    calls = _capture_publish(monkeypatch)

    with pytest.raises(fcr.ForgePublishRefused) as excinfo:
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    message = str(excinfo.value)
    assert "auto-merge" in message
    assert "autoMergeRequest" in message
    assert "gh pr merge --disable-auto" in message
    assert calls == [], "een groene check zou hier de merge zelf uitvoeren"


def test_a_red_publication_over_an_armed_auto_merge_is_logged_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Publishing red onto a self-merging PR is a state an operator must see.

    Not because anything went wrong — it is the correct outcome — but because
    "auto-merge armed" is exactly the situation where a human wants to know a
    machine just decided something on that PR.
    """
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, status="fail"))
    _gh_returning(monkeypatch, ARMED_AUTO_MERGE)
    _capture_publish(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="forge_gate_publisher"):
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "auto-merge" in logged
    assert "failure" in logged
    assert "tegen" in logged, "de reden hoort erbij: rood houdt de auto-merge juist tegen"


def test_a_failed_auto_merge_lookup_never_blocks_a_red_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail CLOSED applies to ``success``; a red check has nothing to fail into.

    ``test_publish_refuses_when_the_auto_merge_lookup_itself_fails`` keeps the
    strict rule where it belongs. Here the answer cannot change the decision, so
    a gh outage must not be the reason a failing gate stays invisible on the PR
    — that would recreate the stale-green hole through the back door.
    """
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, status="fail"))
    _gh_returning(monkeypatch, None, returncode=1)
    calls = _capture_publish(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="forge_gate_publisher"):
        payload = fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert payload["conclusion"] == "failure"
    assert [c["conclusion"] for c in calls] == ["failure"]
    assert "auto-merge" in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "success"),
        ({"status": "fail"}, "failure"),
        ({"status": "unavailable"}, "action_required"),
    ],
)
def test_without_an_armed_auto_merge_every_conclusion_publishes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None,
    overrides: Dict[str, Any], expected: str,
) -> None:
    """The control arm: narrowing the refusal changed nothing on a normal PR."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, **overrides))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    payload = fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert payload["conclusion"] == expected
    assert [c["conclusion"] for c in calls] == [expected]


def test_publish_dry_run_shows_the_payload_and_posts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    payload = fcr.publish_for_record(
        1811, "glm_gate", HEAD, results_dir=results_dir, dry_run=True
    )

    assert calls == []
    assert payload["conclusion"] == "success"
    assert payload["name"] == "vnx-gate/glm_gate"
    assert payload["head_sha"] == HEAD
    assert payload["dry_run"] is True


def test_publish_of_an_absent_record_is_action_required_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True)
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert calls[0]["conclusion"] == "action_required"


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_cli_publish_on_a_pass_for_an_older_sha_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    _registered_app: None,
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, commit_sha=OTHER_HEAD))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(fcr, "_resolve_head_sha", lambda _pr: HEAD)
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: results_dir)

    rc = fcr.main(["publish", "--pr", "1811", "--gate", "glm_gate"])

    assert rc != 0
    out = capsys.readouterr().out
    assert "draai de poort opnieuw" in out
    assert calls[0]["conclusion"] == "action_required", (
        "de nieuwe kop wordt gemarkeerd, maar nooit met het oude oordeel"
    )


def test_cli_publish_never_posts_an_old_verdict_on_a_new_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path, commit_sha=OTHER_HEAD))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(fcr, "_resolve_head_sha", lambda _pr: HEAD)
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: results_dir)

    fcr.main(["publish", "--pr", "1811", "--gate", "glm_gate"])

    assert [c["conclusion"] for c in calls] == ["action_required"]
    assert all(c["conclusion"] != "success" for c in calls)


def test_cli_publish_on_a_fresh_pass_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(fcr, "_resolve_head_sha", lambda _pr: HEAD)
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: results_dir)

    rc = fcr.main(["publish", "--pr", "1811", "--gate", "glm_gate"])

    assert rc == 0
    assert calls[0]["conclusion"] == "success"


def test_cli_without_gate_publishes_every_slot_for_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _write_record(
        results_dir, 1811, "codex_gate", _proven_pass(tmp_path, gate="codex_gate", status="unavailable")
    )
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(fcr, "_resolve_head_sha", lambda _pr: HEAD)
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: results_dir)

    fcr.main(["publish", "--pr", "1811"])

    assert {c["name"] for c in calls} == {"vnx-gate/glm_gate", "vnx-gate/codex_gate"}


def test_cli_refuses_a_pr_without_a_resolvable_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(fcr, "_resolve_head_sha", lambda _pr: "")
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: tmp_path)
    calls = _capture_publish(monkeypatch)

    rc = fcr.main(["publish", "--pr", "1811", "--gate", "glm_gate"])

    assert rc != 0
    assert calls == []
    assert "kop" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The recorder hook — after the write, never fatal, never mutating
# ---------------------------------------------------------------------------


def _recorder_payload() -> Dict[str, Any]:
    return {
        "gate": "glm_gate",
        "pr_id": "",
        "pr_number": 1811,
        "status": "pass",
        "commit_sha": HEAD,
        "branch": "dispatch/x",
        "contract_hash": "sha256:deadbeef",
        "report_path": "/dev/null",
        "blocking_findings": [],
        # Gate-eigen dispatch-id (OI-1725): a harness-lane gate result must
        # never carry the builder's dispatch-id, or the identity guard refuses
        # the terminal write.
        "dispatch_id": "glm-gate-pr1811-1788800000",
        "recorded_at": "2026-09-08T00:00:00Z",
    }


def test_a_throwing_publisher_leaves_the_record_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result_path = tmp_path / "pr-1811-glm_gate.json"
    seen: Dict[str, bytes] = {}

    def exploding_publisher(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        seen["before"] = result_path.read_bytes()
        raise fcr.ForgeAPIError("GitHub gaf 500", status=500, body="boom")

    monkeypatch.setattr(fcr, "publish_for_record", exploding_publisher)

    payload, written = gate_recorder.write_result_guarded(
        result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
    )

    assert written is True
    assert "before" in seen, "de publicatie hangt NA de schrijf, niet ervoor"
    assert result_path.read_bytes() == seen["before"]
    assert payload["status"] == "pass"


def test_a_closed_keychain_logs_the_recovery_command_and_keeps_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    result_path = tmp_path / "pr-1811-glm_gate.json"

    def locked_keychain(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise fcr.ForgeKeychainError("de keychain staat op slot")

    monkeypatch.setattr(fcr, "publish_for_record", locked_keychain)

    with caplog.at_level(logging.WARNING, logger="gate_recorder"):
        payload, written = gate_recorder.write_result_guarded(
            result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
        )

    assert written is True
    assert json.loads(result_path.read_text())["status"] == "pass"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "forge_gate_publisher.py publish --pr 1811 --gate glm_gate" in logged
    assert "keychain" in logged


def test_terminal_result_publishes_after_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gate_depth

    result_path = tmp_path / "pr-1811-glm_gate.json"
    calls: List[Dict[str, Any]] = []

    def recording_publisher(pr_number: int, gate: str, head_sha: str, **kwargs: Any) -> Dict[str, Any]:
        calls.append(
            {
                "pr_number": pr_number,
                "gate": gate,
                "head_sha": head_sha,
                "on_disk": json.loads(result_path.read_text()),
                **kwargs,
            }
        )
        return {}

    monkeypatch.setattr(fcr, "publish_for_record", recording_publisher)

    gate_recorder.record_terminal_result(
        gate="glm_gate",
        pr_id="1811",
        result_path=result_path,
        payload=_recorder_payload(),
        # A real diff length: single_shot_depth(0, ...) is degenerate and
        # record_terminal_result would rewrite the pass to `unavailable`,
        # which would test the reclassification instead of the publication.
        execution_depth=gate_depth.single_shot_depth(4096, False),
    )

    assert len(calls) == 1
    assert calls[0]["pr_number"] == 1811
    assert calls[0]["gate"] == "glm_gate"
    assert calls[0]["head_sha"] == HEAD
    assert calls[0]["on_disk"]["status"] == "pass"


def test_a_record_without_a_head_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No head, nothing to hang a check-run on — and never an ambient fallback
    to the local HEAD (OI-1307)."""
    result_path = tmp_path / "pr-1811-glm_gate.json"
    calls: List[Any] = []
    monkeypatch.setattr(fcr, "publish_for_record", lambda *a, **k: calls.append(a))

    payload = _recorder_payload()
    payload["commit_sha"] = ""
    gate_recorder.write_result_guarded(
        result_path, payload, gate="glm_gate", pr_ref="1811"
    )

    assert calls == []


def test_a_refused_write_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused write left another writer's record standing; publishing would
    describe a write that never happened (OI-1469/OI-1470)."""
    result_path = tmp_path / "pr-1811-glm_gate.json"
    result_path.write_text("{ this is not json", encoding="utf-8")
    calls: List[Any] = []
    monkeypatch.setattr(fcr, "publish_for_record", lambda *a, **k: calls.append(a))

    _payload, written = gate_recorder.write_result_guarded(
        result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
    )

    assert written is False
    assert calls == []


# ---------------------------------------------------------------------------
# The recorder hook — the SUMMARY check, the one branch protection requires
# ---------------------------------------------------------------------------


def test_the_recorder_also_publishes_the_summary_check_for_the_same_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """OP-B3 made ``vnx-gate/review`` a REQUIRED check. Without this wiring the
    only producer of it is a hand-run CLI, so every merge to ``main`` would
    forever wait on an operator typing a command — while the check that IS
    published automatically (``vnx-gate/<gate>``) is required by nothing.
    """
    results_dir = tmp_path / "vnx-dev" / "review_gates" / "results"
    results_dir.mkdir(parents=True)
    result_path = results_dir / "pr-1811-glm_gate.json"
    monkeypatch.setattr(fcr, "publish_for_record", lambda *_a, **_k: {})

    _payload, written = gate_recorder.write_result_guarded(
        result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
    )

    assert written is True
    assert len(_summary_publication_captured) == 1
    call = _summary_publication_captured[0]
    assert call["pr_number"] == 1811
    assert call["head_sha"] == HEAD, (
        "de samenvatting hoort bij de kop die de recorder registreerde, nooit bij "
        "de huidige kop van de PR — een nieuwe push krijgt terecht geen check"
    )
    assert call["results_dir"] == results_dir, (
        "dezelfde opslag als het record dat zojuist geschreven is, zodat de "
        "samenvatting de records leest die de merge-deur straks toetst"
    )
    assert call["branch"] == "dispatch/x"


def test_the_terminal_writer_publishes_the_summary_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """The other writer (``record_terminal_result``), which free-form gates use.
    An asymmetric pair of writers would leave whichever lane used the other one
    unmergeable."""
    import gate_depth

    result_path = tmp_path / "pr-1811-glm_gate.json"
    monkeypatch.setattr(fcr, "publish_for_record", lambda *_a, **_k: {})

    gate_recorder.record_terminal_result(
        gate="glm_gate",
        pr_id="1811",
        result_path=result_path,
        payload=_recorder_payload(),
        execution_depth=gate_depth.single_shot_depth(4096, False),
    )

    assert [c["head_sha"] for c in _summary_publication_captured] == [HEAD]


def test_the_per_gate_publication_goes_first_and_the_summary_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """Order is the guarantee: the per-gate publication succeeds or fails on its
    own merits BEFORE the summary is attempted, so the summary can never be the
    thing that took it down."""
    order: List[str] = []
    monkeypatch.setattr(
        fcr, "publish_for_record", lambda *_a, **_k: order.append("per-gate") or {}
    )
    monkeypatch.setattr(
        fcr,
        "publish_review_summary",
        lambda *_a, **_k: order.append("summary") or {},
    )

    gate_recorder.write_result_guarded(
        tmp_path / "pr-1811-glm_gate.json",
        _recorder_payload(),
        gate="glm_gate",
        pr_ref="1811",
    )

    assert order == ["per-gate", "summary"]


def test_a_throwing_summary_leaves_the_per_gate_publication_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The second publication is a SECOND, separate failure handler.

    A 500 on the summary may not undo the per-gate check-run that already went
    out, may not touch the record on disk, and may not fail the gate run — and
    it must say so loudly, with the command that repairs it.
    """
    result_path = tmp_path / "pr-1811-glm_gate.json"
    per_gate: List[Any] = []
    monkeypatch.setattr(
        fcr, "publish_for_record", lambda *a, **_k: per_gate.append(a) or {}
    )

    def exploding_summary(*_a: Any, **_k: Any) -> Dict[str, Any]:
        raise fcr.ForgeAPIError("GitHub gaf 500", status=500, body="boom")

    monkeypatch.setattr(fcr, "publish_review_summary", exploding_summary)

    with caplog.at_level(logging.WARNING, logger="gate_recorder"):
        payload, written = gate_recorder.write_result_guarded(
            result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
        )

    assert written is True
    assert payload["status"] == "pass"
    assert json.loads(result_path.read_text())["status"] == "pass"
    assert len(per_gate) == 1, "de per-poort-publicatie is geslaagd en blijft geslaagd"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "forge_gate_publisher.py review --pr 1811" in logged
    assert "SAMENVATTENDE CHECK" in logged
    assert "ForgeAPIError" in logged


def test_a_throwing_per_gate_publication_still_lets_the_summary_go_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """The reverse direction, and the one that matters for mergeability: the
    per-gate check is required by nothing, the summary is required by branch
    protection. A gate-specific failure may not take the required check with
    it."""
    def exploding_per_gate(*_a: Any, **_k: Any) -> Dict[str, Any]:
        raise fcr.ForgeAPIError("GitHub gaf 500", status=500, body="boom")

    monkeypatch.setattr(fcr, "publish_for_record", exploding_per_gate)

    gate_recorder.write_result_guarded(
        tmp_path / "pr-1811-glm_gate.json",
        _recorder_payload(),
        gate="glm_gate",
        pr_ref="1811",
    )

    assert [c["head_sha"] for c in _summary_publication_captured] == [HEAD]


def test_two_gate_runs_on_the_same_head_publish_the_summary_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """Idempotence. Two review peers signing the same head is the normal case,
    and a check-run POST for a name+sha that already carries one is an update,
    not a conflict."""
    monkeypatch.setattr(fcr, "publish_for_record", lambda *_a, **_k: {})

    for gate in ("glm_gate", "kimi_gate"):
        payload = _recorder_payload()
        payload["gate"] = gate
        gate_recorder.write_result_guarded(
            tmp_path / f"pr-1811-{gate}.json", payload, gate=gate, pr_ref="1811"
        )

    assert [c["head_sha"] for c in _summary_publication_captured] == [HEAD, HEAD]


def test_a_record_without_a_head_publishes_no_summary_either(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """One identity check for both publications: no head, nothing to attach."""
    monkeypatch.setattr(fcr, "publish_for_record", lambda *_a, **_k: {})
    payload = _recorder_payload()
    payload["commit_sha"] = ""

    gate_recorder.write_result_guarded(
        tmp_path / "pr-1811-glm_gate.json", payload, gate="glm_gate", pr_ref="1811"
    )

    assert _summary_publication_captured == []


def test_a_refused_write_publishes_no_summary_either(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """A refused write left another writer's record standing (OI-1469/OI-1470);
    a summary computed over it would describe a write that never happened."""
    result_path = tmp_path / "pr-1811-glm_gate.json"
    result_path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(fcr, "publish_for_record", lambda *_a, **_k: {})

    _payload, written = gate_recorder.write_result_guarded(
        result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
    )

    assert written is False
    assert _summary_publication_captured == []


def test_an_unregistered_app_costs_one_info_line_and_no_second_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _summary_publication_captured: List[Dict[str, Any]],
) -> None:
    """No App registered is a pending operator step, not a malfunction — and
    the summary cannot publish without one either. Attempting it anyway would
    put a second copy of the same line in the log on every single gate write,
    which is how a reader learns to skip the line that has to be believed on
    the day the keychain really is locked."""
    def no_app(*_a: Any, **_k: Any) -> Dict[str, Any]:
        raise fcr.ForgeAppConfigError("app_id ontbreekt")

    monkeypatch.setattr(fcr, "publish_for_record", no_app)

    with caplog.at_level(logging.INFO, logger="gate_recorder"):
        gate_recorder.write_result_guarded(
            tmp_path / "pr-1811-glm_gate.json",
            _recorder_payload(),
            gate="glm_gate",
            pr_ref="1811",
        )

    assert _summary_publication_captured == []
    logged = [r.getMessage() for r in caplog.records if "nog niet geregistreerd" in r.getMessage()]
    assert len(logged) == 1


def test_publication_never_shells_out_from_the_recorder_on_import() -> None:
    """The recorder imports the publisher lazily: importing gate_recorder must
    not drag closure_verifier (and its import chain) into every gate run."""
    source = (VNX_ROOT / "scripts" / "lib" / "gate_recorder.py").read_text(encoding="utf-8")
    module_level = [
        line for line in source.splitlines()
        if line.startswith("import forge_") or line.startswith("from forge_")
    ]
    assert module_level == []


# ---------------------------------------------------------------------------
# The explicit path — publish the record that was written, or nothing
# ---------------------------------------------------------------------------


def test_the_recorder_publishes_the_record_it_just_wrote_not_the_store_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """The store holds a PASS for this PR and gate; the write that just landed
    is a FAIL, in a DIFFERENT store.

    Not a contrived split: every gate run in this project writes with
    ``VNX_DATA_DIR=~/.vnx-data/vnx-dev``, so "the record just written" and "the
    record the default store resolves to" are routinely two different files. A
    publisher that re-derives its own source can therefore publish a stale
    ``success`` over a verdict that had just failed — a green required check
    for a gate that said no.
    """
    default_store = tmp_path / "default_store"
    _write_record(default_store, 1811, "glm_gate", _proven_pass(tmp_path))
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: default_store)

    written_dir = tmp_path / "vnx-dev" / "review_gates" / "results"
    written_dir.mkdir(parents=True)
    result_path = written_dir / "pr-1811-glm_gate.json"

    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    payload = _recorder_payload()
    payload["status"] = "fail"
    payload["summary"] = "glm_gate FAIL"
    payload["blocking_findings"] = [{"message": "kapot"}]

    _on_disk, written = gate_recorder.write_result_guarded(
        result_path, payload, gate="glm_gate", pr_ref="1811"
    )

    assert written is True
    assert len(calls) == 1, (
        "precies een publicatie hoort bij precies een geslaagde schrijf "
        f"(kreeg {calls!r})"
    )
    assert calls[0]["conclusion"] == "failure", (
        "de publicatie beschrijft het record dat zojuist geschreven is, nooit het "
        "pass-record dat in de standaardopslag voor dezelfde PR en poort staat"
    )


def test_terminal_result_publishes_from_the_path_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """The same rule on the other writer (``record_terminal_result``), which
    resolves its own ``result_path`` and is the one free-form gates use."""
    import gate_depth

    default_store = tmp_path / "default_store"
    _write_record(default_store, 1811, "glm_gate", _proven_pass(tmp_path))
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: default_store)

    written_dir = tmp_path / "vnx-dev" / "review_gates" / "results"
    written_dir.mkdir(parents=True)
    result_path = written_dir / "pr-1811-glm_gate.json"

    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    payload = _recorder_payload()
    payload["status"] = "fail"
    payload["blocking_findings"] = [{"message": "kapot"}]

    gate_recorder.record_terminal_result(
        gate="glm_gate",
        pr_id="1811",
        result_path=result_path,
        payload=payload,
        execution_depth=gate_depth.single_shot_depth(4096, False),
    )

    assert [c["conclusion"] for c in calls] == ["failure"]


def test_an_explicit_record_path_that_is_absent_refuses_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """No record at the named path is a plumbing fault, not an absent verdict.

    The store's own copy is a proven pass here, so a fallback would not merely
    be sloppy — it would publish ``success``.
    """
    default_store = tmp_path / "default_store"
    _write_record(default_store, 1811, "glm_gate", _proven_pass(tmp_path))
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: default_store)
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    missing = tmp_path / "nergens" / "pr-1811-glm_gate.json"
    with pytest.raises(fcr.ForgePublishRefused) as excinfo:
        fcr.publish_for_record(1811, "glm_gate", HEAD, record_path=missing)

    assert str(missing) in str(excinfo.value)
    assert calls == [], "een ontbrekend pad publiceert niets, ook niet uit de opslag"


def test_an_explicit_record_path_that_is_corrupt_refuses_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    default_store = tmp_path / "default_store"
    _write_record(default_store, 1811, "glm_gate", _proven_pass(tmp_path))
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: default_store)
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    torn = tmp_path / "pr-1811-glm_gate.json"
    torn.write_text("{ dit is geen json", encoding="utf-8")

    with pytest.raises(fcr.ForgePublishRefused) as excinfo:
        fcr.publish_for_record(1811, "glm_gate", HEAD, record_path=torn)

    assert "onleesbaar" in str(excinfo.value)
    assert calls == []


def test_the_recorder_hands_the_publisher_the_path_it_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract itself: the hook names the file, the publisher never has to
    guess which one."""
    result_path = tmp_path / "vnx-dev" / "pr-1811-glm_gate.json"
    result_path.parent.mkdir(parents=True)
    seen: Dict[str, Any] = {}

    def recording(pr_number: int, gate: str, head_sha: str, **kwargs: Any) -> Dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(fcr, "publish_for_record", recording)

    gate_recorder.write_result_guarded(
        result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
    )

    assert seen.get("record_path") == result_path
    assert "results_dir" not in seen, "de haak wijst een bestand aan, geen opslag"


def test_a_missing_path_from_the_recorder_is_logged_and_never_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    _registered_app: None,
) -> None:
    """The refusal is loud in the log and still cannot fail the gate run."""
    result_path = tmp_path / "pr-1811-glm_gate.json"
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: tmp_path / "leeg")
    monkeypatch.setattr(
        fcr, "read_result_record_at",
        lambda _p: (_ for _ in ()).throw(fcr.ForgePublishRefused("bestaat niet")),
    )

    with caplog.at_level(logging.WARNING, logger="gate_recorder"):
        _payload, written = gate_recorder.write_result_guarded(
            result_path, _recorder_payload(), gate="glm_gate", pr_ref="1811"
        )

    assert written is True
    assert json.loads(result_path.read_text())["status"] == "pass"
    assert calls == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "PUBLICATIE MISLUKT" in logged


def test_the_cli_publishes_from_the_results_dir_flag_not_a_single_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """The CLI route is unchanged: ``--results-dir`` still names a STORE, and
    ``publish --pr N`` still resolves the record inside it."""
    flag_dir = tmp_path / "flag_store"
    _write_record(flag_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    empty_default = tmp_path / "default_store"
    empty_default.mkdir()
    monkeypatch.setattr(fcr, "_default_results_dir", lambda: empty_default)
    monkeypatch.setattr(fcr, "_resolve_head_sha", lambda _pr: HEAD)
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    rc = fcr.main(
        ["publish", "--pr", "1811", "--gate", "glm_gate", "--results-dir", str(flag_dir)]
    )

    assert rc == 0
    assert [c["conclusion"] for c in calls] == ["success"]


# ---------------------------------------------------------------------------
# The audit line (ADR-005)
# ---------------------------------------------------------------------------


def _forge_events() -> List[Dict[str, Any]]:
    """Every event in the publisher's own lane (conftest pins the data dir)."""
    from event_store import EventStore

    return list(EventStore().tail(fcr.FORGE_EVENT_LANE))


def test_a_publication_leaves_one_ndjson_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    _capture_publish(monkeypatch)

    fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    events = _forge_events()
    assert [e["type"] for e in events] == ["forge_check_run_published"]
    data = events[0]["data"]
    assert data["conclusion"] == "success"
    assert data["check_run_name"] == "vnx-gate/glm_gate"
    assert data["head_sha"] == HEAD
    assert data["pr_number"] == 1811


def test_a_refused_publication_leaves_an_ndjson_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """A publication that did NOT happen is exactly the one worth a trace."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": {"enabledAt": "2026-09-08T00:00:00Z"}})
    _capture_publish(monkeypatch)

    with pytest.raises(fcr.ForgePublishRefused):
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    events = _forge_events()
    assert [e["type"] for e in events] == ["forge_check_run_failed"]
    assert "auto-merge" in events[0]["data"]["detail"]


def test_a_dry_run_leaves_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """A rehearsal posts nothing, so there is nothing to record."""
    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    _capture_publish(monkeypatch)

    fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir, dry_run=True)

    assert _forge_events() == []


def test_an_event_store_failure_never_breaks_the_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None
) -> None:
    """The trace may never take down the thing it traces."""
    import event_store

    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    calls = _capture_publish(monkeypatch)

    def exploding_append(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("de schijf is vol")

    monkeypatch.setattr(event_store.EventStore, "append", exploding_append)

    payload = fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    assert payload["conclusion"] == "success"
    assert len(calls) == 1


def test_a_swallowed_audit_line_is_visible_at_warning_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _registered_app: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A check-run mutated on GitHub with no ledger line behind it is a gap.

    The line may not break the publication (the test above), but swallowing it
    at ``debug`` means the state mutation happened, the ADR-005 trail does not
    show it, and nothing anywhere says so. Warning level is what makes a
    missing trace an observable event instead of a silent one.
    """
    import event_store

    results_dir = tmp_path / "results"
    _write_record(results_dir, 1811, "glm_gate", _proven_pass(tmp_path))
    _gh_returning(monkeypatch, {"autoMergeRequest": None})
    _capture_publish(monkeypatch)

    def exploding_append(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("de schijf is vol")

    monkeypatch.setattr(event_store.EventStore, "append", exploding_append)

    with caplog.at_level(logging.WARNING, logger="forge_gate_publisher"):
        fcr.publish_for_record(1811, "glm_gate", HEAD, results_dir=results_dir)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "een ingeslikte auditregel hoort zichtbaar te zijn"
    logged = "\n".join(r.getMessage() for r in warnings)
    assert "de schijf is vol" in logged, "de reden hoort in de melding"
    assert "glm_gate" in logged and "1811" in logged
