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
import deliverable_predecessor_gate as dpg
import dispatch_cli
from dispatch_spec import DispatchPath

C, PID = "dlv-cand00000001", "vnx-dev"
MODE, LIVE, PRS = {"v": "warn"}, {"v": brs.Sources()}, {"v": brs.Sources()}
PRED_MODE = {"v": None}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir(), (tmp_path / "tests").mkdir(), (tmp_path / "scripts").mkdir()
    MODE["v"], LIVE["v"], PRS["v"], PRED_MODE["v"] = "warn", brs.Sources(), brs.Sources(), None
    monkeypatch.setattr(crt, "get", lambda key: PRED_MODE["v"] if key == dpg.MODE_KEY else (
        MODE["v"] if key == brg.MODE_KEY else None))
    monkeypatch.setattr(brs, "live_subjects", lambda sd, pid: LIVE["v"])
    monkeypatch.setattr(brs, "open_pr_subjects", lambda *a: PRS["v"])
    return tmp_path


def _door(tmp, did=C, paths=("scripts/a.py",), work_ref=None, pr_id="x", base_ref="origin/main", **kw):
    spec = SimpleNamespace(dispatch_id=did, project_id=PID, pr_id=pr_id, work_ref=work_ref, base_ref=base_ref,
                           role="backend-developer",
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


# --- identity comes from the staged spec (OI-3070) -----------------------------------------
OI_DID, OI_BRANCH = "dlv-a17c847bae63-m-r3", "dispatch/dlv-a17c847bae63-mode"
OI_PATHS = ("bin/vnx", "scripts/slot_cli.py")


def _pr(number, did, paths, branch):
    return br.Subject(did, tuple(paths), branch, pr_number=number)


def test_oi3070_own_staged_pr_and_work_ref_are_not_an_overlap(env):
    PRS["v"] = brs.Sources((_pr(3093, "dlv-a17c847bae63-mode", OI_PATHS, OI_BRANCH),))
    assert _door(env, did=OI_DID, paths=OI_PATHS, work_ref=OI_BRANCH, pr_id="3093") is None


def test_self_exclusion_by_pr_number_alone(env):
    PRS["v"] = brs.Sources((_pr(3093, "dlv-other", ("scripts/a.py",), None),))
    assert _door(env, pr_id="3093") is None


def test_self_exclusion_by_work_ref_alone(env):
    PRS["v"] = brs.Sources((br.Subject("dlv-other", ("scripts/a.py",), f"dispatch/{C}-mode"),))
    assert _door(env, pr_id=None, work_ref=f"dispatch/{C}-mode") is None


def test_an_origin_prefixed_work_ref_is_the_same_branch(env):
    PRS["v"] = brs.Sources((br.Subject("dlv-other", ("scripts/a.py",), f"dispatch/{C}-mode"),))
    assert _door(env, pr_id=None, work_ref=f"origin/dispatch/{C}-mode") is None


def test_a_non_numeric_pr_id_neither_raises_nor_matches(env):
    PRS["v"] = brs.Sources((_pr(3093, "dlv-other", ("scripts/a.py",), None),))
    v = _door(env, pr_id="3093x")
    assert v is not None and v.code == "blast-radius-overlap"


def test_a_stacked_successor_does_not_own_its_base_branch(env):
    base = "dispatch/dlv-aaaaaaaaaaaa"
    PRS["v"] = brs.Sources((br.Subject("dlv-aaaaaaaaaaaa", ("scripts/a.py",), base),))
    v = _door(env, did="dlv-bbbbbbbbbbbb", pr_id=None, work_ref=None, base_ref=f"origin/{base}")
    assert v is not None and v.code == "blast-radius-overlap"


# --- a predecessor is excluded only when the gate guarantees it ------------------------------
def _edge(predecessor, kind):
    return dpg.Edge(predecessor, kind, None, None, None)


def _predecessor_open_pr():
    return brs.Sources((br.Subject("dlv-p", ("scripts/a.py",), "dispatch/dlv-p"),))


def test_a_gate_guaranteed_hard_predecessor_is_excluded(env, monkeypatch):
    MODE["v"], PRED_MODE["v"], PRS["v"] = "refuse", "refuse", _predecessor_open_pr()
    monkeypatch.setattr(dpg, "read_edges", lambda *a: (_edge("dlv-p", "hard"),))
    assert _door(env) is None


@pytest.mark.parametrize("pred_mode", ["warn", "off"])
def test_a_hard_predecessor_outside_refuse_mode_still_overlaps(env, monkeypatch, pred_mode):
    MODE["v"], PRED_MODE["v"], PRS["v"] = "refuse", pred_mode, _predecessor_open_pr()
    monkeypatch.setattr(dpg, "read_edges", lambda *a: (_edge("dlv-p", "hard"),))
    v = _door(env)
    assert v is not None and v.code == "blast-radius-overlap" and v.severity == "blocking"


def test_a_hard_predecessor_with_an_override_reason_still_overlaps(env, monkeypatch):
    MODE["v"], PRED_MODE["v"], PRS["v"] = "refuse", "refuse", _predecessor_open_pr()
    monkeypatch.setattr(dpg, "read_edges", lambda *a: (_edge("dlv-p", "hard"),))
    v = _door(env, predecessor_override_reason="merging right after this fire")
    assert v is not None and v.code == "blast-radius-overlap"


def test_a_soft_predecessor_under_refuse_still_overlaps(env, monkeypatch):
    MODE["v"], PRED_MODE["v"], PRS["v"] = "refuse", "refuse", _predecessor_open_pr()
    monkeypatch.setattr(dpg, "read_edges", lambda *a: (_edge("dlv-p", "soft"),))
    v = _door(env)
    assert v is not None and v.code == "blast-radius-overlap"


def test_an_unknown_stored_predecessor_mode_reads_as_refuse(env, monkeypatch):
    MODE["v"], PRED_MODE["v"], PRS["v"] = "refuse", "bogus", _predecessor_open_pr()
    monkeypatch.setattr(dpg, "read_edges", lambda *a: (_edge("dlv-p", "hard"),))
    assert _door(env) is None
