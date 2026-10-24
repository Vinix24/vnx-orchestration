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

``TestBinVnxInlineBranchParity`` extends that to the third copy: it slices the
inline resolver block out of ``bin/vnx`` and runs it in bash for one environment
per branch (explicit override, ``VNX_DATA_HOME``, existing central store,
existing project-local store, fresh install, collision-safe no-id fallback), and
asserts its data *and* state root equal what ``_vnx_resolve_state_root`` returns
for the same environment.

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

# Anchors bounding the inline state-root block in bin/vnx. Both are full code
# lines (not banners) so the same slice works on the pre-fix revision too, which
# is what lets the regression run red against origin/main. The VNX_STATE_DIR
# anchor is the parameterised export only; bin/vnx has three plain
# ``export VNX_STATE_DIR="$.../state"`` lines that are not this block.
_INLINE_START_ANCHOR = 'if [ -z "${VNX_DATA_DIR:-}" ]; then'
_INLINE_END_ANCHOR = 'export VNX_STATE_DIR="${VNX_STATE_DIR:-$VNX_DATA_DIR/state}"'


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
    # live machine's ~/.vnx-data never leaks in.
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

    def test_central_default_branch(self, shell_lib, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        proj = tmp_path / "fresh"
        proj.mkdir()
        env = {"HOME": str(home)}
        sh = _shell_resolve(shell_lib, "vnx-dev", proj, env)
        py = _py_resolve(monkeypatch, "vnx-dev", proj, env)
        assert sh == py == (home / ".vnx-data" / "vnx-dev").resolve()

    def test_xdg_data_home_ignored_branch(self, shell_lib, tmp_path, monkeypatch):
        """XDG_DATA_HOME no longer affects fresh-install path resolution (OI-1055)."""
        home = tmp_path / "home"
        home.mkdir()
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        proj = tmp_path / "fresh"
        proj.mkdir()
        env = {"HOME": str(home), "XDG_DATA_HOME": str(xdg)}
        sh = _shell_resolve(shell_lib, "vnx-dev", proj, env)
        py = _py_resolve(monkeypatch, "vnx-dev", proj, env)
        assert sh == py == (home / ".vnx-data" / "vnx-dev").resolve()

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


# ---------------------------------------------------------------------------
# bin/vnx inline fallback — one parity case per branch
# ---------------------------------------------------------------------------

def _extract_bin_vnx_inline_resolution() -> str:
    """Return the real state-root block sliced out of ``bin/vnx``.

    Slices from the ``if [ -z "${VNX_DATA_DIR:-}" ]`` gate through the
    ``VNX_STATE_DIR`` export — the whole "which store does this run use"
    decision of the inline fallback, project-id walk included. The slice is
    bounded by code lines rather than banner comments so the same extraction
    also runs against a pre-fix ``bin/vnx``: that is what makes the regression
    demonstrable on the revision before the fix.
    """
    lines = BIN_VNX.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if _INLINE_START_ANCHOR in ln]
    ends = [i for i, ln in enumerate(lines) if _INLINE_END_ANCHOR in ln]
    assert len(starts) == 1, (
        f"expected one inline state-root gate in bin/vnx, found {len(starts)}"
    )
    assert len(ends) == 1, (
        f"expected one parameterised VNX_STATE_DIR export in bin/vnx, found {len(ends)}"
    )
    assert starts[0] < ends[0], "inline state-root export precedes its gate"
    block = "\n".join(lines[starts[0]:ends[0] + 1])
    assert "VNX_DATA_HOME" in block, "inline block lost its VNX_DATA_HOME branch"
    assert "VNX_PROJECT_ID" in block, "inline block lost its project-id walk"
    return block


