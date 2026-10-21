"""Provider switch, deliverable D: the panels honor the operator's switch.

The plan-gate leaves a switched-off seat out before anything is dispatched, names it in the run
output and in the round's decision text, and never books it as a seat. Below the floor of the
entitled panel it refuses before any dispatch and books no round. An explicit ``--panel-seats``
that names a switched-off seat refuses. A switched-off tiebreaker refuses before any call. The
deliberation panel drops switched-off seats from its default roster with one named line each,
refuses below its synthesis floor before dispatching, and refuses an explicit ``--seats`` that
names one.

Since P2 (operator decision 2026-10-17, #2104) a plan-gate run dispatches at most two seats: the
default seat, and the escalation seat on a BLOCK or from the start on a schema or security path.
The five-seat panel is therefore pinned on the seat assembly plus the REAL ``run_panel`` with the
shipped five-seat roster, and at gate level on the escalation seat and the second seat.

The gate-level tests drive the REAL ``run_plan_gate_for_track`` and the REAL ``run_panel``; only
the lane is a fake that records every call. The switch file is a tmp file through
``VNX_PROVIDER_SWITCH_FILE``; the operator's real file is never read.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
for _p in (str(_REPO / "scripts" / "lib"), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import deliberation_panel  # noqa: E402
import planning_cli  # noqa: E402
import plan_gate_panel as pgp  # noqa: E402
import plan_gate_tiebreaker as pgt  # noqa: E402
import provider_switch as ps  # noqa: E402
import tracks  # noqa: E402
import track_reconciler  # noqa: E402
from ndjson_hash_chain import walk_chain  # noqa: E402
from test_plan_gate_one_seat import _bootstrap, _Lanes, _seat_report, _tiebreak_report  # noqa: E402

PROJECT = "p1"
TRACK = "feat-switch"
SCHEMA_PATH = "schemas/migrations/0099_new_table.sql"


def _entry(enabled: bool) -> dict:
    return {"enabled": enabled, "changed_at": "2026-10-21T08:00:00Z",
            "changed_by": "op", "reason": "test"}


@pytest.fixture
def switch(tmp_path, monkeypatch):
    """A tmp switch file; the returned callable writes a version 1 document into it."""
    path = tmp_path / "switch" / "providers.json"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv(ps.SWITCH_FILE_ENV, str(path))
    for name in list(os.environ):
        if name.startswith(ps.OVERRIDE_ENV_PREFIX):
            monkeypatch.delenv(name)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)

    def write(global_off=(), project_on=None):
        document = {"version": 1, "global": {f: _entry(False) for f in global_off}, "projects": {}}
        for pid, families in (project_on or {}).items():
            document["projects"][pid] = {f: _entry(True) for f in families}
        path.write_text(json.dumps(document), encoding="utf-8")

    write()
    return write


def _write_config(tmp_path: Path, *, default: str, escalation: str, max_rounds: int) -> Path:
    cfg = tmp_path / "plan_gate_panel.yaml"
    cfg.write_text(
        "version: 1\n"
        f"default_seat: {default}\n"
        f"escalation_seat: {escalation}\n"
        "tiebreaker:\n"
        f"  max_rounds: {max_rounds}\n"
        "  provider: deepseek_harness\n"
        "  model: deepseek-v4-pro\n"
        "seats:\n"
        "  - {label: opus, provider: claude, model_arg: opus}\n"
        "  - {label: kimi, provider: kimi, model_arg: kimi-k3}\n"
        "  - {label: glm-5.2-harness, provider: glm-harness, model_arg: glm-5.2}\n"
        "  - {label: deepseek, provider: deepseek-harness, model_arg: deepseek-v4-pro}\n"
        "  - {label: codex, provider: codex, model_arg: gpt-test}\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def gate(tmp_path, monkeypatch, switch):
    """A seeded, plan-blocked track, an isolated seat ledger, and a config set per test."""
    monkeypatch.setattr(track_reconciler, "reconcile_track", lambda *a, **k: None)
    monkeypatch.delenv("VNX_DATA_DIR", raising=False)
    monkeypatch.delenv("VNX_PANEL_RETRY", raising=False)
    state_dir = _bootstrap(tmp_path)
    tracks.create_track(state_dir, TRACK, PROJECT, "t", "shipped", phase="queued")
    assert planning_cli._seed_plan_blocker(state_dir, TRACK, PROJECT)
    ledger = tmp_path / ".vnx-attest" / "plan-gate-seats.ndjson"
    monkeypatch.setattr(pgp, "_resolve_seat_ledger_path", lambda data_dir: ledger)
    monkeypatch.setattr(planning_cli, "_emit_plan_gate_pass_record", lambda **kw: True)

    class _Gate:
        def __init__(self):
            self.state_dir = state_dir
            self.lanes = None

        def configure(self, lanes, *, default="codex", escalation="opus", max_rounds=2):
            cfg = _write_config(tmp_path, default=default, escalation=escalation, max_rounds=max_rounds)
            monkeypatch.setattr(pgp, "_default_panel_config_path", lambda: cfg)
            monkeypatch.setattr(pgp, "_make_default_dispatcher", lambda *a, **k: lanes)
            self.lanes = lanes

        def run(self, plan_text="## Problem\nplan version one\n", **kw):
            doc = tmp_path / "plan.md"
            doc.write_text(plan_text, encoding="utf-8")
            kw.setdefault("dispatch_paths", "scripts/lib/some_utility.py")
            return planning_cli.run_plan_gate_for_track(
                state_dir=state_dir, track_id=TRACK, project_id=PROJECT, doc=str(doc), **kw,
            )

        def records(self, record_type):
            if not ledger.exists():
                return []
            return [rec for _ln, rec, _h in walk_chain(ledger) if rec.get("type") == record_type]

        def plan_blocker_open(self) -> bool:
            conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
            try:
                row = conn.execute(
                    "SELECT resolved_at FROM track_open_items WHERE track_id = ? AND project_id = ? "
                    "AND oi_id = ? AND link_type = 'blocks'",
                    (TRACK, PROJECT, planning_cli._plan_blocker_oi(TRACK)),
                ).fetchone()
            finally:
                conn.close()
            return row is not None and row[0] is None

    return _Gate()


# --------------------------------------------------------------------------
# The plan-gate seat assembly
# --------------------------------------------------------------------------

def test_a_switched_off_escalation_seat_is_left_out_named_and_never_booked(gate, switch):
    """deepseek off, escalation seat deepseek, codex BLOCK: deepseek is never dispatched, the run
    output and the decision text name it, and the seat ledger carries no record for it."""
    switch(global_off=["deepseek"])
    lanes = _Lanes({"codex": _seat_report("block", ["the rollback is missing"]),
                    "deepseek-harness": _seat_report("pass")})
    gate.configure(lanes, escalation="deepseek")

    outcome = gate.run()

    assert lanes.seat_providers() == ["codex"]
    assert [p["label"] for p in outcome.result["panelists"]] == ["codex"]
    assert outcome.decision == "REVISE"
    output = "\n".join(outcome.stderr_lines + outcome.stdout_text)
    assert "left out (provider switched off): deepseek" in output
    assert "family=deepseek" in output
    assert "left out (provider switched off): deepseek" in outcome.result["summary"]["rationale"]
    assert outcome.result["switched_off_seats"][0]["label"] == "deepseek"
    (round_record,) = gate.records(pgt.ROUND_RECORD_TYPE)
    assert "left out (provider switched off): deepseek" in round_record["gov_trace"]
    assert [r["panelist_id"] for r in gate.records(pgp.SEAT_RECORD_TYPE)] == ["codex"]


def test_a_panel_entitled_to_two_seats_with_one_switched_off_refuses_before_any_dispatch(gate, switch):
    """A schema path puts both seats at the table from the start: N=2, floor 2. With deepseek off
    one remains, so the run refuses, dispatches nothing, books no round and names the seat."""
    switch(global_off=["deepseek"])
    lanes = _Lanes({"codex": _seat_report("pass"), "deepseek-harness": _seat_report("pass")})
    gate.configure(lanes, escalation="deepseek")

    outcome = gate.run(dispatch_paths=SCHEMA_PATH)

    assert lanes.calls == []
    assert gate.records(pgt.ROUND_RECORD_TYPE) == []
    assert gate.records(pgp.SEAT_RECORD_TYPE) == []
    assert outcome.exit_code == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert outcome.outcome == planning_cli.PLAN_GATE_SWITCH_REFUSED
    assert "entitled to 2 seat(s)" in outcome.error
    assert "deepseek" in outcome.error and "family=deepseek" in outcome.error
    assert "vnx providers on deepseek" in outcome.error
    assert "plan-gate attest" in outcome.error
    assert gate.plan_blocker_open()


def test_a_panel_entitled_to_one_seat_with_that_seat_switched_off_refuses(gate, switch):
    switch(global_off=["deepseek"])
    lanes = _Lanes({"deepseek-harness": _seat_report("pass")})
    gate.configure(lanes, default="deepseek")

    outcome = gate.run()

    assert lanes.calls == []
    assert gate.records(pgt.ROUND_RECORD_TYPE) == []
    assert outcome.exit_code == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert outcome.outcome == planning_cli.PLAN_GATE_SWITCH_REFUSED
    assert "entitled to 1 seat(s)" in outcome.error
    assert "0 remain" in outcome.error
    assert gate.plan_blocker_open()


def test_explicit_panel_seats_naming_a_switched_off_seat_refuse(gate, switch):
    switch(global_off=["deepseek"])
    lanes = _Lanes({"codex": _seat_report("pass"), "deepseek-harness": _seat_report("pass")})
    gate.configure(lanes)

    outcome = gate.run(panel_seats="codex,deepseek")

    assert lanes.calls == []
    assert gate.records(pgt.ROUND_RECORD_TYPE) == []
    assert outcome.exit_code == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert outcome.outcome == planning_cli.PLAN_GATE_SWITCH_REFUSED
    assert "--panel-seats names switched-off seat(s): deepseek" in outcome.error
    assert "provider_switched_off: deepseek-harness family=deepseek source=global" in outcome.error


def test_a_project_override_keeps_the_seat(gate, switch):
    """Global deepseek off, the track's project on: the escalation seat stays and is dispatched."""
    switch(global_off=["deepseek"], project_on={PROJECT: ["deepseek"]})
    lanes = _Lanes({"codex": _seat_report("block", ["x"]),
                    "deepseek-harness": _seat_report("revise", ["y"])})
    gate.configure(lanes, escalation="deepseek")

    outcome = gate.run()

    assert lanes.seat_providers() == ["codex", "deepseek-harness"]
    assert "left out" not in "\n".join(outcome.stderr_lines)
    assert "switched_off_seats" not in outcome.result


