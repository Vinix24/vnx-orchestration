#!/usr/bin/env python3
"""Deur D3: the door permit is one run per head, made from GO-evidence, read back, revoked by PATCH.

Every GitHub call and the keychain read is patched: ``_api_request`` is the one
network seam, the installation token is stubbed. No live call, no real key.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(VNX_ROOT / "scripts"))

import forge_check_run as fcr  # noqa: E402
import forge_gate_publisher as fgp  # noqa: E402

SHA = "a" * 40
APP_ID = 4869217
GATES = ["ci", "review", "adr", "contract_invalid", "branch_protection"]


class FakeGitHub:
    """Answers the Checks calls from a list of runs; records every call."""

    def __init__(self, runs: Optional[List[Dict[str, Any]]] = None) -> None:
        self.runs = runs if runs is not None else []
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
        self.patch_status = 200
        self.drop_post_answer = False
        self.next_id = 900
        self.root: Path = Path()
        self.ledger = ""
        self.by_id: Dict[int, Dict[str, Any]] = {}  # answers of GET /check-runs/<id>
        self.lagging_lists = 0  # this many list reads answer with no runs, as a lagging GitHub does

    def __call__(self, method, url, *, headers, payload=None, timeout=30):
        body = json.loads(payload) if payload else None
        self.calls.append((method, url, body))
        if method == "GET" and "/commits/" not in url:
            run = self.by_id.get(int(url.rsplit("/", 1)[1]))
            return (200, json.dumps(run)) if run is not None else (404, "{}")
        if method == "GET" and self.lagging_lists > 0:
            self.lagging_lists -= 1
            return 200, json.dumps({"total_count": 0, "check_runs": []})
        if method == "GET":
            runs = self.runs
            if "filter=all" not in url:  # GitHub's default is filter=latest: one run per name
                runs = runs[-1:]
            return 200, json.dumps({"total_count": len(runs), "check_runs": runs})
        if method == "POST":
            run = {"id": self.next_id, "name": body["name"], "app": {"id": APP_ID},
                   "conclusion": body["conclusion"]}
            self.runs.append(run)
            if self.drop_post_answer:
                raise fcr.ForgeAPIError("POST bereikte GitHub niet: timeout")
            return 201, json.dumps(run)
        if method == "PATCH":
            if self.patch_status >= 400:
                return self.patch_status, "{}"
            run_id = int(url.rsplit("/", 1)[1])
            run = next(r for r in self.runs if r["id"] == run_id)
            run["conclusion"] = body["conclusion"]
            run["output"] = body["output"]
            return 200, json.dumps(run)
        raise AssertionError(f"onverwachte methode {method}")

    def methods(self) -> List[str]:
        return [c[0] for c in self.calls]

    def lists(self) -> int:
        return sum(1 for c in self.calls if c[0] == "GET" and "/commits/" in c[1])

    def gets_by_id(self) -> int:
        return sum(1 for c in self.calls if c[0] == "GET" and "/commits/" not in c[1])


def _door(run_id: int, conclusion: str = "success", app: int = APP_ID) -> Dict[str, Any]:
    return {"id": run_id, "name": fgp.DOOR_CHECK_NAME, "app": {"id": app}, "conclusion": conclusion}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    for cmd in (["init", "-q"], ["remote", "add", "origin", "https://github.com/acme/widgets.git"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True)
    return root


@pytest.fixture
def ledger(tmp_path: Path) -> str:
    return str(tmp_path / "t0_receipts.ndjson")


def _receipts(ledger: str) -> List[Dict[str, Any]]:
    path = Path(ledger)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, repo: Path, ledger: str) -> FakeGitHub:
    fake = FakeGitHub()
    fake.root, fake.ledger = repo, ledger
    monkeypatch.setenv(fcr.TEST_POST_OPT_IN_ENV, "1")
    monkeypatch.setattr(fcr, "_api_request", fake)
    monkeypatch.setattr(fcr, "_installation_token_with_freshness", lambda **kw: ("tok", True))
    monkeypatch.setattr(fgp, "load_app_config", lambda: fcr.AppConfig(slug="vnx-gate", app_id=APP_ID))
    monkeypatch.setattr(fgp.time, "sleep", lambda s: None)
    # conftest stubs the imported name for every test; this file wants the real
    # primitive over the patched network seam.
    monkeypatch.setattr(fgp, "publish_check_run", fcr.publish_check_run)
    return fake


def _evidence(**over: Any) -> Dict[str, Any]:
    ev: Dict[str, Any] = {
        "schema": 1, "source": "pr_merge._run_door", "pr_number": 7, "head_sha": SHA,
        "preflight_gates": [{"gate": g, "verdict": "GO"} for g in GATES],
        "door_version": "x", "integrity": {"paths": 8, "sha256": "0"},
    }
    ev.update(over)
    return ev


def _refusal(ev: Any, **kw: Any) -> str:
    with pytest.raises(fgp.ForgeDoorRefused) as exc:
        fgp.validate_door_evidence(ev, sha=kw.get("sha", SHA), pr=kw.get("pr", 7), expected_gates=GATES)
    return str(exc.value)


# --- evidence ---------------------------------------------------------------


def test_valid_evidence_passes() -> None:
    fgp.validate_door_evidence(_evidence(), sha=SHA, pr=7, expected_gates=GATES)


def test_default_expected_gates_are_preflight_order_without_door() -> None:
    import pr_merge

    assert fgp.door_evidence_gates() == [n for n in pr_merge.PREFLIGHT_ORDER if n != "door"]


def test_each_evidence_refusal_has_its_own_text() -> None:
    gates = _evidence()["preflight_gates"]
    texts = {
        "schema": _refusal(_evidence(schema=2)),
        "schema_missing": _refusal({k: v for k, v in _evidence().items() if k != "schema"}),
        "source": _refusal(_evidence(source="x")),
        "head": _refusal(_evidence(head_sha="b" * 40)),
        "pr": _refusal(_evidence(pr_number=8)),
        "pr_bool": _refusal(_evidence(pr_number=True), pr=1),
        "missing": _refusal(_evidence(preflight_gates=gates[:-1])),
        "extra": _refusal(_evidence(preflight_gates=gates + [{"gate": "door", "verdict": "GO"}])),
        "order": _refusal(_evidence(preflight_gates=[gates[1], gates[0]] + gates[2:])),
        "not_list": _refusal(_evidence(preflight_gates="x")),
    }
    assert "schema" in texts["schema"] and "source" in texts["source"]
    assert "head_sha" in texts["head"] and "pr_number" in texts["pr"]
    assert "mist de poort" in texts["missing"]
    assert "extra of dubbele" in texts["extra"]
    assert "verkeerde volgorde" in texts["order"]
    assert "geen lijst" in texts["not_list"]
    assert len(set(texts.values())) == len(texts)


@pytest.mark.parametrize("verdict", [None, "NO-GO", "not_evaluated", "go", "GO ", True, 1])
def test_a_verdict_that_is_not_exactly_go_refuses(verdict: Any) -> None:
    gates = _evidence()["preflight_gates"]
    gates[2] = {"gate": "adr", "verdict": verdict}
    assert "geen GO" in _refusal(_evidence(preflight_gates=gates))


def test_a_record_without_a_verdict_key_refuses() -> None:
    gates = _evidence()["preflight_gates"]
    gates[0] = {"gate": "ci"}
    assert "geen GO" in _refusal(_evidence(preflight_gates=gates))


def test_evidence_that_is_not_an_object_refuses() -> None:
    assert "geen object" in _refusal([])


def test_read_evidence_missing_unparseable_and_not_object(tmp_path: Path) -> None:
    with pytest.raises(fgp.ForgeDoorRefused, match="evidence ontbreekt"):
        fgp.read_door_evidence(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(fgp.ForgeDoorRefused, match="niet te parsen"):
        fgp.read_door_evidence(bad)
    bad.write_text("[1]", encoding="utf-8")
    with pytest.raises(fgp.ForgeDoorRefused, match="geen object"):
        fgp.read_door_evidence(bad)


# --- reserved slugs ---------------------------------------------------------


@pytest.mark.parametrize("slug", ["review", "door"])
def test_reserved_slugs_refuse_through_the_gate_path(slug: str) -> None:
    with pytest.raises(fgp.ForgeCheckRunError):
        fgp.check_run_name(slug)
    assert fgp.check_run_name("codex_gate") == "vnx-gate/codex_gate"


# --- publish ----------------------------------------------------------------


def test_publish_creates_when_the_head_has_no_door_run(gh: FakeGitHub) -> None:
    run_id = _publish(gh)
    assert run_id == 900
    assert gh.methods() == ["GET", "POST", "GET"]  # list, create, read back
    assert gh.calls[1][2]["conclusion"] == "success" and gh.calls[1][2]["name"] == "vnx-gate/door"


def _publish(gh: FakeGitHub) -> int:
    return fgp.publish_door_permit(
        _evidence(), sha=SHA, pr=7, project_root=gh.root, receipts_file=gh.ledger
    )


def test_publish_re_permits_the_single_existing_run_with_patch(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, "failure")]
    assert _publish(gh) == 55
    assert gh.methods() == ["GET", "PATCH", "GET"]
    assert gh.calls[1][1].endswith("/check-runs/55")
    assert gh.calls[1][2]["conclusion"] == "success"
    assert gh.runs[0]["conclusion"] == "success"


def test_publish_refuses_two_door_runs(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, "failure"), _door(56)]
    with pytest.raises(fgp.ForgeDoorRefused, match="2 deurruns"):
        _publish(gh)
    assert gh.methods() == ["GET"]


def _appless(run_id: int, conclusion: str = "success") -> Dict[str, Any]:
    """A door run GitHub shows without an ``app`` object: the reader cannot call it the own App."""
    return {"id": run_id, "name": fgp.DOOR_CHECK_NAME, "conclusion": conclusion}


def _assert_case_4_refused(
    gh: FakeGitHub, tmp_path: Path, capsys: pytest.CaptureFixture, observed: List[Optional[int]]
) -> None:
    """ADR-061 §3 case 4: a refusal with its own text, no POST, no PATCH, a blocked receipt, exit 1."""
    with pytest.raises(fgp.ForgeDoorRefused, match="andere App") as exc:
        _publish(gh)
    assert f"waargenomen app.id {observed}" in str(exc.value)
    assert gh.methods() == ["GET"]  # the list only: no POST, no PATCH
    intent, outcome = _receipts(gh.ledger)
    assert (intent["phase"], intent["status"]) == ("intent", "info")
    assert (outcome["phase"], outcome["status"]) == ("outcome", "blocked")
    assert "geval 4" in outcome["reason"] and "check_run_id" not in outcome
    assert outcome["foreign_door_runs"] == [
        {"check_run_id": r["id"], "app_id": a} for r, a in zip(gh.runs, observed)
    ]
    rc, out, err = _cli(tmp_path, _evidence(), capsys, gh)
    assert rc == 1 and "check_run_id=" not in out and "geval 4" in err
    assert gh.methods() == ["GET", "GET"]  # the CLI also only listed
    assert len(gh.runs) == len(observed)  # nothing was created next to the foreign run


def test_publish_refuses_a_head_with_only_another_apps_door_run(
    gh: FakeGitHub, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Red run on 90f3f147: the foreign run was filtered away, a POST followed and 900 came back."""
    gh.runs = [_door(55, app=1)]
    _assert_case_4_refused(gh, tmp_path, capsys, [1])


