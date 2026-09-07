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
        "dispatch_id": "20260908-golfb-b2b-forge-integratie",
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


def test_publication_never_shells_out_from_the_recorder_on_import() -> None:
    """The recorder imports the publisher lazily: importing gate_recorder must
    not drag closure_verifier (and its import chain) into every gate run."""
    source = (VNX_ROOT / "scripts" / "lib" / "gate_recorder.py").read_text(encoding="utf-8")
    module_level = [
        line for line in source.splitlines()
        if line.startswith("import forge_") or line.startswith("from forge_")
    ]
    assert module_level == []