def test_switching_back_on_returns_the_seat_on_the_next_run_with_no_residue(gate, switch):
    switch(global_off=["deepseek"])
    lanes = _Lanes({"deepseek-harness": _seat_report("revise", ["split the PR"])})
    gate.configure(lanes, default="deepseek")

    refused = gate.run()
    assert refused.outcome == planning_cli.PLAN_GATE_SWITCH_REFUSED
    assert lanes.calls == [] and gate.records(pgt.ROUND_RECORD_TYPE) == []

    switch()
    again = gate.run()

    assert lanes.seat_providers() == ["deepseek-harness"]
    assert again.decision == "REVISE"
    assert "left out" not in "\n".join(again.stderr_lines)
    (round_record,) = gate.records(pgt.ROUND_RECORD_TYPE)
    assert round_record["round"] == 1
    assert "left out" not in round_record["gov_trace"]


def test_the_seat_assembly_leaves_the_switched_off_seat_out_of_a_panel_of_five(switch):
    """The shipped five-seat roster as one panel: deepseek off leaves four, named with the detail."""
    switch(global_off=["deepseek"])
    seats = pgp.load_panel_seats(_REPO / "configs" / "plan_gate_panel.yaml")
    assert len(seats) == 5
    composition = pgp.PanelComposition(seats=seats, escalation=None, quorum=pgp.panel_floor(5),
                                       chosen_by="default", default_label="codex")

    switched = pgp.apply_provider_switch(composition, project_id=PROJECT)

    assert [s["label"] for s in switched.composition.seats] == ["opus", "kimi", "glm-5.2-harness", "codex"]
    assert [s["label"] for s in switched.left_out] == ["deepseek"]
    assert "family=deepseek" in switched.left_out[0]["detail"]
    assert (switched.entitled, switched.floor, switched.refusal) == (5, 2, "")
    assert switched.composition.quorum == 2
    assert "left out (provider switched off): deepseek" in switched.note()


