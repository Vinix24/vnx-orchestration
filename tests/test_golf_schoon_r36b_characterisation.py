"""Characterisation tests for golf-schoon R36 part b: ``DispatchDaemon._handle``.

Pin the live headless dispatch loop before the extract-method refactor: the
lease, move and audit side effects, their order, the ``_processed`` retry
bookkeeping and every log line. The lease CLI, governance check, enricher and
delivery are patched; files only move inside a temporary data dir.
"""
from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import dispatch_enricher
import governance_audit
import headless_dispatch_daemon as hd

DID = "20261115-r36b-A"
INSTR = "[[TARGET:T1]]\nTrack: A\nRole: backend-developer\nGate: g1\n\n---\n\nDo it.\n"


@pytest.fixture()
def env(tmp_path, monkeypatch, caplog):
    pending = tmp_path / "dispatches" / "pending"
    pending.mkdir(parents=True)
    (pending / f"{DID}.md").write_text(INSTR)
    calls: list = []
    e = types.SimpleNamespace(tmp=tmp_path, path=pending / f"{DID}.md", calls=calls,
                              deliver=lambda meta, ap, sd, **kw: (True, "T1", 5))
    monkeypatch.setattr(hd, "_now_utc", lambda: "NOW")
    monkeypatch.setattr(hd, "_is_terminal_headless", lambda t: True)
    monkeypatch.setattr(hd, "_run_governance_pre_check", lambda m, p, d: (False, [], None))
    monkeypatch.setattr(hd, "_is_terminal_available", lambda t, sd: True)
    monkeypatch.setattr(hd, "_acquire_lease",
                        lambda t, d: calls.append(("acquire", t, d)) or 5)
    monkeypatch.setattr(hd, "_release_lease",
                        lambda t, g: calls.append(("release", t, g)) or True)
    monkeypatch.setattr(hd, "_repo_root", lambda: Path("/repo"))
    ticks = iter([10.0, 12.345])
    monkeypatch.setattr(hd, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    class FakeEnricher:
        def enrich(self, text, ctx):
            calls.append(("enrich", ctx))
            return text + "ENRICHED"

    monkeypatch.setattr(dispatch_enricher, "DispatchEnricher", FakeEnricher)

    def fake_deliver(meta, active_path, state_dir, **kw):
        calls.append(("deliver", meta.raw_instruction, active_path, state_dir, kw))
        return e.deliver(meta, active_path, state_dir, **kw)

    monkeypatch.setattr(hd, "_deliver", fake_deliver)
    monkeypatch.setattr(governance_audit, "log_dispatch_decision",
                        lambda **kw: calls.append(("gov_audit", kw)))
    caplog.set_level(logging.INFO, logger=hd.logger.name)
    e.daemon = hd.DispatchDaemon(data_dir=tmp_path, state_dir=tmp_path / "state",
                                 no_repo_map=True)
    e.log = lambda: [(r.levelname, r.getMessage()) for r in caplog.records
                     if r.name == hd.logger.name]
    e.audit = lambda: [json.loads(x) for x in
                       (tmp_path / "dispatch_audit.jsonl").read_text().splitlines()]
    return e


def _done_audit(outcome, terminal="T1", gen=5, released=True):
    return {"timestamp": "NOW", "dispatch_id": DID, "terminal": terminal, "track": "A",
            "role": "backend-developer", "gate": "g1", "outcome": outcome,
            "elapsed_seconds": 2.35, "lease_generation": gen, "lease_released": released}


def test_claimed_dispatch_delivered_after_reroute(env):
    env.deliver = lambda meta, ap, sd, **kw: (True, "T2", 9)
    env.daemon._handle(env.path)
    active = env.tmp / "dispatches" / "active" / f"{DID}.md"
    assert env.calls == [
        ("acquire", "T1", DID),
        ("enrich", {"role": "backend-developer", "track": "A", "gate": "g1",
                    "no_repo_map": True, "project_root": "/repo"}),
        ("deliver", INSTR + "ENRICHED", active, env.tmp / "state",
         {"original_terminal": "T1", "original_generation": 5}),
        ("release", "T2", 9),
    ]
    assert (env.tmp / "dispatches" / "completed" / f"{DID}.md").read_text() == INSTR
    assert not active.exists()
    assert env.audit() == [_done_audit("done", "T2", 9)]
    assert env.log() == [
        ("INFO", f"Delivering {DID} → T1 (role=backend-developer gate=g1)"),
        ("INFO", f"Dispatch {DID} finished: done (2.3s)"),
    ]
    assert DID in env.daemon._processed


@pytest.mark.parametrize("kind", ["failed", "raised"])
def test_failed_delivery_stays_in_active(env, kind):
    if kind == "failed":
        env.deliver = lambda meta, ap, sd, **kw: (False, "T1", 5)
    else:
        def boom(meta, ap, sd, **kw):
            raise RuntimeError("kaput")
        env.deliver = boom
    env.daemon._handle(env.path)
    assert (env.tmp / "dispatches" / "active" / f"{DID}.md").exists()
    assert env.calls[-1] == ("release", "T1", 5)
    assert env.audit() == [_done_audit("failed")]
    expected = [("INFO", f"Delivering {DID} → T1 (role=backend-developer gate=g1)")]
    if kind == "raised":
        expected.append(("ERROR", f"Delivery error for {DID}: kaput"))
    expected += [
        ("WARNING", f"Delivery of {DID} failed: left in active/ as an open point for a T0"),
        ("INFO", f"Dispatch {DID} finished: failed (2.3s)"),
    ]
    assert env.log() == expected


def test_enrich_fail_completed_move_fail_and_release_fail(env, monkeypatch):
    class Broken:
        def enrich(self, text, ctx):
            raise ValueError("no map")

    monkeypatch.setattr(dispatch_enricher, "DispatchEnricher", Broken)
    real_move = hd._move_dispatch

    def move(src, dest):
        if dest.name == "completed":
            raise OSError("disk full")
        return real_move(src, dest)

    monkeypatch.setattr(hd, "_move_dispatch", move)
    monkeypatch.setattr(hd, "_release_lease", lambda t, g: False)
    env.daemon._handle(env.path)
    assert env.calls[1][1] == INSTR  # delivered unenriched
    assert env.audit() == [_done_audit("done", released=False)]
    assert env.log() == [
        ("WARNING", "Dispatch enrichment failed: no map — delivering unenriched"),
        ("INFO", f"Delivering {DID} → T1 (role=backend-developer gate=g1)"),
        ("WARNING", f"Cannot move {DID}.md to completed: disk full"),
        ("WARNING", "Lease release failed for T1 gen=5"),
        ("INFO", f"Dispatch {DID} finished: done (2.3s)"),
    ]


@pytest.mark.parametrize("audit_raises", [False, True])
def test_governance_refusal_defers_with_audit(env, monkeypatch, audit_raises):
    monkeypatch.setattr(hd, "_run_governance_pre_check",
                        lambda m, p, d: (True, ["gate_a", "gate_b"], 42))
    if audit_raises:
        def raise_(**kw):
            raise RuntimeError("db locked")
        monkeypatch.setattr(governance_audit, "log_dispatch_decision", raise_)
    env.daemon._handle(env.path)
    assert env.audit() == [{"timestamp": "NOW", "dispatch_id": DID, "terminal": "T1",
                            "gate": "g1", "reason": "governance_blocked",
                            "blocked_checks": ["gate_a", "gate_b"]}]
    expected = [] if audit_raises else [("gov_audit", {
        "action": "blocked", "dispatch_id": DID,
        "reasoning": "Governance checks failed: gate_a, gate_b", "pr_number": 42})]
    assert env.calls == expected
    assert env.log() == [("WARNING", f"Dispatch {DID} BLOCKED by governance checks: "
                                     "['gate_a', 'gate_b'] — deferring")]
    assert env.path.exists() and DID not in env.daemon._processed


@pytest.mark.parametrize("case,log,calls,processed", [
    ("unparseable", ("INFO", f"Skipping non-parseable dispatch: {DID}.md"), [], True),
    ("not_headless", ("INFO", f"Terminal T1 is not headless (VNX_ADAPTER_T1 != subprocess) "
                              f"— skipping {DID}"), [], True),
    ("leased", ("INFO", f"Terminal T1 is leased — deferring {DID}"), [], False),
    ("no_lease", ("WARNING", f"Could not acquire lease for {DID} — deferring"), [], False),
    ("move_fails", ("ERROR", f"Cannot move {DID}.md to active/: ro"),
     [("acquire", "T1", DID), ("release", "T1", 5)], False),
])
def test_refusals_before_delivery(env, monkeypatch, case, log, calls, processed):
    if case == "unparseable":
        env.path.write_text("no target\n")
    elif case == "not_headless":
        monkeypatch.setattr(hd, "_is_terminal_headless", lambda t: False)
    elif case == "leased":
        monkeypatch.setattr(hd, "_is_terminal_available", lambda t, sd: False)
    elif case == "no_lease":
        monkeypatch.setattr(hd, "_acquire_lease", lambda t, d: None)
    else:
        def ro(src, dest):
            raise OSError("ro")
        monkeypatch.setattr(hd, "_move_dispatch", ro)
    env.daemon._handle(env.path)
    assert env.log() == [log]
    assert env.calls == calls
    assert env.path.exists()
    assert not (env.tmp / "dispatch_audit.jsonl").exists()
    assert (DID in env.daemon._processed) is processed
