"""tests/test_regen_settings_follows_the_pin.py — render from the pin (fleet PR-5).

MEASURED on the merge commit of #2314: ``scripts/commands/regen_settings.sh`` took
``merge_script="$VNX_HOME/scripts/vnx_settings_merge.py"`` and passed ``--vnx-home
"$VNX_HOME"``, so a project pinned to 1.0.0 got hook commands naming the 1.0.1 tree the
CLI happened to run from — and a pin nothing answers was rendered from anyway.

These tests run the REAL wrapper (``cmd_regen_settings`` sourced from the shipped file)
against a fake central store under ``tmp_path``: two release trees, each with merge-script
stubs that record the ``--vnx-home`` they were handed. ``scripts/lib`` of each fake tree
links to the repo's real ``scripts/lib``, so the shipped ``pinned_engine.py`` (and the
real pin rules under it) is what resolves — nothing is reimplemented here. No real
``settings.json``, ``~/.vnx`` or install tree is ever written.

Covered: a pin renders from the pinned tree; no pin renders from the declared tree as
before; a dead pin refuses naming project root, pin and directory without running the
merge; ``--checkout`` and the project-id form agree; the worker-permissions merge gets
the same tree as the settings merge; ``--validate`` resolves the same way.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "commands" / "regen_settings.sh"
LIB_DIR = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(LIB_DIR))

import pinned_engine
from launchd_test_support import register_engine

PROJECT_ID = "pinned-render-proj"
PIN = "1.0.0"
PIN_TREE = "v1.0.0"
RUNNING_TREE = "v1.0.1"
DEAD_PIN = "1.0.9"

_STUB = """
import json, os, sys
with open(os.environ[{record!r}], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def _release_tree(versions: Path, name: str, with_lib: bool = True) -> Path:
    """One release under ``versions/``: a settings stub, a worker-permissions stub, real lib.

    ``with_lib=False`` builds a pre-PR-3 install: it ships the merge scripts but no
    ``scripts/lib``, so it carries no pin resolver at all.
    """
    tree = versions / name
    (tree / "scripts").mkdir(parents=True)
    if with_lib:
        (tree / "scripts" / "lib").symlink_to(LIB_DIR, target_is_directory=True)
    for script, record in (
        ("vnx_settings_merge.py", "VNX_TEST_SETTINGS_RECORD"),
        ("vnx_worker_permissions_merge.py", "VNX_TEST_WP_RECORD"),
    ):
        (tree / "scripts" / script).write_text(_STUB.format(record=record), encoding="utf-8")
    return tree


def _fixture(tmp_path: Path) -> dict:
    """A fake store (two releases) and a project living under a fake HOME.

    The project sits INSIDE the fake HOME on purpose: ``_find_pin_dir`` stops at HOME, so
    the pin walk can never escape the fixture into the operator's real filesystem.
    """
    home = tmp_path / "home"
    home.mkdir()
    versions = tmp_path / "store" / "versions"
    return {
        "home": home,
        "versions": versions.resolve(),
        "pinned": _release_tree(versions, PIN_TREE),
        "running": _release_tree(versions, RUNNING_TREE),
        "project": home / "project",
        "settings_record": tmp_path / "settings-argv.jsonl",
        "wp_record": tmp_path / "worker-permissions-argv.jsonl",
    }


def _pin(store: dict, pin: str) -> None:
    project = store["project"]
    project.mkdir(parents=True, exist_ok=True)
    (project / ".vnx-version").write_text(pin + "\n", encoding="utf-8")


def _run_wrapper(store: dict, *args: str, vnx_home=None) -> subprocess.CompletedProcess:
    """Source the real wrapper and call it the way ``bin/vnx`` does.

    ``set -euo pipefail`` is the shell ``bin/vnx`` runs the command under, so a refusal
    that only survives under ``set -u`` is caught here (a failing command substitution
    would abort the CLI before its one-line message was printed).
    """
    program = f"""
set -euo pipefail
source "{WRAPPER}"
err() {{ printf 'ERROR: %s\\n' "$*" >&2; }}
log() {{ printf '%s\\n' "$*"; }}
PROJECT_ROOT="{store['project']}"
VNX_HOME="{vnx_home if vnx_home is not None else store['running']}"
_guard_not_vnx_home() {{ return 0; }}
rc=0
cmd_regen_settings "$@" || rc=$?
printf 'RC=%s\\n' "$rc"
"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("VNX_")}
    env.update({
        "HOME": str(store["home"]),
        "VNX_TEST_SETTINGS_RECORD": str(store["settings_record"]),
        "VNX_TEST_WP_RECORD": str(store["wp_record"]),
    })
    return subprocess.run(
        ["bash", "-c", program, "regen", *args], capture_output=True, text=True, env=env
    )


def _vnx_home_flags(record: Path) -> list:
    """Every ``--vnx-home`` value a stub recorded, in call order."""
    assert record.is_file(), f"{record} was never written: the merge script never ran"
    values = []
    for line in record.read_text(encoding="utf-8").splitlines():
        argv = json.loads(line)
        values.append(argv[argv.index("--vnx-home") + 1])
    return values


# ── 1. a pinned project renders from its pin, not from the running tree ──────
def test_pinned_project_renders_from_its_pin(tmp_path: Path) -> None:
    store = _fixture(tmp_path)
    _pin(store, PIN)

    result = _run_wrapper(store, "--merge", "--dry-run")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    pinned_tree = str(store["versions"] / PIN_TREE)
    assert _vnx_home_flags(store["settings_record"]) == [pinned_tree]
    assert pinned_tree != str(store["running"].resolve())
    assert (
        f"[regen-settings] project pinned to {PIN}: rendering from {pinned_tree}"
        in result.stdout
    ), result.stdout


# ── 2. no pin: the declared VNX_HOME, unchanged from before PR-5 ─────────────
def test_no_pin_uses_the_declared_tree(tmp_path: Path) -> None:
    store = _fixture(tmp_path)

    result = _run_wrapper(store, "--merge", "--dry-run")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    assert _vnx_home_flags(store["settings_record"]) == [str(store["running"].resolve())]
    assert "project pinned to" not in result.stdout


# ── 3. a dead pin refuses; the merge never runs ──────────────────────────────
def test_dead_pin_refuses_without_running_the_merge(tmp_path: Path) -> None:
    store = _fixture(tmp_path)
    _pin(store, DEAD_PIN)

    result = _run_wrapper(store, "--merge", "--dry-run")

    assert "RC=0" not in result.stdout
    lines = result.stderr.splitlines()
    assert len(lines) == 1, f"expected one stderr line, got {lines!r}"
    line = lines[0]
    assert str(store["project"].resolve()) in line
    assert DEAD_PIN in line
    assert str(store["versions"]) in line
    assert not store["settings_record"].exists()
    assert not store["wp_record"].exists()


# ── 4. --checkout and the project-id form resolve to the same root ───────────
def test_checkout_form_and_project_form_agree(tmp_path: Path, monkeypatch, capsys) -> None:
    store = _fixture(tmp_path)
    register_engine(store["home"], store["project"], PROJECT_ID)
    _pin(store, PIN)
    monkeypatch.setenv("HOME", str(store["home"]))
    monkeypatch.delenv("VNX_HOME_ROOT", raising=False)
    declared = str(store["running"])

    assert pinned_engine.main([PROJECT_ID, declared]) == 0
    by_project = capsys.readouterr().out.strip()
    assert pinned_engine.main(["--checkout", str(store["project"]), declared]) == 0
    by_checkout = capsys.readouterr().out.strip()

    expected = str(store["versions"] / PIN_TREE)
    assert by_project == expected
    assert by_checkout == expected


# ── 5. the worker-permissions merge gets the same tree as the settings merge ─
def test_worker_permissions_merge_gets_the_same_tree(tmp_path: Path) -> None:
    store = _fixture(tmp_path)
    _pin(store, PIN)

    result = _run_wrapper(store, "--merge", "--dry-run")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    expected = str(store["versions"] / PIN_TREE)
    assert _vnx_home_flags(store["settings_record"]) == [expected]
    assert _vnx_home_flags(store["wp_record"]) == [expected]


# ── a tree that carries no resolver keeps rendering from itself ─────────────
def test_a_tree_without_the_resolver_renders_from_itself(tmp_path: Path) -> None:
    """Backward compatibility: an install older than the resolver cannot honour a pin,
    and refusing would make ``regen-settings`` unusable on it. It renders as before."""
    store = _fixture(tmp_path)
    _pin(store, PIN)
    bare = _release_tree(store["versions"], "v0.9.0", with_lib=False)

    result = _run_wrapper(store, "--merge", "--dry-run", vnx_home=bare)

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    assert _vnx_home_flags(store["settings_record"]) == [str(bare.resolve())]


# ── --validate resolves the same way, and co-runs no write merge ─────────────
def test_validate_resolves_the_pinned_tree(tmp_path: Path) -> None:
    store = _fixture(tmp_path)
    _pin(store, PIN)

    result = _run_wrapper(store, "--validate")

    assert "RC=0" in result.stdout, result.stdout + result.stderr
    assert _vnx_home_flags(store["settings_record"]) == [str(store["versions"] / PIN_TREE)]
    assert not store["wp_record"].exists()