def test_a_panel_of_five_with_deepseek_off_dispatches_and_books_the_four_that_remain(switch, tmp_path):
    """The five-seat roster through the REAL run_panel: the deepseek seat is never dispatched and
    never booked, the decision text names it, and the four remaining seats certify."""
    switch(global_off=["deepseek"])
    seats = pgp.load_panel_seats(_REPO / "configs" / "plan_gate_panel.yaml")
    composition = pgp.PanelComposition(seats=seats, escalation=None, quorum=pgp.panel_floor(5),
                                       chosen_by="default", default_label="codex")
    switched = pgp.apply_provider_switch(composition, project_id=PROJECT)
    lanes = _Lanes({s["provider"]: _seat_report("pass") for s in seats})
    ledger = tmp_path / "seats.ndjson"

    result = pgp.run_panel(doc_text="## Plan\n", track_id=TRACK, project_id=PROJECT,
                           panel=switched.composition.seats, dispatcher=lanes,
                           seat_ledger_path=ledger, quorum=switched.composition.quorum,
                           switched_off_seats=switched.left_out)

    assert lanes.seat_providers() == ["claude", "kimi", "glm-harness", "codex"]
    assert result["decision"] == "PASS"
    assert "left out (provider switched off): deepseek" in result["summary"]["rationale"]
    assert [s["label"] for s in result["switched_off_seats"]] == ["deepseek"]
    booked = [rec["panelist_id"] for _ln, rec, _h in walk_chain(ledger)
              if rec.get("type") == pgp.SEAT_RECORD_TYPE]
    assert booked == ["opus", "kimi", "glm-5.2-harness", "codex"]


