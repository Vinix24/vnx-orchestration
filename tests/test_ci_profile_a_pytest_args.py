"""Tests for scripts/ci/profile_a_pytest_args.sh (track ci-profile-a-xdist, D1).

The script holds the one pytest selection of CI Profile A, moved out of
.github/workflows/vnx-ci.yml so Profile A and the xdist shadow job cannot
drift. Three things are pinned here:

* golden: with VNX_CI_XDIST unset or 0 it emits exactly the argument vector
  the inline workflow block emitted, on the real tree and on an exclusion list
  with every comment shape the parser has to survive;
* the two-phase shape: a file entry of serial_tests.txt moves to the serial
  phase and leaves the parallel phase as --ignore, a nodeid entry as
  --deselect, and the two phases partition the full selection;
* the exact-match rule: an entry that matches nothing (or more than one
  nodeid, since --deselect matches by prefix) is refused with exit 2.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "profile_a_pytest_args.sh"
RESOLVE_ROOT = REPO_ROOT / "scripts" / "lib" / "vnx_resolve_root.sh"
PLUGIN = REPO_ROOT / "scripts" / "ci" / "pytest_collection_manifest.py"
SERIAL_LIST = REPO_ROOT / "scripts" / "ci" / "serial_tests.txt"

QUARANTINE_K = "not TestFlagGateClaude and not test_ensure_receipt_does_not_touch_real_store"
XDIST_FLAGS = ["-n", "3", "--dist", "loadfile", "--no-loadscope-reorder", "--max-worker-restart=0"]

# The selection block of the Profile A step in .github/workflows/vnx-ci.yml
# before D1 (lines 202-230 at a8b51008), code lines verbatim, the long comment
# dropped. The golden tests run it with `pytest` shadowed by a function that
# prints its arguments NUL-delimited: that is the vector Profile A passed.
INLINE_BLOCK = r"""
IGNORE=()
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ''|\#*) continue ;;
  esac
  rel="$(printf '%s' "$line" | sed -E 's/[[:space:]]*#.*//' | tr -d '[:space:]')"
  [ -n "$rel" ] && IGNORE+=(--ignore="$GITHUB_WORKSPACE/$rel")
done < "$GITHUB_WORKSPACE/scripts/ci/test_exclusions.txt"
pytest -q \
  "$GITHUB_WORKSPACE/tests" \
  -p no:cacheprovider \
  --timeout=180 \
  -k "not TestFlagGateClaude and not test_ensure_receipt_does_not_touch_real_store" \
  "${IGNORE[@]}"
