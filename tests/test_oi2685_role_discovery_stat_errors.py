"""OI-2685: role discovery reads an unreadable role the same on every Python version."""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import dispatch_cli  # noqa: E402
import worker_permissions as wp  # noqa: E402

needs_nonroot = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")


def _registry(tmp_path, roles=("backend-developer",)):
    agents = tmp_path / "agents"
    for r in roles:
        (agents / r).mkdir(parents=True)
        (agents / r / "CLAUDE.md").write_text("x")
    return agents


def _ghost(agents):
    ghost = agents / "ghost-role"
    ghost.mkdir()
    ghost.chmod(0o000)
    return ghost


@needs_nonroot
def test_unreadable_entry_is_unverifiable(tmp_path):
    agents = _registry(tmp_path)
    ghost = _ghost(agents)
    try:
        assert wp._agents_registry_roles(agents) is None
        assert dispatch_cli._discover_valid_roles(agents) == frozenset()
    finally:
        ghost.chmod(0o755)


@needs_nonroot
def test_python_314_semantics_still_unverifiable(tmp_path):
    """Path.is_dir/is_file swallow PermissionError (3.14+): result must not change."""
    agents = _registry(tmp_path)
    ghost = _ghost(agents)
    real_dir, real_file = Path.is_dir, Path.is_file

    def quiet(real):
        def inner(self, *a, **k):
            try:
                return real(self, *a, **k)
            except PermissionError:
                return False
        return inner

    try:
        with mock.patch.object(Path, "is_dir", quiet(real_dir)), mock.patch.object(
            Path, "is_file", quiet(real_file)
        ):
            assert wp._agents_registry_roles(agents) is None
            assert dispatch_cli._discover_valid_roles(agents) == frozenset()
    finally:
        ghost.chmod(0o755)


def test_plain_registry_returns_both(tmp_path):
    agents = _registry(tmp_path, ("backend-developer", "reviewer"))
    want = frozenset({"backend-developer", "reviewer"})
    assert wp._agents_registry_roles(agents) == want
    assert dispatch_cli._discover_valid_roles(agents) == want


def test_non_roles_are_skipped_without_raising(tmp_path):
    agents = _registry(tmp_path)
    (agents / "afile").write_text("x")
    (agents / "no-claude").mkdir()
    want = frozenset({"backend-developer"})
    assert wp._agents_registry_roles(agents) == want
    assert dispatch_cli._discover_valid_roles(agents) == want


def test_missing_agents_dir_fails_closed(tmp_path):
    assert dispatch_cli._discover_valid_roles(tmp_path / "nope") == frozenset()
    assert dispatch_cli._discover_valid_roles(_registry(tmp_path) / "backend-developer" / "CLAUDE.md") == frozenset()
