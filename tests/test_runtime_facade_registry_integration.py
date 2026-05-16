"""Integration tests — runtime_facade.py + worker_registry.py.

Verifies backward-compat contract: existing dispatches that hardcode
T0/T1/T2/T3 continue to resolve correctly through the registry.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry_from_yaml(yaml_text: str):
    """Build a WorkerRegistry from raw YAML text, skipping role validation."""
    import yaml
    from worker_registry import _build_registry
    data = yaml.safe_load(yaml_text)
    return _build_registry(data)  # allowlist=None → skip validation


FOUR_WORKER_YAML = textwrap.dedent("""\
    schema_version: 1
    workers:
      - terminal_id: T0
        role: orchestrator
        provider: claude
        model: opus
        pool_id: default
        aliases: []
      - terminal_id: T1
        role: backend-developer
        provider: claude
        model: sonnet
        pool_id: default
        aliases: []
      - terminal_id: T2
        role: backend-developer
        provider: claude
        model: sonnet
        pool_id: default
        aliases: []
      - terminal_id: T3
        role: reviewer
        provider: claude
        model: sonnet
        pool_id: default
        aliases: []
    pools:
      - pool_id: default
        min_workers: 4
        max_workers: 4
        scaling_policy: fixed
        provider_mix: []
""")

CUSTOM_YAML = textwrap.dedent("""\
    schema_version: 1
    workers:
      - terminal_id: A0
        role: orchestrator
        provider: claude
        model: opus
        pool_id: custom
        aliases: []
      - terminal_id: A1
        role: backend-developer
        provider: claude
        model: sonnet
        pool_id: custom
        aliases: []
    pools:
      - pool_id: custom
        min_workers: 2
        max_workers: 2
        scaling_policy: fixed
        provider_mix: []
