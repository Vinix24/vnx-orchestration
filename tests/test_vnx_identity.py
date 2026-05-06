#!/usr/bin/env python3
"""Tests for scripts/lib/vnx_identity.py — Phase 6 P2 identity layer.

Covers:
* Resolution chain order: env > .vnx-project-id > registry > error
* Strict ID regex enforcement (accept/reject)
* Reserved ``_unknown`` literal accepted only with ``allow_unknown=True``
* ``try_resolve_identity()`` returns None instead of raising
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from vnx_identity import (  # noqa: E402
    ENV_AGENT,
    ENV_OPERATOR,
    ENV_ORCHESTRATOR,
    ENV_PROJECT,
    ID_REGEX,
    IdentityError,
    PROJECT_FILE_NAME,
    REGISTRY_SCHEMA_VERSION,
    RESERVED_UNKNOWN,
    VnxIdentity,
    resolve_identity,
    try_resolve_identity,
    validate_id,
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (ENV_OPERATOR, ENV_PROJECT, ENV_ORCHESTRATOR, ENV_AGENT):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


@pytest.fixture
def isolated_cwd(tmp_path):
    """A temp dir that contains no .vnx-project-id and is outside any registry path."""
    return tmp_path / "scratch"


def _registry_path_with(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "projects.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# -----------------------------------------------------------------------
# Regex / validate_id
# -----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["vnx-dev", "mc", "a1", "abc-123-xyz", "x" * 32])
def test_validate_id_accepts_well_formed(value):
    assert validate_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1abc",        # must start with letter
        "ABC",         # uppercase rejected
        "vnx_dev",     # underscore rejected
        "x" * 33,      # too long
        "a",           # too short (regex requires 2+)
        " vnx",        # leading whitespace
        "vnx-DEV",     # uppercase mid-string
    ],
)
def test_validate_id_rejects_bad_input(value):
    with pytest.raises(IdentityError):
        validate_id(value)


def test_validate_id_reserved_unknown_only_with_flag():
    with pytest.raises(IdentityError):
        validate_id(RESERVED_UNKNOWN)
    assert validate_id(RESERVED_UNKNOWN, allow_unknown=True) == RESERVED_UNKNOWN


# -----------------------------------------------------------------------
# Resolution chain
# -----------------------------------------------------------------------


def test_resolve_identity_uses_env_first(clean_env, isolated_cwd, tmp_path):
    isolated_cwd.mkdir()
    clean_env.setenv(ENV_OPERATOR, "vincent-vd")
    clean_env.setenv(ENV_PROJECT, "vnx-dev")
    clean_env.setenv(ENV_ORCHESTRATOR, "dev-t0")
    clean_env.setenv(ENV_AGENT, "t1")
    identity = resolve_identity(cwd=isolated_cwd, registry_path=tmp_path / "absent.json")
    assert identity == VnxIdentity(
        operator_id="vincent-vd",
        project_id="vnx-dev",
        orchestrator_id="dev-t0",
        agent_id="t1",
    )


def test_resolve_identity_falls_back_to_project_file(clean_env, tmp_path):
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    (project_dir / PROJECT_FILE_NAME).write_text(
        "vnx-dev\ndev-t0\n\n", encoding="utf-8"
    )
    clean_env.setenv(ENV_OPERATOR, "vincent-vd")  # operator must come from somewhere
    identity = resolve_identity(cwd=project_dir, registry_path=tmp_path / "absent.json")
    assert identity.operator_id == "vincent-vd"
    assert identity.project_id == "vnx-dev"
    assert identity.orchestrator_id == "dev-t0"
    assert identity.agent_id is None


def test_resolve_identity_walks_up_for_project_file(clean_env, tmp_path):
    root = tmp_path / "root"
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (root / PROJECT_FILE_NAME).write_text("vnx-dev\n\n\n", encoding="utf-8")
    clean_env.setenv(ENV_OPERATOR, "vincent-vd")
    identity = resolve_identity(cwd=nested, registry_path=tmp_path / "absent.json")
    assert identity.project_id == "vnx-dev"


def test_resolve_identity_uses_registry_when_no_env_or_file(clean_env, tmp_path):
    project_dir = tmp_path / "registered-proj"
    project_dir.mkdir()
    registry = _registry_path_with(
        tmp_path,
        {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "operator_id": "vincent-vd",
            "projects": [
                {
                    "name": "test",
                    "project_id": "vnx-dev",
                    "path": str(project_dir),
                    "agents": {"orchestrator_id": "dev-t0", "agent_id": None},
                }
            ],
        },
    )
    identity = resolve_identity(cwd=project_dir, registry_path=registry)
    assert identity == VnxIdentity(
        operator_id="vincent-vd",
        project_id="vnx-dev",
        orchestrator_id="dev-t0",
        agent_id=None,
    )


def test_resolve_identity_registry_v1_ignored(clean_env, tmp_path):
    """v1 registry must not satisfy resolution — operator_id is required."""
    project_dir = tmp_path / "registered-proj"
    project_dir.mkdir()
    registry = _registry_path_with(
        tmp_path,
        {
            "schema_version": 1,
            "projects": [
                {
                    "name": "test",
                    "project_id": "vnx-dev",
                    "path": str(project_dir),
                }
            ],
        },
    )
    with pytest.raises(RuntimeError, match="identity resolution failed"):
        resolve_identity(cwd=project_dir, registry_path=registry)


def test_resolve_identity_raises_when_unresolvable(clean_env, isolated_cwd, tmp_path):
    isolated_cwd.mkdir()
    with pytest.raises(RuntimeError, match="identity resolution failed"):
        resolve_identity(cwd=isolated_cwd, registry_path=tmp_path / "absent.json")


def test_try_resolve_identity_returns_none_on_failure(clean_env, isolated_cwd, tmp_path):
    isolated_cwd.mkdir()
    assert try_resolve_identity(cwd=isolated_cwd, registry_path=tmp_path / "absent.json") is None


def test_resolve_identity_rejects_invalid_env_id(clean_env, isolated_cwd, tmp_path):
    isolated_cwd.mkdir()
    clean_env.setenv(ENV_OPERATOR, "BAD_OP")
    clean_env.setenv(ENV_PROJECT, "vnx-dev")
    with pytest.raises(IdentityError):
        resolve_identity(cwd=isolated_cwd, registry_path=tmp_path / "absent.json")


def test_resolve_identity_allows_unknown_when_explicit(clean_env, isolated_cwd, tmp_path):
    isolated_cwd.mkdir()
    clean_env.setenv(ENV_OPERATOR, RESERVED_UNKNOWN)
    clean_env.setenv(ENV_PROJECT, RESERVED_UNKNOWN)
    identity = resolve_identity(
        cwd=isolated_cwd,
        registry_path=tmp_path / "absent.json",
        allow_unknown=True,
    )
    assert identity.operator_id == RESERVED_UNKNOWN
    assert identity.project_id == RESERVED_UNKNOWN


# -----------------------------------------------------------------------
# VnxIdentity helpers
# -----------------------------------------------------------------------


def test_vnx_identity_to_env_skips_optional_when_absent():
    identity = VnxIdentity(operator_id="vincent-vd", project_id="vnx-dev")
    env = identity.to_env()
    assert env == {ENV_OPERATOR: "vincent-vd", ENV_PROJECT: "vnx-dev"}


def test_vnx_identity_to_env_includes_all_when_set():
    identity = VnxIdentity(
        operator_id="vincent-vd",
        project_id="vnx-dev",
        orchestrator_id="dev-t0",
        agent_id="t1",
    )
    env = identity.to_env()
    assert env == {
        ENV_OPERATOR: "vincent-vd",
        ENV_PROJECT: "vnx-dev",
        ENV_ORCHESTRATOR: "dev-t0",
        ENV_AGENT: "t1",
    }


def test_id_regex_anchored():
    assert ID_REGEX.match("ok") is not None
    assert ID_REGEX.match("ok\nXX") is None
