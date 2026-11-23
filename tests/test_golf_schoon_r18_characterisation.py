"""Golf schoon R18 characterisation: what ``verify_chain`` and ``run_deliberation`` return, book and
log (real git repo + bare origin, real temp ledgers, a recording dispatcher, no model run)."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_r18_{name}", _LIB / f"{name}.py")
    sys.modules[spec.name] = module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


coa = _load("chain_origin_anchor")
dp = _load("deliberation_panel")
from ndjson_hash_chain import append_chained_entry, append_epoch_marker  # noqa: E402


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


@pytest.fixture
def env(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("init", "-b", "main", str(work), cwd=tmp_path)
    (work / "README.md").write_text("seed\n")
    for args in (("config", "user.email", "t@example.com"), ("config", "user.name", "T"), ("add", "README.md"),
                 ("commit", "-m", "seed"), ("remote", "add", "origin", str(bare)), ("push", "-u", "origin", "main")):
        _git(*args, cwd=work)
    data_dir = tmp_path / ".vnx-data" / "vnx-dev"
    ledger = data_dir / "state" / "t0_receipts.ndjson"
    ledger.parent.mkdir(parents=True)
    return work, ledger, data_dir, coa.ledger_identity("vnx-dev", ledger, data_dir)


def _anchor(work: Path, records: list[dict]) -> None:
    path = work / coa.ANCHOR_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in records))
    for args in (("add", str(coa.ANCHOR_REL_PATH)), ("commit", "-m", "anchor"), ("push", "origin", "main")):
        _git(*args, cwd=work)


def _two_epochs(ledger: Path) -> None:
    for epoch, seqs in ((1, "ab"), (2, "c")):
        append_epoch_marker(ledger, epoch)
        for seq in seqs:
            append_chained_entry(ledger, {"seq": seq})


def _records(ledger: Path, identity: str) -> list[dict]:
    """[open 1, open 2, close 1] — the true anchors for a two-epoch ledger."""
    epochs = coa._observed_epochs(ledger)
    out = [coa._origin_record(identity, coa.compute_epoch_fingerprint(ledger, e), sealed_at="t") for e, _ in epochs]
    fp = coa.compute_epoch_closure(ledger, 1, next_epoch_marker_line=epochs[1][1])
    return out + [coa._closure_record(identity, fp, sealed_at="t")]


def _vc(env, **kw):
    work, ledger, data_dir, _identity = env
    return coa.verify_chain(ledger, project_root=work, project_id="vnx-dev", project_data_dir=data_dir, **kw)


def _fake_open(identity, epoch, h):
    fp = coa.OriginFingerprint(epoch=epoch, origin_type="chain_epoch_start", origin_hash=h,
                               origin_line_number=9, entries_before_origin=8)
    return coa._origin_record(identity, fp, sealed_at="t")


def test_vc_verified_and_tampered(env):
    work, ledger, _d, identity = env
    _two_epochs(ledger)
    recs = _records(ledger, identity)
    _anchor(work, recs)
    ok, violations, status, prov = _vc(env)
    assert (ok, violations, status, prov.resolved, prov.ref) == (True, [], "verified-segmented", True, "origin/main")
    assert prov.anchor_commit_sha == _git("rev-parse", "origin/main", cwd=work).strip()
    observed_open, observed_close = recs[0]["origin_hash"], recs[2]["closure_hash"]
    recs[0]["origin_hash"], recs[2]["closure_hash"] = "a" * 64, "b" * 64
    _anchor(work, recs)
    assert _vc(env)[:3] == (False, [
        {"epoch": 1, "note": "epoch opening fingerprint mismatch (tamper)",
         "anchor_origin_hash": "a" * 64, "observed_origin_hash": observed_open},
        {"epoch": 1, "note": "epoch closure fingerprint mismatch (tamper)",
         "anchor_closure_hash": "b" * 64, "observed_closure_hash": observed_close},
    ], "broken")


@pytest.mark.parametrize("pick, expected", [
    ([0], [{"epoch": 1, "note": "no closure record for a CLOSED epoch"},
           {"epoch": 2, "note": "no git anchor for this epoch's chain_epoch_start marker"}]),
    ([0, 1, 1, 2, 2], [{"epoch": 1, "note": "duplicate closure records for this epoch (corrupt)"},
                       {"epoch": 2, "note": "duplicate git anchor records for this epoch (corrupt)"}]),
])
def test_vc_missing_and_corrupt(env, pick, expected):
    work, ledger, _d, identity = env
    _two_epochs(ledger)
    recs = _records(ledger, identity)
    _anchor(work, [recs[i] for i in pick])
    assert _vc(env)[:3] == (False, expected, "broken")


def test_vc_rollback_and_markerless(env):
    work, ledger, _d, identity = env
    _two_epochs(ledger)
    _anchor(work, _records(ledger, identity) + [_fake_open(identity, 3, "c" * 64)])
    assert _vc(env)[:3] == (False, [
        {"note": "git anchor(s) exist for epoch(s) the ledger no longer observes", "missing_epochs": [3]},
        {"note": "highest anchored epoch exceeds highest observed epoch (rollback)",
         "anchored_max_epoch": 3, "observed_max_epoch": 2},
    ], "broken")
    ledger.write_text("")
    append_chained_entry(ledger, {"seq": "re"})
    assert _vc(env)[:3] == (False, [
        {"note": "git anchors exist for this ledger_identity but the ledger shows no "
                 "chain_epoch_start markers (markerless rechain)", "anchor_epochs": [1, 2, 3]},
        {"note": "git anchor(s) exist for epoch(s) the ledger no longer observes", "missing_epochs": [1, 2, 3]},
    ], "broken")


def test_vc_no_anchor_bad_ref_and_no_identity(env):
    work, ledger, _d, identity = env
    _two_epochs(ledger)
    assert _vc(env)[:3] == (False, [
        {"note": "chaining-active ledger has no git anchor anywhere for this "
                 "ledger_identity (unanchored or markerless chain)"},
        {"epoch": 1, "note": "no git anchor for this epoch's chain_epoch_start marker"},
        {"epoch": 2, "note": "no git anchor for this epoch's chain_epoch_start marker"},
    ], "broken")
    id_err = coa.read_git_anchors_for_identity(work, identity, ref="origin/nope")[1].error
    ep_err = coa.read_git_anchor(work, identity, 1, kind="open", ref="origin/nope")[1].error
    ok, violations, status, prov = _vc(env, anchor_ref="origin/nope")
    assert (ok, status, prov.resolved, prov.ref) == (False, "broken", False, "origin/nope")
    assert violations == [{"note": "identity anchor scan failed (fail-closed)", "error": id_err}] + [
        {"epoch": e, "note": "anchor resolution failed (fail-closed)", "error": ep_err} for e in (1, 2)]
    assert coa.verify_chain(ledger) == (False, [{"note": "project_root/project_id/project_data_dir required "
                                                         "for a chaining-enabled ledger (ADR-034 §2)"}], "broken", None)


def test_vc_unchained_paths(env):
    work, ledger, _d, identity = env
    ledger.write_text(json.dumps({"seq": 0}) + "\n")
    assert coa.verify_chain(ledger) == (True, [], "unchained", None)
    ok, violations, status, prov = _vc(env)
    assert (ok, violations, status, prov.resolved) == (True, [], "unchained", True)
    bad = coa.read_git_anchors_for_identity(work, identity, ref="origin/nope")[1].error
    assert _vc(env, anchor_ref="origin/nope")[:3] == (
        False, [{"note": "anchor resolution failed (fail-closed)", "error": bad}], "broken")
    rec = _fake_open(identity, 1, "d" * 64)
    _anchor(work, [rec])
    assert _vc(env)[:3] == (False, [{"note": "git anchor exists for this ledger_identity but the ledger is "
                                             "missing/empty/unchained", "anchor_keys": [rec["key"]]}], "broken")


ROSTER = [("codex", "gpt-5.5"), ("kimi", "k2"), ("claude", "sonnet")]


def _norm(did: str) -> str:
    return re.sub(r"-[0-9a-f]{6}$", "-X", did)


@pytest.fixture
def panel(tmp_path, monkeypatch, caplog):
    """Runs a panel where seats in ``fail`` get a failed receipt, seats in ``unmeasured`` none."""
    receipts, booked, calls = tmp_path / "state" / "t0_receipts.ndjson", [], []
    original = dp.LegBooker.book
    monkeypatch.setattr(dp.LegBooker, "book", lambda self, **kw: (
        booked.append((kw["stage"], kw["provider"], _norm(kw["dispatch_id"]), kw["failure"])), original(self, **kw))[1])
    monkeypatch.setattr(dp, "_seat_model", lambda m: m)

    def run(fail=(), unmeasured=(), **kw):
        calls.clear(), booked.clear(), caplog.clear()

        def disp(provider, model, prompt, did):
            calls.append((provider, _norm(did), prompt))
            diverge = "-diverge-" in did
            failed = diverge and provider in fail
            if not (diverge and provider in unmeasured):
                receipts.parent.mkdir(parents=True, exist_ok=True)
                with receipts.open("a") as fh:
                    fh.write(json.dumps({"dispatch_id": did, "status": "timeout" if failed else "success",
                                         "verdict": {"decision": "reject" if failed else "accept"}}) + "\n")
            return "" if failed else f"report {provider} {_norm(did)}"
        with caplog.at_level(logging.WARNING, logger=dp.logger.name):
            res = dp.run_deliberation("sweep", "q", dispatcher=disp, roster=ROSTER, context="ctx", max_workers=1,
                                      receipts_path=receipts, ledger_path=tmp_path / "cost.ndjson",
                                      project_id="vnx-dev", panel_run_id="run-1", **kw)
        logs = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == dp.logger.name]
        return res, calls, booked, logs
    return run


def test_rd_full_run(panel):
    res, calls, booked, logs = panel()
    assert [c[:2] for c in calls[3:]] == [("codex", "panel-sweep-contrarian-codex-X"),
                                          ("codex", "panel-sweep-verify-codex-X"),
                                          ("claude", "panel-sweep-synth-claude-X")]
    assert booked[3:] == [("contrarian", "codex", "panel-sweep-contrarian-codex-X", None),
                          ("verify", "codex", "panel-sweep-verify-codex-X", None),
                          ("synthesis", "claude", "panel-sweep-synth-claude-X", None)]
    assert [b[:2] for b in booked[:3]] == [("diverge", p) for p, _m in ROSTER]
    assert [sorted(fo) for fo in res.fan_out] == [["dispatch_id", "lens", "provider", "seat_outcome", "text"]] * 3
    assert (res.coverage, res.failed_seats, res.unmeasured_seats, res.ledger_available, res.panel_run_id) == (
        "3/3 lenses present", [], [], True, "run-1")
    assert (res.contrarian, res.factcheck, res.synthesis) == (
        "report codex panel-sweep-contrarian-codex-X", "report codex panel-sweep-verify-codex-X",
        "report claude panel-sweep-synth-claude-X")
    assert [m["stage"] for m in res.seat_measurements] == ["diverge"] * 3 + ["contrarian", "verify", "synthesis"]
    assert "PANEL COVERAGE" not in calls[-1][2] and logs == []


def test_rd_failed_and_unmeasured(panel):
    res, calls, _booked, logs = panel(fail={"kimi"}, unmeasured={"claude"})
    assert [lvl for lvl, _ in logs] == ["WARNING", "WARNING"]
    assert logs[0][1].startswith("panel: 1/3 seats produced no usable report (kimi (")
    assert logs[1][1].startswith("panel: 1/3 seats are UNMEASURED — the receipt ledger has no decisive record (claude (")
    assert [fo["seat_outcome"] for fo in res.fan_out] == [dp.SEAT_PRESENT, dp.SEAT_FAILED, dp.SEAT_UNMEASURED]
    assert res.failed_seats == [{k: res.fan_out[1][k] for k in ("provider", "lens", "dispatch_id")}]
    assert [(u["provider"], u["dispatch_id"]) for u in res.unmeasured_seats] == [("claude", res.fan_out[2]["dispatch_id"])]
    assert all(f"PANEL COVERAGE: {res.coverage}." in c[2] for c in calls[3:])


def test_rd_coverage_floor_refuses_or_degrades(panel):
    res, calls, booked, logs = panel(fail={"kimi", "claude"}, min_seats=2)
    assert res.synthesis_refused_reason == "refusing synthesis: 1/3 lenses delivered (minimum 2)"
    assert len(calls) == 3 and [b[0] for b in booked] == ["diverge"] * 3
    assert (res.contrarian, res.factcheck, res.synthesis, res.degraded_synthesis) == ("", "", "", False)
    assert logs[-1] == ("ERROR", "panel: synthesis refused — 1/3 lenses delivered (minimum 2); "
                                 "re-run with --allow-degraded to proceed with degraded coverage")
    res, _calls, booked, logs = panel(fail={"kimi", "claude"}, min_seats=2, allow_degraded=True)
    assert (res.degraded_synthesis, res.synthesis_refused_reason) == (True, "")
    assert logs[-1] == ("WARNING", "panel: proceeding with DEGRADED synthesis (--allow-degraded) at "
                                   "1/3 lenses delivered (minimum 2)")
    assert [b[0] for b in booked[3:]] == ["contrarian", "verify", "synthesis"]
