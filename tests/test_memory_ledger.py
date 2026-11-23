"""ADR-078 M2 test duties: binding refusals and the observed memory ledger."""
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import memory_ledger as ml

BODY, DESC = "SECRET-BODY-TEXT-7731", "SECRET-DESC-TEXT-9912"


def note(meta="type: feedback", body=BODY):
    return "---\nname: n\ndescription: %s\n%s\n---\n%s\n" % (DESC, meta, body)


def ok_emit(_file, **_kw):
    return SimpleNamespace(status="appended")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    real = ml.bind  # emit is required since round 4; default the tests to a persisting one
    monkeypatch.setattr(ml, "bind", lambda *a, emit=ok_emit, **k: real(*a, emit=emit, **k))
    mem = tmp_path / ".claude" / "projects" / "-proj" / "memory"
    mem.mkdir(parents=True)
    state = tmp_path / ".vnx-data" / "proj" / "state"
    state.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [a](a.md)\n")
    (mem / "a.md").write_text(note("metadata:\n  type: user\n  node_type: lesson"))
    (mem / "b.md").write_text(note("type: project\noriginSessionId: abc-123"))
    return SimpleNamespace(mem=mem, state=state, home=tmp_path)


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
        for rel in ("", "memory", "memory/sub"):
            folder = env.mem.parent.parent / name / rel
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "n.md").write_text("n")
            with pytest.raises(ml.BindRefused):
                ml.bind(env.state, str(folder), "proj", boundary=boundary)
            ml.bind(env.state, str(folder), "client-p", boundary=boundary)


def test_bind_refuses_personal_root_memory_subfolders(env, tmp_path):
    root = tmp_path / "Private" / "me.x"
    boundary = SimpleNamespace(configured=True, client_roots=(), personal_roots=(root,),
                               client_project_ids=frozenset())
    encs = (str(root).replace("/", "-"), __import__("re").sub(r"[/._]", "-", str(root)))
    for name in (encs[0], encs[1] + "-notes"):
        for rel in ("memory", "memory/sub"):
            folder = env.mem.parent.parent / name / rel
            folder.mkdir(parents=True)
            (folder / "n.md").write_text("n")
            with pytest.raises(ml.BindRefused):
                ml.bind(env.state, str(folder), "proj", boundary=boundary)


def test_bind_sibling_with_same_prefix_is_not_refused(env, tmp_path):
    root = tmp_path / "Business" / "clients"
    boundary = SimpleNamespace(configured=True, client_roots=(root,), personal_roots=(),
                               client_project_ids=frozenset())
    sibling = env.mem.parent.parent / (str(root).replace("/", "-") + "archive") / "memory"
    sibling.mkdir(parents=True)
    (sibling / "n.md").write_text("n")
    ml.bind(env.state, str(sibling), "proj", boundary=boundary)


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
        return SimpleNamespace(status="appended")
    ml.bind(env.state, str(env.mem), "proj", emit=emit)
    doc = ml.bind(env.state, str(env.mem), "proj", emit=emit)
    assert doc["memory_dirs"] == [str(env.mem.resolve())]
    assert len(sent) == 2 and "old=[]" in sent[0]


def test_two_binds_of_different_folders_both_land(env):
    second = env.mem.parent.parent / "-second"
    second.mkdir()
    (second / "n.md").write_text("n")
    ml.bind(env.state, str(env.mem), "proj")
    doc = ml.bind(env.state, str(second), "proj")
    assert doc["memory_dirs"] == [str(env.mem.resolve()), str(second.resolve())]
    assert ml.load_sources(env.state)[0]["memory_dirs"] == doc["memory_dirs"]


def _raise(*_a, **_k):
    raise RuntimeError("receipt store down")


@pytest.mark.parametrize("emit", [lambda *a, **k: None, _raise,
                                  lambda *a, **k: SimpleNamespace(status="refused"),
                                  lambda *a, **k: SimpleNamespace()])
def test_receipt_not_persisted_leaves_binding_unchanged(env, emit):
    target = env.state / ml.SOURCES_FILE
    with pytest.raises(ml.BindRefused, match="receipt not persisted"):
        ml.bind(env.state, str(env.mem), "proj", emit=emit)
    assert not target.exists()
    second = env.mem.parent.parent / "-second"
    second.mkdir()
    (second / "n.md").write_text("n")
    ml.bind(env.state, str(env.mem), "proj")
    before = target.read_bytes()
    with pytest.raises(ml.BindRefused, match="receipt not persisted"):
        ml.bind(env.state, str(second), "proj", emit=emit)
    assert target.read_bytes() == before


