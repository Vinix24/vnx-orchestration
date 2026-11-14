"""ADR-078 M2 test duties: the binding and its refusals."""
import json
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


def test_two_binds_of_different_folders_both_land(env):
    second = env.mem.parent.parent / "-second"
    second.mkdir()
    (second / "n.md").write_text("n")
    ml.bind(env.state, str(env.mem), "proj")
    doc = ml.bind(env.state, str(second), "proj")
    assert doc["memory_dirs"] == [str(env.mem.resolve()), str(second.resolve())]
    assert ml.load_sources(env.state)[0]["memory_dirs"] == doc["memory_dirs"]
