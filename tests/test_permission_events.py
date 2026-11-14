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
    args = dict(project_id=PID, session_id="s1", ts=ts, account="vnx", origin="worker",
                bron="B1", besluit="refused_config", command=command)
    args.update(kw)
    return pe.build_event(**args)


def lines(tmp_path):
    return (tmp_path / "permission_events.ndjson").read_bytes()


def test_event_id_is_idempotent(tmp_path):
    ev = make()
    assert ev["event_id"] == make()["event_id"] and len(ev["event_id"]) == 16
    assert pe.write_event(ev, PID, tmp_path) is True
    assert pe.write_event(make(), PID, tmp_path) is False
    assert lines(tmp_path).count(b"\n") == 1
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
])
def test_normalisation_forms(raw, expected):
    assert pe.normalise(raw) == expected
    assert pe.normalise(expected) == expected  # idempotent


def test_leading_value_uses_shell_grammar():
    assert pe.normalise("cd 'a && b' && ls") == "ls"
    assert pe.normalise("X='a b' ls") == "ls"


@pytest.mark.parametrize("name", list(FIXTURES))
def test_secret_never_reaches_the_file(tmp_path, name):
    text, secret = FIXTURES[name]
    pe.write_event(make(command=text, reason_text=text, dispatch_id="x"), PID, tmp_path)
    assert secret.encode() not in lines(tmp_path)


def test_reason_is_code_plus_120_chars():
    ev = make(reason_code="rc_x", reason_text="y" * 300)
    assert ev["reden"] == "rc_x " + "y" * 120


def test_short_write_ends_complete(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:7]))
    evs = [make(ts=f"2026-11-14T10:00:0{i}+00:00") for i in range(3)]
    assert pe.write_events(evs, PID, tmp_path) == 3
    monkeypatch.undo()
    assert [json.loads(l)["event_id"] for l in lines(tmp_path).splitlines()] == [e["event_id"] for e in evs]


def test_zero_write_raises_and_restores_length(tmp_path, monkeypatch):
    pe.write_event(make(), PID, tmp_path)
    before = lines(tmp_path)
    real = os.write
    calls = []

    def flaky(fd, data):
        calls.append(1)
        return real(fd, bytes(data)[:5]) if len(calls) == 1 else 0

    monkeypatch.setattr(os, "write", flaky)
    with pytest.raises(OSError):
        pe.write_event(make(ts="2026-11-14T11:00:00+00:00"), PID, tmp_path)
    monkeypatch.undo()
    assert lines(tmp_path) == before


def test_write_error_restores_length(tmp_path, monkeypatch):
    pe.write_event(make(), PID, tmp_path)
    before = lines(tmp_path)

    def boom(fd):
        raise OSError("disk")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        pe.write_event(make(ts="2026-11-14T11:00:00+00:00"), PID, tmp_path)
    monkeypatch.undo()
    assert lines(tmp_path) == before


def test_torn_tail_gets_its_own_newline(tmp_path):
    first = make()
    pe.write_event(first, PID, tmp_path)
    path = tmp_path / "permission_events.ndjson"
    path.write_bytes(path.read_bytes() + b'{"event_id": "abc')
    second = make(ts="2026-11-14T12:00:00+00:00")
    assert pe.write_event(second, PID, tmp_path) is True
    raw = path.read_bytes()
    assert b'{"event_id": "abc\n{' in raw  # torn bytes stay, the new event starts a line
    good = []
    for line in raw.splitlines():
        try:
            good.append(json.loads(line)["event_id"])
        except ValueError:
            pass
    assert good == [first["event_id"], second["event_id"]]


def test_unparseable_lines_counted_without_content(tmp_path, capsys):
    path = tmp_path / "permission_events.ndjson"
    path.write_bytes(b'not json TOPSECRETWORD\n{"no_id": 1}\n[1]\n\n')
    assert pe.write_event(make(), PID, tmp_path) is True
    err = capsys.readouterr().err
    assert "3 unparseable" in err and "TOPSECRETWORD" not in err
    cov = json.loads((tmp_path / "permission_coverage.json").read_text())
    assert cov["unparseable_lines"] == 3 and "TOPSECRETWORD" not in json.dumps(cov)
    pe.write_coverage(PID, "B1", state="ok", files_read=2, window="7d", state_dir=tmp_path)
    cov = json.loads((tmp_path / "permission_coverage.json").read_text())
    assert cov["unparseable_lines"] == 3 and cov["sources"]["B1"]["files_read"] == 2


@pytest.mark.parametrize("pid", ["other-project", "", None])
def test_foreign_or_missing_project_id_refused(tmp_path, pid):
    ev = make()
    ev["project_id"] = pid
    with pytest.raises(ValueError):
        pe.write_event(ev, PID, tmp_path)
    assert not (tmp_path / "permission_events.ndjson").exists()


@pytest.mark.parametrize("field,value", [("account", "root"), ("origin", "x"), ("bron", "B9"), ("besluit", "maybe")])
def test_closed_lists_enforced(tmp_path, field, value):
    ev = make()
    ev[field] = value
    with pytest.raises(ValueError):
        pe.write_event(ev, PID, tmp_path)


def test_coverage_unknown_is_never_zero(tmp_path):
    with pytest.raises(ValueError):
        pe.write_coverage(PID, "B2", state="unknown", files_read=0, count=0, window="7d", state_dir=tmp_path)
    entry = pe.write_coverage(PID, "B2", state="unknown", files_read=0, files_unreadable=2,
                              unreadable_reason="no access", window="7d", state_dir=tmp_path)
    assert entry["count"] is None and entry["files_unreadable"] == 2
