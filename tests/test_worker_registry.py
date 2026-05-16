"""Tests for worker_registry.py — YAML-driven worker registry."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from subprocess import CalledProcessError
from typing import Optional
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from worker_registry import (
    Pool,
    Worker,
    WorkerRegistry,
    _build_registry,
    _find_yaml_file,
    _HARDCODED_FALLBACK,
    _load_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALL_ROLES: Optional[frozenset] = None  # None = skip role validation in most tests


def _registry_from_yaml(text: str, valid_roles: Optional[frozenset] = _ALL_ROLES) -> WorkerRegistry:
    data = yaml.safe_load(text)
    return _build_registry(data, valid_roles)


MINIMAL_YAML = textwrap.dedent("""\
    schema_version: 1
    workers:
      - terminal_id: T0
        role: orchestrator
        provider: claude
        model: opus
        pool_id: core
        aliases: []
      - terminal_id: T1
        role: backend-developer
        provider: claude
        model: sonnet
        pool_id: core
        aliases: [backend, impl]
    pools:
      - pool_id: core
        min_workers: 2
        max_workers: 2
        scaling_policy: fixed
        provider_mix: []
""")

DEFAULT_YAML_TEXT = (_REPO_ROOT / ".vnx" / "vnx_workers.default.yaml").read_text()

SIX_WORKER_YAML_TEXT = (
    _REPO_ROOT / ".vnx" / "vnx_workers.examples" / "six_worker_pool.yaml"
).read_text()

PROVIDER_MIX_YAML_TEXT = (
    _REPO_ROOT / ".vnx" / "vnx_workers.examples" / "provider_mix.yaml"
).read_text()


# ---------------------------------------------------------------------------
# Basic loading tests
# ---------------------------------------------------------------------------

class TestDefaultYamlLoads:
    def test_loads_4_workers(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        assert len(reg.list_workers()) == 4

    def test_terminal_ids_are_t0_t3(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        ids = [w.terminal_id for w in reg.list_workers()]
        assert ids == ["T0", "T1", "T2", "T3"]

    def test_t0_is_orchestrator(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        t0 = reg.by_id("T0")
        assert t0 is not None
        assert t0.role == "orchestrator"
        assert t0.provider == "claude"

    def test_t1_is_backend_developer(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        t1 = reg.by_id("T1")
        assert t1 is not None
        assert t1.role == "backend-developer"


# ---------------------------------------------------------------------------
# by_id
# ---------------------------------------------------------------------------

class TestById:
    def test_returns_worker_for_known_id(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        worker = reg.by_id("T1")
        assert worker is not None
        assert worker.terminal_id == "T1"

    def test_returns_none_for_unknown_id(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        assert reg.by_id("T99") is None

    def test_returns_none_for_empty_string(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        assert reg.by_id("") is None


# ---------------------------------------------------------------------------
# by_role
# ---------------------------------------------------------------------------

class TestByRole:
    def test_returns_matching_workers(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        workers = reg.by_role("backend-developer")
        assert len(workers) == 2
        ids = {w.terminal_id for w in workers}
        assert ids == {"T1", "T2"}

    def test_returns_empty_for_unknown_role(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        assert reg.by_role("nonexistent-role") == []

    def test_orchestrator_role(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        orch = reg.by_role("orchestrator")
        assert len(orch) == 1
        assert orch[0].terminal_id == "T0"


# ---------------------------------------------------------------------------
# by_pool
# ---------------------------------------------------------------------------

class TestByPool:
    def test_returns_all_workers_in_pool(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        workers = reg.by_pool("default")
        assert len(workers) == 4

    def test_returns_empty_for_unknown_pool(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        assert reg.by_pool("nonexistent") == []

    def test_pool_filter_works_with_multiple_pools(self):
        reg = _registry_from_yaml(SIX_WORKER_YAML_TEXT)
        primary = reg.by_pool("primary")
        reserve = reg.by_pool("reserve")
        assert len(primary) == 3
        assert len(reserve) == 2


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

class TestAliasResolution:
    def test_resolve_alias_returns_worker(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        worker = reg.resolve_alias("backend")
        assert worker is not None
        assert worker.terminal_id == "T1"

    def test_resolve_second_alias(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        worker = reg.resolve_alias("impl")
        assert worker is not None
        assert worker.terminal_id == "T1"

    def test_resolve_unknown_alias_returns_none(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        assert reg.resolve_alias("unknown") is None

    def test_aliases_for_returns_list(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        aliases = reg.aliases_for("T1")
        assert set(aliases) == {"backend", "impl"}

    def test_aliases_for_unknown_terminal_returns_empty(self):
        reg = _registry_from_yaml(MINIMAL_YAML)
        assert reg.aliases_for("TX") == []

    def test_aliases_for_worker_with_no_aliases(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        assert reg.aliases_for("T0") == []


# ---------------------------------------------------------------------------
# Duplicate alias detection
# ---------------------------------------------------------------------------

class TestDuplicateAlias:
    def test_duplicate_alias_raises_on_load(self):
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: pool1
                aliases: [shared]
              - terminal_id: T2
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: pool1
                aliases: [shared]
            pools:
              - pool_id: pool1
                min_workers: 2
                max_workers: 2
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="Duplicate alias"):
            _registry_from_yaml(bad_yaml)


# ---------------------------------------------------------------------------
# Duplicate terminal_id detection
# ---------------------------------------------------------------------------

class TestDuplicateTerminalId:
    def test_duplicate_terminal_id_raises_on_load(self):
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: pool1
                aliases: []
              - terminal_id: T1
                role: reviewer
                provider: claude
                model: sonnet
                pool_id: pool1
                aliases: []
            pools:
              - pool_id: pool1
                min_workers: 2
                max_workers: 2
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="duplicate terminal_id"):
            _registry_from_yaml(bad_yaml)

    def test_unique_terminal_ids_load_fine(self):
        ok_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: pool1
                aliases: []
              - terminal_id: T2
                role: reviewer
                provider: claude
                model: sonnet
                pool_id: pool1
                aliases: []
            pools:
              - pool_id: pool1
                min_workers: 2
                max_workers: 2
                scaling_policy: fixed
                provider_mix: []
        """)
        reg = _registry_from_yaml(ok_yaml)
        assert len(reg.list_workers()) == 2


