"""ADR-078 M5a test duties: the structural doctor proposes and never writes."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))
import memory_doctor as md
import memory_ledger as ml


def _hash_tree(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _bind(state: Path, *dirs: Path):
    state.mkdir(parents=True, exist_ok=True)
    (state / ml.SOURCES_FILE).write_text(json.dumps({
        "schema": ml.SOURCES_SCHEMA, "project_id": "proj", "memory_dirs": [str(d.resolve()) for d in dirs],
        "bound_at": "2026-11-15T00:00:00Z"}))


@pytest.fixture
def env(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- [a](a.md)\n- [b](b.md)\n")
    (mem / "a.md").write_text("note a, see [[b]]\n")
    (mem / "b.md").write_text("note b\n")
    state = tmp_path / "state"
    _bind(state, mem)
    return mem, state


def kinds(doc):
    return sorted(p["kind"] for p in doc["proposals"])


def test_clean_folder_has_no_proposals(env):
    mem, state = env
    (state / "capture_sources.json").write_text(json.dumps({"memory_dir": str(mem)}))  # matching: no divergence
    doc = md.run(state)
    assert doc["status"] == "ok" and doc["proposals"] == [] and doc["errors"] == []


def test_dead_wikilink(env):
    mem, state = env
    (mem / "a.md").write_text("see [[b]] and [[ghost|alias]] and [[nope#h]]\n")
    doc = md.run(state)
    assert sorted(p["evidence"] for p in doc["proposals"]) == ["ghost", "nope"]
    assert kinds(doc) == ["dead_wikilink"] * 2


def test_index_line_points_to_missing_file(env):
    mem, state = env
    (mem / "MEMORY.md").write_text("- [a](a.md)\n- [b](b.md)\n- [x](gone.md)\n- [w](https://e.org/x.md)\n")
    doc = md.run(state)
    assert kinds(doc) == ["index_missing_file"] and doc["proposals"][0]["evidence"] == "gone.md"


def test_note_no_index_line_names(env):
    mem, state = env
    (mem / "orphan.md").write_text("alone\n")
    doc = md.run(state)
    assert kinds(doc) == ["file_not_in_index"] and doc["proposals"][0]["note"] == "orphan.md"
    (mem / "MEMORY.md").write_text("- [t](t-index.md)\n")  # coverage follows an index note
    (mem / "t-index.md").write_text("[[a]] [[b]] [[orphan]]\n")
    assert md.run(state)["proposals"] == []


@pytest.mark.parametrize("text,over", [
    ("- [a](a.md)\n- [b](b.md)\n" + "x\n" * 199, "lines>200"),
    ("- [a](a.md)\n- [b](b.md)\n" + "x" * 26000 + "\n", "bytes>25600"),
    ("- [a](a.md)\n- [b](b.md)\n" + "x\n" * 198, None),  # 200 lines is within budget
])
def test_index_budget(env, text, over):
    mem, state = env
    (mem / "MEMORY.md").write_text(text)
    found = [p for p in md.run(state)["proposals"] if p["kind"] == "index_over_budget"]
    assert [p["evidence"] for p in found] == ([over] if over else [])


def test_unbound_without_binding(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    doc = md.run(state)
    assert doc["status"] == "unbound" and kinds(doc) == ["unbound"]
    (state / "steering_sources.json").write_text(json.dumps({"memory_dir": str(tmp_path)}))  # a binding without memory_sources
    doc = md.run(state)
    assert kinds(doc) == ["binding_divergence", "unbound"]
    assert [p for p in doc["proposals"] if p["kind"] == "binding_divergence"][0]["evidence"]["memory_sources"] is None


@pytest.mark.parametrize("content", [
    "{not json", "[]", json.dumps({"schema": "x"}),
    json.dumps({"schema": ml.SOURCES_SCHEMA, "project_id": "p", "memory_dirs": [None]})])
def test_invalid_binding_stays_unbound(tmp_path, content):
    (tmp_path / ml.SOURCES_FILE).write_text(content)
    assert md.run(tmp_path)["status"] == "unbound"


def test_divergence_extra_dir(env, tmp_path):
    mem, state = env
    other = tmp_path / "other"
    (state / "capture_sources.json").write_text(json.dumps({"memory_dir": str(other)}))
    (state / "steering_sources.json").write_text(json.dumps({"memory_dir": str(mem)}))
    before = _hash_tree(state)
    doc = md.run(state)
    (div,) = [p for p in doc["proposals"] if p["kind"] == "binding_divergence"]
    assert div["evidence"] == {"memory_sources": [str(mem.resolve())], "capture_sources": str(other.resolve()),
                               "steering_sources": str(mem.resolve())}
    assert {k: v for k, v in _hash_tree(state).items() if k != md.RESULT_FILE} == before


@pytest.mark.parametrize("content", ["{broken", "[]", json.dumps({"memory_dir": None}), '{"memory_dir": 7}'])
def test_unreadable_other_binding_is_reported(env, content):
    mem, state = env
    (state / "capture_sources.json").write_text(content)
    doc = md.run(state)
    assert doc["status"] == "degraded" and doc["errors"][0]["note"] == "capture_sources.json"


def test_same_proposal_ids_on_two_runs(env):
    mem, state = env
    (mem / "orphan.md").write_text("alone\n")
    first, second = md.run(state), md.run(state)
    assert [p["id"] for p in first["proposals"]] == [p["id"] for p in second["proposals"]] != []


def test_unreadable_folder_reported_with_reason(tmp_path):
    state, missing = tmp_path / "state", tmp_path / "gone"
    _bind(state, missing)
    doc = md.run(state)
    assert doc["status"] == "degraded" and doc["proposals"] == []
    assert doc["errors"][0]["reason"].startswith("os_error:")


def test_unreadable_note_and_missing_index_reported(env):
    mem, state = env
    (mem / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    doc = md.run(state)
    assert any(e["note"] == "bad.md" and e["reason"].startswith("unreadable:") for e in doc["errors"])
    (mem / "MEMORY.md").unlink()
    doc = md.run(state)
    assert any(e["note"] == "MEMORY.md" and e["reason"] == "index_missing" for e in doc["errors"])
    assert doc["status"] == "degraded"


def test_folder_is_byte_identical_after_a_run(env):
    mem, state = env
    (mem / "orphan.md").write_text("[[ghost]]\n")
    (mem / "MEMORY.md").write_text("- [x](gone.md)\n" + "y\n" * 300)
    before = _hash_tree(mem)
    md.run(state)
    md.run(state)
    assert _hash_tree(mem) == before