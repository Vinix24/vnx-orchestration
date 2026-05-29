#!/usr/bin/env python3
"""tests/test_cheap_lane_default.py — cheap-lanes DEFAULT-ON for fresh installs.

Verifies that:
  1. `vnx init` writes `routing_policy_enabled: true` to .vnx/config.yml.
  2. `_routing_enabled_from_project_config` reads that flag correctly.
  3. `_select_dispatch_path` routes via project config when VNX_ROUTING_POLICY_ENABLED
     is absent from the environment (the launch-gap fix).
  4. Explicit VNX_ROUTING_POLICY_ENABLED=0 opts out regardless of config.
  5. Existing checkouts without a config file behave as before (no silent flip).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(REPO_ROOT))

from subprocess_dispatch import (
    _routing_enabled_from_project_config,
    _select_dispatch_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project_config(tmp_path: Path, *, routing_enabled: bool) -> Path:
    """Write a minimal .vnx/config.yml into tmp_path and return its path."""
    vnx_dir = tmp_path / ".vnx"
    vnx_dir.mkdir(parents=True, exist_ok=True)
    config = vnx_dir / "config.yml"
    value = "true" if routing_enabled else "false"
    config.write_text(
        f'project_root: "{tmp_path}"\n'
        f'project_id: "test-proj"\n'
        f'routing_policy_enabled: {value}\n',
        encoding="utf-8",
    )
    return config


# ---------------------------------------------------------------------------
# _routing_enabled_from_project_config
# ---------------------------------------------------------------------------

class TestRoutingEnabledFromProjectConfig:
    def test_returns_false_when_no_project_root(self):
        assert _routing_enabled_from_project_config({}) is False

    def test_returns_false_when_no_config_file(self, tmp_path):
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(tmp_path)}) is False

    def test_returns_true_when_config_has_true(self, tmp_path):
        _make_project_config(tmp_path, routing_enabled=True)
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(tmp_path)}) is True

    def test_returns_false_when_config_has_false(self, tmp_path):
        _make_project_config(tmp_path, routing_enabled=False)
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(tmp_path)}) is False

    def test_returns_false_when_config_missing_key(self, tmp_path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        (vnx_dir / "config.yml").write_text('project_root: "/x"\n', encoding="utf-8")
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(tmp_path)}) is False

    def test_returns_false_on_malformed_yaml(self, tmp_path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        (vnx_dir / "config.yml").write_text(":: this is not valid yaml ::\n", encoding="utf-8")
        # Must not raise
        result = _routing_enabled_from_project_config({"PROJECT_ROOT": str(tmp_path)})
        assert result is False

    def test_returns_false_on_non_mapping_yaml(self, tmp_path):
        vnx_dir = tmp_path / ".vnx"
        vnx_dir.mkdir()
        (vnx_dir / "config.yml").write_text("- item1\n- item2\n", encoding="utf-8")
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(tmp_path)}) is False

    def test_path_with_spaces_reads_correctly(self, tmp_path):
        """PROJECT_ROOT containing spaces must not break config reads."""
        spaced = tmp_path / "my project root"
        spaced.mkdir()
        _make_project_config(spaced, routing_enabled=True)
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(spaced)}) is True

    def test_path_with_single_quote_reads_correctly(self, tmp_path):
        """PROJECT_ROOT containing a single quote must not break config reads."""
        quoted = tmp_path / "vincent's project"
        quoted.mkdir()
        _make_project_config(quoted, routing_enabled=True)
        assert _routing_enabled_from_project_config({"PROJECT_ROOT": str(quoted)}) is True


# ---------------------------------------------------------------------------
# _select_dispatch_path — config-based default-on
# ---------------------------------------------------------------------------

class TestSelectDispatchPathDefaultOn:
    """When VNX_ROUTING_POLICY_ENABLED is absent, project config drives routing."""

    def test_routes_when_config_enabled_and_no_env_var(self, tmp_path):
        """Fresh install: config enabled, no env var → routing active."""
        _make_project_config(tmp_path, routing_enabled=True)
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={"PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"
        assert model == "sonnet"

    def test_no_routing_when_config_disabled_and_no_env_var(self, tmp_path):
        """Config explicitly disabled → no routing."""
        _make_project_config(tmp_path, routing_enabled=False)
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={"PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap is None
        assert model == "sonnet"

    def test_no_routing_when_no_config_and_no_env_var(self, tmp_path):
        """Existing checkout (no config.yml, no env var) → unchanged behaviour."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={"PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap is None
        assert model == "sonnet"

    def test_explicit_env_var_1_overrides_missing_config(self, tmp_path):
        """Explicit VNX_ROUTING_POLICY_ENABLED=1 always enables, no config needed."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={"VNX_ROUTING_POLICY_ENABLED": "1"},  # no PROJECT_ROOT
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default"

    def test_explicit_env_var_0_disables_even_with_config_enabled(self, tmp_path):
        """VNX_ROUTING_POLICY_ENABLED=0 opts out regardless of config file."""
        _make_project_config(tmp_path, routing_enabled=True)
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={"VNX_ROUTING_POLICY_ENABLED": "0", "PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap is None
        assert model == "sonnet"

    def test_empty_task_class_returns_defaults_even_with_config(self, tmp_path):
        """Empty task_class short-circuits even when project config is enabled."""
        _make_project_config(tmp_path, routing_enabled=True)
        cheap, model = _select_dispatch_path(
            task_class="",
            complexity="medium",
            current_model="sonnet",
            env={"PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap is None
        assert model == "sonnet"

    def test_auto_route_applied_skips_routing_even_with_config(self, tmp_path):
        """smart_router precedence is preserved even when project config is enabled."""
        _make_project_config(tmp_path, routing_enabled=True)
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="haiku",
            auto_route_applied=True,
            env={"PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap is None
        assert model == "haiku"

    def test_existing_checkout_no_env_no_project_root_unchanged(self):
        """Completely bare env (no PROJECT_ROOT, no flag) → legacy disabled behaviour."""
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={},
        )
        assert cheap is None
        assert model == "sonnet"


# ---------------------------------------------------------------------------
# vnx init writes routing_policy_enabled into config.yml
# ---------------------------------------------------------------------------

class TestVnxInitWritesRoutingConfig:
    def test_fresh_init_config_has_routing_enabled(self, tmp_path):
        """vnx init must write routing_policy_enabled: true to .vnx/config.yml."""
        import argparse as _ap
        from vnx_cli.commands.init_cmd import vnx_init

        ns = _ap.Namespace(
            project_path=None,
            project_dir=str(tmp_path),
            project_id=None,
            template="default",
            force=False,
            non_interactive=False,
        )
        rc = vnx_init(ns)
        assert rc == 0, "vnx init must return 0"

        config = tmp_path / ".vnx" / "config.yml"
        assert config.is_file(), ".vnx/config.yml must exist after init"

        import yaml
        data = yaml.safe_load(config.read_text())
        assert isinstance(data, dict), "config.yml must be a YAML mapping"
        assert data.get("routing_policy_enabled") is True, (
            "routing_policy_enabled must be true in freshly generated config.yml"
        )

    def test_fresh_init_config_enables_routing_via_select_dispatch(self, tmp_path):
        """End-to-end: vnx init → config.yml → _select_dispatch_path routes cheap."""
        import argparse as _ap
        from vnx_cli.commands.init_cmd import vnx_init

        ns = _ap.Namespace(
            project_path=None,
            project_dir=str(tmp_path),
            project_id=None,
            template="default",
            force=False,
            non_interactive=False,
        )
        vnx_init(ns)

        # Simulate a fresh subprocess_dispatch call: no VNX_ROUTING_POLICY_ENABLED,
        # but PROJECT_ROOT set to the newly initialised project.
        cheap, model = _select_dispatch_path(
            task_class="code-review",
            complexity="medium",
            current_model="sonnet",
            env={"PROJECT_ROOT": str(tmp_path)},
        )
        assert cheap == "litellm:moonshot:kimi-k2-0905-default", (
            "code-review must route to kimi cheap lane after vnx init"
        )
        assert model == "sonnet"

    def test_init_force_rewrites_routing_enabled(self, tmp_path):
        """--force must write routing_policy_enabled: true even when reinitialising."""
        import argparse as _ap
        from vnx_cli.commands.init_cmd import vnx_init

        ns = _ap.Namespace(
            project_path=None,
            project_dir=str(tmp_path),
            project_id=None,
            template="default",
            force=False,
            non_interactive=False,
        )
        vnx_init(ns)

        # Manually disable routing in config
        config = tmp_path / ".vnx" / "config.yml"
        config.write_text(
            config.read_text().replace("routing_policy_enabled: true", "routing_policy_enabled: false"),
            encoding="utf-8",
        )

        # Force-reinit must restore routing_policy_enabled: true
        ns_force = _ap.Namespace(
            project_path=None,
            project_dir=str(tmp_path),
            project_id=None,
            template="default",
            force=True,
            non_interactive=False,
        )
        rc = vnx_init(ns_force)
        assert rc == 0

        import yaml
        data = yaml.safe_load(config.read_text())
        assert data.get("routing_policy_enabled") is True


# ---------------------------------------------------------------------------
# Shell snippet safety — env-var pass avoids path interpolation injection
# ---------------------------------------------------------------------------

_SHELL_SNIPPET = """\
VNX_PROJECT_ROOT="$1" python3 -c "
import os, yaml
from pathlib import Path
cfg = Path(os.environ['VNX_PROJECT_ROOT']) / '.vnx' / 'config.yml'
try:
    d = yaml.safe_load(cfg.read_text()) if cfg.is_file() else {}
    print('1' if isinstance(d, dict) and d.get('routing_policy_enabled') else '0')
