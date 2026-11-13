#!/usr/bin/env python3
"""Deur D4: the door publishes ``vnx-gate/door`` as its sixth preflight, reads it back,
revokes it when the merge does not happen, and records all of it (ADR-061 §3-§5, contract (c)).

GitHub is a fake behind ``forge_check_run._api_request`` (the one network seam), so the
real D3 publisher runs. The keychain, ``gh``, the merge and the open-item store are
patched; receipts land in a tmp state dir. No live call, no real key, no real store.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import forge_check_run as fcr  # noqa: E402
import forge_gate_publisher as fgp  # noqa: E402
import pr_merge  # noqa: E402
from forge_protection_drift import YamlFetchResult  # noqa: E402
from merge_target import MergeTarget  # noqa: E402

SHA, PR, APP_ID = "d" * 40, 7, 4869217
BEFORE_DOOR = ["ci", "review", "adr", "contract_invalid", "branch_protection"]
DOOR_CHECK_KEYS = {
    "schema", "state", "check_name", "head_sha", "app_id", "check_run_id", "permit_id",
    "published_at", "read_back", "revoked", "revoke_outcome", "merge_outcome",
    "door_version", "integrity_sha256",
}
_REVIEW_ENTRY = '    - {context: "vnx-gate/review", app_id: 4869217}\n'
_DOOR_ENTRY = '    - {context: "vnx-gate/door", app_id: 4869217}\n'
PLAIN_YAML = (VNX_ROOT / "scripts/forge/branch_protection.yaml").read_text(encoding="utf-8").replace(_DOOR_ENTRY, "")
DOOR_YAML = PLAIN_YAML.replace(_REVIEW_ENTRY, _REVIEW_ENTRY + _DOOR_ENTRY)
GO = {"verdict": "GO", "message": "ok", "overridden": False, "override_reason": None}
PR_DATA = {"number": PR, "title": "feat: deur", "state": "OPEN", "headRefName": "feature/door",
           "baseRefName": "main", "headRefOid": SHA}


class FakeGitHub:
    """The Checks API over a list of runs; every call recorded. Only GET, POST and PATCH exist."""

    def __init__(self, state_dir: Path) -> None:
        self.runs: List[Dict[str, Any]] = []
        self.hidden: set = set()
        self.calls: List[tuple] = []
        self.patch_status = 200
        self.items: List[Dict[str, Any]] = []
        self.yaml, self.main_reads = DOOR_YAML, 0
        self.state: Any = {"state": "OPEN", "autoMergeRequest": None}
        self.merge: Any = lambda: (True, "")
        self.ledger = state_dir / "t0_receipts.ndjson"

    def __call__(self, method, url, *, headers, payload=None, timeout=30):
        body = json.loads(payload) if payload else None
        self.calls.append((method, url, body))
        if method == "GET":
            runs = [r for r in self.runs if r["id"] not in self.hidden]
            return 200, json.dumps({"total_count": len(runs), "check_runs": runs})
        if method == "POST":
            run = {"id": 900 + len(self.runs), "name": body["name"], "app": {"id": APP_ID},
                   "conclusion": body["conclusion"]}
            self.runs.append(run)
            return 201, json.dumps(run)
        if method == "PATCH":
            run = next((r for r in self.runs if r["id"] == int(url.rsplit("/", 1)[1])), None)
            if run is None or self.patch_status >= 400:
                return (404 if run is None else self.patch_status), "{}"
            run["conclusion"] = body["conclusion"]
            return 200, json.dumps(run)
        raise AssertionError(f"de deur stuurde {method} {url}: alleen GET, POST en PATCH bestaan")

    def of(self, method: str) -> List[tuple]:
        return [call for call in self.calls if call[0] == method]

    def receipts(self, event: str) -> List[Dict[str, Any]]:
        lines = self.ledger.read_text(encoding="utf-8").splitlines() if self.ledger.exists() else []
        return [r for r in map(json.loads, filter(str.strip, lines)) if r.get("event_type") == event]


def _gh_pr_state(gh: FakeGitHub, args: List[str]) -> subprocess.CompletedProcess:
    assert args[:2] == ["pr", "view"] and args[-1] == "state,autoMergeRequest", args
    if gh.state is None:
        return subprocess.CompletedProcess(args, 1, "", "gh: geen verbinding")
    return subprocess.CompletedProcess(args, 0, json.dumps(gh.state), "")


@pytest.fixture
def door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    data = tmp_path / "data"
    for sub in ("state", "dispatches"):
        (data / sub).mkdir(parents=True)
    env = {"PROJECT_ROOT": tmp_path, "VNX_DATA_DIR": data, "VNX_STATE_DIR": data / "state",
           "VNX_HOME": VNX_ROOT, "VNX_DISPATCH_DIR": data / "dispatches", "VNX_LOGS_DIR": data / "logs",
           "VNX_PIDS_DIR": data / "pids", "VNX_LOCKS_DIR": data / "locks", "VNX_DB_DIR": data / "database",
           "VNX_REPORTS_DIR": data / "unified_reports", fcr.TEST_POST_OPT_IN_ENV: "1"}
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    target = tmp_path / "target"
    target.mkdir()
    for cmd in (["init", "-q"], ["remote", "add", "origin", "https://github.com/acme/widgets.git"]):
        subprocess.run(["git", "-C", str(target), *cmd], check=True)
    gh = FakeGitHub(data / "state")

    def fetch_yaml(root, ref, path):
        gh.main_reads += ref == "main"
        return YamlFetchResult(text=gh.yaml, not_found=False, error=None)

    patches = {
        "resolve_merge_target": lambda root, **k: MergeTarget(project_root=target, repo="acme/widgets"),
        "_run_ci_gate": lambda pr, **k: (dict(GO), dict(PR_DATA)),
        "_run_review_gate": lambda pr, **k: (dict(GO), dict(PR_DATA)),
        "_run_adr_gate": lambda pr, **k: dict(GO),
        "_run_contract_invalid_gate": lambda *a, **k: dict(GO),
        "fetch_yaml_from_ref": fetch_yaml,
        "fetch_live_protection": lambda root, branch: pr_merge.protection_to_normalized_dict(
            pr_merge.parse_protection_config(gh.yaml)),
        "_do_merge": lambda n, m, h="": gh.merge(),
        "_branch_overlaps": lambda n, b: [],
        "_emit_register_event": lambda **k: True,
        "_gh": lambda args, **k: _gh_pr_state(gh, args),
    }
    for name, value in patches.items():
        monkeypatch.setattr(pr_merge, name, value)
    monkeypatch.setattr(fcr, "_api_request", gh)
    monkeypatch.setattr(fcr, "_installation_token_with_freshness", lambda **kw: ("tok", True))
    monkeypatch.setattr(fgp, "load_app_config", lambda: fcr.AppConfig(slug="vnx-gate", app_id=APP_ID))
    monkeypatch.setattr(fgp, "publish_check_run", fcr.publish_check_run)
    monkeypatch.setattr(fgp.time, "sleep", lambda s: None)
    monkeypatch.setitem(sys.modules, "open_items_manager", types.SimpleNamespace(
        add_item_programmatic=lambda **kw: gh.items.append(kw) or ("OI-1", True)))
    assert DOOR_YAML != PLAIN_YAML, "branch_protection.yaml draagt de vnx-gate/review-regel niet meer"
    return gh


def _run(*extra: str) -> int:
    return pr_merge.main(["--pr", str(PR), "--dispatch-id", "dlv-door-d4", *extra])


def _merged(gh: FakeGitHub) -> Dict[str, Any]:
    (receipt,) = gh.receipts("pr_merged")
    return receipt


def _refusal(gh: FakeGitHub) -> Dict[str, Any]:
    refusals = gh.receipts("pr_merge_refused")
    assert len(refusals) == 1, f"verwacht één pr_merge_refused, kreeg {len(refusals)}"
    return refusals[0]


# --- the permit on a merge --------------------------------------------------------------


def test_merge_through_the_door_carries_a_permit(door: FakeGitHub) -> None:
    assert _run() == pr_merge.EXIT_OK
    check = _merged(door).get("door_check") or {}
    assert check.get("state") == "permitted"
    assert check.get("permit_id") == f"{APP_ID}:{SHA}:900"
    assert (check["check_run_id"], check["app_id"], check["head_sha"]) == (900, APP_ID, SHA)
    assert check["read_back"] == {"conclusion": "success", "id": 900, "readback_attempts": 1} and check["published_at"]
    assert (check["revoked"], check["revoke_outcome"], check["merge_outcome"]) == (False, "not_needed", "merged")
    assert len(door.of("POST")) == 1 and not door.of("PATCH")


def test_door_is_the_sixth_and_last_preflight() -> None:
    assert pr_merge.PREFLIGHT_ORDER[-1] == "door"
    assert list(pr_merge.PREFLIGHT_ORDER[:-1]) == BEFORE_DOOR == fgp.door_evidence_gates()


def test_evidence_is_the_gates_before_the_door_from_the_receipts_own_list(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[Dict[str, Any]] = []
    real = fgp.publish_door_permit
    monkeypatch.setattr(fgp, "publish_door_permit", lambda ev, **kw: seen.append(ev) or real(ev, **kw))
    assert _run() == pr_merge.EXIT_OK
    (evidence,) = seen
    receipt = _merged(door)
    gates, check = receipt["preflight_gates"], receipt["door_check"]
    assert [g["gate"] for g in evidence["preflight_gates"]] == BEFORE_DOOR
    assert all(g["verdict"] == "GO" for g in evidence["preflight_gates"])
    assert evidence["preflight_gates"] == gates[:-1]
    assert (gates[-1]["gate"], gates[-1]["verdict"], gates[-1]["head_sha"]) == ("door", "GO", SHA)
    assert (evidence["source"], evidence["pr_number"], evidence["head_sha"]) == ("pr_merge._run_door", PR, SHA)
    assert evidence["integrity"] == {"paths": len(pr_merge._DOOR_INTEGRITY_PATHS), "sha256": check["integrity_sha256"]}
    assert evidence["door_version"] == check["door_version"] == (VNX_ROOT / "VERSION").read_text().strip()
    assert door.main_reads == 1, "de deur leest main's YAML niet opnieuw: zij hergebruikt de preflight"


def test_one_receipt_carries_door_check_preflight_gates_review_seats_and_identity(door: FakeGitHub) -> None:
    assert _run() == pr_merge.EXIT_OK
    receipt = _merged(door)
    assert {"door_check", "preflight_gates", "review_seats", "project_id", "delivery_id", "actor_class"} <= set(receipt)
    assert set(receipt["door_check"]) == DOOR_CHECK_KEYS and receipt["delivery_id"] == "dlv-door-d4"


def test_not_required_on_main_publishes_nothing(door: FakeGitHub) -> None:
    door.yaml = PLAIN_YAML
    assert _run() == pr_merge.EXIT_OK
    check = _merged(door)["door_check"]
    assert door.calls == [] and (check["state"], check["check_run_id"]) == ("not_required", None)
    assert set(check) == DOOR_CHECK_KEYS


def test_refusal_before_the_door_records_not_evaluated(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_merge, "_run_adr_gate", lambda pr, **k: dict(GO, verdict="NO-GO", message="ADR-botsing"))
    assert _run() == pr_merge.EXIT_ERROR
    check = _refusal(door)["door_check"]
    assert door.calls == [] and check["state"] == "not_evaluated" and set(check) == DOOR_CHECK_KEYS


def test_missing_app_key_is_a_no_go_never_not_required(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_key(**kw: Any) -> Any:
        raise fcr.ForgeKeychainError("vnx-gate-app-key ontbreekt in de keychain")

    monkeypatch.setattr(fcr, "_installation_token_with_freshness", no_key)
    assert _run() == pr_merge.EXIT_ERROR
    refusal = _refusal(door)
    assert refusal["refused_by"] == "door" and "keychain" in refusal["preflight_gates"][-1]["message"]
    assert refusal["door_check"]["state"] == "not_evaluated" and door.calls == [] and door.items == []


def test_dry_run_publishes_nothing(door: FakeGitHub, capsys: pytest.CaptureFixture) -> None:
    assert _run("--dry-run", "--json") == pr_merge.EXIT_OK
    out = capsys.readouterr().out
    check = json.loads(out[out.index("{"):])["door_check"]
    assert door.calls == [] and door.receipts("pr_merged") == []
    assert (check["state"], check["revoke_outcome"]) == ("not_evaluated", "skipped_dry_run")


# --- the read-back binds the permit -----------------------------------------------------


def _other_app(gh: FakeGitHub) -> None:
    gh.runs[-1]["app"] = {"id": 999}


def _other_run(gh: FakeGitHub) -> None:
    gh.runs.append(dict(gh.runs[-1], id=901))
    gh.hidden.add(900)


def _not_success(gh: FakeGitHub) -> None:
    gh.runs[-1]["conclusion"] = "neutral"


@pytest.mark.parametrize("mutate", [_other_app, _other_run, _not_success])
def test_read_back_that_does_not_bind_refuses_and_revokes(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, mutate: Any,
) -> None:
    real = fgp.publish_door_permit
    monkeypatch.setattr(fgp, "publish_door_permit", lambda ev, **kw: (real(ev, **kw), mutate(door))[0])
    merges: List[int] = []
    door.merge = lambda: merges.append(1) or (True, "")
    assert _run() == pr_merge.EXIT_ERROR and merges == []
    refusal = _refusal(door)
    assert refusal["refused_by"] == "door" and refusal["preflight_gates"][-1]["verdict"] == "NO-GO"
    assert door.of("PATCH") and {c[2]["conclusion"] for c in door.of("PATCH")} == {"failure"}
    assert (refusal["door_check"]["revoked"], refusal["door_check"]["revoke_outcome"]) == (True, "revoked")


# --- the three revocation triggers ------------------------------------------------------


@pytest.mark.parametrize("state, outcome", [
    ({"state": "OPEN", "autoMergeRequest": None}, "not_merged"),
    ({"state": "OPEN", "autoMergeRequest": {"enabledAt": "2026-11-08T10:00:00Z"}}, "unknown"),
    (None, "unknown"),
])
def test_merge_that_does_not_happen_revokes_and_records(door: FakeGitHub, state: Any, outcome: str) -> None:
    door.merge, door.state = (lambda: (False, "merge niet bevestigd")), state
    assert _run() == pr_merge.EXIT_ERROR
    refusals = door.receipts("pr_merge_refused")
    assert refusals, "een mislukte merge onder een toestemming laat een pr_merge_refused achter"
    check = refusals[0]["door_check"]
    assert (check["revoked"], check["revoke_outcome"], check["merge_outcome"]) == (True, "revoked", outcome)
    assert refusals[0]["refused_by"] == "merge" and check["state"] == "permitted"
    assert [(c[1].rsplit("/", 1)[1], c[2]["conclusion"]) for c in door.of("PATCH")] == [("900", "failure")]


def test_exception_after_publication_revokes_and_still_propagates(door: FakeGitHub) -> None:
    def boom() -> Any:
        raise RuntimeError("verbinding weg tijdens gh pr merge")

    door.merge = boom
    with pytest.raises(RuntimeError, match="verbinding weg"):
        _run()
    check = _refusal(door)["door_check"]
    assert [c[2]["conclusion"] for c in door.of("PATCH")] == ["failure"]
    assert (check["revoked"], check["merge_outcome"]) == (True, "unknown")


def test_revocation_that_fails_three_times_is_a_blocker(door: FakeGitHub) -> None:
    def merge_fails_and_patches_fail() -> Any:
        door.patch_status = 502
        return False, "kop verschoven"

    door.merge = merge_fails_and_patches_fail
    assert _run() == pr_merge.EXIT_ERROR
    assert len(door.of("PATCH")) == fgp.DOOR_REVOKE_ATTEMPTS
    check = _refusal(door)["door_check"]
    assert (check["revoked"], check["revoke_outcome"]) == (False, "revoke_failed")
    assert [item["severity"] for item in door.items] == ["blocker"]
    assert f"python3 scripts/pr_merge.py --pr {PR}" in door.items[0]["details"]


def test_second_door_run_re_permits_the_same_run_and_merges(door: FakeGitHub) -> None:
    door.merge = lambda: (False, "kop verschoven")
    assert _run() == pr_merge.EXIT_ERROR
    door.merge = lambda: (True, "")
    assert _run() == pr_merge.EXIT_OK
    assert len(door.of("POST")) == 1
    assert [c[2]["conclusion"] for c in door.of("PATCH")] == ["failure", "success"]
    assert _merged(door)["door_check"]["check_run_id"] == 900
    assert {call[0] for call in door.calls} <= {"GET", "POST", "PATCH"}, "geen enkel pad stuurt een DELETE"


# --- round 2: every step between a published permit and a confirmed merge is guarded --------


def _failed_merge(door: FakeGitHub) -> None:
    door.merge = lambda: (False, "kop verschoven")


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, RuntimeError])
def test_outcome_lookup_that_raises_after_a_failed_merge_revokes_once_and_propagates(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, exc_type: type,
) -> None:
    def lookup(pr: int) -> str:
        raise exc_type("uitkomst niet op te halen")

    _failed_merge(door)
    monkeypatch.setattr(pr_merge, "_merge_outcome_after_failure", lookup)
    with pytest.raises(exc_type):
        _run()
    assert [(c[1].rsplit("/", 1)[1], c[2]["conclusion"]) for c in door.of("PATCH")] == [("900", "failure")]
    check = _refusal(door)["door_check"]
    assert (check["revoked"], check["merge_outcome"]) == (True, "unknown")
    assert door.runs[0]["conclusion"] == "failure"


def test_exception_after_a_completed_revocation_does_not_patch_again(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def receipt_write_fails(**kwargs: Any) -> None:
        raise OSError("schijf vol")

    _failed_merge(door)
    monkeypatch.setattr(pr_merge, "_write_refusal_receipt", receipt_write_fails)
    with pytest.raises(OSError, match="schijf vol"):
        _run()
    assert len(door.of("PATCH")) == 1
    assert door.runs[0]["conclusion"] == "failure"


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, RuntimeError])
def test_exception_right_after_publication_before_the_merge_guard_revokes_once(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, exc_type: type,
) -> None:
    real = pr_merge._preflight_record

    def record(name: str, gate: Dict[str, Any], head_sha: str = "") -> Dict[str, Any]:
        if name == pr_merge.DOOR_GATE and gate.get("reason_code") == "permitted":
            raise exc_type("na het publiceren")
        return real(name, gate, head_sha)

    monkeypatch.setattr(pr_merge, "_preflight_record", record)
    with pytest.raises(exc_type):
        _run()
    assert [(c[1].rsplit("/", 1)[1], c[2]["conclusion"]) for c in door.of("PATCH")] == [("900", "failure")]
    assert door.runs[0]["conclusion"] == "failure"


# --- round 3: the guard ends at the confirmed merge -------------------------------------
#
# Every exit after a published permit, one test each. A confirmed merge is never followed
# by a revocation or a pr_merge_refused; every exit before it revokes once.


def _patch_runs(door: FakeGitHub) -> List[tuple]:
    return [(c[1].rsplit("/", 1)[1], c[2]["conclusion"]) for c in door.of("PATCH")]


def _raiser(exc_type: type, message: str) -> Any:
    def raise_it(*args: Any, **kwargs: Any) -> Any:
        raise exc_type(message)
    return raise_it


def _assert_permit_stands_after_merge(door: FakeGitHub) -> None:
    assert door.of("PATCH") == [], "na een bevestigde merge trekt de deur niets in"
    assert door.runs[0]["conclusion"] == "success"
    assert door.receipts("pr_merge_refused") == [], "een bevestigde merge krijgt geen pr_merge_refused"
    assert door.items == []


def test_codex_reproduction_interrupt_in_the_register_event_after_a_confirmed_merge_never_revokes(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(pr_merge, "_emit_register_event", _raiser(KeyboardInterrupt, "register onderbroken"))
    with pytest.raises(KeyboardInterrupt):
        _run()
    _assert_permit_stands_after_merge(door)
    assert _merged(door)["door_check"]["merge_outcome"] == "merged"
    assert "De toestemming blijft staan" in capsys.readouterr().err


@pytest.mark.parametrize("step, exc_type, receipt_landed", [
    ("_extract_pr_id", RuntimeError, False),
    ("_extract_pr_id", KeyboardInterrupt, False),
    ("_emit_receipt", KeyboardInterrupt, False),
    ("_emit_register_event", RuntimeError, True),
])
def test_bookkeeping_exception_that_escapes_after_a_confirmed_merge_propagates_and_never_revokes(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, step: str, exc_type: type, receipt_landed: bool,
) -> None:
    monkeypatch.setattr(pr_merge, step, _raiser(exc_type, f"{step} brak af"))
    with pytest.raises(exc_type):
        _run()
    _assert_permit_stands_after_merge(door)
    assert len(door.receipts("pr_merged")) == (1 if receipt_landed else 0)


def test_receipt_write_runtime_error_after_a_confirmed_merge_is_reported_and_never_revokes(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(pr_merge, "_emit_receipt", _raiser(RuntimeError, "t0_receipts.ndjson niet schrijfbaar"))
    assert _run() == pr_merge.EXIT_ERROR
    _assert_permit_stands_after_merge(door)
    assert door.receipts("pr_merged") == []
    err = capsys.readouterr().err
    assert "receipt could NOT be written" in err and "WAS MERGED" in err


def test_report_that_raises_after_a_confirmed_merge_never_revokes(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_merge, "_report_merge", _raiser(KeyboardInterrupt, "uitvoer onderbroken"))
    with pytest.raises(KeyboardInterrupt):
        _run()
    _assert_permit_stands_after_merge(door)
    assert len(door.receipts("pr_merged")) == 1


def test_report_that_raises_after_a_revoked_merge_does_not_revoke_again(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failed_merge(door)
    monkeypatch.setattr(pr_merge, "_report_merge", _raiser(KeyboardInterrupt, "uitvoer onderbroken"))
    with pytest.raises(KeyboardInterrupt):
        _run()
    assert _patch_runs(door) == [("900", "failure")]
    assert _refusal(door)["door_check"]["merge_outcome"] == "not_merged"
    assert door.receipts("pr_merged") == []


def test_merge_that_is_interrupted_before_confirmation_revokes_once_and_propagates(door: FakeGitHub) -> None:
    door.merge = _raiser(KeyboardInterrupt, "onderbroken tijdens gh pr merge")
    with pytest.raises(KeyboardInterrupt):
        _run()
    assert _patch_runs(door) == [("900", "failure")]
    check = _refusal(door)["door_check"]
    assert (check["revoked"], check["merge_outcome"]) == (True, "unknown")
    assert door.receipts("pr_merged") == []


def test_door_gate_print_that_raises_before_the_merge_revokes_once(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    merges: List[int] = []
    door.merge = lambda: merges.append(1) or (True, "")

    def print_fails_on_the_door_line(*args: Any, **kwargs: Any) -> None:
        if args and str(args[0]).startswith("Door gate:"):
            raise BrokenPipeError("stdout dicht")

    monkeypatch.setattr(pr_merge, "print", print_fails_on_the_door_line, raising=False)
    with pytest.raises(BrokenPipeError):
        _run()
    assert merges == [] and _patch_runs(door) == [("900", "failure")]
    assert _refusal(door)["door_check"]["merge_outcome"] == "unknown"


def test_revocation_that_raises_after_a_failed_merge_is_completed_once_by_the_guard(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real, calls = pr_merge._revoke_door_runs, []

    def first_attempt_raises(*args: Any) -> str:
        calls.append(args[3])
        if len(calls) == 1:
            raise RuntimeError("intrekking brak af")
        return real(*args)

    _failed_merge(door)
    monkeypatch.setattr(pr_merge, "_revoke_door_runs", first_attempt_raises)
    with pytest.raises(RuntimeError, match="intrekking brak af"):
        _run()
    assert len(calls) == 2 and _patch_runs(door) == [("900", "failure")]
    check = _refusal(door)["door_check"]
    assert (check["revoked"], check["merge_outcome"]) == (True, "unknown")


def test_revocation_that_raises_twice_propagates_the_first_and_writes_no_refusal(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    _failed_merge(door)
    monkeypatch.setattr(pr_merge, "_revoke_door_runs", _raiser(RuntimeError, "intrekking brak af"))
    with pytest.raises(RuntimeError, match="intrekking brak af"):
        _run()
    assert door.of("PATCH") == [] and door.receipts("pr_merge_refused") == []
    assert "intrekking tijdens een uitzondering faalde zelf" in capsys.readouterr().err


def test_confirmed_merge_with_clean_bookkeeping_leaves_one_pr_merged_and_no_refusal(door: FakeGitHub) -> None:
    assert _run() == pr_merge.EXIT_OK
    _assert_permit_stands_after_merge(door)
    assert _merged(door)["door_check"]["merge_outcome"] == "merged"


# --- the confirmation itself: merge_pr's on_merged, and the return value as the same evidence ---


def test_merge_pr_confirms_once_before_any_bookkeeping(door: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    order: List[str] = []
    monkeypatch.setattr(pr_merge, "_emit_receipt", lambda **k: order.append("receipt") or {"append_status": "appended"})
    monkeypatch.setattr(pr_merge, "_emit_register_event", lambda **k: order.append("register") or True)
    result = pr_merge.merge_pr(PR, dispatch_id="dlv-door-d4", pr_data=dict(PR_DATA), head_sha=SHA,
                               on_merged=lambda: order.append("confirmed"))
    assert result["success"] is True and order == ["confirmed", "receipt", "register"]


@pytest.mark.parametrize("dry_run, merge", [(False, (False, "kop verschoven")), (True, (True, ""))])
def test_merge_pr_never_confirms_a_merge_that_did_not_happen(
    door: FakeGitHub, dry_run: bool, merge: tuple,
) -> None:
    confirmed: List[int] = []
    door.merge = lambda: merge
    pr_merge.merge_pr(PR, dispatch_id="dlv-door-d4", pr_data=dict(PR_DATA), head_sha=SHA, dry_run=dry_run,
                      on_merged=lambda: confirmed.append(1))
    assert confirmed == []


_MISSING = object()


def _fake_merge_pr(success: Any) -> Any:
    def merge_pr(**kwargs: Any) -> Dict[str, Any]:
        result = {"pr_number": PR, "dispatch_id": "dlv-door-d4", "merge_method": "squash", "pr_title": "",
                  "branch": "", "receipt_status": "appended", "receipt_ok": True, "register_ok": True,
                  "error": "", "dry_run": False, "overlaps": []}
        if success is not _MISSING:
            result["success"] = success
        return result
    return merge_pr


def test_a_merge_that_reports_success_only_on_return_is_confirmed_and_never_revoked(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pr_merge, "merge_pr", _fake_merge_pr(True))
    assert _run() == pr_merge.EXIT_OK
    _assert_permit_stands_after_merge(door)


@pytest.mark.parametrize("success", [_MISSING, None, "true", 1, False], ids=["missing", "none", "str", "int", "false"])
def test_a_merge_without_a_true_success_and_without_the_callback_revokes_once(
    door: FakeGitHub, monkeypatch: pytest.MonkeyPatch, success: Any,
) -> None:
    monkeypatch.setattr(pr_merge, "merge_pr", _fake_merge_pr(success))
    assert _run() == pr_merge.EXIT_ERROR, "the CLI reads success by the same rule as the guard"
    assert _patch_runs(door) == [("900", "failure")]
    assert _refusal(door)["door_check"]["merge_outcome"] == "not_merged"
