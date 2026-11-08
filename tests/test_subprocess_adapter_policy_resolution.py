"""Dispatch-side policy resolution refuses on an unexpected failure (permissions P2 r4)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

import subprocess_adapter as sa
from worker_permissions import PermissionPolicyRefused, UnknownRoleError


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setenv("VNX_ENFORCE_WORKER_PERMISSIONS", "1")


def test_unexpected_resolution_exception_refuses_with_the_slug(scoped):
    with patch.object(sa, "resolve_worker_profile", side_effect=RuntimeError("secret text")):
        with pytest.raises(PermissionPolicyRefused) as info:
            sa._build_worker_scope_args("code-reviewer")
    assert info.value.reason == "policy-resolution-error"
    assert "RuntimeError" in str(info.value) and "secret text" not in str(info.value)
    assert sa._refusal_slug(info.value) == "policy-resolution-error"


def test_unknown_role_still_refuses_with_its_legacy_slug(scoped):
    with patch.object(sa, "resolve_worker_profile", side_effect=UnknownRoleError("absent")):
        with pytest.raises(UnknownRoleError) as info:
            sa._build_worker_scope_args("ghost")
    assert sa._refusal_slug(info.value) == "unknown_role"