except Exception:
    print('0')
"
"""


class TestStartShShellSnippetSafety:
    """Regression: PROJECT_ROOT passed via env var survives special chars in path."""

    def _run_snippet(self, project_root: str) -> str:
        result = subprocess.run(
            ["bash", "-c", _SHELL_SNIPPET, "--", project_root],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"snippet failed: {result.stderr}"
        return result.stdout.strip()

    def test_plain_path_enabled(self, tmp_path):
        _make_project_config(tmp_path, routing_enabled=True)
        assert self._run_snippet(str(tmp_path)) == "1"

    def test_plain_path_disabled(self, tmp_path):
        _make_project_config(tmp_path, routing_enabled=False)
        assert self._run_snippet(str(tmp_path)) == "0"

    def test_path_with_spaces(self, tmp_path):
        """Path with spaces must not break the env-var-based shell snippet."""
        spaced = tmp_path / "my project root"
        spaced.mkdir()
        _make_project_config(spaced, routing_enabled=True)
        assert self._run_snippet(str(spaced)) == "1"

    def test_path_with_single_quote(self, tmp_path):
        """Single quote in path must not break or inject into the shell snippet."""
        quoted = tmp_path / "vincent's project"
        quoted.mkdir()
        _make_project_config(quoted, routing_enabled=True)
        assert self._run_snippet(str(quoted)) == "1"

    def test_missing_config_returns_0(self, tmp_path):
        assert self._run_snippet(str(tmp_path)) == "0"
