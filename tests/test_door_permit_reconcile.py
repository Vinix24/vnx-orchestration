#!/usr/bin/env python3
"""Deur D7c: the reconciliation reads every open PR and revokes a stale or orphaned door permit.

GitHub is stubbed at its two seams: `bf._gh_api_json` (the PR list, `gh api`) and
`forge_check_run._api_request` (the App-authenticated check-run calls). No real `gh`,
no keychain, no central store: ledgers, open items and the beacon live under tmp_path.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import backfill_pr_merged_receipts as bf
import forge_check_run as fcr
import forge_gate_publisher as fgp

APP_ID = 4869217
SHA = "a" * 40

def _ago(minutes: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _run(run_id: int, minutes: float = 5, conclusion: Optional[str] = "success", app: Optional[int] = APP_ID) -> Dict[str, Any]:
    return {"id": run_id, "name": fgp.DOOR_CHECK_NAME, "conclusion": conclusion, "started_at": _ago(minutes + 1),
            "completed_at": _ago(minutes), "app": {"id": app} if app is not None else None}


class FakeGitHub:
    """The check-run seam: lists runs per sha, records every call, PATCH flips the conclusion."""

    def __init__(self) -> None:
        self.runs: Dict[str, List[Dict[str, Any]]] = {}
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
        self.patch_status = 200

    def __call__(self, method, url, *, headers, payload=None, timeout=30):
        body = json.loads(payload) if payload else None
        self.calls.append((method, url, body))
        if method == "GET":
            sha = url.split("/commits/")[1].split("/")[0]
            runs = self.runs.get(sha, [])
            return 200, json.dumps({"total_count": len(runs), "check_runs": runs})
        if method == "PATCH":
            if self.patch_status >= 400:
                return self.patch_status, "{}"
            run_id = int(url.rsplit("/", 1)[1])
            run = next(r for runs in self.runs.values() for r in runs if r["id"] == run_id)
            run["conclusion"] = body["conclusion"]
            return 200, json.dumps(run)
        raise AssertionError(f"unexpected method {method}")

    def patches(self) -> List[Dict[str, Any]]:
        return [c[2] for c in self.calls if c[0] == "PATCH"]


class Env:
    def __init__(self, gh: FakeGitHub, receipts: Path, items: List[Dict[str, Any]]) -> None:
        self.gh, self.receipts, self.items = gh, receipts, items
        self.prs: Any = []


@pytest.fixture()
def env(tmp_path, monkeypatch) -> Env:
    root = tmp_path / "target"
    root.mkdir()
    for cmd in (["init", "-q"], ["remote", "add", "origin", "https://github.com/acme/widgets.git"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True)
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    (tmp_path / "data" / "events").mkdir()
    receipts = state / "t0_receipts.ndjson"
    receipts.write_text("")
    fake, items = FakeGitHub(), []
    holder = Env(fake, receipts, items)
    monkeypatch.setenv(fcr.TEST_POST_OPT_IN_ENV, "1")
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VNX_STATE_DIR", str(state))
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(root))
    monkeypatch.setattr(fcr, "_api_request", fake)
    monkeypatch.setattr(fcr, "_installation_token_with_freshness", lambda **kw: ("tok", True))
    monkeypatch.setattr(fgp, "load_app_config", lambda: fcr.AppConfig(slug="vnx-gate", app_id=APP_ID))
    monkeypatch.setattr(fgp, "publish_check_run", fcr.publish_check_run)
    monkeypatch.setattr(fgp.time, "sleep", lambda s: None)
    monkeypatch.setattr(bf, "_gh_api_json", lambda endpoint, what: holder.prs, raising=False)
    monkeypatch.setattr(bf, "_audit_project_id", lambda state_dir: "proj")
    monkeypatch.setattr(bf, "_heartbeat_audit", lambda *a, **k: None, raising=False)

    def add_item(**kw):
        for item in items:
            if item["dedup_key"] == kw["dedup_key"]:
                return item["id"], False
        items.append(dict(kw, id=f"OI-{len(items) + 1}"))
        return items[-1]["id"], True

    import open_items_manager
    monkeypatch.setattr(open_items_manager, "add_item_programmatic", add_item)
    return holder


def _setup(env: Env, runs: List[Dict[str, Any]], pr: int = 7, sha: str = SHA) -> None:
    env.prs = [{"number": pr, "head": {"sha": sha}}]
    env.gh.runs[sha] = runs


def _ledger(env: Env, *records: Dict[str, Any]) -> None:
    with env.receipts.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _vouch(run_id: int, event: str = "pr_merged", sha: str = SHA) -> Dict[str, Any]:
    return {"event_type": event, "pr_number": 7, "door_check": {"permit_id": f"{APP_ID}:{sha}:{run_id}"}}


def _main(*extra: str) -> int:
    return bf.main(["--reconcile-open-permits", "--json", *extra])


def test_stale_permit_is_revoked(env, capsys):
    _setup(env, [_run(11, minutes=45)])
    _ledger(env, _vouch(11))
    assert _main() == 0
    assert [p["conclusion"] for p in env.gh.patches()] == ["failure"]
    assert env.gh.runs[SHA][0]["conclusion"] == "failure"
    assert [(i["severity"], i["dedup_key"]) for i in env.items] == [("warn", f"door-permit:proj:7:11")]
    out = json.loads(capsys.readouterr().out)
    assert out["counts"] == {"checked": 1, "stale": 1, "orphaned": 0, "revoked": 1, "revoke_failed": 0, "foreign": 0}


def test_orphaned_permit_is_revoked(env):
    _setup(env, [_run(12, minutes=2)])
    assert _main() == 0
    assert len(env.gh.patches()) == 1 and env.gh.runs[SHA][0]["conclusion"] == "failure"
    assert env.items[0]["severity"] == "warn"


def test_no_request_is_a_delete(env):
    _setup(env, [_run(11, minutes=45), _run(12, minutes=2)])
    _main()
    assert {c[0] for c in env.gh.calls} <= {"GET", "PATCH"}


@pytest.mark.parametrize("event", ["pr_merged", "pr_merge_refused"])
def test_vouched_young_permit_is_left_alone(env, event):
    _setup(env, [_run(11, minutes=5)])
    _ledger(env, _vouch(11, event))
    assert _main() == 0
    assert env.gh.patches() == [] and env.items == []


def test_permit_vouched_only_by_another_event_is_orphaned(env):
    _setup(env, [_run(11, minutes=5)])
    _ledger(env, _vouch(11, "something_else"))
    _main()
    assert len(env.gh.patches()) == 1


def test_permit_already_failure_is_no_finding(env):
    _setup(env, [_run(11, minutes=90, conclusion="failure")])
    assert _main() == 0
    assert env.gh.patches() == []


@pytest.mark.parametrize("conclusion", [None, "neutral", "skipped", "weird"])
def test_any_conclusion_but_failure_is_revoked(env, conclusion):
    _setup(env, [_run(11, minutes=90, conclusion=conclusion)])
    assert _main() == 0
    assert len(env.gh.patches()) == 1


@pytest.mark.parametrize("stamp", [None, "", "not-a-date", 5])
def test_unreadable_age_is_stale_not_clean(env, stamp):
    run = _run(11)
    run["completed_at"], run["started_at"] = stamp, stamp
    _setup(env, [run])
    _ledger(env, _vouch(11))
    _main()
    assert len(env.gh.patches()) == 1


def test_foreign_door_check_is_reported_and_not_touched(env, capsys):
    _setup(env, [_run(21, minutes=90, app=999), _run(22, minutes=90, app=None)])
    assert _main() == 1
    assert env.gh.patches() == []
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["foreign"] == 2 and out["counts"]["revoked"] == 0
    assert {f["kind"] for f in out["findings"]} == {"foreign_door_check"}


def test_failed_revocation_is_a_blocker_and_exit_1(env, capsys):
    _setup(env, [_run(11, minutes=45)])
    env.gh.patch_status = 500
    assert _main() == 1
    assert len(env.gh.patches()) == fgp.DOOR_REVOKE_ATTEMPTS
    assert [i["severity"] for i in env.items] == ["blocker"]
    assert "python3 scripts/pr_merge.py --pr 7" in env.items[0]["details"]
    assert json.loads(capsys.readouterr().out)["counts"]["revoke_failed"] == 1


def test_failed_pr_list_read_exits_2(env, monkeypatch):
    def broken(endpoint, what):
        raise bf.AuditUnmeasurable("gh api open pulls page 1 failed (rc=1): boom")

    monkeypatch.setattr(bf, "_gh_api_json", broken)
    assert _main() == 2
    assert env.gh.calls == [] and env.items == []


@pytest.mark.parametrize("bad", ["x", [{"number": True, "head": {"sha": SHA}}], [{"number": 7}], [{"number": 7, "head": {"sha": ""}}]])
def test_malformed_pr_list_exits_2(env, bad):
    env.prs = bad
    assert _main() == 2


def test_unreadable_check_run_list_exits_2(env, monkeypatch):
    _setup(env, [_run(11)])
    monkeypatch.setattr(fcr, "_api_request", lambda *a, **k: (500, "{}"))
    assert _main() == 2


def test_missing_ledger_exits_2(env):
    _setup(env, [_run(11, minutes=45)])
    env.receipts.unlink()
    assert _main() == 2
    assert env.gh.patches() == []


def test_dry_run_patches_nothing_and_writes_nothing(env, capsys):
    _setup(env, [_run(11, minutes=45)])
    assert _main("--dry-run") == 1
    assert env.gh.patches() == [] and env.items == []
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["stale"] == 1 and out["counts"]["revoked"] == 0


def test_ttl_is_configurable(env):
    _setup(env, [_run(11, minutes=10)])
    _ledger(env, _vouch(11))
    assert _main("--permit-ttl", "5") == 0
    assert len(env.gh.patches()) == 1


@pytest.mark.parametrize("ttl", ["0", "-3"])
def test_non_positive_ttl_is_refused(env, ttl):
    with pytest.raises(SystemExit) as exc:
        _main("--permit-ttl", ttl)
    assert exc.value.code == 2


def test_same_finding_on_two_runs_is_one_open_item(env):
    _setup(env, [_run(11, minutes=45)])
    _main()
    env.gh.runs[SHA][0]["conclusion"] = "success"  # the permit stands again: the same run is found again
    _main()
    assert len(env.items) == 1 and len(env.gh.patches()) == 2


def test_open_pr_pagination_reads_until_a_short_page(env, monkeypatch):
    pages = {1: [{"number": n, "head": {"sha": f"{n:040d}"}} for n in range(bf.AUDIT_PAGE_SIZE)],
             2: [{"number": 900, "head": {"sha": SHA}}]}
    asked: List[str] = []
    monkeypatch.setattr(bf, "_gh_api_json", lambda endpoint, what: (asked.append(endpoint), pages[len(asked)])[1])
    assert len(bf.list_open_prs()) == bf.AUDIT_PAGE_SIZE + 1
    assert all("state=open" in e for e in asked) and asked[1].endswith("page=2")


def test_report_only_with_reconcile_runs_both_modes_in_one_process(env, monkeypatch, capsys):
    _setup(env, [_run(11, minutes=45)])
    monkeypatch.setattr(bf, "list_merged_in_window", lambda since: [])
    assert bf.main(["--report-only", "--reconcile-open-permits", "--json", "--since", "2026-11-01"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "report-only" and out["reconcile"]["counts"]["revoked"] == 1
    assert len(env.gh.patches()) == 1


def test_failed_revocation_makes_the_combined_report_exit_1(env, monkeypatch, capsys):
    _setup(env, [_run(11, minutes=45)])
    env.gh.patch_status = 500
    monkeypatch.setattr(bf, "list_merged_in_window", lambda since: [])
    assert bf.main(["--report-only", "--reconcile-open-permits", "--json", "--since", "2026-11-01"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "gaps" and out["exit_code"] == 1


def test_fleet_child_runs_both_modes(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    seen: List[List[str]] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        report = {"outcome": "clean", "exit_code": 0, "missing": [], "reason": ""}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(report), "")

    monkeypatch.setattr(bf.subprocess, "run", fake_run)
    monkeypatch.setattr(bf, "_project_data_dir", lambda pid, r: tmp_path / "d")
    project = {"project_id": "p", "path": str(root), "finding": "", "registry_ok": True}
    assert bf._run_child(project, None, 12, True)[1] == 0
    cmd = seen[0]
    assert "--report-only" in cmd and "--reconcile-open-permits" in cmd and "--dry-run" in cmd
    assert cmd[cmd.index("--permit-ttl") + 1] == "12"
