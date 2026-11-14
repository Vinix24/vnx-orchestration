"""permission_events: schema, normalisation, masking, writer, coverage (ADR-075)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import permission_events as pe
from test_secret_patterns import FIXTURES

PID = "proj-a"
TS = "2026-11-14T08:40:00Z"
SHA = "0123456789abcdef0123456789abcdef01234567"

def _event(command="bin/vnx status", **kw):
    args = dict(project_id=PID, session_id="s1", ts=TS, account="unknown", origin="t0",
                bron="B1", besluit="refused_config", command=command)
    args.update(kw)
    return pe.build_event(**args)

def _ledger(tmp_path):
    return tmp_path / "permission_events.ndjson"

@pytest.mark.parametrize("raw,expected", [
    ("python3 - <<'PY'\nprint(1)\nPY", "python3 - <<heredoc"),
    ("PY=/opt/py/python3 $PY -m pytest tests/a.py", "$PY -m pytest tests/a.py"),
    ("rm -rf /private/tmp/claude-1/x", "rm -rf <tmp>"),
    ("cat > /tmp/f.py <<'EOF'\nx\nEOF", "cat > <tmp> <<heredoc"),
    ("cat >> /tmp/f.py <<EOF\nx\nEOF", "cat >> <tmp> <<heredoc"),
    ("git stash list", "git stash list"),
    ("sed -i 's/a/b/' ~/Development/x.py", "sed -i 's/a/b/' ~/..."),
    ("grep -ln pattern /Users/me/repo/f", "grep -ln pattern ~/..."),
    ("python3.12 -m pytest -n 4 tests/x.py", "python3.12 -m pytest -n <n> tests/x.py"),
    (f"git show {SHA}", "git show <sha>"),
    ("mkdir -p /tmp/work/a", "mkdir -p <tmp>"),
    ("cd /Users/x/repo && VNX_X=1 bin/vnx deliverable promote dlv-4856e810fb14",
     "bin/vnx deliverable promote <dlv>"),
    ("ls ~/.vnx-data/p/state/x /tmp/s/scratchpad/a", "ls <vnx-data> <scratchpad>"),
    ("ls /Users/me/Development/clients/acme/f", "ls ~/..."),
])
def test_normalise_forms_and_idempotent(raw, expected):
    out = pe.normalise(raw)
    assert out == expected
    assert pe.normalise(out) == out

def test_event_id_stable_and_uses_normalised_vorm():
    a = pe.event_id(PID, "B1", "s1", TS, "bin/vnx promote dlv-4856e810fb14")
    assert a == pe.event_id(PID, "B1", "s1", TS, "bin/vnx promote <dlv>")
    assert a != pe.event_id("other", "B1", "s1", TS, "bin/vnx promote <dlv>")

def test_same_event_twice_gives_one_line_and_bytes_stay(tmp_path):
    ev = _event()
    assert pe.write_event(ev, PID, tmp_path) is True
    first = _ledger(tmp_path).read_bytes()
    assert pe.write_event(dict(ev), PID, tmp_path) is False
    assert pe.write_events([_event(), _event(ts="2026-11-14T09:00:00Z")], PID, tmp_path) == 1
    assert _ledger(tmp_path).read_bytes().startswith(first)
    assert len(first.splitlines()) == 1

@pytest.mark.parametrize("change", [
    {"project_id": "other"}, {"project_id": ""}, {"project_id": None},  # decision 1
    {"account": "a@b.nl"}, {"origin": "cron"}, {"bron": "B9"}, {"besluit": "yolo"}, {"ts": "gisteren"},
    {"event_id": "0" * 16}, {"prompt": "hi"},
])
def test_foreign_missing_open_set_and_tampered_events_refused(tmp_path, change):
    with pytest.raises(ValueError):
        pe.write_event({**_event(), **change}, PID, tmp_path)
    assert not _ledger(tmp_path).exists()

def test_missing_project_id_key_refused(tmp_path):
    ev = _event()
    del ev["project_id"]
    with pytest.raises(ValueError):
        pe.write_event(ev, PID, tmp_path)

@pytest.mark.parametrize("name", list(FIXTURES))
def test_no_secret_value_in_ledger(tmp_path, name):
    text, value = FIXTURES[name]
    pe.write_event(_event(command=text, reason_text=text, dispatch_id="d-" + text.replace("\n", " ")), PID, tmp_path)
    assert value not in _ledger(tmp_path).read_text()

def test_raw_command_and_long_reden_never_reach_disk(tmp_path):
    unique = "Qz9" * 13 + "Q"
    reden_text = "w " * 61 + unique
    raw = "TOKEN=abc123realish bin/vnx deliverable promote dlv-4856e810fb14"
    pe.write_event(_event(command=raw, reason_code="rule:cliArg", reason_text=reden_text), PID, tmp_path)
    data = _ledger(tmp_path).read_text()
    assert "abc123realish" not in data and raw not in data and unique not in data
    row = json.loads(data)
    assert row["vorm"] == "bin/vnx deliverable promote <dlv>"
    assert row["reden"].startswith("rule:cliArg ") and len(row["reden"]) <= len("rule:cliArg ") + 120

def test_unmaskable_field_becomes_placeholder(tmp_path, monkeypatch):
    ev = _event(command=object(), reason_text=object())
    assert ev["vorm"] == "<unmaskable>" and ev["reden"] == "<unmaskable>"
    monkeypatch.setattr(pe, "mask", lambda _t: (_ for _ in ()).throw(RuntimeError("x")))
    ev = _event(command="TOKEN=raw99 ls")
    assert ev["vorm"] == "<unmaskable>"
    pe.write_event(ev, PID, tmp_path)
    assert "raw99" not in _ledger(tmp_path).read_text()

def test_coverage_unreadable_source_is_unknown_with_reason(tmp_path):
    pe.write_coverage(PID, "B1", state="ok", files_read=2, window="7d", count=0, state_dir=tmp_path)
    pe.write_coverage(PID, "B2", state="unknown", files_read=0, files_unreadable=1,
                      unreadable_reason="Errno 13", window="7d", state_dir=tmp_path)
    doc = json.loads((tmp_path / "permission_coverage.json").read_text())
    assert doc["schema"] == "permission_coverage/1"
    assert doc["sources"]["B2"]["state"] == "unknown" and doc["sources"]["B2"]["count"] is None
    assert doc["sources"]["B1"]["count"] == 0
    assert not list(tmp_path.glob("*.tmp"))

@pytest.mark.parametrize("kw", [
    dict(state="unknown", files_read=2, count=0),
    dict(state="ok", files_read=0, count=0),
    dict(state="ok", files_read=1, files_unreadable=1, unreadable_reason="x"),
    dict(state="unknown", files_read=0, files_unreadable=1),
    dict(state="fine", files_read=1),
])
def test_coverage_refusals(tmp_path, kw):
    args = dict(state="ok", files_read=1, window="7d")
    args.update(kw)
    with pytest.raises(ValueError):
        pe.write_coverage(PID, "B1", state_dir=tmp_path, **args)
    assert not (tmp_path / "permission_coverage.json").exists()
