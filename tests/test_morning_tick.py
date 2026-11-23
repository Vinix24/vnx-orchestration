"""morning round D4: tick and stop, the default on, and the missing-snapshot rule (ADR-095)."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "scripts", ROOT / "scripts" / "lib"):
    sys.path.insert(0, str(_p))

import morning_snapshot as ms
import morning_tick as mt
import operator_queue as oq

P, DAY = "proj-a", "2026-11-19"
BEFORE = datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc)  # 06:00 Europe/Amsterdam
START = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)  # 07:00
LATE = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)  # 09:00, the deadline
CFG = {"VNX_MORNING_PAUSE_ENABLED": "true", "VNX_MORNING_PAUSE_START": "07:00",
       "VNX_MORNING_PAUSE_DEADLINE": "09:00", "VNX_MORNING_PAUSE_TZ": "Europe/Amsterdam"}


class Tmux:
    def __init__(self, command="claude", rc=0):
        self.calls, self.command, self.rc = [], command, rc

    def __call__(self, args):
        self.calls.append(list(args))
        out = self.command if args[0] == "display-message" else ""
        return subprocess.CompletedProcess(args, self.rc if args[0] == "send-keys" else 0, out, "")


@pytest.fixture
def world(tmp_path):
    sd, fleet = tmp_path / "state", tmp_path / "fleet"
    oq.open_point(P, bron=1, ref="a", vraag="q a", sluit_bij="x", opened_by="t", state_dir=sd)
    run = sd / "t0_rotation" / "rotation-1"
    run.mkdir(parents=True)
    (run / "delivery.json").write_text(json.dumps({"new_window": "@7"}))
    pushed = []
    return {"sd": sd, "fleet": fleet, "tmux": Tmux(), "pushed": pushed}


def tick(w, now, dry=False, cfg=None, tmux=None):
    cfg = cfg or CFG
    return mt.tick_project(P, now, state_dir=w["sd"], get=cfg.__getitem__, tmux=tmux or w["tmux"],
                           push=lambda pid, sd: w["pushed"].append(pid), fleet_dir=w["fleet"], dry_run=dry)


def kinds(sd):
    path = sd / ms.EVENTS_FILENAME
    return [json.loads(x)["kind"] for x in path.read_text().splitlines()] if path.exists() else []


def test_first_tick_writes_wakes_and_reserves(world):
    out = tick(world, START)
    assert (world["sd"] / "morning_round" / f"{DAY}.json").exists() and kinds(world["sd"]) == ["pause_start"]
    sends = [c for c in world["tmux"].calls if c[0] == "send-keys"]
    assert len(sends) == 2 and sends[0][-2] == "-l" and "agenda --project-id proj-a" in sends[0][-1]
    assert sends[1] == ["send-keys", "-t", "@7", "Enter"]  # Enter is its own call
    assert world["pushed"] == [P] and "wake @7" in out[1]


def test_second_tick_is_a_noop(world):
    tick(world, START)
    world["tmux"].calls.clear()
    out = tick(world, START)
    assert world["tmux"].calls == [] and world["pushed"] == [P] and kinds(world["sd"]) == ["pause_start"]
    assert "already started" in out[0]


def test_before_start_and_disabled_do_nothing(world):
    assert "nothing to do" in tick(world, BEFORE)[0]
    assert "disabled" in tick(world, START, cfg={**CFG, "VNX_MORNING_PAUSE_ENABLED": "false"})[0]
    assert not (world["sd"] / "morning_round").exists() and world["tmux"].calls == []


def test_past_deadline_without_start_does_nothing_and_with_start_closes(world):
    tick(world, LATE)
    assert kinds(world["sd"]) == []
    tick(world, START)
    tick(world, LATE)
    tick(world, LATE)
    assert kinds(world["sd"]) == ["pause_start", "pause_end"]
    ends = [json.loads(x) for x in (world["sd"] / ms.EVENTS_FILENAME).read_text().splitlines()]
    assert ends[-1]["reason"] == "deadline"


def test_every_point_decided_closes_with_decided(world):
    tick(world, START)
    oq.close_point(P, oq.point_id(P, 1, "a", 1), uitkomst="beantwoord", bewijs="e", state_dir=world["sd"])
    tick(world, datetime(2026, 11, 19, 6, 30, tzinfo=timezone.utc))
    last = [json.loads(x) for x in (world["sd"] / ms.EVENTS_FILENAME).read_text().splitlines()][-1]
    assert (last["kind"], last["reason"]) == ("pause_end", "decided")


@pytest.mark.parametrize("pane", ["zsh", "", "claudeless-bash"])
def test_a_pane_without_the_harness_gets_no_keys_and_an_inbox_point(world, pane):
    tmux = Tmux(command=pane)
    out = tick(world, START, tmux=tmux)
    assert not [c for c in tmux.calls if c[0] == "send-keys"] and "inbox point opened" in out[1]
    opened = oq.read_state("vnx-dev", state_dir=world["fleet"])["points"]
    assert len(opened) == 1 and P in next(iter(opened.values()))["vraag"]


def test_no_rotation_state_opens_an_inbox_point_and_sends_nothing(world):
    (world["sd"] / "t0_rotation" / "rotation-1" / "delivery.json").unlink()
    out = tick(world, START)
    assert world["tmux"].calls == [] and "no rotation state" in out[1]
    assert len(oq.read_state("vnx-dev", state_dir=world["fleet"])["points"]) == 1


def test_a_failed_send_opens_an_inbox_point(world):
    out = tick(world, START, tmux=Tmux(rc=1))
    assert "inbox point opened" in out[1]


def test_dry_run_writes_nothing(world):
    out = tick(world, START, dry=True)
    assert all("(dry run)" in line for line in out) and "wake @7" in out[1]
    assert not (world["sd"] / "morning_round").exists() and kinds(world["sd"]) == []
    assert not [c for c in world["tmux"].calls if c[0] == "send-keys"] and world["pushed"] == []
    assert not world["fleet"].exists()


def test_stop_is_idempotent_and_needs_a_start(world):
    assert "no round started" in mt.stop_round(P, DAY, "decided", state_dir=world["sd"], now=START)
    tick(world, START)
    assert "pause_end (decided)" in mt.stop_round(P, DAY, "decided", state_dir=world["sd"], now=START)
    assert "already stopped" in mt.stop_round(P, DAY, "deadline", state_dir=world["sd"], now=LATE)
    assert kinds(world["sd"]) == ["pause_start", "pause_end"]


def test_stop_refuses_an_unknown_reason(world):
    tick(world, START)
    with pytest.raises(ValueError):
        mt.stop_round(P, DAY, "whenever", state_dir=world["sd"], now=START)


def test_an_unreadable_event_ledger_stays_visible(world):
    (world["sd"]).mkdir(exist_ok=True)
    (world["sd"] / ms.EVENTS_FILENAME).write_text("not json\n")
    with pytest.raises(ValueError):
        tick(world, START)


def test_cli_refuses_an_unknown_project_and_a_bad_reason(tmp_path):
    reg = tmp_path / "projects.json"
    reg.write_text(json.dumps({"projects": [{"project_id": P, "data_dir": str(tmp_path)}]}))
    cli = ROOT / "scripts" / "morning_round_cli.py"
    for verb in (["tick", "--project-id", "other"], ["stop", "--project-id", "other", "--reason", "decided"]):
        done = subprocess.run([sys.executable, str(cli), *verb, "--registry", str(reg)], capture_output=True, text=True)
        assert done.returncode == 2 and "unknown project" in done.stderr
    done = subprocess.run([sys.executable, str(cli), "stop", "--reason", "whenever", "--registry", str(reg)],
                          capture_output=True, text=True)
    assert done.returncode == 2


def test_tmux_that_does_not_run_gives_an_inbox_point_in_both_steps(world):
    from tmux_adapter import AdapterTransportError

    def broken(args):
        raise AdapterTransportError("tmux did not run", transport_detail="missing")

    out = tick(world, START, tmux=broken)
    assert "inbox point opened" in out[1] and "no wake" in out[1]

    class SendBroken(Tmux):
        def __call__(self, args):
            if args[0] == "send-keys":
                raise AdapterTransportError("tmux did not run", transport_detail="timeout")
            return super().__call__(args)

    assert mt.wake("w:0", P, "2026-11-20", SendBroken()).startswith("tmux send-keys did not run")


def test_run_tmux_is_the_adapter_primitive():
    import tmux_adapter
    assert mt.run_tmux is tmux_adapter.run_tmux