def test_the_dry_run_preview_names_the_left_out_seat(gate, switch):
    switch(global_off=["deepseek"])
    gate.configure(_Lanes({}), escalation="deepseek")

    preview = planning_cli._dry_run_track(
        gate.state_dir, TRACK, PROJECT,
        run_kwargs={"dispatch_paths": "scripts/lib/some_utility.py"}, min_goal_chars=1,
    )

    assert preview["seats"] == 1
    assert "left out (provider switched off): deepseek (family=deepseek source=global)" in preview["detail"]
    assert "refused" not in preview["detail"]


def test_a_seat_switched_off_does_not_stop_a_run_that_goes_straight_to_the_tiebreaker(gate, switch):
    """max_rounds 1, default seat kimi. The first run reads the plan and the tiebreaker delivers
    nothing. With kimi then off, the second run on the SAME plan needs no seat round: the seat
    refusal does not apply, and the tiebreaker (deepseek, on) decides."""
    lanes = _Lanes({"kimi": _seat_report("revise", ["split the PR"])})
    gate.configure(lanes, default="kimi", max_rounds=1)
    first = gate.run()
    assert [c["tiebreak"] for c in lanes.calls] == [False, True]
    assert first.exit_code == 1

    switch(global_off=["kimi"])
    lanes.tiebreak = _tiebreak_report("START")
    second = gate.run()

    assert [c["tiebreak"] for c in lanes.calls] == [False, True, True]
    assert second.exit_code == 0 and second.decision == "START"
    assert "left out (provider switched off): kimi" in "\n".join(second.stderr_lines)


