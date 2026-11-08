"""T0 playbook source contract (ADR-058), scripts/lib/t0_playbook.py. Dispatch-ID: dlv-db8232f8e1af (SP-3)."""

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import t0_playbook as tp

SKILL = Path("skills") / "t0-orchestrator" / "SKILL.md"
ORDER = ["fabric", "vnx_home", "installed", "project", "legacy"]


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def world(tmp_path):
    """The five source paths (none created) plus a resolve() bound to them."""
    scripts = tmp_path / "fabric" / "scripts"
    scripts.mkdir(parents=True)
    paths = {
        "fabric": scripts.parent / SKILL,
        "vnx_home": tmp_path / "vnxhome" / SKILL,
        "installed": tmp_path / "home" / ".vnx-system" / "current" / SKILL,
        "project": tmp_path / "project" / SKILL,
        "legacy": tmp_path / "project" / ".claude" / SKILL,
    }

    class W(dict):
        def put(self, name, text=None):
            self[name].parent.mkdir(parents=True, exist_ok=True)
            self[name].write_text(text if text is not None else f"from {name}\n")

        def resolve(self):
            return tp.resolve(tmp_path / "project", scripts_root=scripts,
                              vnx_home=str(tmp_path / "vnxhome"), home=str(tmp_path / "home"))

    return W(paths)


@pytest.mark.parametrize("winner,degraded", [
    ("fabric", False), ("vnx_home", False), ("installed", False), ("project", True), ("legacy", True)])
def test_each_step_wins_when_it_is_the_first_that_exists(world, winner, degraded):
    for name in ORDER[ORDER.index(winner):]:
        world.put(name)
    r = world.resolve()
    assert (r.state, r.source_id, r.degraded) == ("ok", winner, degraded)
    assert r.path == str(world[winner].resolve())
    assert r.sha256 == _sha(f"from {winner}\n")


def test_symlinked_source_is_stored_as_the_real_path(world, tmp_path):
    real = tmp_path / "releases" / "1.8.0" / SKILL
    real.parent.mkdir(parents=True)
    real.write_text("released\n")
    world["installed"].parent.mkdir(parents=True)
    world["installed"].symlink_to(real)
    r = world.resolve()
    assert r.source_id == "installed" and r.path == str(real.resolve()) and "current" not in r.path


def test_all_five_missing_is_a_result_not_an_exception(world):
    r = world.resolve()
    assert (r.state, r.path, r.source_id, r.sha256) == ("missing", None, None, None)
    assert len(r.probed) == 5


def test_unreadable_source_does_not_win(world):
    world.put("fabric")
    world["fabric"].chmod(0)
    try:
        r = world.resolve()
    finally:
        world["fabric"].chmod(0o644)
    assert r.state == "missing" and any(p.startswith("fabric:") for p in r.probed)


def test_render_parse_round_trip_carries_no_body(world):
    world.put("fabric", "SECRET-BODY\n")
    r = world.resolve()
    block = tp.render_block(r)
    assert block.startswith(tp.BEGIN_MARKER + "\n") and block.endswith(tp.END_MARKER + "\n")
    assert "SECRET-BODY" not in block
    assert tp.parse_block("# notes\n\n" + block + "\ntail\n") == tp.Pointer(r.path, r.source_id, r.sha256)


def test_render_refuses_missing(world):
    with pytest.raises(ValueError):
        tp.render_block(world.resolve())


@pytest.mark.parametrize("text", ["", "no block here\n", None])
def test_parse_returns_none_without_a_block(text):
    assert tp.parse_block(text) is None


@pytest.mark.parametrize("body,expected", [
    ("path: /a\nsource_id: fabric\nsha256: nothex\n", tp.Pointer("/a", "fabric", None)),
    (f"path: /a\npath: /b\nsource_id: fabric\nsha256: {'a' * 64}\n", tp.Pointer(None, "fabric", "a" * 64)),
])
def test_parse_marks_malformed_fields_none(body, expected):
    assert tp.parse_block(f"{tp.BEGIN_MARKER}\n{body}{tp.END_MARKER}\n") == expected


def test_parse_block_without_end_marker_is_all_none():
    assert tp.parse_block(f"{tp.BEGIN_MARKER}\npath: /a\n") == tp.Pointer(None, None, None)


@pytest.fixture
def stored(world):
    world.put("fabric", "v1\n")
    return tp.parse_block(tp.render_block(world.resolve()))


def test_state_ok(world, stored):
    assert tp.pointer_state(stored, world.resolve()) == "ok"


def test_state_stale_sha_when_the_file_changed(world, stored):
    world["fabric"].write_text("v2\n")
    assert tp.pointer_state(stored, world.resolve()) == "stale_sha"


def test_state_stale_source_after_the_resolution_moved(world, stored):
    world.put("installed", "v1\n")
    world["fabric"].unlink()
    assert tp.pointer_state(stored, world.resolve()) == "stale_source"


def test_older_release_with_consistent_hash_is_stale_source(world, tmp_path):
    old = tmp_path / "releases" / "1.7" / SKILL
    old.parent.mkdir(parents=True)
    old.write_text("old\n")
    world.put("installed", "new\n")
    assert tp.pointer_state(tp.Pointer(str(old), "installed", _sha("old\n")), world.resolve()) == "stale_source"


def test_state_missing_and_absent(world):
    assert tp.pointer_state(tp.Pointer("/a", "fabric", "a" * 64), world.resolve()) == "missing"
    world.put("fabric")
    assert tp.pointer_state(None, world.resolve()) == "absent"


@pytest.mark.parametrize("field", ["path", "source_id", "sha256"])
def test_none_field_is_never_ok(world, stored, field):
    assert tp.pointer_state(tp.Pointer(**{**stored.__dict__, field: None}), world.resolve()) != "ok"


def test_unknown_source_id_is_never_ok(world, stored):
    assert tp.pointer_state(tp.Pointer(stored.path, "bogus", stored.sha256), world.resolve()) == "stale_source"