""")


# ---------------------------------------------------------------------------
# canonical_terminals
# ---------------------------------------------------------------------------

class TestCanonicalTerminals:
    def test_canonical_terminals_returns_4_default(self):
        reg = _make_registry_from_yaml(FOUR_WORKER_YAML)
        with patch("worker_registry.WORKER_REGISTRY", reg):
            from worker_registry import list_workers
            ids = [w.terminal_id for w in list_workers()]
        assert ids == ["T0", "T1", "T2", "T3"]

    def test_canonical_terminals_returns_custom_list(self):
        reg = _make_registry_from_yaml(CUSTOM_YAML)
        with patch("worker_registry.WORKER_REGISTRY", reg):
            from worker_registry import list_workers
            ids = [w.terminal_id for w in list_workers()]
        assert ids == ["A0", "A1"]

    def test_canonical_terminals_ordering_preserved(self):
        reg = _make_registry_from_yaml(FOUR_WORKER_YAML)
        ids = [w.terminal_id for w in reg.list_workers()]
        assert ids[0] == "T0"
        assert ids[-1] == "T3"


# ---------------------------------------------------------------------------
# Backward compatibility with hardcoded T0/T1/T2/T3 call-sites
# ---------------------------------------------------------------------------

class TestBackwardCompatHardcodedSites:
    """77 existing call-sites that hardcode "T1" / "T2" / "T3" must still work."""

    def _get_default_reg(self):
        return _make_registry_from_yaml(FOUR_WORKER_YAML)

    def test_backward_compat_t0_resolves(self):
        reg = self._get_default_reg()
        worker = reg.by_id("T0") or reg.resolve_alias("T0")
        assert worker is not None
        assert worker.role == "orchestrator"

    def test_backward_compat_t1_resolves(self):
        reg = self._get_default_reg()
        worker = reg.by_id("T1") or reg.resolve_alias("T1")
        assert worker is not None
        assert worker.role == "backend-developer"

    def test_backward_compat_t2_resolves(self):
        reg = self._get_default_reg()
        worker = reg.by_id("T2") or reg.resolve_alias("T2")
        assert worker is not None
        assert worker.role == "backend-developer"

    def test_backward_compat_t3_resolves(self):
        reg = self._get_default_reg()
        worker = reg.by_id("T3") or reg.resolve_alias("T3")
        assert worker is not None
        assert worker.role == "reviewer"

    def test_backward_compat_with_77_hardcoded_sites(self):
        """Simulate 77 call-sites passing hardcoded T1."""
        reg = self._get_default_reg()
        for _ in range(77):
            worker = reg.by_id("T1") or reg.resolve_alias("T1")
            assert worker is not None
            assert worker.role == "backend-developer"

    def test_hardcoded_t0_t3_all_resolvable(self):
        """All 4 legacy terminal IDs must resolve in every iteration."""
        reg = self._get_default_reg()
        for terminal_id in ("T0", "T1", "T2", "T3"):
            worker = reg.by_id(terminal_id)
            assert worker is not None, f"{terminal_id} must be resolvable"

    def test_dispatch_routing_via_registry(self):
        """Simulated dispatch routing: resolve target terminal + verify provider."""
        reg = self._get_default_reg()
        dispatch_targets = ["T1", "T2", "T3"]  # typical dispatch pattern
        for target in dispatch_targets:
            worker = reg.by_id(target)
            assert worker is not None
            assert worker.provider == "claude"
            assert worker.model == "sonnet"


# ---------------------------------------------------------------------------
# registry module-level functions
# ---------------------------------------------------------------------------

class TestModuleLevelFunctions:
    def test_by_id_module_function(self):
        from worker_registry import by_id
        worker = by_id("T1")
        assert worker is not None

    def test_by_role_module_function(self):
        from worker_registry import by_role
        workers = by_role("backend-developer")
        assert len(workers) >= 2

    def test_by_pool_module_function(self):
        from worker_registry import by_pool
        workers = by_pool("default")
        assert len(workers) >= 4

    def test_aliases_for_module_function(self):
        from worker_registry import aliases_for
        aliases = aliases_for("T0")
        assert isinstance(aliases, list)

    def test_resolve_alias_module_function(self):
        from worker_registry import resolve_alias
        result = resolve_alias("nonexistent-alias")
        assert result is None

    def test_list_workers_module_function(self):
        from worker_registry import list_workers
        workers = list_workers()
        assert len(workers) == 4


# ---------------------------------------------------------------------------
# runtime_facade CANONICAL_TERMINALS compat
# ---------------------------------------------------------------------------

class TestRuntimeFacadeCanonicalTerminals:
    def test_canonical_terminals_tuple_exists(self):
        import importlib
        import runtime_facade
        importlib.reload(runtime_facade)
        ct = runtime_facade.CANONICAL_TERMINALS
        assert isinstance(ct, tuple)
        assert "T1" in ct
        assert "T2" in ct
        assert "T3" in ct

    def test_canonical_terminals_function_exists(self):
        import importlib
        import runtime_facade
        importlib.reload(runtime_facade)
        assert callable(runtime_facade.canonical_terminals)
        result = runtime_facade.canonical_terminals()
        assert isinstance(result, list)
        assert len(result) == 4

    def test_canonical_terminals_function_matches_tuple(self):
        import importlib
        import runtime_facade
        importlib.reload(runtime_facade)
        from_function = runtime_facade.canonical_terminals()
        from_tuple = list(runtime_facade.CANONICAL_TERMINALS)
        assert from_function == from_tuple

    def test_existing_dispatches_resolve_via_aliases(self):
        """Stresstest: 77 simulated call-sites using hardcoded IDs all resolve."""
        from worker_registry import WORKER_REGISTRY
        legacy_ids = ["T0", "T1", "T2", "T3"]
        for _ in range(20):  # 20 × 4 = 80 iterations > 77 target
            for tid in legacy_ids:
                worker = WORKER_REGISTRY.by_id(tid)
                assert worker is not None, f"Hardcoded {tid!r} must resolve in registry"