"""

# Every comment shape the exclusion parser meets: whole-line comments, an
# indented comment (survives the `case`, emptied by the sed), blank and
# whitespace-only lines, trailing whitespace, tabs, CRLF, and a reason that
# itself contains '#'. Ends on a plain entry.
TRICKY_EXCLUSIONS = (
    "# header comment\n"
    "\n"
    "tests/test_a.py  # OI-1: red solo\n"
    "   # indented comment line\n"
    "tests/test_b.py\t# OI-2: tab before the reason\n"
    "   \n"
    "tests/test_c.py   \n"
    "tests/test_d.py  # OI-3: a reason with # inside\r\n"
    "\ttests/test_e.py # OI-4: leading tab\n"
    "tests/test_f.py # OI-5: last entry\n"
)

SUITE_FILES = {
    "tests/test_alpha.py": (
        "import pytest\n\n\n"
        "def test_one():\n    pass\n\n\n"
        "def test_one_more():\n    pass\n\n\n"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_param(n):\n    pass\n\n\n"
        "class TestFlagGateClaude:\n"
        "    def test_quarantined(self):\n        pass\n"
    ),
    "tests/test_beta.py": (
        "class TestBeta:\n"
        "    def test_x(self):\n        pass\n\n"
        "    def test_y(self):\n        pass\n"
    ),
    "tests/test_gamma.py": "def test_g():\n    pass\n",
    "tests/test_space.py": (
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('v', ['a b', 'c#d'], ids=['a b', 'c#d'])\n"
        "def test_v(v):\n    pass\n"
    ),
    "tests/test_modskip.py": (
        "import pytest\n\n"
        "pytest.importorskip('module_that_does_not_exist_vnx_d1')\n\n\n"
        "def test_never():\n    pass\n"
    ),
    "tests/test_excluded.py": "def test_e():\n    pass\n",
}
SUITE_EXCLUSIONS = "tests/test_excluded.py  # OI-0: excluded in this fixture\n"


def _clean_env(**extra: str) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_", "GIT_")) and k not in ("VNX_CI_XDIST", "PYTHONPATH")
    }
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    env.update(extra)
    return env


def _read_vector(path: Path) -> list:
    data = path.read_bytes()
    assert data.endswith(b"\0"), f"{path} is not NUL-terminated"
    return data[:-1].decode("utf-8").split("\0")


def _run_inline(workspace: Path) -> list:
    shim = "set -euo pipefail\npytest() { printf '%s\\0' \"$@\"; }\n"
    proc = subprocess.run(
        ["bash", "-c", shim + INLINE_BLOCK],
        env=_clean_env(GITHUB_WORKSPACE=str(workspace)),
        capture_output=True,
        check=True,
    )
    return proc.stdout[:-1].decode("utf-8").split("\0")


def _run_script(script: Path, out_dir: Path, xdist: str | None = None) -> subprocess.CompletedProcess:
    extra = {} if xdist is None else {"VNX_CI_XDIST": xdist}
    return subprocess.run(
        ["bash", str(script), str(out_dir)],
        env=_clean_env(**extra),
        capture_output=True,
        text=True,
        cwd=str(script.parents[2]),
    )


def _make_root(tmp_path: Path, exclusions: str, serial: str = "", files: dict | None = None) -> Path:
    """A throwaway git repo with copies of the script, its root helper and the plugin."""
    root = tmp_path / "repo"
    (root / "scripts" / "ci").mkdir(parents=True)
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "tests").mkdir()
    for src, rel in ((SCRIPT, "scripts/ci"), (RESOLVE_ROOT, "scripts/lib"), (PLUGIN, "scripts/ci")):
        shutil.copy2(src, root / rel / src.name)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "scripts" / "ci" / "test_exclusions.txt").write_text(exclusions, encoding="utf-8")
    (root / "scripts" / "ci" / "serial_tests.txt").write_text(serial, encoding="utf-8")
    for rel, body in (files or {}).items():
        (root / rel).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], env=_clean_env(), check=True)
    return root.resolve()


def _suite_root(tmp_path: Path, serial: str) -> Path:
    return _make_root(tmp_path, SUITE_EXCLUSIONS, serial, SUITE_FILES)


def _collected(root: Path, vector: list, tmp_path: Path, name: str) -> list:
    """Nodeids pytest collects for *vector*, read from the collection manifest."""
    manifest = tmp_path / f"manifest-{name}.json"
    proc = subprocess.run(
        ["pytest", *vector, "--collect-only", "-p", "pytest_collection_manifest",
         f"--collection-manifest={manifest}", f"--collection-manifest-phase={name}"],
        env=_clean_env(PYTHONPATH=str(root / "scripts" / "ci")),
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(manifest.read_text(encoding="utf-8"))["nodeids"]


# --- golden: the full vector is the inline block's vector -------------------


def test_full_vector_matches_the_inline_block_on_the_real_tree(tmp_path):
    proc = _run_script(SCRIPT, tmp_path / "out")
    assert proc.returncode == 0, proc.stderr
    vector = _read_vector(tmp_path / "out" / "full.args")
    assert vector == _run_inline(REPO_ROOT)
    ignores = [a for a in vector if a.startswith("--ignore=")]
    assert len(ignores) > 100, "the real exclusion list was not read"
    assert vector[:7] == ["-q", f"{REPO_ROOT}/tests", "-p", "no:cacheprovider", "--timeout=180", "-k", QUARANTINE_K]


def test_full_vector_matches_the_inline_block_on_every_comment_shape(tmp_path):
    root = _make_root(tmp_path, TRICKY_EXCLUSIONS)
    proc = _run_script(root / "scripts" / "ci" / SCRIPT.name, tmp_path / "out")
    assert proc.returncode == 0, proc.stderr
    vector = _read_vector(tmp_path / "out" / "full.args")
    assert vector == _run_inline(root)
    assert [a for a in vector if a.startswith("--ignore=")] == [
        f"--ignore={root}/tests/test_{name}.py" for name in "abcdef"
    ]


@pytest.mark.parametrize("xdist", [None, "", "0"])
def test_serial_mode_writes_only_the_full_vector(tmp_path, xdist):
    stale = tmp_path / "out"
    stale.mkdir()
    (stale / "serial.args").write_bytes(b"stale\0")
    proc = _run_script(SCRIPT, stale, xdist)
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in stale.iterdir()) == ["full.args"]


@pytest.mark.parametrize("xdist", ["2", "true", "yes", "1 "])
def test_an_unknown_xdist_value_is_refused(tmp_path, xdist):
    proc = _run_script(SCRIPT, tmp_path / "out", xdist)
    assert proc.returncode == 2
    assert "VNX_CI_XDIST must be 0 or 1" in proc.stderr
    assert not (tmp_path / "out").exists()


def test_missing_out_dir_argument_is_refused():
    proc = subprocess.run(["bash", str(SCRIPT)], env=_clean_env(), capture_output=True, text=True)
    assert proc.returncode == 2
    assert "usage" in proc.stderr


# --- the two-phase shape ----------------------------------------------------


def test_the_committed_serial_list_is_empty_and_states_the_entry_rule():
    lines = SERIAL_LIST.read_text(encoding="utf-8").splitlines()
    assert lines, "serial_tests.txt lost its header"
    assert all(not line.strip() or line.lstrip().startswith("#") for line in lines)
    text = "\n".join(lines)
    for phrase in ("ENTRY RULE", "--ignore", "--deselect", "import-time", "exit 2"):
        assert phrase in text


def test_empty_serial_list_emits_no_serial_phase(tmp_path):
    proc = _run_script(SCRIPT, tmp_path / "out", "1")
    assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["parallel.args"]
    full = _read_vector(_run_full(tmp_path))
    assert _read_vector(tmp_path / "out" / "parallel.args") == full + XDIST_FLAGS


def _run_full(tmp_path: Path) -> Path:
    out = tmp_path / "full-out"
    proc = _run_script(SCRIPT, out)
    assert proc.returncode == 0, proc.stderr
    return out / "full.args"


def test_a_file_entry_and_a_nodeid_entry_move_to_the_serial_phase(tmp_path):
    serial = "tests/test_gamma.py  # OI-9: file entry\ntests/test_beta.py::TestBeta::test_x\n"
    root = _suite_root(tmp_path, serial)
    script = root / "scripts" / "ci" / SCRIPT.name
    out = tmp_path / "out"
    proc = _run_script(script, out, "1")
    assert proc.returncode == 0, proc.stderr
    serial_vec = _read_vector(out / "serial.args")
    parallel_vec = _read_vector(out / "parallel.args")
    assert serial_vec[-2:] == [f"{root}/tests/test_gamma.py", f"{root}/tests/test_beta.py::TestBeta::test_x"]
    assert serial_vec[:3] == ["-q", "-p", "no:xdist"]
    assert f"--ignore={root}/tests/test_gamma.py" in parallel_vec
    assert "--deselect=tests/test_beta.py::TestBeta::test_x" in parallel_vec
    assert parallel_vec[-6:] == XDIST_FLAGS
    assert f"--ignore={root}/tests/test_excluded.py" in serial_vec + parallel_vec


def test_the_two_phases_partition_the_full_selection(tmp_path):
    pytest.importorskip("xdist")
    serial = "tests/test_gamma.py\ntests/test_beta.py::TestBeta::test_x\ntests/test_alpha.py::test_param[2]\n"
    root = _suite_root(tmp_path, serial)
    script = root / "scripts" / "ci" / SCRIPT.name
    assert _run_script(script, tmp_path / "x", "1").returncode == 0
    assert _run_script(script, tmp_path / "f").returncode == 0
    full = _collected(root, _read_vector(tmp_path / "f" / "full.args"), tmp_path, "full")
    ser = _collected(root, _read_vector(tmp_path / "x" / "serial.args"), tmp_path, "serial")
    par = _collected(root, _read_vector(tmp_path / "x" / "parallel.args"), tmp_path, "parallel")
    assert sorted(ser) == [
        "tests/test_alpha.py::test_param[2]",
        "tests/test_beta.py::TestBeta::test_x",
        "tests/test_gamma.py::test_g",
    ]
    assert not set(ser) & set(par)
    assert sorted(ser + par) == sorted(full)
    assert "tests/test_alpha.py::TestFlagGateClaude::test_quarantined" not in full
    assert not any(n.startswith("tests/test_excluded.py") for n in full)


def test_inline_comments_end_at_whitespace_hash_only(tmp_path):
    serial = (
        "# whole-line comment\n"
        "   # indented comment\n"
        "tests/test_space.py::test_v[a b]   # OI-9: the id keeps its space\n"
        "tests/test_space.py::test_v[c#d]\n"
    )
    root = _suite_root(tmp_path, serial)
    proc = _run_script(root / "scripts" / "ci" / SCRIPT.name, tmp_path / "out", "1")
    assert proc.returncode == 0, proc.stderr
    parallel_vec = _read_vector(tmp_path / "out" / "parallel.args")
    assert "--deselect=tests/test_space.py::test_v[a b]" in parallel_vec
    assert "--deselect=tests/test_space.py::test_v[c#d]" in parallel_vec


# --- the exact-match rule ---------------------------------------------------


@pytest.mark.parametrize(
    ("serial", "message"),
    [
        ("tests/test_missing.py\n", "does not exist"),
        ("tests/test_beta.py::TestBeta::test_nope\n", "prefixes 0"),
        ("tests/test_alpha.py::test_one\n", "prefixes 2"),
        ("tests/test_alpha.py::test_param\n", "prefixes 2"),
        ("tests/test_alpha.py::TestFlagGateClaude::test_quarantined\n", "prefixes 0"),
        ("tests/test_excluded.py\n", "is excluded in test_exclusions.txt"),
        ("tests/test_modskip.py\n", "collects no test"),
        ("tests/test_gamma.py\ntests/test_gamma.py\n", "duplicate entry"),
        ("tests/test_beta.py\ntests/test_beta.py::TestBeta::test_x\n", "which is itself a serial entry"),
        ("scripts/ci/profile_a_pytest_args.sh\n", "not a test file under tests/"),
        ("tests/../tests/test_gamma.py\n", "must be plain"),
    ],
)
def test_an_entry_that_does_not_match_exactly_is_refused(tmp_path, serial, message):
    root = _suite_root(tmp_path, serial)
    out = tmp_path / "out"
    proc = _run_script(root / "scripts" / "ci" / SCRIPT.name, out, "1")
    assert proc.returncode == 2, proc.stderr
    assert message in proc.stderr
    assert not (out / "parallel.args").exists()
    assert not (out / "serial.args").exists()


def test_a_broken_entry_file_is_refused_with_the_collection_log(tmp_path):
    files = dict(SUITE_FILES)
    files["tests/test_broken.py"] = "raise RuntimeError('boom at import')\n"
    root = _make_root(tmp_path, SUITE_EXCLUSIONS, "tests/test_broken.py\n", files)
    proc = _run_script(root / "scripts" / "ci" / SCRIPT.name, tmp_path / "out", "1")
    assert proc.returncode == 2
    assert "collecting the serial entries failed" in proc.stderr
    assert "boom at import" in proc.stderr


def test_serial_mode_ignores_the_serial_list(tmp_path):
    root = _suite_root(tmp_path, "tests/test_missing.py\n")
    proc = _run_script(root / "scripts" / "ci" / SCRIPT.name, tmp_path / "out")
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "full.args").is_file()
