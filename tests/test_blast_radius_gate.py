"""ADR-096 B4b: the door checks the blast radius of a fire against live work and open PRs."""
import json
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib"):
    sys.path.insert(0, str(_p))

import blast_radius as br
import blast_radius_gate as brg
import blast_radius_sources as brs
import build_t0_state as bts
import config_runtime as crt
import dispatch_cli
from dispatch_spec import DispatchPath

C, PID = "dlv-cand00000001", "vnx-dev"
MODE, LIVE, PRS = {"v": "warn"}, {"v": brs.Sources()}, {"v": brs.Sources()}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir(), (tmp_path / "tests").mkdir(), (tmp_path / "scripts").mkdir()
    MODE["v"], LIVE["v"], PRS["v"] = "warn", brs.Sources(), brs.Sources()
    monkeypatch.setattr(crt, "get", lambda key: MODE["v"] if key == brg.MODE_KEY else None)
    monkeypatch.setattr(brs, "live_subjects", lambda sd, pid: LIVE["v"])
    monkeypatch.setattr(brs, "open_pr_subjects", lambda *a: PRS["v"])
    return tmp_path


def _door(tmp, did=C, paths=("scripts/a.py",), **kw):
    spec = SimpleNamespace(dispatch_id=did, project_id=PID, pr_id="x", role="backend-developer",
                           dispatch_paths=tuple(DispatchPath(PurePosixPath(p)) for p in paths))
    return dispatch_cli._check_blast_radius_verdict(spec, repo_root=tmp, state_dir=tmp / "state", **kw)


def _ledger(tmp):
    p = tmp / "state" / brg.LEDGER
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _src(*subjects, **kw):
    return brs.Sources(tuple(br.Subject(d, p, b) for d, p, b in subjects), **kw)


def test_no_overlap_is_clear_and_writes_nothing(env):
    LIVE["v"] = _src(("dlv-other", ("scripts/b.py",), "dispatch/dlv-other"))
    assert _door(env) is None and _ledger(env) == []
    assert _door(env, paths=("scripts",)).code == "blast-radius-overlap"  # an existing directory keeps its "/"


def test_overlap_with_live_work_warns_and_is_booked(env):
    LIVE["v"] = _src(("dlv-other", ("scripts/",), "dispatch/dlv-other"))
    assert _door(env, dry_run=True).severity == "warn" and _ledger(env) == []  # a dry run writes nothing
    v = _door(env)
    assert v.severity == "warn" and brg.OVERLAP in v.message and "dlv-other" in v.message
    [line] = _ledger(env)
    assert (line["project_id"], line["dispatch_id"], line["seq"], line["source"], line["path"], line["outcome"]) == (
        PID, C, 0, "live_work", "scripts/a.py", "warned")


def test_overlap_with_an_open_pr_refuses_in_refuse_mode(env):
    MODE["v"], PRS["v"] = "refuse", _src(("", ("scripts/a.py",), "feature/x"))
    v = _door(env)
    assert v.severity == "blocking" and "feature/x" in v.message and _ledger(env)[0]["outcome"] == "refused"


def test_self_and_own_round_and_predecessor_branch_are_excluded(env):
    LIVE["v"] = _src((f"{C}-r2", ("scripts/a.py",), f"dispatch/{C}-r2"))
    PRS["v"] = _src((C, ("scripts/a.py",), f"dispatch/{C}"))
    assert _door(env, did=f"{C}-c") is None


@pytest.mark.parametrize("src", [
    brs.Sources(unavailable="gh pr list failed"),                            # source missing
    _src(("dlv-x", None, "dispatch/dlv-x"), reasons={"dlv-x": "gh pr view 7 failed"}),  # files None
    _src(("dlv-x", ("../evil.py",), "dispatch/dlv-x")),                     # malformed path
    _src(("dlv-x", ("tests/",), "dispatch/dlv-x")),                         # other scope incomplete
])
def test_an_unreadable_other_is_unknown_never_clear(env, src):
    MODE["v"], PRS["v"] = "refuse", src
    v = _door(env)
    assert v.code == "blast-radius-unknown" and v.severity == "blocking"


def test_an_unknown_outcome_is_not_clear(env, monkeypatch):
    LIVE["v"] = _src(("dlv-x", ("scripts/z.py",), "dispatch/dlv-x"))
    monkeypatch.setattr(br, "check", lambda *a, **k: [br.Result("dlv-x", "maybe")])
    assert _door(env).code == "blast-radius-unknown"


@pytest.mark.parametrize("paths", [(), ("tests/",), ("../x.py",)])
def test_incomplete_or_invalid_candidate_scope_is_unknown_with_nothing_running(env, paths):
    assert _door(env, paths=paths).code == "blast-radius-unknown"


def test_live_work_stale_is_ignored_and_unmeasured_is_unknown(env, monkeypatch):
    monkeypatch.undo()
    lw = {"available": True, "live": [], "starting": [], "stale": [{"dispatch_id": "dlv-z"}], "unmeasured": []}
    monkeypatch.setattr(bts, "_build_live_work", lambda *a: lw)
    assert brs.live_subjects(env, PID).subjects == ()
    lw["unmeasured"] = [{"dispatch_id": "dlv-u", "lock": "unmeasured: EIO"}]
    assert [s.paths for s in brs.live_subjects(env, PID).subjects] == [None]
    assert brg.judge(br.Subject(C, ("scripts/a.py",)), [("live_work", brs.live_subjects(env, PID))], env)[0] == brg.UNKNOWN


@pytest.mark.parametrize("rows, truncated", [(brs.PR_LIST_LIMIT, True), (brs.PR_LIST_LIMIT - 1, False)])
def test_open_pr_list_at_the_limit_is_truncated(env, monkeypatch, rows, truncated):  # OI-3055
    monkeypatch.undo()
    listing = [{"number": n, "headRefName": f"b{n}", "headRefOid": "s"} for n in range(rows)]
    view = {"files": [{"path": "x.py"}], "headRefOid": "s"}
    gh = lambda argv, cwd: json.dumps(listing if argv[1] == "list" else view)
    out = brs.open_pr_subjects(env, PID, env / "cache.json", gh)
    assert (out.unavailable or "").startswith(brs.REASON_PRS_TRUNCATED) is truncated
    assert len(out.subjects) == (0 if truncated else rows)


@pytest.mark.parametrize("mode, sev", [("warn", "warn"), (None, "warn"), ("refuse", "blocking"), ("bogus", "blocking")])
def test_modes(env, mode, sev):
    MODE["v"], LIVE["v"] = mode, _src(("dlv-o", ("scripts/a.py",), "dispatch/dlv-o"))
    assert _door(env).severity == sev


def test_off_reads_nothing(env, monkeypatch):
    MODE["v"] = "off"
    monkeypatch.setattr(brs, "live_subjects", lambda *a: pytest.fail("read in off mode"))
    assert _door(env) is None


def test_override_fires_and_is_booked_and_an_empty_reason_is_refused(env):
    MODE["v"], LIVE["v"] = "refuse", _src(("dlv-o", ("scripts/a.py",), "dispatch/dlv-o"))
    v = _door(env, override_reason="PR #1 touches a different function")
    assert v.severity == "warn" and v.override_applied and _ledger(env)[0]["outcome"] == "override"
    with pytest.raises(SystemExit):
        dispatch_cli.main(["--spec-file", "/nonexistent.json", "--override-overlap", "  "])
