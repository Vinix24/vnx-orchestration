#!/usr/bin/env python3
"""P8 (gate-profiel-per-project): ``vnx init`` writes no inert profile file.

Before this change, ``vnx init`` wrote ``.vnx/governance_profiles.yaml`` from
``GOVERNANCE_PROFILES_YAML``. That template carried ``gates:``, ``extra:`` and
``approval_required:`` keys while the only loader
(``scripts/lib/governance_profiles.py``) reads ``required_gates:`` and
``scopes:`` — a config file that told an operator a setting existed where it
did not. The profile is selected with ``VNX_GOVERNANCE_PROFILE`` instead.

The comparison against ``origin/main`` executes the real ``init_cmd.py`` at the
commit the plan measured this row on, so the file list is compared with the
behaviour of the code this PR replaces, not with a copy of it pasted here.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.init_cmd import vnx_init  # noqa: E402

PROFILE_REL = ".vnx/governance_profiles.yaml"

# The origin/main commit gate-profiel-per-project revision 2 measured P8 on.
# Pinned, not ``origin/main``: after this branch merges, origin/main carries the
# change and would stop being the pre-change reference. CI checks out with
# fetch-depth 0, so the object is present.
_BASE_SHA = "d0d5be99"
_BASE_FILE = "vnx_cli/commands/init_cmd.py"


def _args(project_dir: Path, **overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        project_path=None,
        project_dir=str(project_dir),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
        set_version=None,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, relative path -> raw bytes."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _empty(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _load_init_cmd_at(sha: str) -> types.ModuleType:
    """``vnx_cli/commands/init_cmd.py`` at ``sha``, executed from the git object."""
    proc = subprocess.run(
        ["git", "show", f"{sha}:{_BASE_FILE}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"cannot read {_BASE_FILE} at {sha}: {proc.stderr.strip()} "
            "(the reference is a git object; CI checks out with full history)"
        )
    module = types.ModuleType("vnx_cli.commands._init_cmd_at_base")
    module.__file__ = str(REPO_ROOT / _BASE_FILE)
    exec(compile(proc.stdout, module.__file__, "exec"), module.__dict__)
    return module


def test_fresh_project_gets_no_profile_file(tmp_path):
    assert vnx_init(_args(tmp_path)) == 0
    assert not (tmp_path / PROFILE_REL).exists()


def test_guidance_line_names_the_selector_values_and_floor(tmp_path, capsys):
    # Imported here so the module still collects against the code this PR
    # replaces (the red run): the constant did not exist there.
    from vnx_cli.commands.init_cmd import GOVERNANCE_PROFILE_GUIDANCE

    assert vnx_init(_args(tmp_path)) == 0
    out = capsys.readouterr().out

    assert GOVERNANCE_PROFILE_GUIDANCE in out
    lines = [line for line in out.splitlines() if "VNX_GOVERNANCE_PROFILE" in line]
    assert len(lines) == 1, out
    line = lines[0]
    for token in ("VNX_GOVERNANCE_PROFILE", "light", "standard", "heavy", "ADR-008"):
        assert token in line, f"{token!r} missing from guidance line: {line!r}"
    assert "today's behaviour" in line


def test_existing_profile_file_is_byte_equal_after_init(tmp_path):
    vnx_dir = tmp_path / ".vnx"
    vnx_dir.mkdir()
    profile = vnx_dir / "governance_profiles.yaml"
    original = (
        b"# operator-owned; init must not touch this file\n"
        b"profiles:\n  default:\n    review_mode: full\n"
        b"scopes:\n  \"agents/\": light\n"
    )
    profile.write_bytes(original)

    assert vnx_init(_args(tmp_path)) == 0
    assert profile.read_bytes() == original

    assert vnx_init(_args(tmp_path, force=True)) == 0
    assert profile.read_bytes() == original


def test_every_other_written_file_matches_origin_main(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    base = _load_init_cmd_at(_BASE_SHA)
    assert base.vnx_init(_args(project)) == 0
    baseline = _snapshot(project)
    _empty(project)

    assert vnx_init(_args(project)) == 0
    head = _snapshot(project)

    assert PROFILE_REL in baseline, f"{_BASE_SHA} did not write {PROFILE_REL}"
    assert PROFILE_REL not in head
    assert set(head) == set(baseline) - {PROFILE_REL}
    for rel, content in head.items():
        assert content == baseline[rel], f"{rel} changed versus {_BASE_SHA}"