# ---------------------------------------------------------------------------
# Terminal ID validation
# ---------------------------------------------------------------------------

class TestTerminalIdValidation:
    def test_invalid_terminal_id_starting_with_digit(self):
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: "1T"
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="Invalid terminal_id"):
            _registry_from_yaml(bad_yaml)

    def test_valid_custom_prefix_terminal_id(self):
        ok_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: WORKER1
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        reg = _registry_from_yaml(ok_yaml)
        assert reg.by_id("WORKER1") is not None


# ---------------------------------------------------------------------------
# Role validation
# ---------------------------------------------------------------------------

class TestRoleValidation:
    def test_invalid_role_rejected_when_roles_provided(self):
        valid_roles = frozenset({"backend-developer", "reviewer"})
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: made-up-role
                provider: claude
                model: sonnet
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="Invalid role"):
            _registry_from_yaml(bad_yaml, valid_roles=valid_roles)

    def test_orchestrator_always_valid(self):
        valid_roles = frozenset({"backend-developer"})
        yaml_text = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T0
                role: orchestrator
                provider: claude
                model: opus
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        reg = _registry_from_yaml(yaml_text, valid_roles=valid_roles)
        assert reg.by_id("T0").role == "orchestrator"

    def test_none_valid_roles_skips_validation(self):
        yaml_text = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: any-role-at-all
                provider: claude
                model: sonnet
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        reg = _registry_from_yaml(yaml_text, valid_roles=None)
        assert reg.by_id("T1").role == "any-role-at-all"

    def test_role_validation_raises_on_subprocess_crash(self, tmp_path):
        """_load_registry propagates CalledProcessError when validate_skill.py exits non-zero."""
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        (scripts_dir / "validate_skill.py").write_text("# stub — exists so file check passes")

        with patch("worker_registry.subprocess.run", side_effect=CalledProcessError(1, "validate_skill.py")):
            with pytest.raises(CalledProcessError):
                _load_registry(repo_root=tmp_path)


# ---------------------------------------------------------------------------
# Provider validation
# ---------------------------------------------------------------------------

class TestProviderValidation:
    def test_invalid_provider_rejected(self):
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: backend-developer
                provider: unknown-llm
                model: something
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="Invalid provider"):
            _registry_from_yaml(bad_yaml)

    def test_claude_provider_valid(self):
        reg = _registry_from_yaml(DEFAULT_YAML_TEXT)
        assert all(w.provider == "claude" for w in reg.list_workers())

    def test_litellm_provider_valid(self):
        reg = _registry_from_yaml(PROVIDER_MIX_YAML_TEXT)
        providers = {w.provider for w in reg.list_workers()}
        assert "litellm:openai" in providers or "litellm:deepseek" in providers

    def test_codex_provider_valid(self):
        reg = _registry_from_yaml(PROVIDER_MIX_YAML_TEXT)
        providers = {w.provider for w in reg.list_workers()}
        assert "codex" in providers

    def test_litellm_without_sub_invalid(self):
        # "litellm:" as a quoted YAML string (bare litellm: is a parse error)
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: backend-developer
                provider: "litellm:"
                model: something
                pool_id: p
                aliases: []
            pools:
              - pool_id: p
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="Invalid provider"):
            _registry_from_yaml(bad_yaml)


