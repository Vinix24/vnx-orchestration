"""test_subprocess_adapter_scope_fallback.py — the import-fault fallback IS the scoped default.

When ``subprocess_adapter`` cannot import ``worker_permissions`` it can resolve
neither the scoping switches nor a role profile. The fallback used to be a
weaker hand-copied posture and, with the switches unset, the legacy skip flag
itself; the empty ``--mcp-config`` also does not reach an extension bridge
(claude-in-chrome) that surfaces ``mcp__*`` tools, hence the namespace deny.

The probe imports ``subprocess_adapter`` in a child interpreter where
``worker_permissions`` is unimportable (a ``None`` entry in ``sys.modules``),
then compares its argv with the scoped default from a healthy interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from worker_permissions import (
    MCP_NAMESPACE_DENY,
    build_claude_scope_args,
    default_code_worker_profile,
)

LEGACY_SKIP_FLAG = "--dangerously-skip-permissions"
_ENV_KNOBS = (
    "VNX_WORKER_SCOPED",
    "VNX_WORKER_BLANKET_SKIP",
    "VNX_ENFORCE_WORKER_PERMISSIONS",
    "VNX_WORKER_ENFORCEMENT_SKIP",
)

_PROBE = """
import json, sys
sys.path.insert(0, {scripts_lib!r})
sys.modules["worker_permissions"] = None
import subprocess_adapter
print(json.dumps({{
    "import_failed": subprocess_adapter.worker_scoped_enabled is None,
    "default": subprocess_adapter._build_worker_scope_args("backend-developer"),
    "requires_mcp": subprocess_adapter._build_worker_scope_args(
        "backend-developer", requires_mcp=True
    ),
    "constant": subprocess_adapter._FALLBACK_SCOPE_ARGS,
}}))
"""


def _probe(**env_overrides: str) -> dict:
    """Run the child probe with worker_permissions import-broken."""
    env = {k: v for k, v in os.environ.items() if k not in _ENV_KNOBS}
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(scripts_lib=str(SCRIPTS_LIB))],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"probe failed (rc={proc.returncode})\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def fallback_probe() -> dict:
    return _probe()


@pytest.fixture(scope="module")
def scoped_code_worker_default() -> list:
    return build_claude_scope_args(default_code_worker_profile())


def test_fallback_is_the_scoped_code_worker_default(
    fallback_probe: dict, scoped_code_worker_default: list
) -> None:
    # Guard the guard: if the import succeeded this compares the healthy path.
    assert fallback_probe["import_failed"] is True
    argv = fallback_probe["default"]
    assert argv == scoped_code_worker_default, "fallback is not the scoped default posture"
    assert fallback_probe["constant"] == scoped_code_worker_default
    assert LEGACY_SKIP_FLAG not in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert MCP_NAMESPACE_DENY in argv[argv.index("--disallowedTools") + 1].split(",")


def test_fallback_requires_mcp_omits_force_empty_mcp(fallback_probe: dict) -> None:
    argv = fallback_probe["requires_mcp"]
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    assert MCP_NAMESPACE_DENY not in argv
    assert argv == build_claude_scope_args(default_code_worker_profile(), requires_mcp=True)


@pytest.mark.parametrize(
    "env_override", [{"VNX_WORKER_SCOPED": "0"}, {"VNX_WORKER_BLANKET_SKIP": "1"}]
)
def test_opt_out_cannot_reopen_the_blast_radius_on_import_failure(env_override: dict) -> None:
    """The switches that pick the legacy posture belong to the failed module."""
    probe = _probe(**env_override)
    assert probe["import_failed"] is True
    argv = probe["default"]
    assert LEGACY_SKIP_FLAG not in argv
    assert MCP_NAMESPACE_DENY in argv[argv.index("--disallowedTools") + 1].split(",")
