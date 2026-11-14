"""ADR-078 M2 test duties: binding refusals and the observed memory ledger."""
import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import memory_ledger as ml

BODY, DESC = "SECRET-BODY-TEXT-7731", "SECRET-DESC-TEXT-9912"


def note(meta="type: feedback", body=BODY):
    return "---\nname: n\ndescription: %s\n%s\n---\n%s\n" % (DESC, meta, body)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mem = tmp_path / ".claude" / "projects" / "-proj" / "memory"
    mem.mkdir(parents=True)
    state = tmp_path / ".vnx-data" / "proj" / "state"
    state.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [a](a.md)\n")
    (mem / "a.md").write_text(note("metadata:\n  type: user\n  node_type: lesson"))
    (mem / "b.md").write_text(note("type: project\noriginSessionId: abc-123"))
    return SimpleNamespace(mem=mem, state=state, home=tmp_path)


def bound(env):
    ml.bind(env.state, str(env.mem), "proj")
    return env


def lines(env):
    return ml.read_ledger(env.state / ml.LEDGER_FILE)[0]


def test_unbound_writes_nothing(env):
    assert ml.scan(env.state)["status"] == "unbound"
    assert list(env.state.iterdir()) == []


def test_bind_refuses_outside_tree_and_missing_dir(env, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.md").write_text("x")
    for target in (other, tmp_path / "nope", ml.projects_root()):
        with pytest.raises(ml.BindRefused):
            ml.bind(env.state, str(target), "proj")


def test_bind_refuses_symlink_leaving_the_tree(env, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "x.md").write_text("x")
    link = env.mem.parent.parent / "link"
    link.symlink_to(other)
    with pytest.raises(ml.BindRefused):
        ml.bind(env.state, str(link), "proj")


def test_bind_refuses_index_only_folder(env):
    only = env.mem.parent.parent / "-terminal"
    only.mkdir()
    (only / "MEMORY.md").write_text("i")
    with pytest.raises(ml.BindRefused):
        ml.bind(env.state, str(only), "proj")


def test_bind_refuses_client_root_in_both_encodings(env, tmp_path):
    root = tmp_path / "Business" / "clients"
    boundary = SimpleNamespace(configured=True, client_roots=(root,), personal_roots=(),
                               client_project_ids=frozenset({"client-p"}))
    for name in (str(root).replace("/", "-") + "-acme", str(root).replace("/", "-") + "-x_y"):
        folder = env.mem.parent.parent / name
        folder.mkdir()
        (folder / "n.md").write_text("n")
        with pytest.raises(ml.BindRefused):
            ml.bind(env.state, str(folder), "proj", boundary=boundary)
        ml.bind(env.state, str(folder), "client-p", boundary=boundary)


def test_bind_refuses_folder_of_another_project(env):
    other = env.home / ".vnx-data" / "other" / "state"
    other.mkdir(parents=True)
    ml.bind(other, str(env.mem), "other")
    with pytest.raises(ml.BindRefused):
        ml.bind(env.state, str(env.mem), "proj")


def test_bind_records_old_and_new_list_and_dedups(env):
    sent = []

    def emit(_file, **kw):
        sent.append(kw["trigger"])
    ml.bind(env.state, str(env.mem), "proj", emit=emit)
    doc = ml.bind(env.state, str(env.mem), "proj", emit=emit)
    assert doc["memory_dirs"] == [str(env.mem.resolve())]
    assert len(sent) == 2 and "old=[]" in sent[0]


def test_added_for_every_note_and_second_scan_writes_zero(env):
    bound(env)
    assert ml.scan(env.state)["written"] == 3
    assert {r["note"] for r in lines(env)} == {"MEMORY.md", "a.md", "b.md"}
    assert {r["event"] for r in lines(env)} == {"added"}
    assert ml.scan(env.state)["written"] == 0


def test_change_gives_one_changed_with_prev_hash(env):
    bound(env)
    ml.scan(env.state)
    old = [r for r in lines(env) if r["note"] == "a.md"][0]["sha256"]
    (env.mem / "a.md").write_text(note("metadata:\n  type: user\n  node_type: lesson", "new"))
    assert ml.scan(env.state)["written"] == 1
    last = lines(env)[-1]
    assert (last["event"], last["note"], last["prev_sha256"]) == ("changed", "a.md", old)


def test_validity_change_replaces_changed(env):
    bound(env)
    ml.scan(env.state)
    (env.mem / "b.md").write_text(note("type: project\ninvalid_at: 2026-12-01"))
    assert ml.scan(env.state)["written"] == 1
    assert lines(env)[-1]["event"] == "validity_changed"
    assert lines(env)[-1]["invalid_at"] == "2026-12-01"


def test_removed_then_added_again(env):
    bound(env)
    ml.scan(env.state)
    (env.mem / "b.md").unlink()
    ml.scan(env.state)
    assert lines(env)[-1]["event"] == "removed" and ml.scan(env.state)["written"] == 0
    (env.mem / "b.md").write_text(note())
    ml.scan(env.state)
    assert lines(env)[-1]["event"] == "added"


def test_unreadable_after_change_during_read_even_on_retry(env, monkeypatch):
    bound(env)
    real = os.stat(env.mem / "a.md")
    other = SimpleNamespace(st_size=real.st_size + 1, st_mtime_ns=real.st_mtime_ns)
    monkeypatch.setattr(ml, "_read_once", lambda p: (b"x", real, other))
    ml.scan(env.state)
    rec = lines(env)[0]
    assert (rec["event"], rec["reason"]) == ("unreadable", "changed_during_read")
    assert not any(r["event"] == "added" for r in lines(env))


def test_retry_succeeds_when_second_read_is_stable(env, monkeypatch):
    bound(env)
    real, calls = os.stat(env.mem / "a.md"), []
    drift = SimpleNamespace(st_size=real.st_size + 1, st_mtime_ns=real.st_mtime_ns)

    def flaky(_path):
        calls.append(1)
        return (b"x", real, drift if len(calls) == 1 else real)
    monkeypatch.setattr(ml, "_read_once", flaky)
    assert ml.scan(env.state)["written"] == 3


def test_torn_tail_set_apart(env):
    bound(env)
    ml.scan(env.state)
    ledger = env.state / ml.LEDGER_FILE
    good = ledger.read_bytes()
    ledger.write_bytes(good + b'{"half":')
    (env.mem / "c.md").write_text(note())
    ml.scan(env.state)
    side = json.loads((env.state / (ml.LEDGER_FILE + ".afgescheurd")).read_text())
    assert base64.b64decode(side["bytes_b64"]) == b'{"half":' and side["offset"] == len(good)
    assert ledger.read_bytes().startswith(good) and ml.read_ledger(ledger)[1:] == (0, [])


def test_reader_counts_invalid_lines_and_torn_tail(env):
    ledger = env.state / ml.LEDGER_FILE
    ledger.write_bytes(b'{"schema":"x"}\n{"torn')
    assert ml.read_ledger(ledger) == ([], 1, [1])


def test_partial_when_budget_is_spent(env):
    bound(env)
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0])
    result = ml.scan(env.state, budget=2.0, clock=lambda: next(ticks))
    assert result["status"] == "partial"
    part = lines(env)[-1]
    assert (part["event"], part["reason"], part["processed"], part["total"]) == ("partial", "budget", 1, 3)
    assert ml.scan(env.state)["written"] == 2  # resumes: only the unseen notes


