"""ADR-078 M2 test duties: the binding and its refusals."""
import json
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
