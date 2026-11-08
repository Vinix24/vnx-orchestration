#!/usr/bin/env python3
"""Deur D3: the door permit is one run per head, made from GO-evidence, read back, revoked by PATCH.

Every GitHub call and the keychain read is patched: ``_api_request`` is the one
network seam, the installation token is stubbed. No live call, no real key.
"""

from __future__ import annotations

import json
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

    def __call__(self, method, url, *, headers, payload=None, timeout=30):
        body = json.loads(payload) if payload else None
        self.calls.append((method, url, body))
        if method == "GET":
            return 200, json.dumps({"total_count": len(self.runs), "check_runs": self.runs})
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


def _door(run_id: int, conclusion: str = "success", app: int = APP_ID) -> Dict[str, Any]:
    return {"id": run_id, "name": fgp.DOOR_CHECK_NAME, "app": {"id": app}, "conclusion": conclusion}


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setenv(fcr.TEST_POST_OPT_IN_ENV, "1")
    monkeypatch.setattr(fcr, "_api_request", fake)
    monkeypatch.setattr(fcr, "_installation_token_with_freshness", lambda **kw: ("tok", True))
    monkeypatch.setattr(fcr, "resolve_owner_repo", lambda root=None: "o/r")
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
    assert gh.methods() == ["GET", "POST"]
    assert gh.calls[1][2]["conclusion"] == "success" and gh.calls[1][2]["name"] == "vnx-gate/door"


def _publish(gh: FakeGitHub) -> int:
    return fgp.publish_door_permit(_evidence(), sha=SHA, pr=7)


def test_publish_re_permits_the_single_existing_run_with_patch(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, "failure")]
    assert _publish(gh) == 55
    assert gh.methods() == ["GET", "PATCH"]
    assert gh.calls[1][1].endswith("/check-runs/55")
    assert gh.calls[1][2]["conclusion"] == "success"
    assert gh.runs[0]["conclusion"] == "success"


def test_publish_refuses_two_door_runs(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, "failure"), _door(56)]
    with pytest.raises(fgp.ForgeDoorRefused, match="2 deurruns"):
        _publish(gh)
    assert gh.methods() == ["GET"]


def test_publish_ignores_a_door_run_of_another_app(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, app=1)]
    assert _publish(gh) == 900
    assert gh.methods() == ["GET", "POST"]


def test_publish_validates_before_any_call(gh: FakeGitHub) -> None:
    with pytest.raises(fgp.ForgeDoorRefused):
        fgp.publish_door_permit(_evidence(source="x"), sha=SHA, pr=7)
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
    assert fgp.read_back_door(SHA, 55)["id"] == 55


def test_read_back_refuses_another_app_another_id_and_not_success(gh: FakeGitHub) -> None:
    gh.runs = [_door(55, app=1)]
    with pytest.raises(fgp.ForgeDoorRefused, match="andere App"):
        fgp.read_back_door(SHA, 55)
    gh.runs = [_door(55)]
    with pytest.raises(fgp.ForgeDoorRefused, match="staat niet op"):
        fgp.read_back_door(SHA, 56)
    gh.runs = [_door(55, "failure")]
    with pytest.raises(fgp.ForgeDoorRefused, match="conclusion 'failure'"):
        fgp.read_back_door(SHA, 55)
    gh.runs = [_door(55, None)]  # type: ignore[arg-type]
    with pytest.raises(fgp.ForgeDoorRefused, match="conclusion None"):
        fgp.read_back_door(SHA, 55)


def test_read_back_refuses_two_own_runs(gh: FakeGitHub) -> None:
    gh.runs = [_door(55), _door(56)]
    with pytest.raises(fgp.ForgeDoorRefused, match="2 deurruns"):
        fgp.read_back_door(SHA, 55)


def test_a_lost_post_answer_is_found_by_the_read_back(gh: FakeGitHub) -> None:
    gh.drop_post_answer = True
    with pytest.raises(fcr.ForgeAPIError):
        _publish(gh)
    # GitHub did create the run; the caller learns that from the read back, not from the POST.
    assert fgp.read_back_door(SHA, 900)["conclusion"] == "success"


# --- revoke -----------------------------------------------------------------


def test_revoke_patches_failure_with_the_title(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    assert fgp.revoke_door(55, "merge faalde", sha=SHA) == "revoked"
    method, url, body = gh.calls[-1]
    assert method == "PATCH" and url.endswith("/check-runs/55")
    assert body["conclusion"] == "failure"
    assert body["output"]["title"] == "permit revoked: merge faalde"


def test_revoke_failed_after_three_failed_patches(gh: FakeGitHub) -> None:
    gh.runs = [_door(55)]
    gh.patch_status = 500
    assert fgp.revoke_door(55, "x") == "revoke_failed"
    assert gh.methods().count("PATCH") == 3
    assert gh.runs[0]["conclusion"] == "success"


def test_revoke_failed_when_the_answer_does_not_show_failure(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fgp, "patch_check_run", lambda *a, **k: {"id": 55, "conclusion": "success"})
    assert fgp.revoke_door(55, "x") == "revoke_failed"
    monkeypatch.setattr(fgp, "patch_check_run", lambda *a, **k: {"id": 99, "conclusion": "failure"})
    assert fgp.revoke_door(55, "x") == "revoke_failed"


def test_revoke_succeeds_on_the_third_attempt(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([{}, {}, {"id": 55, "conclusion": "failure"}])
    monkeypatch.setattr(fgp, "patch_check_run", lambda *a, **k: next(answers))
    assert fgp.revoke_door(55, "x") == "revoked"


def test_patch_rejects_a_bad_id(gh: FakeGitHub) -> None:
    with pytest.raises(fcr.ForgeCheckRunError):
        fcr.patch_check_run(True, "failure", "t", "s", name="n")  # type: ignore[arg-type]


def test_no_delete_anywhere() -> None:
    for module in (fcr, fgp):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert '"DELETE"' not in text and "'DELETE'" not in text


# --- CLI --------------------------------------------------------------------


def _cli(tmp_path: Path, ev: Optional[Dict[str, Any]], capsys: pytest.CaptureFixture) -> Tuple[int, str, str]:
    path = tmp_path / "ev.json"
    if ev is not None:
        path.write_text(json.dumps(ev), encoding="utf-8")
    rc = fgp.main(["door", "--evidence", str(path), "--sha", SHA, "--pr", "7"])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_cli_missing_evidence_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    rc, _, err = _cli(tmp_path, None, capsys)
    assert rc == 1 and "evidence ontbreekt" in err
    assert gh.calls == []


def test_cli_refuses_a_no_go_record(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    ev = _evidence()
    ev["preflight_gates"][0]["verdict"] = "NO-GO"
    rc, _, err = _cli(tmp_path, ev, capsys)
    assert rc == 1 and "geen GO" in err and gh.calls == []


def test_cli_valid_evidence_prints_the_check_run_id(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub) -> None:
    rc, out, _ = _cli(tmp_path, _evidence(), capsys)
    assert rc == 0 and "check_run_id=900" in out
    assert gh.methods() == ["GET", "POST", "GET"]


def test_cli_read_back_refusal_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fgp, "read_back_door", lambda sha, rid: (_ for _ in ()).throw(fgp.ForgeDoorRefused("weg")))
    rc, _, err = _cli(tmp_path, _evidence(), capsys)
    assert rc == 1 and "weg" in err
