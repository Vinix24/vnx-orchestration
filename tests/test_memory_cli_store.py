"""OI-2907: vnx memory binds, scans and diagnoses in the central store, like build_t0_state."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
sys.path.insert(0, str(ROOT / "scripts"))
import memory_cli
import state_mutation

PID = "oi2907-proj"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".vnx-project-id").write_text(PID + "\n")
    mem = home / ".claude" / "projects" / "-proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [a](a.md)\n")
    (mem / "a.md").write_text("---\nname: a\ndescription: d\ntype: user\n---\nbody\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VNX_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("PROJECT_ROOT", str(repo))
    for var in ("VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_PROJECT_ID",
                "VNX_DATA_HOME", "VNX_CANONICAL_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(state_mutation, "emit_state_mutation",
                        lambda _file, **_kw: SimpleNamespace(status="appended"))
    return SimpleNamespace(home=home, repo=repo, mem=mem,
                           central=home / ".vnx-data" / PID / "state",
                           local=repo / ".vnx-data" / "state")


def test_bind_writes_central_store_not_repo_local(world, capsys):
    assert memory_cli.main(["bind", "--dir", str(world.mem)]) == 0
    assert (world.central / "memory_sources.json").is_file()
    assert not (world.local / "memory_sources.json").exists()
    assert "bound 1 folder(s) in %s" % world.central in capsys.readouterr().out


def test_scan_and_doctor_read_the_same_central_dir(world, capsys):
    assert memory_cli.main(["scan"]) == 3
    assert memory_cli.main(["bind", "--dir", str(world.mem)]) == 0
    capsys.readouterr()
    assert memory_cli.main(["scan"]) == 0
    assert "unbound" not in capsys.readouterr().out
    assert (world.central / "memory_ledger.ndjson").is_file()
    assert not (world.local / "memory_ledger.ndjson").exists()
    assert memory_cli.main(["doctor"]) in (0, 1)
    assert "unbound" not in capsys.readouterr().out
    assert not world.local.exists()


def test_explicit_state_dir_is_honoured(world, tmp_path, monkeypatch):
    explicit = tmp_path / "explicit-state"
    monkeypatch.setenv("VNX_STATE_DIR", str(explicit))
    assert memory_cli.main(["bind", "--dir", str(world.mem)]) == 0
    assert (explicit / "memory_sources.json").is_file()
    assert not (world.central / "memory_sources.json").exists()


def test_bind_refused_keeps_exit_4(world, tmp_path):
    assert memory_cli.main(["bind", "--dir", str(tmp_path / "missing")]) == 4