def test_a_switch_refusal_is_tallied_and_not_persisted_so_the_next_batch_runs_the_track(gate, switch, tmp_path):
    switch(global_off=["deepseek"])
    lanes = _Lanes({"deepseek-harness": _seat_report("revise", ["split the PR"])})
    gate.configure(lanes, default="deepseek")
    doc = tmp_path / "plan.md"
    doc.write_text("## Problem\nplan version one\n", encoding="utf-8")
    run_kwargs = {"doc": str(doc), "dispatch_paths": "scripts/lib/some_utility.py"}

    refused = planning_cli._execute_batch(
        [TRACK], state_dir=gate.state_dir, project_id=PROJECT, run_kwargs=run_kwargs,
        restart=False, min_goal_chars=1, interrupt_check=lambda: False,
    )
    assert lanes.calls == []
    assert refused["tally"][planning_cli.PLAN_GATE_SWITCH_REFUSED] == 1
    assert "vnx providers on deepseek" in refused["results"][0]["detail"]
    assert TRACK not in planning_cli.load_batch_progress(gate.state_dir)

    switch()
    again = planning_cli._execute_batch(
        [TRACK], state_dir=gate.state_dir, project_id=PROJECT, run_kwargs=run_kwargs,
        restart=False, min_goal_chars=1, interrupt_check=lambda: False,
    )
    assert again["skipped"] == []
    assert lanes.seat_providers() == ["deepseek-harness"]
    assert again["tally"]["REVISE"] == 1


def test_the_seat_assembly_with_every_seat_on_changes_nothing(switch):
    composition = pgp.compose_panel(config_path=_REPO / "configs" / "plan_gate_panel.yaml")
    switched = pgp.apply_provider_switch(composition, project_id=PROJECT)
    assert switched.composition == composition
    assert switched.left_out == [] and switched.note() == "" and switched.refusal == ""


def test_the_floor_is_one_function_called_by_the_rule_and_by_the_seat_assembly(switch, monkeypatch):
    """apply_panel_rule and the seat assembly ask the same helper for the same N, and get the same floor."""
    original = pgp.panel_floor
    calls = []

    def spy(entitled):
        floor = original(entitled)
        calls.append((entitled, floor))
        return floor

    monkeypatch.setattr(pgp, "panel_floor", spy)
    seat = {"label": "codex", "provider": "codex", "model_arg": "gpt-test"}
    for n in range(1, 6):
        calls.clear()
        results = [pgp.PanelistResult(label=f"s{i}", provider="codex", verdict="pass",
                                      dispatched=True, seat_status=pgp.SEAT_SCORED) for i in range(n)]
        pgp.apply_panel_rule(results)
        composition = pgp.PanelComposition(seats=[dict(seat, label=f"s{i}") for i in range(n)],
                                           escalation=None, quorum=1, chosen_by="default",
                                           default_label="s0")
        switched = pgp.apply_provider_switch(composition, project_id=PROJECT)
        assert calls == [(n, original(n)), (n, original(n))]
        assert switched.floor == original(n)
    assert [original(n) for n in range(1, 6)] == [1, 2, 2, 2, 2]


# --------------------------------------------------------------------------
# The tiebreaker
# --------------------------------------------------------------------------

def test_a_switched_off_tiebreaker_refuses_before_any_call_and_the_track_stays_blocked(gate, switch):
    """max_rounds 1: the codex seat reads and REVISEs, the tiebreaker (deepseek) would decide in the
    same run. With deepseek off it refuses before its call; the track stays blocked."""
    switch(global_off=["deepseek"])
    lanes = _Lanes({"codex": _seat_report("revise", ["split the PR"])},
                   tiebreak=_tiebreak_report("START"))
    gate.configure(lanes, max_rounds=1)

    outcome = gate.run()

    assert [c["tiebreak"] for c in lanes.calls] == [False]
    assert outcome.exit_code == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert outcome.outcome == planning_cli.PLAN_GATE_SWITCH_REFUSED
    assert "tiebreaker" in outcome.error and "family=deepseek" in outcome.error
    assert "vnx providers on deepseek" in outcome.error
    assert "plan-gate attest" in outcome.error
    assert gate.plan_blocker_open()
    assert [r["outcome"] for r in gate.records(pgt.ROUND_RECORD_TYPE)] == [pgt.SEAT_ROUND_OUTCOME]


def test_switching_the_tiebreaker_back_on_lets_it_decide_on_the_next_run(gate, switch):
    """The refused tiebreaker left no residue: the seat round it followed stays booked, and once
    deepseek is back on the next run on the same plan goes straight to the tiebreaker."""
    switch(global_off=["deepseek"])
    lanes = _Lanes({"codex": _seat_report("revise", ["split the PR"])},
                   tiebreak=_tiebreak_report("START"))
    gate.configure(lanes, max_rounds=1)
    refused = gate.run()
    assert refused.outcome == planning_cli.PLAN_GATE_SWITCH_REFUSED
    assert refused.exit_code == ps.EXIT_PROVIDER_SWITCHED_OFF

    switch()
    again = gate.run()

    assert [c["tiebreak"] for c in lanes.calls] == [False, True]
    assert lanes.calls[-1]["provider"] == "deepseek-harness"
    assert again.exit_code == 0 and again.decision == "START"
    assert not gate.plan_blocker_open()