# ---------------------------------------------------------------------------
# Operator YAML override
# ---------------------------------------------------------------------------

class TestOperatorYamlOverride:
    def test_operator_yaml_overrides_default(self, tmp_path: Path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        operator_yaml = vnx_dir / "vnx_workers.yaml"
        operator_yaml.write_text(textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: A1
                role: backend-developer
                provider: claude
                model: haiku
                pool_id: custom
                aliases: []
            pools:
              - pool_id: custom
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """), encoding="utf-8")
        (vnx_dir / "vnx_workers.default.yaml").write_text(DEFAULT_YAML_TEXT, encoding="utf-8")

        reg = _load_registry(repo_root=tmp_path)
        workers = reg.list_workers()
        assert len(workers) == 1
        assert workers[0].terminal_id == "A1"

    def test_missing_yaml_falls_back_to_hardcoded_default(self, tmp_path: Path):
        reg = _load_registry(repo_root=tmp_path)
        ids = [w.terminal_id for w in reg.list_workers()]
        assert ids == ["T0", "T1", "T2", "T3"]

    def test_default_yaml_used_when_no_operator_override(self, tmp_path: Path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        (vnx_dir / "vnx_workers.default.yaml").write_text(DEFAULT_YAML_TEXT, encoding="utf-8")

        reg = _load_registry(repo_root=tmp_path)
        assert len(reg.list_workers()) == 4


# ---------------------------------------------------------------------------
# Pool reference validation
# ---------------------------------------------------------------------------

class TestPoolReference:
    def test_pool_reference_must_exist(self):
        bad_yaml = textwrap.dedent("""\
            schema_version: 1
            workers:
              - terminal_id: T1
                role: backend-developer
                provider: claude
                model: sonnet
                pool_id: nonexistent-pool
                aliases: []
            pools:
              - pool_id: actual-pool
                min_workers: 1
                max_workers: 1
                scaling_policy: fixed
                provider_mix: []
        """)
        with pytest.raises(ValueError, match="nonexistent-pool"):
            _registry_from_yaml(bad_yaml)


# ---------------------------------------------------------------------------
# provider_mix validation in pool
# ---------------------------------------------------------------------------

class TestProviderMixValidation:
    def test_provider_mix_loads_correctly(self):
        reg = _registry_from_yaml(PROVIDER_MIX_YAML_TEXT)
        mixed_pool = reg.pool("mixed")
        assert mixed_pool is not None
        assert "claude" in mixed_pool.provider_mix


# ---------------------------------------------------------------------------
# Example files load without error
# ---------------------------------------------------------------------------

class TestExampleFiles:
    def test_six_worker_example_loads(self):
        reg = _registry_from_yaml(SIX_WORKER_YAML_TEXT)
        assert len(reg.list_workers()) == 6

    def test_provider_mix_example_loads(self):
        reg = _registry_from_yaml(PROVIDER_MIX_YAML_TEXT)
        assert len(reg.list_workers()) == 6

    def test_single_worker_example_loads(self):
        single_yaml = (
            _REPO_ROOT / ".vnx" / "vnx_workers.examples" / "single_worker.yaml"
        ).read_text()
        reg = _registry_from_yaml(single_yaml)
        assert len(reg.list_workers()) == 2


# ---------------------------------------------------------------------------
# find_yaml_file
# ---------------------------------------------------------------------------

class TestFindYamlFile:
    def test_prefers_operator_yaml(self, tmp_path: Path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        op = vnx_dir / "vnx_workers.yaml"
        op.write_text("schema_version: 1\nworkers: []\npools: []")
        default = vnx_dir / "vnx_workers.default.yaml"
        default.write_text("schema_version: 1\nworkers: []\npools: []")

        found = _find_yaml_file(tmp_path)
        assert found == op

    def test_falls_back_to_default_yaml(self, tmp_path: Path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        default = vnx_dir / "vnx_workers.default.yaml"
        default.write_text("schema_version: 1\nworkers: []\npools: []")

        found = _find_yaml_file(tmp_path)
        assert found == default

    def test_returns_none_when_neither_exists(self, tmp_path: Path):
        assert _find_yaml_file(tmp_path) is None
