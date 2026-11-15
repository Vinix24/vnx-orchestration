"""permission_events: schema, event_id, normalisation, masking and the append-only writer."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import permission_events as pe
from test_secret_patterns import FIXTURES

PID = "vnx-dev"
TS = "2026-11-14T10:00:00+00:00"
def make(command="git status", ts=TS, **kw):
    args = dict(project_id=PID, session_id="s1", ts=ts, account="vnx", origin="worker", bron="B1", besluit="refused_config", command=command)
    return pe.build_event(**{**args, **kw})

def lines(tmp_path):
    return (tmp_path / "permission_events.ndjson").read_bytes()
def ids(tmp_path):  # a torn line (no closing brace) is skipped
    return [json.loads(line)["event_id"] for line in lines(tmp_path).splitlines() if line.endswith(b"}")]

def test_event_id_is_idempotent(tmp_path):
    ev = make()
    assert ev["event_id"] == make()["event_id"] and len(ev["event_id"]) == 16
    assert pe.write_event(ev, PID, tmp_path) is True
    assert pe.write_event(make(), PID, tmp_path) is False and lines(tmp_path).count(b"\n") == 1
    assert make(ts="2026-11-14T10:00:01+00:00")["event_id"] != ev["event_id"]

@pytest.mark.parametrize("raw,expected", [
    ("cd /tmp/x && pytest tests/", "pytest tests/"),
    ("cd '/a b/c' && FOO=1 BAR=\"x y\" make test", "make test"),
    ("FOO=bar BAZ=1 git status", "git status"),
    ("cat <<'EOF' > f\nhello 42\nEOF", "cat <<heredoc"),
    ("git show dlv-3833c5ee7e0f-r2", "git show <dlv>"),
    ("git show 4ccca498d66146", "git show <sha>"),
    ("gh pr view 2863", "gh pr view <n>"),
    ("see PR 2863 and pulls/12", "see <pr> and <pr>"),
    ("gh pr merge #2863", "gh pr merge <pr>"),
    ("ls /private/tmp/claude-501/scratchpad/out", "ls <scratchpad>"),
    ("cp x /tmp/work/y", "cp x <tmp>"),
    ("tail ~/.vnx-data/vnx-dev/state/a.json", "tail <vnx-data>"),
    ("sleep 30", "sleep <n>"),
    ("grep -n x <<< foo", "grep -n x <<< foo"),
    ("cd 'a && b' && ls", "ls"),
    ("X='a b' ls", "ls"),
])
def test_normalisation_forms(raw, expected):
    assert pe.normalise(raw) == expected
    assert pe.normalise(expected) == expected  # idempotent

@pytest.mark.parametrize("name", list(FIXTURES))
def test_secret_never_reaches_the_file(tmp_path, name):
    text, secret = FIXTURES[name]
    pe.write_event(make(command=text, reason_text=text, dispatch_id="x"), PID, tmp_path)
    assert secret.encode() not in lines(tmp_path)

def test_reason_is_code_plus_120_chars():
    assert make(reason_code="rc_x", reason_text="y" * 300)["reden"] == "rc_x " + "y" * 120

def test_short_write_ends_complete(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:7]))
    evs = [make(ts=f"2026-11-14T10:00:0{i}+00:00") for i in range(3)]
    assert pe.write_events(evs, PID, tmp_path) == 3
    monkeypatch.undo()
    assert ids(tmp_path) == [e["event_id"] for e in evs]

def _fail(*_, **__):
    raise OSError("disk")
def _zero_after_partial():
    real, n = os.write, []
    return lambda fd, data: real(fd, bytes(data)[:5]) if not n.append(1) and len(n) == 1 else 0

@pytest.mark.parametrize("target,fake", [("write", _zero_after_partial()), ("fsync", _fail)])
def test_failed_write_raises_and_restores_length(tmp_path, monkeypatch, target, fake):
    pe.write_event(make(), PID, tmp_path)
    before = lines(tmp_path)
    monkeypatch.setattr(os, target, fake)
    with pytest.raises(OSError):
        pe.write_event(make(ts="2026-11-14T11:00:00+00:00"), PID, tmp_path)
    monkeypatch.undo()
    assert lines(tmp_path) == before

def test_torn_tail_gets_its_own_newline(tmp_path):
    first, second = make(), make(ts="2026-11-14T12:00:00+00:00")
    pe.write_event(first, PID, tmp_path)
    path = tmp_path / "permission_events.ndjson"
    path.write_bytes(path.read_bytes() + b'{"event_id": "abc')
    assert pe.write_event(second, PID, tmp_path) is True
    assert b'{"event_id": "abc\n{' in path.read_bytes()  # torn bytes stay, the new event starts a line
    assert ids(tmp_path) == [first["event_id"], second["event_id"]]

def test_unparseable_lines_counted_without_content(tmp_path, capsys):
    path = tmp_path / "permission_events.ndjson"
    path.write_bytes(b'not json TOPSECRETWORD\n{"no_id": 1}\n[1]\n\n')
    assert pe.write_event(make(), PID, tmp_path) is True
    err = capsys.readouterr().err
    assert "3 unparseable" in err and "TOPSECRETWORD" not in err
    cov_file = tmp_path / "permission_coverage.json"
    assert json.loads(cov_file.read_text())["unparseable_lines"] == 3 and "TOPSECRETWORD" not in cov_file.read_text()
    _record(tmp_path, "B1")
    assert json.loads(cov_file.read_text())["unparseable_lines"] == 3 and _sources(tmp_path) == ["B1"]

@pytest.mark.parametrize("field,value", [("project_id", "other-project"), ("project_id", ""), ("project_id", None),
                                         ("account", "root"), ("origin", "x"), ("bron", "B9"), ("besluit", "maybe")])
def test_foreign_missing_or_unlisted_field_refused(tmp_path, field, value):
    with pytest.raises(ValueError):
        pe.write_event({**make(), field: value}, PID, tmp_path)
    assert not (tmp_path / "permission_events.ndjson").exists()

def test_coverage_unknown_is_never_zero(tmp_path):
    with pytest.raises(ValueError):
        pe.write_coverage(PID, "B2", state="unknown", files_read=0, count=0, window="7d", state_dir=tmp_path)
    entry = pe.write_coverage(PID, "B2", state="unknown", files_read=0, files_unreadable=2, unreadable_reason="x", window="7d", state_dir=tmp_path)
    assert _sources(tmp_path) == ["B2"]  # a missing coverage file starts empty
    assert entry["count"] is None and entry["files_unreadable"] == 2

def _record(tmp_path, source):
    pe.write_coverage(PID, source, state="ok", files_read=1, window="7d", state_dir=tmp_path)
def _sources(tmp_path):
    return list(json.loads((tmp_path / "permission_coverage.json").read_text())["sources"])

def test_coverage_read_oserror_keeps_old_sources(tmp_path, monkeypatch, capsys):
    _record(tmp_path, "B1")
    monkeypatch.setattr(Path, "read_text", _fail)
    with pytest.raises(OSError):
        _record(tmp_path, "B2")
    monkeypatch.undo()
    assert "permission_coverage.json" in capsys.readouterr().err and _sources(tmp_path) == ["B1"]

@pytest.mark.parametrize("content", [
    "{not json", "[]", "null", '{"schema": "other", "sources": {}}', '{"schema": "S"}', '{"schema": "S", "sources": []}',
    '{"schema": "S", "sources": {}, "unparseable_lines": "x"}'])
def test_malformed_coverage_is_not_overwritten(tmp_path, content, capsys):
    content = content.replace('"S"', json.dumps(pe.COVERAGE_SCHEMA))
    (tmp_path / "permission_coverage.json").write_text(content)
    with pytest.raises((ValueError, KeyError, TypeError)):
        _record(tmp_path, "B2")
    assert "permission_coverage.json" in capsys.readouterr().err
    assert (tmp_path / "permission_coverage.json").read_text() == content

BAD_IDS = ["../other-project", "/tmp/x", "a/b", "", "Global", "vnx-dev\n", "vnx-dev/../x", None]
ENTRY_POINTS = {
    "write_event": lambda pid, d: pe.write_event({**make(), "project_id": pid}, pid, d),
    "write_events": lambda pid, d: pe.write_events([], pid, d),
    "write_coverage": lambda pid, d: pe.write_coverage(pid, "B1", state="ok", files_read=1, window="7d", state_dir=d),
}
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return tmp_path / "home" / ".vnx-data"
@pytest.mark.parametrize("explicit", [True, False])
@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
@pytest.mark.parametrize("pid", BAD_IDS)
def test_non_fleet_project_id_refused_before_any_file(tmp_path, home, monkeypatch, pid, entry, explicit):
    monkeypatch.setattr(pe, "_locked", _fail)  # nothing may reach the lock, the ledger or the coverage file
    with pytest.raises(ValueError, match="not a fleet project id"):
        ENTRY_POINTS[entry](pid, tmp_path / "s" if explicit else None)
    assert not (tmp_path / "s").exists() and not (tmp_path / "home").exists()
@pytest.mark.parametrize("entry", list(ENTRY_POINTS))
def test_central_dir_must_stay_under_the_central_root(tmp_path, home, entry):
    ENTRY_POINTS[entry](PID, None)
    assert (home / PID / "state" / "permission_events.lock").exists()
    (tmp_path / "outside").mkdir()
    (home / "sales-copilot").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="outside the central store"):
        ENTRY_POINTS[entry]("sales-copilot", None)
    assert list((tmp_path / "outside").iterdir()) == []


@pytest.mark.parametrize("raw", [
    "cd /tmp/x && pytest tests/", "cd '/a b/c' && FOO=1 BAR=\"x y\" make test",
    "cat <<'EOF' > f\nhello 42\nEOF", "git show dlv-3833c5ee7e0f-r2",
    "ls /private/tmp/claude-501/scratchpad/out", "cp x /tmp/work/y",
    "tail ~/.vnx-data/vnx-dev/state/a.json", "cd 'a && b' && ls", "X='a b' ls",
    "see PR 2863 and pulls/12",
])
def test_normalisation_forms_are_idempotent(raw):
    assert pe.normalise(pe.normalise(raw)) == pe.normalise(raw)