def test_run_tiebreaker_refuses_before_the_dispatcher_is_called(switch):
    switch(global_off=["deepseek"])
    called = []

    def dispatcher(*args):
        called.append(args)
        return _tiebreak_report("START")

    cfg = {"max_rounds": 1, "provider": "deepseek_harness", "model": "deepseek-v4-pro"}
    with pytest.raises(pgt.TiebreakerSwitchedOffError) as caught:
        pgt.run_tiebreaker(doc_text="## Plan\n", track_id=TRACK, project_id=PROJECT,
                           round_number=2, dispatcher=dispatcher, config=cfg)

    assert called == []
    assert caught.value.decision.family == "deepseek"
    assert caught.value.decision.scope == PROJECT


def test_run_tiebreaker_with_the_project_on_calls_the_dispatcher(switch):
    switch(global_off=["deepseek"], project_on={PROJECT: ["deepseek"]})
    called = []

    def dispatcher(provider, model, instruction, dispatch_id):
        called.append(provider)
        return _tiebreak_report("START")

    cfg = {"max_rounds": 1, "provider": "deepseek_harness", "model": "deepseek-v4-pro"}
    result = pgt.run_tiebreaker(doc_text="## Plan\n", track_id=TRACK, project_id=PROJECT,
                                round_number=2, dispatcher=dispatcher, config=cfg)

    assert called == ["deepseek-harness"]
    assert result.outcome == pgt.START


# --------------------------------------------------------------------------
# The deliberation panel
# --------------------------------------------------------------------------

class _FakeResult:
    synthesis_refused_reason = ""

    def to_report(self) -> str:
        return "# fake panel report\n"


