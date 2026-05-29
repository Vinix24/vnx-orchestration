#!/usr/bin/env python3
"""Tests for `vnx init` CLI command (A-11 PR-1 scaffold).

Validates the .claude/ skeleton, local .vnx-data/ layout, .vnx-version pin,
root CLAUDE.md, FEATURE_PLAN.md, and safety/idempotency semantics.
"""

import argparse
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vnx_cli.commands.init_cmd import vnx_init
from vnx_cli import __version__


def _args(tmp_path, **overrides):
    ns = argparse.Namespace(
        project_path=None,
        project_dir=str(tmp_path),
        project_id=None,
        template="default",
        force=False,
        non_interactive=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class TestVnxInitCli:
    def test_init_creates_claude_dir(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        assert (tmp_path / ".claude" / "terminals" / "T0" / "CLAUDE.md").is_file()
        assert (tmp_path / ".claude" / "skills").is_dir()
        assert (tmp_path / ".claude" / "settings.json").is_file()

    def test_init_creates_vnx_data_dir(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        vnx_data = tmp_path / ".vnx-data"
        assert vnx_data.is_dir()
        assert (vnx_data / "dispatches" / "pending").is_dir()
        assert (vnx_data / "dispatches" / "active").is_dir()
        assert (vnx_data / "dispatches" / "completed").is_dir()
        assert (vnx_data / "events").is_dir()
        assert (vnx_data / "unified_reports").is_dir()

    def test_init_writes_vnx_version(self, tmp_path):
        rc = vnx_init(_args(tmp_path))
        assert rc == 0
        version_file = tmp_path / ".vnx-version"
        assert version_file.is_file()
        assert version_file.read_text().strip() == __version__

    def test_init_idempotent_with_force(self, tmp_path):
        vnx_init(_args(tmp_path))
        original = (tmp_path / "CLAUDE.md").read_text()
        (tmp_path / "CLAUDE.md").write_text("modified")

        rc = vnx_init(_args(tmp_path, force=True))
        assert rc == 0
        assert (tmp_path / "CLAUDE.md").read_text() == original

    def test_init_aborts_on_existing_without_force(self, tmp_path):
        vnx_init(_args(tmp_path))
        rc = vnx_init(_args(tmp_path))
        assert rc != 0

    def test_init_minimal_template(self, tmp_path):
        rc = vnx_init(_args(tmp_path, template="minimal"))
        assert rc == 0
        assert (tmp_path / ".claude" / "terminals" / "T0" / "CLAUDE.md").is_file()
        assert (tmp_path / ".vnx-version").is_file()

    def test_init_project_path_positional(self, tmp_path):
        ns = argparse.Namespace(
            project_path=str(tmp_path),
            project_dir=".",
            project_id=None,
            template="default",
            force=False,
            non_interactive=False,
        )
        rc = vnx_init(ns)
        assert rc == 0
        assert (tmp_path / ".vnx-version").is_file()