def test_absent_and_unparseable_frontmatter_give_unknown(env):
    (env.mem / "c.md").write_text("no block\n")
    (env.mem / "d.md").write_text("---\ndescription: a: b: c\n---\nx\n")
    bound(env)
    ml.scan(env.state)
    by = {r["note"]: r for r in lines(env)}
    for name, fm in (("c.md", "absent"), ("d.md", "unparseable")):
        r = by[name]
        assert r["frontmatter"] == fm
        assert {r[k] for k in ("type", "node_type", "origin_session", "valid_from", "invalid_at",
                               "superseded_by")} == {"unknown"}


def test_metadata_field_beats_top_level(env):
    (env.mem / "c.md").write_text(note("type: top\nmetadata:\n  type: nested\n  originSessionId: s-1"))
    (env.mem / "d.md").write_text(note("metadata: plain\ntype: top\noriginSessionId: s-2"))
    bound(env)
    ml.scan(env.state)
    by = {r["note"]: r for r in lines(env)}
    assert (by["c.md"]["type"], by["c.md"]["origin_session"]) == ("nested", "s-1")
    assert (by["d.md"]["type"], by["d.md"]["origin_session"]) == ("top", "s-2")
    assert by["b.md"]["origin_session"] == "abc-123"


def test_ledger_never_holds_note_text_nor_empty_or_zero_fields(env):
    (env.mem / "c.md").write_text(note("type: bad value with spaces"))
    bound(env)
    ml.scan(env.state)
    raw = (env.state / ml.LEDGER_FILE).read_text()
    assert BODY not in raw and DESC not in raw and "bad value" not in raw
    for rec in lines(env):
        assert all(v not in ("", 0) and (v is not None or k in ("valid_from", "invalid_at", "superseded_by"))
                   for k, v in rec.items())
    assert {r["type"] for r in lines(env) if r["note"] == "MEMORY.md"} == {"index"}


def test_writer_refuses_invalid_records():
    base = {"schema": ml.LEDGER_SCHEMA, "observed_at": "t", "project_id": "p", "event": "removed",
            "source": "s", "note": "n.md", "prev_sha256": "h"}
    ml.validate_record(base)
    for bad in ({**base, "extra": 1}, {**base, "event": "weird"}, {**base, "note": ""}):
        with pytest.raises(ValueError):
            ml.validate_record(bad)


def test_cli_scan_unbound_exits_with_reason(env, monkeypatch, capsys):
    import memory_cli
    monkeypatch.setattr(memory_cli, "resolve_state_dir", lambda: env.state)
    assert memory_cli.main(["scan"]) == 3
    assert capsys.readouterr().out.startswith("unbound")
    assert list(env.state.iterdir()) == []