def test_receipt_is_emitted_before_the_binding_is_written(env):
    seen = []

    def emit(file, **_kw):
        seen.append(str(env.mem.resolve()) in Path(file).read_text() if Path(file).exists() else False)
        return SimpleNamespace(status="duplicate")
    ml.bind(env.state, str(env.mem), "proj", emit=emit)
    assert seen == [False]
    assert ml.load_sources(env.state)[0]["memory_dirs"] == [str(env.mem.resolve())]


def test_bind_without_emitter_refuses(env):
    with pytest.raises(ml.BindRefused):
        ml.bind(env.state, str(env.mem), "proj", emit=None)
    assert not (env.state / ml.SOURCES_FILE).exists()


@pytest.mark.parametrize("a_first", [True, False])
def test_uniqueness_across_repo_local_and_central_stores(env, a_first):
    local_a = env.home / "repoA" / ".vnx-data" / "state"
    central_b = env.home / ".vnx-data" / "B" / "state"
    first, second = ((local_a, "A"), (central_b, "B")) if a_first else ((central_b, "B"), (local_a, "A"))
    ml.bind(first[0], str(env.mem), first[1])
    with pytest.raises(ml.BindRefused, match="already bound to another project"):
        ml.bind(second[0], str(env.mem), second[1])


def test_concurrent_binds_of_one_folder_one_wins(env):
    def slow_emit(*_a, **_k):
        time.sleep(0.2)
        return SimpleNamespace(status="appended")
    outcomes = []

    def run(pid, state):
        try:
            ml.bind(state, str(env.mem), pid, emit=slow_emit)
            outcomes.append("ok")
        except ml.BindRefused:
            outcomes.append("refused")
    threads = [threading.Thread(target=run, args=(pid, env.home / ("r" + pid) / ".vnx-data" / "state"))
               for pid in ("p1", "p2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["ok", "refused"]


@pytest.mark.parametrize("body", ["{not json", "[]", '{"bindings": {}}',
                                  '{"schema": "memory_bindings/9", "bindings": {}}',
                                  '{"schema": "memory_bindings/1", "bindings": null}'])
def test_corrupt_registry_refuses(env, body):
    ml.registry_path().write_text(body)
    with pytest.raises(ml.BindRefused, match="registry"):
        ml.bind(env.state, str(env.mem), "proj")
    assert not (env.state / ml.SOURCES_FILE).exists()


def test_crash_between_registry_and_binding_leaves_a_claim_not_a_binding(env, monkeypatch):
    real_write = ml.atomic_io.atomic_write_json

    def crash_on_binding(path, payload, **kw):
        if Path(path).name == ml.SOURCES_FILE:
            raise OSError("crash between the two writes")
        return real_write(path, payload, **kw)
    monkeypatch.setattr(ml.atomic_io, "atomic_write_json", crash_on_binding)
    with pytest.raises(OSError):
        ml.bind(env.state, str(env.mem), "proj")
    assert ml._load_registry() == {str(env.mem.resolve()): "proj"}  # half one: the claim landed
    assert ml.load_sources(env.state) == (None, "unbound: binding file missing")  # half two
    assert ml.scan(env.state)["status"] == "unbound" and not (env.state / ml.LEDGER_FILE).exists()
    other = env.home / ".vnx-data" / "other" / "state"
    monkeypatch.setattr(ml.atomic_io, "atomic_write_json", real_write)
    with pytest.raises(ml.BindRefused, match="another project"):
        ml.bind(other, str(env.mem), "other")
    assert ml.bind(env.state, str(env.mem), "proj")["memory_dirs"] == [str(env.mem.resolve())]


def bound(env):
    ml.bind(env.state, str(env.mem), "proj")


def lines(env):
    return ml.read_ledger(env.state / ml.LEDGER_FILE)[0]


def test_added_for_every_note_and_second_scan_writes_zero(env):
    bound(env)
    before = {p.name: p.read_bytes() for p in env.mem.iterdir()}
    assert ml.scan(env.state) == {"status": "ok", "reason": "ok", "written": 3, "unreadable": 0}
    assert {r["note"] for r in lines(env)} == {"MEMORY.md", "a.md", "b.md"}
    assert {r["event"] for r in lines(env)} == {"added"}
    assert ml.scan(env.state)["written"] == 0 and len(lines(env)) == 3
    assert {p.name: p.read_bytes() for p in env.mem.iterdir()} == before  # read-only


def test_changed_validity_changed_removed_and_added_again(env):
    bound(env)
    ml.scan(env.state)
    old = [r for r in lines(env) if r["note"] == "a.md"][0]["sha256"]
    (env.mem / "a.md").write_text(note("metadata:\n  type: user\n  node_type: lesson", "new"))
    (env.mem / "b.md").write_text(note("type: project\ninvalid_at: 2026-12-01"))
    assert ml.scan(env.state)["written"] == 2
    by = {r["note"]: r for r in lines(env)[-2:]}
    assert (by["a.md"]["event"], by["a.md"]["prev_sha256"]) == ("changed", old)
    assert (by["b.md"]["event"], by["b.md"]["invalid_at"]) == ("validity_changed", "2026-12-01")
    (env.mem / "b.md").unlink()
    ml.scan(env.state)
    assert lines(env)[-1]["event"] == "removed" and ml.scan(env.state)["written"] == 0
    (env.mem / "b.md").write_text(note())
    ml.scan(env.state)
    assert lines(env)[-1]["event"] == "added"


@pytest.mark.parametrize("drifting_reads, seen", [(6, ("unreadable", "changed_during_read")),
                                                  (1, ("added", None))])
def test_change_during_read_retries_once_then_unreadable(env, monkeypatch, drifting_reads, seen):
    bound(env)
    real, calls = os.stat(env.mem / "a.md"), []
    drift = SimpleNamespace(st_size=real.st_size + 1, st_mtime_ns=real.st_mtime_ns)

    def flaky(_path):  # 3 notes x 2 attempts: 6 drifting reads exhaust every retry
        calls.append(1)
        return (b"x", real, drift if len(calls) <= drifting_reads else real)
    monkeypatch.setattr(ml, "_read_once", flaky)
    assert ml.scan(env.state)["written"] == 3
    assert {(r["event"], r.get("reason")) for r in lines(env)} == {seen}


def test_unreadable_note_and_folder_are_unknown_lines_not_skips(env, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(note())
    (env.mem / "link.md").symlink_to(outside)
    (env.mem / "dir.md").mkdir()
    bound(env)
    ml.scan(env.state)
    by = {r["note"]: (r["event"], r.get("reason")) for r in lines(env)}
    assert (by["link.md"], by["dir.md"]) == (("unreadable", "outside_dir"), ("unreadable", "os_error:EISDIR"))
    os.chmod(env.mem, 0)
    try:
        result = ml.scan(env.state)
    finally:
        os.chmod(env.mem, 0o755)
    last = lines(env)[-1]
    assert (result["unreadable"], last["event"], last["note"], last["reason"]) == (
        1, "unreadable", "unknown", "dir_os_error:EACCES")
    assert not any(r["event"] == "removed" for r in lines(env))


def test_torn_tail_set_apart(env):
    bound(env)
    ml.scan(env.state)
    ledger = env.state / ml.LEDGER_FILE
    good = ledger.read_bytes()
    ledger.write_bytes(good + b'{"half":')
    (env.mem / "c.md").write_text(note())
    assert ml.scan(env.state)["written"] == 1
    side = json.loads((env.state / (ml.LEDGER_FILE + ".afgescheurd")).read_text())
    assert base64.b64decode(side["bytes_b64"]) == b'{"half":' and side["offset"] == len(good)
    assert ledger.read_bytes().startswith(good) and ml.read_ledger(ledger)[1:] == (0, [])
    ledger.write_bytes(b'{"schema":"x"}\n{"torn')
    assert ml.read_ledger(ledger) == ([], 1, [1])  # the reader counts, never crashes


def test_partial_when_budget_is_spent(env):
    bound(env)
    ticks = iter([0.0, 0.0, 5.0, 5.0, 5.0, 5.0])
    result = ml.scan(env.state, budget=2.0, clock=lambda: next(ticks))
    assert (result["status"], result["reason"]) == ("partial", "budget")
    part = lines(env)[-1]
    assert (part["event"], part["reason"], part["processed"], part["total"]) == ("partial", "budget", 1, 3)
    assert ml.scan(env.state)["written"] == 2  # resumes: only the unseen notes


def test_frontmatter_absent_unparseable_unknown_and_metadata_beats_top_level(env):
    (env.mem / "c.md").write_text("no block\n")
    (env.mem / "d.md").write_text("---\ndescription: a: b: c\n---\nx\n")
    (env.mem / "e.md").write_bytes(b"---\n\xff\n---\n")
    (env.mem / "f.md").write_text(note("type: top\nmetadata:\n  type: nested\n  originSessionId: s-1"))
    (env.mem / "g.md").write_text(note("metadata: plain\ntype: top\noriginSessionId: s-2"))
    bound(env)
    ml.scan(env.state)
    by = {r["note"]: r for r in lines(env)}
    for name, fm in (("c.md", "absent"), ("d.md", "unparseable"), ("e.md", "unparseable")):
        assert by[name]["frontmatter"] == fm
        assert {by[name][k] for k in ("type", "node_type", "origin_session", "valid_from",
                                      "invalid_at", "superseded_by")} == {"unknown"}
    assert (by["f.md"]["type"], by["f.md"]["origin_session"]) == ("nested", "s-1")
    assert (by["g.md"]["type"], by["g.md"]["origin_session"]) == ("top", "s-2")
    assert (by["b.md"]["origin_session"], by["a.md"]["node_type"]) == ("abc-123", "lesson")


def test_ledger_never_holds_note_text_nor_empty_or_zero_fields(env):
    sentence = "The operator prefers short answers on Fridays."
    (env.mem / "c.md").write_text(note("type: bad value with spaces", sentence))
    bound(env)
    ml.scan(env.state)
    raw = (env.state / ml.LEDGER_FILE).read_bytes()
    for text in (BODY, DESC, sentence, "bad value", "short answers"):
        assert text.encode() not in raw
    for rec in lines(env):
        assert all(v not in ("", 0) and (v is not None or k in ("valid_from", "invalid_at", "superseded_by"))
                   for k, v in rec.items())
    assert {r["type"] for r in lines(env) if r["note"] == "MEMORY.md"} == {"index"}


@pytest.mark.parametrize("change", [{"extra": 1}, {"event": "weird"}, {"note": ""},
                                    {"schema": "memory_ledger/2"}, {"prev_sha256": None}])
def test_writer_refuses_invalid_records(change):
    base = {"schema": ml.LEDGER_SCHEMA, "observed_at": "t", "project_id": "p", "event": "removed",
            "source": "s", "note": "n.md", "prev_sha256": "h"}
    ml.validate_record(base)
    with pytest.raises(ValueError):  # a None in ``change`` drops that field: a missing field
        ml.validate_record({k: v for k, v in {**base, **change}.items() if v is not None})


def test_unbound_writes_nothing_then_cli_scan_observes(env, monkeypatch, capsys):
    import memory_cli
    monkeypatch.setenv("VNX_STATE_DIR", str(env.state))
    result = ml.scan(env.state)
    assert (result["status"], result["written"], result["reason"]) == (
        "unbound", 0, "unbound: binding file missing")
    assert memory_cli.main(["scan"]) == 3
    assert capsys.readouterr().out.startswith("unbound: binding file missing")
    assert list(env.state.iterdir()) == []
    bound(env)
    assert memory_cli.main(["scan"]) == 0
    assert "3 line(s) written" in capsys.readouterr().out


def _disk_full(*_a):
    raise OSError(28, "No space left on device")


def _short_then(real, then):
    calls = []

    def write(fd, data):
        calls.append(1)
        if len(calls) == 1:
            return real(fd, bytes(data[:3]))
        return then(fd, data)
    return write


def test_append_writes_every_byte_after_a_short_write(tmp_path, monkeypatch):
    path = tmp_path / "l.ndjson"
    path.write_bytes(b'{"a":1}\n')
    monkeypatch.setattr(ml.os, "write", _short_then(os.write, os.write))
    ml._append_fsync(path, b'{"b":22}\n')
    assert path.read_bytes() == b'{"a":1}\n{"b":22}\n'


def test_append_failure_after_short_write_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "l.ndjson"
    path.write_bytes(b'{"a":1}\n')
    monkeypatch.setattr(ml.os, "write", _short_then(os.write, _disk_full))
    with pytest.raises(OSError):
        ml._append_fsync(path, b'{"b":22}\n')
    assert path.read_bytes() == b'{"a":1}\n'


def test_tail_is_kept_when_the_archive_append_fails(tmp_path, monkeypatch):
    ledger = tmp_path / ml.LEDGER_FILE
    ledger.write_bytes(b'{"a":1}\n{"half":')
    monkeypatch.setattr(ml.os, "write", _short_then(os.write, _disk_full))
    with pytest.raises(OSError):
        ml._set_apart_tail(tmp_path, ledger, ledger.read_bytes())
    assert ledger.read_bytes() == b'{"a":1}\n{"half":'


@pytest.mark.parametrize("bad", ["valid_from: 2026-13-01", "valid_to: 2026-02-30"])
def test_impossible_date_is_unparseable_and_the_scan_goes_on(env, bad):
    assert ml.parse_note(note(bad).encode(), "x.md")["frontmatter"] == "unparseable"
    (env.mem / "c.md").write_text(note(bad))
    bound(env)
    assert ml.scan(env.state)["written"] == 4
    by = {r["note"]: r for r in lines(env)}
    assert (by["c.md"]["frontmatter"], by["a.md"]["frontmatter"]) == ("unparseable", "ok")
