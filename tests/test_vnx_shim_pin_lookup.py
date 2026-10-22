"""The pin shim finds versions/<pin> and, for a bare pin, versions/v<pin> (OI-2270)."""
import os
import shutil
import subprocess
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "scripts" / "templates" / "vnx_shim.sh.tpl"

FAKE_VNX = "#!/usr/bin/env bash\necho \"VNX_HOME=$VNX_HOME\"\n"


def _system(tmp_path: Path, folders: list[str]) -> Path:
    system = tmp_path / "vnx-system"
    (system / "bin").mkdir(parents=True)
    shim = system / "bin" / "vnx"
    shutil.copy(TEMPLATE, shim)
    shim.chmod(0o755)
    for name in folders:
        fake = system / "versions" / name / "bin" / "vnx"
        fake.parent.mkdir(parents=True)
        fake.write_text(FAKE_VNX)
        fake.chmod(0o755)
    return system


def _run(tmp_path: Path, system: Path, pin: str) -> subprocess.CompletedProcess:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / ".vnx-version").write_text(pin + "\n")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(home)}
    return subprocess.run(
        ["bash", str(system / "bin" / "vnx")],
        cwd=project, env=env, capture_output=True, text=True, timeout=30,
    )


def _home_of(result: subprocess.CompletedProcess) -> str:
    return result.stdout.strip().removeprefix("VNX_HOME=")


def test_bare_pin_resolves_to_v_folder(tmp_path: Path) -> None:
    system = _system(tmp_path, ["v1.7.5"])
    result = _run(tmp_path, system, "1.7.5")
    assert result.returncode == 0, result.stderr
    assert Path(_home_of(result)).name == "v1.7.5"


def test_v_pin_resolves_to_v_folder(tmp_path: Path) -> None:
    system = _system(tmp_path, ["v1.7.5"])
    result = _run(tmp_path, system, "v1.7.5")
    assert result.returncode == 0, result.stderr
    assert Path(_home_of(result)).name == "v1.7.5"


def test_bare_folder_wins_over_v_folder(tmp_path: Path) -> None:
    system = _system(tmp_path, ["1.7.5", "v1.7.5"])
    result = _run(tmp_path, system, "1.7.5")
    assert result.returncode == 0, result.stderr
    assert Path(_home_of(result)).name == "1.7.5"


def test_missing_pin_exits_1_and_names_both_paths(tmp_path: Path) -> None:
    system = _system(tmp_path, ["v1.7.5"])
    result = _run(tmp_path, system, "1.7.9")
    assert result.returncode == 1
    assert str(system / "versions" / "1.7.9") in result.stderr
    assert str(system / "versions" / "v1.7.9") in result.stderr


def test_traversal_pin_still_exits_78(tmp_path: Path) -> None:
    system = _system(tmp_path, ["v1.7.5"])
    result = _run(tmp_path, system, "../etc")
    assert result.returncode == 78
