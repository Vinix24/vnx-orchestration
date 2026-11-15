"""besluitenregister B4: queue answers become decisions, the index block, `vnx inbox answer`. tmp_path only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT / "scripts" / "lib", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import besluiten_adapter as ba
import inbox_cli
import operator_queue as oq
from besluitenregister.lezer import lees
from besluitenregister.schrijver import Register

PID = "proj"
SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


@pytest.fixture
def env(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "log").mkdir()
    return tmp_path / "state", tmp_path / "log"


def _ask(state, ref, quote=None, day="2026-11-15T08:00:00Z"):
    pid = oq.open_point(PID, bron=8, ref=f"antwoord:{ref}", vraag=f"Vraag {ref}?", sluit_bij="x", opened_by="t",
                        now=day, asked_at=day, state_dir=state)["point_id"]
    if quote:
        oq.append_line(PID, "answer", {"point_id": pid, "citaat": quote, "vastgelegd_door": "T0"}, now=day, state_dir=state)
    return pid


def _lines(log):
    return [r for _, r in lees(log / "besluiten.ndjson").regels]


def test_repeat_writes_once(env):
    state, log = env
    pid = _ask(state, "a", "Ja, doen.")
    first = ba.sync_answers(PID, log, state_dir=state)
    again = ba.sync_answers(PID, log, state_dir=state)
    assert (first["written"], first["present"], again["written"], again["present"]) == (1, 0, 0, 1)
    (r,) = _lines(log)
    assert r["besloten_door"] == "operator" and r["vastgelegd_door"] == "agent:operator-queue"
    assert r["bron"] == {"soort": "chat", "ref": pid, "citaat": "Ja, doen."} and r["besloten_op"] == "2026-11-15"


def test_crash_between_writes_is_caught_by_next_sync(env, monkeypatch):
    state, log = env
    _ask(state, "a", "Een.")
    _ask(state, "b", "Twee.")
    real, calls = Register.schrijf, []

    def flaky(self, *v):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("crash")
        return real(self, *v)

    monkeypatch.setattr(Register, "schrijf", flaky)
    with pytest.raises(OSError):
        ba.sync_answers(PID, log, state_dir=state)
    assert len(_lines(log)) == 1
    res = ba.sync_answers(PID, log, state_dir=state)
    assert (res["written"], res["present"]) == (1, 1) and len(_lines(log)) == 2


def test_torn_tail_is_quarantined(env):
    state, log = env
    _ask(state, "a", "Een.")
    (log / "besluiten.ndjson").write_bytes(b'{"half":')
    assert ba.sync_answers(PID, log, state_dir=state)["written"] == 1
    assert len(_lines(log)) == 1 and (log / "besluiten.ndjson.afgescheurd").exists()


def test_secret_is_refused_without_value_and_others_written(env, capsys):
    state, log = env
    bad = _ask(state, "a")
    oq._append(state, {"kind": "answer", "project_id": PID, "ts": "2026-11-15T09:00:00Z", "point_id": bad,
                       "citaat": SECRET, "vastgelegd_door": "T0"})
    _ask(state, "b", "Goed.")
    res = ba.sync_answers(PID, log, state_dir=state)
    assert res["written"] == 1 and [p for p, _ in res["refused"]] == [bad]
    assert SECRET not in repr(res) and SECRET not in (log / "besluiten.ndjson").read_text()


def test_point_closed_through_source_writes_nothing(env):
    state, log = env
    _ask(state, "a")
    assert ba.sync_answers(PID, log, state_dir=state) == {"written": 0, "present": 0, "refused": [], "ids": {}}
    assert not (log / "besluiten.ndjson").exists()


def test_unreadable_queue_raises(env):
    state, log = env
    _ask(state, "a", "x.")
    with open(state / oq.LEDGER_FILENAME, "a") as f:
        f.write("niet json\n")
    with pytest.raises(oq.QueueUnreadable):
        ba.sync_answers(PID, log, state_dir=state)


def test_index_block(env):
    state, log = env
    assert ba.index_block(PID, log, state_dir=state) == {"status": "absent"}
    a, b = _ask(state, "a", "Een."), _ask(state, "b", "Twee.")
    ba.sync_answers(PID, log, state_dir=state)
    reg = Register(log)
    reg.schrijf({"event": "herzien", "betreft": _lines(log)[0]["besluit_id"], "context": "vnx-orchestration",
                 "besloten_op": "2026-11-15", "vastgelegd_door": "operator", "waarom": "later", "herzien_op": "2026-11-20"})
    c = _ask(state, "c", "Drie.")
    blok = ba.index_block(PID, log, state_dir=state, nu="2026-11-20")
    assert blok["status"] == "ok" and blok["geldig"] == 2 and blok["te_herzien"] == [_lines(log)[0]["besluit_id"]]
    assert blok["niet_vastgelegd"] == [c] and blok["afgescheurd"] == 0 and blok["ongeldig"] == 0
    assert len(blok["recent"]) == 2 and set(blok["recent"][0]) == {"id", "titel", "besloten_op"}
    assert ba.index_block(PID, log, state_dir=state, nu="2026-11-19")["te_herzien"] == []


def test_index_block_error(env):
    state, log = env
    (log / "besluiten.ndjson").mkdir()
    blok = ba.index_block(PID, log, state_dir=state)
    assert blok["status"] == "error" and blok["reason"]


def test_inbox_answer_prints_decision(env, capsys):
    state, log = env
    pid = _ask(state, "a")
    rc = inbox_cli.main(["--project-id", PID, "--state-dir", str(state), "--logdir", str(log), "answer", pid,
                         "--quote", "Akkoord."])
    out = capsys.readouterr().out
    assert rc == 0 and f"decision B-20" in out and f"in {log}/besluiten.ndjson" in out
    assert len(_lines(log)) == 1
