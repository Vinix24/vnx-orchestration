#!/usr/bin/env python3
"""Shell ⇄ Python state-root resolver lockstep (PR-PIP-2).

The state root is resolved in three places that MUST stay byte-for-byte
identical: ``scripts/lib/vnx_paths.py`` (``_resolve_state_root`` /
``_project_id_from_marker``), ``scripts/lib/vnx_paths.sh`` (``_vnx_resolve_state_root``
/ ``_vnx_state_project_id``), and the inline fallback in ``bin/vnx``. Drift
between the Python and shell resolvers is a known failure mode, so this guard
runs the *real* shell functions via bash (no reimplementation — it slices the
canonical function bodies out of vnx_paths.sh and sources them) and asserts the
output equals the Python resolver for the same inputs across every branch.

The explicit ``VNX_DATA_DIR_EXPLICIT=1`` override is applied by the *callers*
(vnx_paths.sh / bin/vnx) before the resolver function runs, so it is exercised
by the Python unit tests in test_path_resolution_regression.py, not here.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import vnx_paths  # noqa: E402

VNX_PATHS_SH = SCRIPTS_DIR / "lib" / "vnx_paths.sh"
BIN_VNX = SCRIPTS_DIR.parent / "bin" / "vnx"

# Markers bounding the resolver function block in vnx_paths.sh.
_BLOCK_START = "# ── State-root resolver (PR-PIP-2)"
_BLOCK_END = "# Always compute VNX_HOME from this"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _extract_shell_functions() -> str:
    """Return the real resolver function bodies sliced from vnx_paths.sh.

    Slices the lines between the resolver banner and the next major section so
    the test executes the canonical code, not a copy.
    """
    text = VNX_PATHS_SH.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if _BLOCK_START in ln)
    end = next(i for i, ln in enumerate(lines) if _BLOCK_END in ln)
    block = "\n".join(lines[start:end])
    assert "_vnx_resolve_state_root()" in block, "resolver function not extracted"
    assert "_vnx_state_project_id()" in block, "project-id helper not extracted"
    return block


@pytest.fixture(scope="module")
def shell_lib(tmp_path_factory) -> Path:
    """A sourceable shell file holding only the real resolver functions."""
    lib = tmp_path_factory.mktemp("shell_lib") / "resolver.sh"
    lib.write_text(_extract_shell_functions() + "\n", encoding="utf-8")
    return lib


def _run_shell(shell_lib: Path, snippet: str, env_overrides: dict) -> str:
    """Source the resolver functions and run ``snippet``; return stripped stdout."""
    env = {
        # Minimal, deterministic environment — no inherited VNX_* / XDG state.
        "PATH": os.environ.get("PATH", ""),
    }
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    script = f"set -eu\nsource '{shell_lib}'\n{snippet}\n"
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"shell failed: {proc.stderr}\n--script--\n{script}"
    return proc.stdout.strip()


def _shell_resolve(shell_lib, pid, project_root, env_overrides) -> Path:
    out = _run_shell(
        shell_lib,
        f'_vnx_resolve_state_root {_q(pid)} {_q(str(project_root))}',
        env_overrides,
    )
    return Path(out).resolve()


def _shell_project_id(shell_lib, project_root, env_overrides) -> str:
    return _run_shell(
        shell_lib,
        f'_vnx_state_project_id {_q(str(project_root))}',
        env_overrides,
    )


def _q(value: str) -> str:
    """Single-quote a value for safe embedding in the bash snippet."""
    s = "" if value is None else str(value)
    return "'" + s.replace("'", "'\\''") + "'"


def _py_resolve(monkeypatch, pid, project_root, env_overrides) -> Path:
    for key in ("VNX_DATA_HOME", "XDG_DATA_HOME", "VNX_DATA_DIR",
                "VNX_DATA_DIR_EXPLICIT", "VNX_PROJECT_ID"):
        monkeypatch.delenv(key, raising=False)
    for k, v in env_overrides.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    # _resolve_state_root reads Path.home(); pin it to the test HOME so the
    # live machine's ~/.vnx-data / ~/.local/share never leak in.
    home = env_overrides.get("HOME")
    if home:
        monkeypatch.setattr(Path, "home", classmethod(lambda cls, h=home: Path(h)))
    return vnx_paths._resolve_state_root(pid, Path(project_root))


# ---------------------------------------------------------------------------
# Resolver parity — every branch
# ---------------------------------------------------------------------------

class TestResolverLockstep:
    def test_vnx_data_home_branch(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        data_home = tmp_path / "dh"
        data_home.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        env = {"HOME": str(home), "VNX_DATA_HOME": str(data_home)}
        sh = _shell_resolve(shell_lib, "seocrawler-v2", proj, env)
        py = _py_resolve(monkeypatch, "seocrawler-v2", proj, env)
        assert sh == py == (data_home / "seocrawler-v2").resolve()

    def test_existing_central_branch(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        env = {"HOME": str(home)}
        sh = _shell_resolve(shell_lib, "vnx-dev", proj, env)
        py = _py_resolve(monkeypatch, "vnx-dev", proj, env)
        assert sh == py == (home / ".vnx-data" / "vnx-dev").resolve()

    def test_existing_project_local_branch(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        proj = tmp_path / "checkout"
        (proj / ".vnx-data").mkdir(parents=True)
        env = {"HOME": str(home)}
        sh = _shell_resolve(shell_lib, "vnx-dev", proj, env)
        py = _py_resolve(monkeypatch, "vnx-dev", proj, env)
        assert sh == py == (proj / ".vnx-data").resolve()

    def test_xdg_default_branch(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        proj = tmp_path / "fresh"
        proj.mkdir()
        env = {"HOME": str(home)}
        sh = _shell_resolve(shell_lib, "vnx-dev", proj, env)
        py = _py_resolve(monkeypatch, "vnx-dev", proj, env)
        assert sh == py == (home / ".local" / "share" / "vnx" / "vnx-dev").resolve()

    def test_xdg_data_home_override_branch(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        proj = tmp_path / "fresh"
        proj.mkdir()
        env = {"HOME": str(home), "XDG_DATA_HOME": str(xdg)}
        sh = _shell_resolve(shell_lib, "vnx-dev", proj, env)
        py = _py_resolve(monkeypatch, "vnx-dev", proj, env)
        assert sh == py == (xdg / "vnx" / "vnx-dev").resolve()

    def test_collision_safety_no_pid_stays_local(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        proj = tmp_path / "noid"
        proj.mkdir()
        env = {"HOME": str(home)}
        sh = _shell_resolve(shell_lib, "", proj, env)
        py = _py_resolve(monkeypatch, None, proj, env)
        assert sh == py == (proj / ".vnx-data").resolve()

    def test_no_pid_ignores_data_home(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        proj = tmp_path / "noid"
        proj.mkdir()
        env = {"HOME": str(home), "VNX_DATA_HOME": str(tmp_path / "dh")}
        sh = _shell_resolve(shell_lib, "", proj, env)
        py = _py_resolve(monkeypatch, None, proj, env)
        assert sh == py == (proj / ".vnx-data").resolve()


# ---------------------------------------------------------------------------
# project_id resolution parity (marker + env)
# ---------------------------------------------------------------------------

class TestProjectIdLockstep:
    def test_marker_first_line(self, shell_lib, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".vnx-project-id").write_text("my-project\norch-1\n", encoding="utf-8")
        env = {"HOME": str(tmp_path / "home")}
        sh = _shell_project_id(shell_lib, proj, env)
        for key in ("VNX_PROJECT_ID",):
            monkeypatch.delenv(key, raising=False)
        py = vnx_paths._project_id_from_marker(proj)
        assert sh == py == "my-project"

    def test_env_overrides_marker(self, shell_lib, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".vnx-project-id").write_text("marker-id\n", encoding="utf-8")
        env = {"HOME": str(tmp_path / "home"), "VNX_PROJECT_ID": "env-id"}
        sh = _shell_project_id(shell_lib, proj, env)
        monkeypatch.setenv("VNX_PROJECT_ID", "env-id")
        py = vnx_paths._project_id_from_marker(proj)
        assert sh == py == "env-id"

    def test_invalid_marker_yields_empty(self, shell_lib, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        # Leading digit → invalid per ^[a-z][a-z0-9-]{1,31}$.
        (proj / ".vnx-project-id").write_text("9bad\n", encoding="utf-8")
        env = {"HOME": str(tmp_path / "home")}
        sh = _shell_project_id(shell_lib, proj, env)
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        py = vnx_paths._project_id_from_marker(proj)
        assert sh == "" and py is None

    def test_no_marker_yields_empty(self, shell_lib, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        env = {"HOME": str(tmp_path / "home")}
        sh = _shell_project_id(shell_lib, proj, env)
        monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        py = vnx_paths._project_id_from_marker(proj)
        assert sh == "" and py is None


# ---------------------------------------------------------------------------
# bin/vnx inline fallback parity (the third resolver copy)
# ---------------------------------------------------------------------------

class TestBinVnxRegexParity:
    """bin/vnx, vnx_paths.sh, and vnx_paths.py must share one project_id regex."""

    def test_project_id_regex_identical(self):
        py_pattern = vnx_paths._PROJECT_ID_RE.pattern
        sh_text = VNX_PATHS_SH.read_text(encoding="utf-8")
        bin_text = BIN_VNX.read_text(encoding="utf-8")
        # The ERE used by grep -Eq across both shell copies.
        ere = "^[a-z][a-z0-9-]{1,31}$"
        assert ere in sh_text, "vnx_paths.sh missing the canonical project_id ERE"
        assert ere in bin_text, "bin/vnx missing the canonical project_id ERE"
        # Python regex is the same pattern (anchored identically).
        assert py_pattern == r"^[a-z][a-z0-9-]{1,31}$"