def test_publish_refuses_a_head_with_only_a_door_run_without_an_app(
    gh: FakeGitHub, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Red run on 90f3f147: a run without an app object counted as no run at all."""
    gh.runs = [_appless(55)]
    _assert_case_4_refused(gh, tmp_path, capsys, [None])


def test_publish_validates_before_any_call(gh: FakeGitHub) -> None:
    with pytest.raises(fgp.ForgeDoorRefused):
        fgp.publish_door_permit(
            _evidence(source="x"), sha=SHA, pr=7, project_root=gh.root, receipts_file=gh.ledger
        )
    assert gh.calls == []


def test_a_paged_list_is_not_read_as_no_door_run(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fcr, "_api_request",
        lambda *a, **k: (200, json.dumps({"total_count": 3, "check_runs": [_door(1)]})),
    )
    with pytest.raises(fcr.ForgeAPIError, match="niet volledig"):
        _publish(gh)


# --- read back --------------------------------------------------------------


def test_read_back_accepts_the_own_success_run(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    assert fgp.read_back_door(SHA, 55, project_root=gh.root)["id"] == 55


def test_read_back_refuses_another_app_another_id_and_not_success(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, app=1)]
    with pytest.raises(fgp.ForgeDoorRefused, match="andere App"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)
    gh.runs = [_door(55)]
    with pytest.raises(fgp.ForgeDoorRefused, match="staat niet op"):
        fgp.read_back_door(SHA, 56, project_root=gh.root)
    gh.runs = [_door(55, "failure")]
    with pytest.raises(fgp.ForgeDoorRefused, match="conclusion 'failure'"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)
    gh.runs = [_door(55, None)]  # type: ignore[arg-type]
    with pytest.raises(fgp.ForgeDoorRefused, match="conclusion None"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)


def test_read_back_refuses_two_own_runs(gh: FakeGitHub) -> None:
    gh.runs = [_door(55), _door(56)]
    with pytest.raises(fgp.ForgeDoorRefused, match="2 deurruns"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)


def test_a_lost_post_answer_is_found_by_the_read_back(gh: FakeGitHub) -> None:
    gh.drop_post_answer = True
    with pytest.raises(fcr.ForgeAPIError):
        _publish(gh)
    # GitHub did create the run; the caller learns that from the read back, not from the POST.
    assert fgp.read_back_door(SHA, 900, project_root=gh.root)["conclusion"] == "success"


# --- revoke -----------------------------------------------------------------


def _revoke(gh: FakeGitHub, reason: str) -> str:
    return fgp.revoke_door(
        55, reason, sha=SHA, pr=7, project_root=gh.root, receipts_file=gh.ledger
    )


def test_revoke_patches_failure_with_the_title(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    assert _revoke(gh, "merge faalde") == "revoked"
    method, url, body = gh.calls[-1]
    assert method == "PATCH" and url.endswith("/check-runs/55")
    assert body["conclusion"] == "failure"
    assert body["output"]["title"] == "permit revoked: merge faalde"


def test_revoke_failed_after_three_failed_patches(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    gh.patch_status = 500
    assert _revoke(gh, "x") == "revoke_failed"
    assert gh.methods().count("PATCH") == 3
    assert gh.runs[0]["conclusion"] == "success"


def test_revoke_failed_when_the_answer_does_not_show_failure(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fgp, "patch_check_run", lambda *a, **k: {"id": 55, "conclusion": "success"})
    assert _revoke(gh, "x") == "revoke_failed"
    monkeypatch.setattr(fgp, "patch_check_run", lambda *a, **k: {"id": 99, "conclusion": "failure"})
    assert _revoke(gh, "x") == "revoke_failed"


def test_revoke_succeeds_on_the_third_attempt(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([{}, {}, {"id": 55, "conclusion": "failure"}])
    monkeypatch.setattr(fgp, "patch_check_run", lambda *a, **k: next(answers))
    assert _revoke(gh, "x") == "revoked"


def test_patch_rejects_a_bad_id(gh: FakeGitHub) -> None:
    with pytest.raises(fcr.ForgeCheckRunError):
        fcr.patch_check_run(True, "failure", "t", "s", name="n")  # type: ignore[arg-type]


def test_no_delete_anywhere() -> None:
    for module in (fcr, fgp):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert '"DELETE"' not in text and "'DELETE'" not in text


# --- CLI --------------------------------------------------------------------


def _cli(tmp_path: Path, ev: Optional[Dict[str, Any]], capsys: pytest.CaptureFixture, gh: FakeGitHub) -> Tuple[int, str, str]:
    path = tmp_path / "ev.json"
    if ev is not None:
        path.write_text(json.dumps(ev), encoding="utf-8")
    rc = fgp.main([
        "door", "--evidence", str(path), "--sha", SHA, "--pr", "7",
        "--project-root", str(gh.root), "--receipts-file", gh.ledger,
    ])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_missing_evidence_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    rc, _, err = _cli(tmp_path, None, capsys, gh)
    assert rc == 1 and "evidence ontbreekt" in err
    assert gh.calls == []


def test_cli_refuses_a_no_go_record(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    ev = _evidence()
    ev["preflight_gates"][0]["verdict"] = "NO-GO"
    rc, _, err = _cli(tmp_path, ev, capsys, gh)
    assert rc == 1 and "geen GO" in err and gh.calls == []


def test_cli_valid_evidence_prints_the_check_run_id(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    rc, out, _ = _cli(tmp_path, _evidence(), capsys, gh)
    assert rc == 0 and "check_run_id=900" in out
    assert gh.methods() == ["GET", "POST", "GET"]  # list, create, read back


def test_cli_read_back_refusal_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fgp, "read_back_door_counted", lambda sha, rid, **k: (_ for _ in ()).throw(fgp.ForgeDoorRefused("weg"))
    )
    rc, _, err = _cli(tmp_path, _evidence(), capsys, gh)
    assert rc == 1 and "weg" in err


# --- deur D3 ronde 2: filter=all, doelroot, grootboek ----------------------------


def test_the_list_asks_for_all_runs_and_sees_the_second_door_run(gh: FakeGitHub) -> None:
    """Red run on fa061a22: no filter=all, so the fake (like GitHub) showed one run and the head was accepted."""
    gh.runs = [_door(55, "failure"), _door(56)]
    with pytest.raises(fgp.ForgeDoorRefused, match="2 deurruns"):
        _publish(gh)
    assert "filter=all" in gh.calls[0][1]
    assert gh.methods() == ["GET"]


def test_the_list_follows_every_page(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {1: [_door(i) for i in range(1, 101)], 2: [_door(101)]}

    def fake(method, url, *, headers, payload=None, timeout=30):
        page = int(url.split("&page=")[1])
        return 200, json.dumps({"total_count": 101, "check_runs": pages.get(page, [])})

    monkeypatch.setattr(fcr, "_api_request", fake)
    runs = fcr.list_check_runs(SHA, fgp.DOOR_CHECK_NAME, project_root=gh.root)
    assert len(runs) == 101


def test_a_head_without_total_count_is_not_complete(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    for total in (None, True, "2", 5):
        monkeypatch.setattr(
            fcr, "_api_request",
            lambda *a, _t=total, **k: (200, json.dumps({"total_count": _t, "check_runs": [_door(1)]})),
        )
        with pytest.raises(fcr.ForgeAPIError, match="niet volledig"):
            fcr.list_check_runs(SHA, fgp.DOOR_CHECK_NAME, project_root=gh.root)


def test_publish_resolves_the_real_owner_repo_from_the_target_root(gh: FakeGitHub) -> None:
    """Red run on fa061a22: publish_door_permit had no project_root and raised ForgeCheckRunError before any request."""
    _publish(gh)
    assert all("/repos/acme/widgets/" in url for _, url, _ in gh.calls)


def test_a_root_without_a_github_remote_refuses_before_any_request(gh: FakeGitHub, tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "-C", str(bare), "init", "-q"], check=True)
    with pytest.raises(fcr.ForgeCheckRunError, match="owner/repo"):
        fgp.publish_door_permit(
            _evidence(), sha=SHA, pr=7, project_root=bare, receipts_file=gh.ledger
        )
    assert gh.calls == []


def test_every_transport_call_of_revoke_carries_the_target_root(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    assert _revoke(gh, "x") == "revoked"
    assert "/repos/acme/widgets/check-runs/55" in gh.calls[-1][1]


def test_cli_writes_intent_and_outcome_receipts(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    """Red run on fa061a22: the CLI wrote no receipt line."""
    rc, out, _ = _cli(tmp_path, _evidence(), capsys, gh)
    assert rc == 0
    lines = _receipts(gh.ledger)
    assert [(r["event_type"], r["phase"], r["status"]) for r in lines] == [
        ("door_permit_published", "intent", "info"),
        ("door_permit_published", "outcome", "success"),
    ]
    outcome = lines[1]
    assert outcome["check_run_id"] == 900 and outcome["conclusion"] == "success"
    assert outcome["action"] == "create" and outcome["head_sha"] == SHA and outcome["pr_number"] == 7
    assert outcome["source"] == "forge_gate_publisher" and outcome["receipt_kind"] == "state_mutation"
    assert outcome["evidence_gates"] == GATES and len(outcome["evidence_digest"]) == 64


def test_cli_refused_evidence_writes_a_blocked_receipt(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    ev = _evidence()
    ev["preflight_gates"][0]["verdict"] = "NO-GO"
    rc, _, _ = _cli(tmp_path, ev, capsys, gh)
    assert rc == 1 and gh.calls == []
    (receipt,) = _receipts(gh.ledger)
    assert receipt["status"] == "blocked" and "geen GO" in receipt["reason"]


def test_cli_missing_evidence_writes_a_blocked_receipt(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    rc, _, _ = _cli(tmp_path, None, capsys, gh)
    (receipt,) = _receipts(gh.ledger)
    assert rc == 1 and receipt["status"] == "blocked" and receipt["head_sha"] == SHA


def test_two_door_runs_write_intent_then_blocked(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, "failure"), _door(56)]
    with pytest.raises(fgp.ForgeDoorRefused):
        _publish(gh)
    assert [r["status"] for r in _receipts(gh.ledger)] == ["info", "blocked"]


def test_a_failing_receipt_write_leaves_github_untouched_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise OSError("schijf vol")

    monkeypatch.setattr(fgp, "emit_governance_receipt", boom)
    rc, _, err = _cli(tmp_path, _evidence(), capsys, gh)
    assert rc == 1 and "grootboek" in err
    assert gh.calls == []
    with pytest.raises(fgp.ForgeLedgerError):
        _publish(gh)
    with pytest.raises(fgp.ForgeLedgerError):
        _revoke(gh, "x")
    assert gh.calls == []


def test_a_failed_github_step_writes_a_failure_outcome(gh: FakeGitHub) -> None:
    gh.drop_post_answer = True
    with pytest.raises(fcr.ForgeAPIError):
        _publish(gh)
    assert [r["status"] for r in _receipts(gh.ledger)] == ["info", "failure"]


def test_revoke_writes_both_receipts_with_the_same_head_sha(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    assert _revoke(gh, "merge faalde") == "revoked"
    intent, outcome = _receipts(gh.ledger)
    assert intent["event_type"] == outcome["event_type"] == "door_permit_revoked"
    assert intent["head_sha"] == outcome["head_sha"] == SHA
    assert (intent["phase"], intent["status"]) == ("intent", "info")
    assert (outcome["phase"], outcome["status"], outcome["revoke_result"]) == ("outcome", "success", "revoked")
    assert outcome["check_run_id"] == 55 and outcome["reason"] == "merge faalde"


def test_a_failed_revoke_is_a_failure_outcome(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    gh.patch_status = 500
    assert _revoke(gh, "x") == "revoke_failed"
    assert _receipts(gh.ledger)[-1]["status"] == "failure"


def test_revoke_needs_a_head(gh: FakeGitHub) -> None:
    with pytest.raises(fcr.ForgeCheckRunError):
        fgp.revoke_door(55, "x", sha=" ", pr=7, project_root=gh.root, receipts_file=gh.ledger)
    assert _receipts(gh.ledger) == []


# --- r3: every permit transition has its own receipt identity ---------------

SHA_B = "b" * 40


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every receipt lands in the same second."""
    import governance_receipts

    monkeypatch.setattr(governance_receipts, "utc_now_iso", lambda: "2026-11-08T10:00:00Z")


def _publish_head(gh: FakeGitHub, sha: str) -> int:
    return fgp.publish_door_permit(
        _evidence(head_sha=sha), sha=sha, pr=7, project_root=gh.root, receipts_file=gh.ledger
    )


def test_two_heads_of_one_pr_in_one_second_keep_both_intent_lines(gh: FakeGitHub, frozen_clock: None) -> None:
    """Red run on 5cf353d3: one intent line, the second was dropped as a duplicate."""
    _publish_head(gh, SHA)
    _publish_head(gh, SHA_B)
    intents = [r for r in _receipts(gh.ledger) if r["phase"] == "intent"]
    assert [r["head_sha"] for r in intents] == [SHA, SHA_B]
    assert len({r["trigger"] for r in intents}) == 2


def test_create_and_re_permit_on_one_head_in_one_second_have_distinct_identity(
    gh: FakeGitHub, frozen_clock: None
) -> None:
    _publish_head(gh, SHA)  # create
    _publish_head(gh, SHA)  # re_permit
    outcomes = [r for r in _receipts(gh.ledger) if r["phase"] == "outcome"]
    assert [r["action"] for r in outcomes] == ["create", "re_permit"]
    assert len(outcomes) == 2 and outcomes[0]["trigger"] != outcomes[1]["trigger"]


def test_intent_and_outcome_of_one_operation_share_the_operation_id(gh: FakeGitHub, frozen_clock: None) -> None:
    _publish_head(gh, SHA)
    _publish_head(gh, SHA)
    first_intent, first_outcome, second_intent, second_outcome = _receipts(gh.ledger)
    assert first_intent["operation_id"] == first_outcome["operation_id"]
    assert second_intent["operation_id"] == second_outcome["operation_id"]
    assert first_intent["operation_id"] != second_intent["operation_id"]
    assert first_intent["trigger"] != first_outcome["trigger"]
    assert SHA in first_intent["trigger"] and "pr7" in first_intent["trigger"]


def test_revoke_intent_and_outcome_share_an_operation_id(gh: FakeGitHub, frozen_clock: None) -> None:
    gh.runs = [_door(55)]
    _revoke(gh, "a")
    _revoke(gh, "a")
    lines = _receipts(gh.ledger)
    assert len(lines) == 4
    assert lines[0]["operation_id"] == lines[1]["operation_id"] != lines[2]["operation_id"]


def test_a_duplicate_intent_receipt_stops_the_github_call(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """Red run on 5cf353d3: the GitHub call went out although the intent was a duplicate."""
    monkeypatch.setattr(
        fgp, "emit_governance_receipt", lambda *a, **k: {"append_status": "duplicate", "idempotency_key": "k"}
    )
    with pytest.raises(fgp.ForgeLedgerError):
        _publish(gh)
    assert gh.calls == []
    with pytest.raises(fgp.ForgeLedgerError):
        _revoke(gh, "x")
    assert gh.calls == []


@pytest.mark.parametrize("status", [None, "", "skipped", "weird"])
def test_an_unknown_or_missing_intent_status_also_refuses(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, status: Any) -> None:
    monkeypatch.setattr(fgp, "emit_governance_receipt", lambda *a, **k: {"append_status": status})
    with pytest.raises(fgp.ForgeLedgerError):
        _publish(gh)
    assert gh.calls == []


def test_cli_exits_nonzero_on_a_duplicate_intent(
    tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fgp, "emit_governance_receipt", lambda *a, **k: {"append_status": "duplicate"})
    rc, _, err = _cli(tmp_path, _evidence(), capsys, gh)
    assert rc == 1 and "grootboek" in err and gh.calls == []


def test_a_duplicate_outcome_is_logged_and_does_not_undo_the_call(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = fgp.emit_governance_receipt

    def second_is_duplicate(event: str, **kw: Any) -> Dict[str, Any]:
        if kw.get("phase") == "outcome":
            return {"append_status": "duplicate"}
        return real(event, **kw)

    monkeypatch.setattr(fgp, "emit_governance_receipt", second_is_duplicate)
    with caplog.at_level("ERROR"):
        assert _publish(gh) == 900
    assert "ledger: duplicate" in caplog.text
    assert gh.methods() == ["GET", "POST", "GET"]


# --- r4: ADR-061 §3 case by case, the foreign run is kept, not filtered away -----


def test_case_4_names_the_count_and_every_observed_app_id(gh: FakeGitHub) -> None:
    """Red run on 90f3f147: two foreign runs read as an empty head and a POST went out."""
    gh.runs = [_door(55, app=1), _appless(56, "failure"), _door(57, app=APP_ID + 1)]
    with pytest.raises(fgp.ForgeDoorRefused) as exc:
        _publish(gh)
    text = str(exc.value)
    assert text.startswith("3 deurrun(s) van een andere App")
    assert f"waargenomen app.id [1, None, {APP_ID + 1}]" in text and f"app_id {APP_ID}" in text
    assert gh.methods() == ["GET"]


@pytest.mark.parametrize("app", [None, "vnx-gate", [], {"slug": "vnx-gate"}, {"id": str(APP_ID)}, {"id": True}])
def test_case_4_holds_for_every_app_value_that_is_not_the_own_id(gh: FakeGitHub, app: Any) -> None:
    """Red run on 90f3f147: each of these was dropped before the decision and a POST followed."""
    gh.runs = [{"id": 55, "name": fgp.DOOR_CHECK_NAME, "app": app, "conclusion": "success"}]
    with pytest.raises(fgp.ForgeDoorRefused, match="geval 4"):
        _publish(gh)
    assert gh.methods() == ["GET"]


def test_the_split_is_the_readers_own_rule() -> None:
    """One rule in one place: the publisher imports the reader's split instead of copying it."""
    import ci_contexts

    assert fgp._split_by_app is ci_contexts._split_by_app


def test_case_1_no_run_at_all_posts_once_and_names_no_foreign_run(gh: FakeGitHub) -> None:
    assert _publish(gh) == 900
    assert gh.methods() == ["GET", "POST", "GET"]
    outcome = _receipts(gh.ledger)[-1]
    assert (outcome["status"], outcome["action"], outcome["foreign_door_runs"]) == ("success", "create", [])


def test_case_2_patches_the_one_own_run_and_posts_nothing(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, "failure")]
    assert _publish(gh) == 55
    assert [m for m in gh.methods() if m != "GET"] == ["PATCH"]
    assert gh.calls[1][1].endswith("/check-runs/55") and gh.calls[1][2]["conclusion"] == "success"
    outcome = _receipts(gh.ledger)[-1]
    assert (outcome["action"], outcome["check_run_id"], outcome["foreign_door_runs"]) == ("re_permit", 55, [])


def test_a_mixed_head_follows_the_own_run_and_names_the_foreign_one(gh: FakeGitHub) -> None:
    """Red run on 90f3f147: the PATCH went out, but the receipt did not name the foreign run."""
    gh.runs = [_door(60, "failure", app=1), _door(55, "failure"), _appless(61)]
    assert _publish(gh) == 55
    assert gh.methods() == ["GET", "PATCH", "GET"]
    assert gh.calls[1][1].endswith("/check-runs/55")
    assert [r["conclusion"] for r in gh.runs] == ["failure", "success", "success"]  # foreign runs untouched
    outcome = _receipts(gh.ledger)[-1]
    assert (outcome["status"], outcome["action"], outcome["check_run_id"]) == ("success", "re_permit", 55)
    assert outcome["foreign_door_runs"] == [
        {"check_run_id": 60, "app_id": 1}, {"check_run_id": 61, "app_id": None},
    ]


def test_a_mixed_head_with_two_own_runs_is_case_3_and_names_the_foreign_one(gh: FakeGitHub) -> None:
    """Red run on 90f3f147: refused, but the blocked receipt did not name the foreign run."""
    gh.runs = [_door(55, "failure"), _door(56), _door(60, app=1)]
    with pytest.raises(fgp.ForgeDoorRefused, match="2 deurruns van de eigen App"):
        _publish(gh)
    assert gh.methods() == ["GET"]
    outcome = _receipts(gh.ledger)[-1]
    assert outcome["status"] == "blocked"
    assert outcome["foreign_door_runs"] == [{"check_run_id": 60, "app_id": 1}]


def test_a_head_that_could_not_be_listed_carries_no_foreign_field(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unread head is not a head without foreign runs: the field is absent, never ``[]``."""
    monkeypatch.setattr(
        fcr, "_api_request",
        lambda *a, **k: (200, json.dumps({"total_count": 3, "check_runs": [_door(1)]})),
    )
    with pytest.raises(fcr.ForgeAPIError):
        _publish(gh)
    outcome = _receipts(gh.ledger)[-1]
    assert outcome["status"] == "failure" and "foreign_door_runs" not in outcome


def test_read_back_accepts_the_own_success_run_next_to_a_foreign_one(gh: FakeGitHub) -> None:
    """The reader reads only the own runs when one is present, so a foreign neighbour does not refuse."""
    gh.runs = [_door(60, "failure", app=1), _door(55), _appless(61, "failure")]
    assert fgp.read_back_door(SHA, 55, project_root=gh.root)["id"] == 55


def test_read_back_refuses_the_asked_id_when_it_is_a_foreign_run(gh: FakeGitHub) -> None:
    gh.runs = [_door(55), _door(60, app=1), _appless(61)]
    for foreign_id in (60, 61):
        with pytest.raises(fgp.ForgeDoorRefused, match=f"deurrun {foreign_id} is van een andere App"):
            fgp.read_back_door(SHA, foreign_id, project_root=gh.root)


@pytest.mark.parametrize("app", ["vnx-gate", [], 7])
def test_read_back_refuses_a_run_whose_app_is_not_an_object(gh: FakeGitHub, app: Any) -> None:
    """Red run on 90f3f147: ``(run.get("app") or {}).get`` raised AttributeError, not a named refusal."""
    gh.runs = [{"id": 55, "name": fgp.DOOR_CHECK_NAME, "app": app, "conclusion": "success"}]
    with pytest.raises(fgp.ForgeDoorRefused, match="andere App"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)


def test_read_back_refuses_two_own_runs_next_to_a_foreign_one(gh: FakeGitHub) -> None:
    gh.runs = [_door(55), _door(56), _door(60, app=1)]
    with pytest.raises(fgp.ForgeDoorRefused, match="teruglezing: 2 deurruns van de eigen App"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)


def test_a_foreign_run_appearing_after_the_create_is_named_and_does_not_undo_the_permit(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1 at the list, a mixed head at the read back: the own success run is still the permit.

    Red run on 90f3f147: the permit stood, but the receipt did not name the foreign run.
    """
    real = fgp.publish_check_run

    def post_then_foreign(*a: Any, **k: Any) -> Dict[str, Any]:
        created = real(*a, **k)
        gh.runs.append(_door(70, app=1))
        return created

    monkeypatch.setattr(fgp, "publish_check_run", post_then_foreign)
    assert _publish(gh) == 900
    assert gh.methods() == ["GET", "POST", "GET"]  # list, create, read back: no extra call
    outcome = _receipts(gh.ledger)[-1]
    assert (outcome["status"], outcome["action"]) == ("success", "create")
    assert outcome["foreign_door_runs"] == [{"check_run_id": 70, "app_id": 1}]


# --- read back under GitHub's read-after-write lag (OI-2787) ----------------------------


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> List[float]:
    slept: List[float] = []
    monkeypatch.setattr(fgp.time, "sleep", slept.append)
    return slept


def _by_id(run_id: int, sha: str = SHA, **over: Any) -> Dict[str, Any]:
    return {**_door(run_id), "head_sha": sha, **over}


def test_a_lagging_list_is_accepted_on_the_run_read_by_id(gh: FakeGitHub, sleeps: List[float]) -> None:
    gh.runs, gh.by_id = [], {55: _by_id(55)}
    run, attempts = fgp.read_back_door_counted(SHA, 55, project_root=gh.root)
    assert (run["id"], attempts) == (55, 1)
    assert sleeps == [] and gh.lists() == 1 and gh.gets_by_id() == 1


def test_a_failed_get_then_a_second_list_with_the_run_is_accepted(gh: FakeGitHub, sleeps: List[float]) -> None:
    gh.runs = [_door(55)]  # GET by id answers 404
    empty = fgp.DoorRuns(own=[], foreign=[], app_id=APP_ID)
    run, attempts = fgp.read_back_door_counted(SHA, 55, project_root=gh.root, listed=empty)
    assert (run["id"], attempts) == (55, 2)
    assert sleeps == [1.0] and gh.gets_by_id() == 1 and gh.lists() == 1


def test_a_list_that_stays_empty_refuses_after_three_attempts(gh: FakeGitHub, sleeps: List[float]) -> None:
    gh.runs = []
    with pytest.raises(fgp.ForgeDoorRefused, match=r"staat niet op .* \(na 3 pogingen\)"):
        fgp.read_back_door_counted(SHA, 55, project_root=gh.root)
    assert sleeps == [1.0, 2.0] and gh.lists() == 3 and gh.gets_by_id() == 3


def test_a_run_read_by_id_on_another_head_is_not_accepted(gh: FakeGitHub, sleeps: List[float]) -> None:
    gh.runs, gh.by_id = [], {55: _by_id(55, sha="b" * 40)}
    with pytest.raises(fgp.ForgeDoorRefused, match="na 3 pogingen"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)


@pytest.mark.parametrize("over", [
    {"app": {"id": 1}}, {"app": None}, {"name": "other"}, {"conclusion": "failure"},
    {"conclusion": None}, {"id": 56}, {"head_sha": None},
])
def test_a_run_read_by_id_that_misses_one_point_is_not_accepted(
    gh: FakeGitHub, sleeps: List[float], over: Dict[str, Any]
) -> None:
    gh.runs, gh.by_id = [], {55: _by_id(55, **over)}
    with pytest.raises(fgp.ForgeDoorRefused, match="na 3 pogingen"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)


def test_another_own_run_in_the_list_is_not_covered_by_the_get(gh: FakeGitHub, sleeps: List[float]) -> None:
    gh.runs, gh.by_id = [_door(56)], {55: _by_id(55)}
    with pytest.raises(fgp.ForgeDoorRefused, match="na 3 pogingen"):
        fgp.read_back_door(SHA, 55, project_root=gh.root)
    assert gh.gets_by_id() == 0


@pytest.mark.parametrize("runs, match", [
    ([_door(55), _door(56)], "2 deurruns"),
    ([_door(55, app=1)], "andere App"),
    ([_door(55, "failure")], "conclusion 'failure'"),
])
def test_the_other_refusals_are_immediate_without_get_or_sleep(
    gh: FakeGitHub, sleeps: List[float], runs: List[Dict[str, Any]], match: str
) -> None:
    gh.runs, gh.by_id = runs, {55: _by_id(55)}
    with pytest.raises(fgp.ForgeDoorRefused, match=match) as exc:
        fgp.read_back_door(SHA, 55, project_root=gh.root)
    assert "pogingen" not in str(exc.value)
    assert sleeps == [] and gh.gets_by_id() == 0 and gh.lists() == 1


def test_publish_survives_a_lagging_list_after_the_create(
    gh: FakeGitHub, sleeps: List[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.by_id = {900: _by_id(900)}
    real = fcr._api_request

    def lag_after_post(method, url, *, headers, payload=None, timeout=30):
        if method == "GET" and "/commits/" in url and "POST" in gh.methods():
            gh.calls.append((method, url, None))
            return 200, json.dumps({"total_count": 0, "check_runs": []})
        return real(method, url, headers=headers, payload=payload, timeout=timeout)

    monkeypatch.setattr(fcr, "_api_request", lag_after_post)
    run_id = fgp.publish_door_permit(
        _evidence(), sha=SHA, pr=7, project_root=gh.root, receipts_file=gh.ledger
    )
    assert run_id == 900 and "PATCH" not in gh.methods()
    outcome = [r for r in _receipts(gh.ledger) if r.get("phase") == "outcome"][-1]
    assert (outcome["status"], outcome["readback_attempts"]) == ("success", 1)


def test_publish_that_never_sees_the_run_is_blocked_with_the_attempt_count(
    gh: FakeGitHub, sleeps: List[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fcr._api_request

    def always_empty(method, url, *, headers, payload=None, timeout=30):
        if method == "GET" and "/commits/" in url and "POST" in gh.methods():
            gh.calls.append((method, url, None))
            return 200, json.dumps({"total_count": 0, "check_runs": []})
        return real(method, url, headers=headers, payload=payload, timeout=timeout)

    monkeypatch.setattr(fcr, "_api_request", always_empty)
    with pytest.raises(fgp.ForgeDoorRefused, match="na 3 pogingen"):
        fgp.publish_door_permit(_evidence(), sha=SHA, pr=7, project_root=gh.root, receipts_file=gh.ledger)
    outcome = [r for r in _receipts(gh.ledger) if r.get("phase") == "outcome"][-1]
    assert outcome["status"] == "blocked" and "na 3 pogingen" in outcome["reason"]