def _load_panel_cli():
    spec = importlib.util.spec_from_file_location("panel_cli_switch_under_test", _REPO / "scripts" / "panel.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def panel_cli(monkeypatch, switch):
    """scripts/panel.py with the lane factory and run_deliberation recorded, for the current project."""
    monkeypatch.setenv("VNX_PROJECT_ID", PROJECT)
    panel = _load_panel_cli()
    calls = {"dispatcher_built": 0, "run_deliberation": []}

    def factory(data_dir, timeout, role=None):
        calls["dispatcher_built"] += 1
        return lambda provider, model, prompt, dispatch_id: "ok"

    def fake_run_deliberation(*args, **kwargs):
        calls["run_deliberation"].append(kwargs)
        return _FakeResult()

    monkeypatch.setattr("plan_gate_panel._make_default_dispatcher", factory)
    monkeypatch.setattr("plan_gate_panel.load_synthesis_min_seats", lambda config_path=None: 3)
    monkeypatch.setattr(panel, "run_deliberation", fake_run_deliberation)
    panel.calls = calls
    return panel


def test_the_default_roster_drops_switched_off_seats_with_a_named_line(panel_cli, switch, tmp_path, capsys):
    switch(global_off=["glm", "deepseek"])

    rc = panel_cli.main(["sweep", "audit src/", "--out", str(tmp_path / "r.md")])

    err = capsys.readouterr().err
    assert rc == 0
    (kwargs,) = panel_cli.calls["run_deliberation"]
    assert [p for p, _ in kwargs["roster"]] == ["codex", "kimi", "claude"]
    lines = [ln for ln in err.splitlines() if "left out (provider switched off)" in ln]
    assert len(lines) == 2
    assert "glm-harness" in lines[0] and "family=glm" in lines[0]
    assert "deepseek-harness" in lines[1] and "family=deepseek" in lines[1]
    assert f"scope={PROJECT}" in lines[1]


def test_the_default_roster_below_the_synthesis_floor_refuses_before_any_dispatch(panel_cli, switch, tmp_path, capsys):
    switch(global_off=["kimi", "glm", "deepseek"])

    rc = panel_cli.main(["sweep", "audit src/", "--out", str(tmp_path / "r.md")])

    err = capsys.readouterr().err
    assert rc == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert panel_cli.calls["run_deliberation"] == []
    assert panel_cli.calls["dispatcher_built"] == 0
    assert not (tmp_path / "r.md").exists()
    assert "2 of 5 seat(s) remain" in err and "minimum 3" in err
    assert "kimi" in err and "glm-harness" in err and "deepseek-harness" in err
    assert "vnx providers on" in err


def test_explicit_seats_naming_a_switched_off_seat_refuse(panel_cli, switch, tmp_path, capsys):
    switch(global_off=["deepseek"])

    rc = panel_cli.main(["sweep", "audit src/", "--seats", "codex,deepseek-harness",
                         "--out", str(tmp_path / "r.md")])

    err = capsys.readouterr().err
    assert rc == ps.EXIT_PROVIDER_SWITCHED_OFF
    assert panel_cli.calls["run_deliberation"] == []
    assert panel_cli.calls["dispatcher_built"] == 0
    assert "--seats names switched-off seat(s): deepseek-harness" in err
    assert "provider_switched_off: deepseek-harness family=deepseek" in err


def test_the_default_roster_with_every_seat_on_keeps_the_full_fleet_path(panel_cli, tmp_path, capsys):
    rc = panel_cli.main(["sweep", "audit src/", "--out", str(tmp_path / "r.md")])

    assert rc == 0
    (kwargs,) = panel_cli.calls["run_deliberation"]
    assert "roster" not in kwargs
    assert "left out" not in capsys.readouterr().err


def test_the_project_override_keeps_a_deliberation_seat(panel_cli, switch, tmp_path):
    switch(global_off=["deepseek"], project_on={PROJECT: ["deepseek"]})

    rc = panel_cli.main(["sweep", "audit src/", "--seats", "codex,deepseek-harness",
                         "--out", str(tmp_path / "r.md")])

    assert rc == 0
    (kwargs,) = panel_cli.calls["run_deliberation"]
    assert [p for p, _ in kwargs["roster"]] == ["codex", "deepseek-harness"]


def test_allow_degraded_runs_the_remaining_seats_below_the_synthesis_floor(panel_cli, switch, tmp_path, capsys):
    """--allow-degraded is the operator's existing escape from the synthesis floor: the run goes on
    with the seats that remain, and the floor still reaches run_deliberation so the report stamps
    the degraded synthesis."""
    switch(global_off=["kimi", "glm", "deepseek"])

    rc = panel_cli.main(["sweep", "audit src/", "--allow-degraded", "--out", str(tmp_path / "r.md")])

    err = capsys.readouterr().err
    assert rc == 0
    (kwargs,) = panel_cli.calls["run_deliberation"]
    assert [p for p, _ in kwargs["roster"]] == ["codex", "claude"]
    assert (kwargs["min_seats"], kwargs["allow_degraded"]) == (3, True)
    assert len([ln for ln in err.splitlines() if "left out (provider switched off)" in ln]) == 3


def test_switching_back_on_returns_the_deliberation_seat(panel_cli, switch, tmp_path):
    switch(global_off=["deepseek"])
    argv = ["sweep", "audit src/", "--seats", "codex,deepseek-harness", "--out", str(tmp_path / "r.md")]
    assert panel_cli.main(argv) == ps.EXIT_PROVIDER_SWITCHED_OFF

    switch()

    assert panel_cli.main(argv) == 0
    (kwargs,) = panel_cli.calls["run_deliberation"]
    assert [p for p, _ in kwargs["roster"]] == ["codex", "deepseek-harness"]


def test_switch_roster_splits_the_roster_by_the_switch(switch):
    switch(global_off=["kimi"])
    kept, left_out = deliberation_panel.switch_roster(deliberation_panel.default_roster(), project_id=PROJECT)
    assert [p for p, _ in kept] == ["codex", "claude", "glm-harness", "deepseek-harness"]
    assert [(s["provider"], s["family"]) for s in left_out] == [("kimi", "kimi")]
    assert "source=global" in left_out[0]["detail"]