def _run_inline_resolution(
    block: str, shell_lib: Path, env_overrides: dict
) -> dict:
    """Run the sliced inline block and the canonical resolver in one bash.

    Both answers come from real shell code in the same process: the sliced
    ``bin/vnx`` block runs first and its ``VNX_DATA_DIR`` / ``VNX_STATE_DIR``
    are captured, then ``_vnx_resolve_state_root`` is asked for the same
    environment. The caller's step-1 gate (an explicit ``VNX_DATA_DIR`` wins
    and short-circuits the ordered resolver) is reproduced exactly as
    ``vnx_paths.sh`` applies it.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    script = "\n".join(
        [
            "set -eu",
            f"source {_q(str(shell_lib))}",
            '_incoming_data="${VNX_DATA_DIR:-}"',
            '_incoming_state="${VNX_STATE_DIR:-}"',
            block,
            'printf "inline_data=%s\\n" "$VNX_DATA_DIR"',
            'printf "inline_state=%s\\n" "$VNX_STATE_DIR"',
            'if [ -n "$_incoming_data" ]; then',
            '  _expected_data="$_incoming_data"',
            "else",
            '  _expected_pid="$(_vnx_state_project_id "$PROJECT_ROOT")"',
            '  _expected_data="$(_vnx_resolve_state_root "$_expected_pid" "$PROJECT_ROOT")"',
            "fi",
            'printf "lib_data=%s\\n" "$_expected_data"',
            'printf "lib_state=%s\\n" "${_incoming_state:-$_expected_data/state}"',
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, f"shell failed: {proc.stderr}\n--script--\n{script}"
    parsed = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        assert sep, f"unexpected driver output line: {line!r}"
        parsed[key] = value
    for key in ("inline_data", "inline_state", "lib_data", "lib_state"):
        assert key in parsed, f"driver printed no {key}: {proc.stdout!r}"
    return parsed


#: One case per branch of the ordered state-root resolution, plus the
#: collision-safety fallbacks, the marker-based project-id walk and the state
#: root override.
_INLINE_BRANCH_CASES = (
    "explicit_data_dir",
    "explicit_state_dir",
    "data_home",
    "existing_central",
    "existing_project_local",
    "fresh_central_default",
    "xdg_data_home_ignored",
    "project_id_from_marker",
    "collision_no_pid",
    "no_pid_ignores_data_home",
)


def _inline_case_env(case: str, root: Path):
    """Build one branch's environment; return ``(env, expected_data, expected_state)``."""
    home = root / "home"
    home.mkdir()
    proj = root / "proj"
    proj.mkdir()
    env = {
        "HOME": str(home),
        "PROJECT_ROOT": str(proj),
        "VNX_PROJECT_ID": "vnx-dev",
    }
    state_override = None
    if case == "explicit_data_dir":
        # Branch 1, applied by the caller: a pinned VNX_DATA_DIR is passed
        # through untouched, whatever the ordered resolver would say.
        expected = root / "explicit-data"
        env["VNX_DATA_DIR"] = str(expected)
    elif case == "explicit_state_dir":
        # An explicit VNX_STATE_DIR is preserved while the data root resolves
        # normally (OI-1160).
        state_override = root / "explicit-state"
        env["VNX_STATE_DIR"] = str(state_override)
        expected = home / ".vnx-data" / "vnx-dev"
    elif case == "data_home":
        data_home = root / "data-home"
        env["VNX_DATA_HOME"] = str(data_home)
        expected = data_home / "vnx-dev"
    elif case == "existing_central":
        expected = home / ".vnx-data" / "vnx-dev"
        expected.mkdir(parents=True)
    elif case == "existing_project_local":
        expected = proj / ".vnx-data"
        expected.mkdir()
    elif case == "fresh_central_default":
        # No legacy dir anywhere: the canonical central per-project store.
        expected = home / ".vnx-data" / "vnx-dev"
    elif case == "xdg_data_home_ignored":
        env["XDG_DATA_HOME"] = str(root / "xdg")
        expected = home / ".vnx-data" / "vnx-dev"
    elif case == "project_id_from_marker":
        del env["VNX_PROJECT_ID"]
        (proj / ".vnx-project-id").write_text("marker-proj\norch-1\n", encoding="utf-8")
        expected = home / ".vnx-data" / "marker-proj"
    elif case == "collision_no_pid":
        del env["VNX_PROJECT_ID"]
        expected = proj / ".vnx-data"
    elif case == "no_pid_ignores_data_home":
        del env["VNX_PROJECT_ID"]
        env["VNX_DATA_HOME"] = str(root / "data-home")
        expected = proj / ".vnx-data"
    else:
        raise AssertionError(f"unknown inline branch case: {case!r}")
    expected_state = state_override if state_override is not None else expected / "state"
    return env, expected.resolve(), expected_state.resolve()


class TestBinVnxInlineBranchParity:
    """Every branch of bin/vnx's inline fallback returns the bash lib's answer."""

    @pytest.fixture(scope="class")
    def inline_block(self) -> str:
        return _extract_bin_vnx_inline_resolution()

    @pytest.mark.parametrize("case", _INLINE_BRANCH_CASES)
    def test_branch_matches_canonical_resolver(
        self, inline_block, shell_lib, tmp_path, case
    ):
        env, expected_data, expected_state = _inline_case_env(case, tmp_path)
        out = _run_inline_resolution(inline_block, shell_lib, env)
        assert out["inline_data"] == out["lib_data"], (
            f"bin/vnx inline branch {case!r} disagrees with "
            f"_vnx_resolve_state_root: inline={out['inline_data']} "
            f"lib={out['lib_data']}"
        )
        assert out["inline_state"] == out["lib_state"], (
            f"bin/vnx inline branch {case!r} state root disagrees with the "
            f"bash lib: inline={out['inline_state']} lib={out['lib_state']}"
        )
        assert Path(out["inline_data"]).resolve() == expected_data, (
            f"bin/vnx inline branch {case!r} resolved to {out['inline_data']}, "
            f"expected {expected_data}"
        )
        assert Path(out["inline_state"]).resolve() == expected_state, (
            f"bin/vnx inline branch {case!r} state root resolved to "
            f"{out['inline_state']}, expected {expected_state}"
        )
