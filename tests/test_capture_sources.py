"""Capture PR-2 (split 1): the binding, the memory reader and the open-item reader.

Every fixture lives under tmp_path and HOME points there, so nothing touches
~/.claude or ~/.vnx-data.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

import capture_sources as cs  # noqa: E402

PROJECT = "proj-a"
NOTE = "geheime-notitienaam"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = home / ".vnx-data" / PROJECT / "state"
    memory = home / ".claude" / "projects" / "-x" / "memory"
    outside = tmp_path / "elsewhere"
    for d in (state, memory, outside):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    return {"home": home, "state": state, "memory": memory, "outside": outside, "tmp": tmp_path}


def _binding(env, **override):
    data = {"schema": cs.BINDING_SCHEMA, "project_id": PROJECT, "memory_dir": str(env["memory"]),
            "bucket_dir": str(env["tmp"] / "bucket"), "bucket_slug": "mine",
            "bound_at": "2026-11-10T00:00:00+00:00"}
    data.update(override)
    data = {k: v for k, v in data.items() if v is not cs}
    (env["state"] / cs.BINDING_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    return data


def _state(env):
    binding, status = cs.load_binding(env["state"])
    return binding, cs.state_of(status)


# --- load_binding: every unusable binding is unbound, never ok ---------------

def test_binding_ok(env):
    _binding(env)
    binding, status = cs.load_binding(env["state"])
    assert status == "ok" and binding["bucket_slug"] == "mine"


def test_binding_file_missing_is_unbound(env):
    assert _state(env) == (None, "unbound")


@pytest.mark.parametrize("content", ["{not json", "[]", "null", '"text"', ""])
def test_binding_corrupt_is_unbound(env, content):
    (env["state"] / cs.BINDING_FILENAME).write_text(content, encoding="utf-8")
    assert _state(env) == (None, "unbound")


@pytest.mark.parametrize("field", ["project_id", "memory_dir", "bucket_dir", "bucket_slug", "schema"])
@pytest.mark.parametrize("value", [cs, None, "", 3, "  "])
def test_binding_bad_field_is_unbound(env, field, value):
    # ``cs`` as the value means: leave the field out of the file.
    _binding(env, **{field: value})
    assert _state(env) == (None, "unbound")


@pytest.mark.parametrize("value", ["proj-b", 5, ["proj-a"]])
def test_binding_project_id_different_is_unbound(env, value):
    _binding(env, project_id=value)
    assert _state(env) == (None, "unbound")


def test_binding_unknown_schema_version_is_unbound(env):
    _binding(env, schema="capture_sources/9")
    assert _state(env) == (None, "unbound")


def test_binding_empty_derived_project_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "plain" / "state"
    state.mkdir(parents=True)
    (state / cs.BINDING_FILENAME).write_text(json.dumps({"schema": cs.BINDING_SCHEMA, "project_id": PROJECT}))
    assert cs.state_of(cs.load_binding(state)[1]) == "unbound"


@pytest.mark.parametrize("kind", ["outside", "symlink_outside", "projects_root_itself"])
def test_binding_memory_dir_not_under_projects_is_unbound(env, kind):
    target = {"outside": env["outside"], "projects_root_itself": env["home"] / ".claude" / "projects"}.get(kind)
    if kind == "symlink_outside":
        target = env["home"] / ".claude" / "projects" / "link"
        target.symlink_to(env["outside"])
    _binding(env, memory_dir=str(target))
    assert _state(env) == (None, "unbound")


# --- memory notes -------------------------------------------------------------

def test_memory_notes_read(env):
    (env["memory"] / f"{NOTE}.md").write_text(
        f"---\nname: {NOTE}\ndescription: d\nmodified: 2026-11-09T10:00:00Z\n---\n"
        "Zie OI-12 en [[andere-notitie|alias]].\n", encoding="utf-8")
    (env["memory"] / "kaal.md").write_text("zonder frontmatter OI-7\n", encoding="utf-8")
    (env["memory"] / "MEMORY.md").write_text("- index\n", encoding="utf-8")
    notes, status = cs.read_memory_notes(_binding(env))
    by_key = {n["key"]: n for n in notes}
    assert status == "ok" and set(by_key) == {NOTE, "kaal"}
    assert by_key[NOTE]["body_refs"] == {"oi": ["OI-12"], "memory": ["andere-notitie"]}
    assert by_key[NOTE]["modified_source"] == "frontmatter"
    assert by_key[NOTE]["modified"].startswith("2026-11-09T10:00:00")
    assert by_key["kaal"]["modified_source"] == "mtime"


@pytest.mark.parametrize("kind", ["outside", "symlink_dir", "note_symlink", "bad_bytes", "dir_missing"])
def test_memory_notes_unsafe_or_unreadable_is_error(env, kind):
    memory_dir = env["memory"]
    (env["outside"] / "n.md").write_text("x", encoding="utf-8")
    if kind == "outside":
        memory_dir = env["outside"]
    elif kind == "symlink_dir":
        memory_dir = env["home"] / ".claude" / "projects" / "link"
        memory_dir.symlink_to(env["outside"])
    elif kind == "note_symlink":
        (env["memory"] / "stiekem.md").symlink_to(env["outside"] / "n.md")
    elif kind == "bad_bytes":
        (env["memory"] / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    else:
        memory_dir = env["home"] / ".claude" / "projects" / "gone"
    notes, status = cs.read_memory_notes(_binding(env, memory_dir=str(memory_dir)))
    assert notes == [] and cs.state_of(status) == "error"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_memory_dir_unlistable_is_error_not_empty_ok(env):
    (env["memory"] / "n.md").write_text("x", encoding="utf-8")
    binding = _binding(env)
    os.chmod(env["memory"], 0)
    try:
        notes, status = cs.read_memory_notes(binding)
    finally:
        os.chmod(env["memory"], 0o755)
    assert notes == [] and cs.state_of(status) == "error"


def test_memory_notes_without_memory_dir_key_is_error():
    assert cs.state_of(cs.read_memory_notes({})[1]) == "error"


# --- open items ---------------------------------------------------------------

def test_open_items_absent_is_not_an_empty_list(env):
    assert cs.read_open_items(env["state"]) == ([], "absent", 0)


def test_open_items_foreign_project_skipped_and_counted(env):
    (env["state"] / "open_items.json").write_text(json.dumps({"items": [
        {"id": "OI-1", "status": "open", "title": "t", "created_at": "2026-11-01T10:00:00"},
        {"id": "OI-2", "status": "open", "title": "vreemd", "project_id": "proj-b"},
        {"id": "OI-3", "status": "done", "title": "eigen", "project_id": PROJECT},
    ]}), encoding="utf-8")
    items, status, foreign = cs.read_open_items(env["state"])
    assert status == "ok" and foreign == 1
    assert [i["id"] for i in items] == ["OI-1", "OI-3"]


@pytest.mark.parametrize("content", ["{broken", "[]", '{"items": null}', '{"items": ["x"]}', "{}"])
def test_open_items_corrupt_or_malformed_is_error(env, content):
    (env["state"] / "open_items.json").write_text(content, encoding="utf-8")
    items, status, _ = cs.read_open_items(env["state"])
    assert items == [] and cs.state_of(status) == "error"


def test_open_items_without_derivable_project_is_unbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "plain" / "state"
    state.mkdir(parents=True)
    (state / "open_items.json").write_text('{"items": []}')
    assert cs.state_of(cs.read_open_items(state)[1]) == "unbound"
