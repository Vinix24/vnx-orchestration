"""test_dispatch_oi921_role_validation.py — OI-921: role validation is enforced, not a silent default.

Closes the closed non-enforcement chain end to end:
  1. dispatch_bridge.stage_spec_bundle must NOT silently fill the backend-developer
     sentinel — an unset role is staged as an explicit "" so the door rejects it
     loud, and the distinction with a conscious "backend-developer" is preserved.
  2. compile_plan implements the registry validation dispatch_spec Rule 7 defers:
     the role must exist in agents/ (unknown-role Reject otherwise).
  3. A consciously chosen "backend-developer" (a real agents/ role) still passes —
     only the SILENT default was the defect, never the value itself.

Tests 1 and 2 are red on origin/main (main silently defaults / never validates).
Test 3 is green on main and stays green here — it guards against overreach.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import dispatch_bridge  # noqa: E402
import dispatch_cli  # noqa: E402
from dispatch_plan import RuntimeSnapshot, compile_plan  # noqa: E402
from dispatch_spec import (  # noqa: E402
    DispatchSpec,
    Provider,
    Reject,
    ValidatedSpec,
)

_REPO_ROOT = _LIB.parents[1]
_GOOD_ID = "20260801-120000-oi921"

# The registry-discovery helper is new in OI-921; on origin/main the symbol does not
# exist, so its tests are skipped there (the red-on-main proof for this item is the
# bridge-no-silent-default and compile_plan-unknown-role tests, not discovery).
_DISCOVER_VALID_ROLES = getattr(dispatch_cli, "_discover_valid_roles", None)


def _stage(tmp_path, **over):
    """Stage a spec bundle under an isolated tmp_path data root."""
    base = dict(
        instruction_text="do the thing", dispatch_id=_GOOD_ID, role="",
        target_slot="T1", project_id="p1", provider="claude", data_dir=tmp_path,
    )
    base.update(over)
    return dispatch_bridge.stage_spec_bundle(**base)


def _snapshot(valid_roles: frozenset[str]) -> RuntimeSnapshot:
    """Build a promoted RuntimeSnapshot carrying the role registry.

    Compatible with origin/main, where RuntimeSnapshot has no ``valid_roles`` field:
    a TypeError there falls back to a plain snapshot, so the red-on-main proof for
    the unknown-role test is the assertion failure ("main does not reject"), not a
    collection/kwarg error.
    """
    try:
        return RuntimeSnapshot(staging_promoted=True, valid_roles=valid_roles)
    except TypeError:  # origin/main: no valid_roles field
        return RuntimeSnapshot(staging_promoted=True)


def _make_vspec(role: str, *, tmp_path: Path) -> ValidatedSpec:
    """Build a ValidatedSpec for compile_plan with the given role."""
    ifile = tmp_path / "instruction.md"
    ifile.write_text("# Do the work\n", encoding="utf-8")
    spec = DispatchSpec(
        schema_version=1,
        project_id="vnx-dev",
        dispatch_id="oi921-test-dispatch",
        staging_id="oi921-test-staging",
        instruction_file=ifile,
        role=role,
        target_slot="T1",
        gate="codex_gate",
        dispatch_paths=(),
        provider=Provider.CLAUDE,
        model=None,
    )
    text = ifile.read_text(encoding="utf-8")
    return ValidatedSpec(
        spec=spec,
        instruction_text=text,
        normalized_paths=(),
        instruction_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


# ---------------------------------------------------------------------------
# 1. Bridge: an unset role is never silently defaulted to backend-developer
# ---------------------------------------------------------------------------

class TestBridgeDoesNotSilentlyDefaultRole:
    def test_empty_role_staged_as_explicit_empty(self, tmp_path):
        """Red on main: main writes role='backend-developer' for an empty role."""
        payload = json.loads(_stage(tmp_path, role="").read_text(encoding="utf-8"))
        assert payload["role"] == ""
        assert payload["role"] != "backend-developer"

    def test_none_role_staged_as_explicit_empty(self, tmp_path):
        """Red on main: main writes role='backend-developer' for a None role."""
        payload = json.loads(_stage(tmp_path, role=None).read_text(encoding="utf-8"))
        assert payload["role"] == ""

    def test_whitespace_role_staged_as_explicit_empty(self, tmp_path):
        """A whitespace-only role is indistinguishable from no role — stays empty."""
        payload = json.loads(_stage(tmp_path, role="   ").read_text(encoding="utf-8"))
        assert payload["role"] == ""

    def test_conscious_backend_developer_is_preserved(self, tmp_path):
        """The VALUE is never the problem — a conscious choice passes through verbatim."""
        payload = json.loads(
            _stage(tmp_path, role="backend-developer").read_text(encoding="utf-8")
        )
        assert payload["role"] == "backend-developer"

    def test_other_explicit_role_is_preserved(self, tmp_path):
        """A non-default explicit role is preserved, not coerced."""
        payload = json.loads(
            _stage(tmp_path, role="system-architect").read_text(encoding="utf-8")
        )
        assert payload["role"] == "system-architect"

    def test_an_empty_role_fails_the_door_loud(self, tmp_path, monkeypatch):
        """End-to-end bridge → door: an unset role is rejected (bad-role), not silently run."""
        monkeypatch.setenv("VNX_SINGLE_ENTRY_DISPATCH", "1")
        rc = dispatch_bridge.bridge_dispatch(
            instruction_text="x", dispatch_id=_GOOD_ID, role="",
            target_slot="T1", project_id="p1", data_dir=tmp_path, dry_run=True,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# 2. compile_plan: the deferred registry validation is actually built
# ---------------------------------------------------------------------------

class TestCompilePlanRoleRegistry:
    def test_unknown_role_is_rejected(self, tmp_path):
        """Red on main: main compiles a plan for ANY non-empty role."""
        vspec = _make_vspec("not-a-real-role", tmp_path=tmp_path)
        result = compile_plan(
            vspec,
            _snapshot(frozenset({
                "backend-developer", "code-reviewer", "system-architect",
            })),
        )
        assert isinstance(result, Reject)
        assert result.code == "unknown-role"
        assert "backend-developer" in result.reason

    def test_every_agent_role_is_accepted(self, tmp_path):
        """Each real agents/ role compiles a plan when the registry lists it."""
        roles = {
            "backend-developer", "blog-writer", "code-reviewer", "frontend-developer",
            "linkedin-writer", "orchestrator", "quality-engineer", "research-analyst",
            "security-engineer", "system-architect",
        }
        for role in roles:
            vspec = _make_vspec(role, tmp_path=tmp_path)
            result = compile_plan(vspec, _snapshot(frozenset(roles)))
            assert not isinstance(result, Reject), f"role {role!r} rejected: {result}"

    def test_empty_registry_fails_closed(self, tmp_path):
        """An empty valid_roles set (undiscoverable agents/ dir) rejects EVERY role."""
        vspec = _make_vspec("backend-developer", tmp_path=tmp_path)
        result = compile_plan(vspec, _snapshot(frozenset()))
        assert isinstance(result, Reject)
        assert result.code == "unknown-role"

    def test_without_valid_roles_keeps_legacy_behavior(self, tmp_path):
        """valid_roles=None (registry not provided) skips the check — direct callers
        and pre-OI-921 tests keep working unchanged."""
        vspec = _make_vspec("backend-developer", tmp_path=tmp_path)
        result = compile_plan(vspec, RuntimeSnapshot(staging_promoted=True))
        assert not isinstance(result, Reject)

    def test_conscious_backend_developer_passes_with_registry(self, tmp_path):
        """Green on main AND here: a consciously chosen backend-developer is valid."""
        vspec = _make_vspec("backend-developer", tmp_path=tmp_path)
        result = compile_plan(
            vspec,
            _snapshot(frozenset({"backend-developer", "code-reviewer"})),
        )
        assert not isinstance(result, Reject)


# ---------------------------------------------------------------------------
# 3. dispatch_cli: the door's snapshot carries the agents/ registry
# ---------------------------------------------------------------------------

class TestDiscoverValidRoles:
    pytestmark = pytest.mark.skipif(
        _DISCOVER_VALID_ROLES is None,
        reason="OI-921 registry discovery not present on this source revision",
    )

    def test_discovers_real_agents_registry(self):
        """The engine agents/ dir yields exactly the ten documented roles."""
        roles = _DISCOVER_VALID_ROLES(_REPO_ROOT / "agents")
        assert {
            "backend-developer", "blog-writer", "code-reviewer", "frontend-developer",
            "linkedin-writer", "orchestrator", "quality-engineer", "research-analyst",
            "security-engineer", "system-architect",
        }.issubset(roles)
        assert "backend-developer" in roles

    def test_missing_agents_dir_is_empty(self, tmp_path):
        """A missing registry dir → empty set (compile_plan fails closed)."""
        assert _DISCOVER_VALID_ROLES(tmp_path / "no-agents-here") == frozenset()
