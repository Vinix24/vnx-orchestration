"""OB-1: binding and containment per file (ADR-072)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import operator_steering_source as src

CLI = ROOT / "scripts" / "operator_steering.py"
SLUG = "-proj-vnx"


def _read(state, projects):
    seen = []
    return src.read_sources(state, projects, lambda handle, name: seen.append((name, handle.read()))), seen


@pytest.fixture
def env(tmp_path):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".vnx-project-id").write_text("vnx-dev\n")
    state = tmp_path / "proj" / "state"
    projects = tmp_path / "projects"
    (projects / SLUG / "memory").mkdir(parents=True)
    return state, projects, projects / SLUG


def _bind(state, projects):
    src.bind(state, projects, [SLUG], projects / SLUG / "memory")


def _cli(env, *args):
    state, projects, _ = env
    return subprocess.run([sys.executable, str(CLI), "--state-dir", str(state), "--projects-root", str(projects), *args],
                          capture_output=True, text=True)


def test_symlink_pointing_outside_is_refused_and_never_opened(env, tmp_path):
    state, projects, tdir = env
    _bind(state, projects)
    (tdir / "ok.jsonl").write_text("prompt\n")
    outside = tmp_path / "klant.jsonl"
    outside.write_text("klantdata\n")
    outside.chmod(0)  # opening the target would fail the read; a refusal never gets there
    (tdir / "link.jsonl").symlink_to(outside)
    result, seen = _read(state, projects)
    outside.chmod(0o600)
    assert (result.state, result.refused, result.refused_files) == ("error", {"outside_bound_dir": 1}, ["link.jsonl"])
    assert all("klantdata" not in text for _, text in seen)


def test_dotdot_path_and_swap_after_listing_are_refused(env, tmp_path):
    state, projects, tdir = env
    _bind(state, projects)
    outside = tmp_path / "secret.jsonl"
    outside.write_text("geheim\n")
    with pytest.raises(src.Refused, match="outside_bound_dir"):
        src.open_contained(tdir / ".." / ".." / "secret.jsonl", tdir)
    victim = tdir / "s1.jsonl"
    victim.write_text("prompt\n")
    listed = sorted(tdir.glob("*.jsonl"))
    victim.unlink()
    victim.symlink_to(outside)  # swapped for a symlink after listing
    with pytest.raises(src.Refused, match="outside_bound_dir"):
        src.open_contained(listed[0], tdir)


def test_nothing_is_read_before_a_binding_exists(env):
    state, projects, tdir = env
    (tdir / "s1.jsonl").write_text("prompt\n")
    result, seen = _read(state, projects)
    assert result.state == "unbound" and seen == []
    _bind(state, projects)
    result, seen = _read(state, projects)
    assert result.state == "ok" and seen == [("s1.jsonl", "prompt\n")]


@pytest.mark.parametrize("slug", ["clients-x", "My-Business", "-salesminds-app", "a/b", ".."])
def test_bind_refuses_client_and_business_slugs(env, slug):
    state, projects, _ = env
    done = _cli(env, "bind", f"--transcript-dir={slug}", "--memory-dir", str(projects / SLUG / "memory"))
    assert done.returncode == 2 and not (state / src.BINDING_FILE).exists()


def test_binding_with_wrong_project_or_schema_is_unbound(env):
    state, projects, _ = env
    _bind(state, projects)
    path = state / src.BINDING_FILE
    record = json.loads(path.read_text())
    for bad in ({"project_id": "other"}, {"schema": "x/9"}, {"transcript_dirs": []}, {"memory_dir": "/etc"}):
        path.write_text(json.dumps({**record, **bad}))
        assert _read(state, projects)[0].state == "unbound"
