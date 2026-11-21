"""besluitenregister B4: queue answers become decisions, the index block, `vnx inbox answer`. tmp_path only."""
from __future__ import annotations

import json
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
NU = "2026-11-20"
SECRET = "sk-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


def populate_register(logdir: Path, n: int = 60, herzien: int = 8) -> None:
    logdir.mkdir(parents=True, exist_ok=True)
    reg, ids = Register(logdir), []
    for i in range(n):
        titel = f"Besluit {i:03d} " + "x" * 89  # the writer allows 100
        ans = {"citaat": f"Akkoord {i}.", "ts": f"2026-11-{1 + i % 14:02d}T08:00:00Z", "point_id": f"pt-{i:03d}"}
        ids.append(reg.schrijf(ba._velden("vnx-orchestration", titel, ans)).besluit_id)
    for bid in ids[:herzien]:
        reg.schrijf({"event": "herzien", "betreft": bid, "context": "vnx-orchestration", "besloten_op": "2026-11-15",
                     "vastgelegd_door": "operator", "waarom": "later", "herzien_op": "2026-11-18"})


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
    assert ba.index_block(PID, log, state_dir=state)["status"] == "absent"
    a, b = _ask(state, "a", "Een."), _ask(state, "b", "Twee.")
    ba.sync_answers(PID, log, state_dir=state)
    reg = Register(log)
    reg.schrijf({"event": "herzien", "betreft": _lines(log)[0]["besluit_id"], "context": "vnx-orchestration",
                 "besloten_op": "2026-11-15", "vastgelegd_door": "operator", "waarom": "later", "herzien_op": "2026-11-20"})
    c = _ask(state, "c", "Drie.")
    blok = ba.index_block(PID, log, state_dir=state, nu="2026-11-20")
    assert blok["status"] == "ok" and blok["geldig"] == 2 and blok["te_herzien_ids"] == [_lines(log)[0]["besluit_id"]]
    assert (blok["te_herzien"], blok["niet_vastgelegd"], blok["niet_vastgelegd_ids"]) == (1, 1, [c])
    assert "afgescheurd" not in blok and "ongeldig" not in blok and len(blok["laatste"]) == 2
    assert ba.index_block(PID, log, state_dir=state, nu="2026-11-19")["te_herzien_ids"] == []
    assert set(ba.state_block(PID, log, state_dir=state, nu=NU)["recent"][0]) == {"id", "titel", "besloten_op"}


def test_index_block_is_compact_for_a_big_register(env):
    state, log = env
    populate_register(log, 60, 8)
    for i in range(20):
        _ask(state, f"gat{i}", f"Open {i}.")
    blok = ba.index_block(PID, log, state_dir=state, nu=NU)
    assert len(json.dumps(blok).encode()) <= 400 and blok["geldig"] == 60 and blok["te_herzien"] == 8 and blok["niet_vastgelegd"] == 20
    assert len(blok["te_herzien_ids"]) == len(blok["niet_vastgelegd_ids"]) == 3 and len(blok["laatste"]) == 5


def test_index_block_error(env):
    state, log = env
    (log / "besluiten.ndjson").mkdir()
    blok = ba.index_block(PID, log, state_dir=state)
    assert blok["status"] == "error" and blok["reason"]


def test_fresh_project_creates_the_folder_and_records_the_decision(tmp_path):
    state, log = tmp_path / "state", tmp_path / "fresh" / "daily-log"
    state.mkdir()
    _ask(state, "a", "Ja.")
    assert ba.index_block(PID, log, state_dir=state)["niet_vastgelegd"] == 1
    inbox_cli.main(["--project-id", PID, "--state-dir", str(state), "--logdir", str(log), "answer", _ask(state, "b"), "--quote", "Nee."])
    blok = ba.index_block(PID, log, state_dir=state)
    assert len(_lines(log)) == 2 and blok["status"] == "ok" and blok["niet_vastgelegd"] == 0


def test_a_failed_write_shows_in_niet_vastgelegd(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "blocked").write_text("a file, not a folder")
    log = tmp_path / "blocked" / "daily-log"
    _ask(state, "a", "Ja.")
    with pytest.raises(OSError):
        ba.sync_answers(PID, log, state_dir=state)
    blok = ba.index_block(PID, log, state_dir=state)
    assert blok["status"] == "absent" and blok["niet_vastgelegd"] == 1


def test_tests_never_resolve_the_real_register(monkeypatch):
    """The conftest isolation redirects the checkout's daily-log: no register path under it reaches the reader."""
    import build_t0_state
    real = build_t0_state._besluiten_logdir().resolve()
    assert real.name == "daily-log"
    seen = []

    class _Spy(type(Path())):
        def exists(self, *args, **kwargs):
            seen.append(Path(self))
            return super().exists(*args, **kwargs)

    monkeypatch.setattr(ba, "Path", _Spy)
    block = build_t0_state._build_besluiten(PID, Path("/nonexistent-state-dir"))
    assert seen, "the register reader was never reached"
    for pad in seen:
        assert pad.name == "besluiten.ndjson"
        assert real not in pad.resolve().parents, f"real register touched: {pad}"
    assert block["status"] in ("absent", "ok", "error")


def test_inbox_answer_prints_decision(env, capsys):
    state, log = env
    pid = _ask(state, "a")
    rc = inbox_cli.main(["--project-id", PID, "--state-dir", str(state), "--logdir", str(log), "answer", pid,
                         "--quote", "Akkoord."])
    out = capsys.readouterr().out
    assert rc == 0 and f"decision B-20" in out and f"in {log}/besluiten.ndjson" in out
    assert len(_lines(log)) == 1
