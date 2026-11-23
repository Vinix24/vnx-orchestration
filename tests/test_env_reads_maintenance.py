"""PR-8 of config-een-oppervlak: the maintenance and operations constants are gone.

An AST scan over ``scripts/``, ``vnx_cli/`` and ``dashboard/`` (Python) plus a text scan of
seven bash files asserts zero reads of every variable this PR deleted. The scan itself is
exercised on synthetic sources so a blind scanner cannot pass for a clean tree.

``VNX_LAUNCHD_GUARD_TEMP_ROOTS`` is deliberately absent from the list: no seam other than the
environment reaches the ``reload_plist.sh`` subprocess the guard tests run (see the report).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

DELETED = (
    "VNX_DB_BACKUP_KEEP", "VNX_DB_RETENTION_DAYS", "VNX_DOCTOR_BACKUP_KEEP",
    "VNX_PREMIGRATE_BACKUP_KEEP", "VNX_PRESNAP_BACKUP_KEEP", "VNX_PROC_CPU_IDLE_MAX",
    "VNX_PROC_IDLE_SECONDS", "VNX_CLAUDE_PROJECTS_DIR", "VNX_AGGREGATOR_DIR",
    "VNX_AGGREGATOR_DB_DIR", "VNX_REGISTRY_PATH", "VNX_DAILY_INTEL_REFRESH",
    "VNX_DIGEST_INTERVAL", "VNX_INTELLIGENCE_DASHBOARD_WRITE", "VNX_LOCK_MAX_AGE",
    "VNX_PIN_SCAN_ROOTS", "VNX_PROJECT_REGISTRY", "VNX_INSTALL_CENTRAL_SCRIPT",
    "VNX_GLOBAL_MCP_CONFIG_PATH", "VNX_DOCS_DIRS", "VNX_INBOX_PUSH_INTERVAL_H",
    "VNX_DASHBOARD_BIND", "VNX_REGISTER_STREAM_HEARTBEAT", "VNX_NODE_PATH",
    "VNX_INTEL_STRICT_SCOPE", "VNX_ACTOR",
)
PY_ROOTS = ("scripts", "vnx_cli", "dashboard")
BASH_FILES = (
    "scripts/aggregator/refresh_loop.sh", "scripts/commands/recover.sh",
    "scripts/lib/process_lifecycle.sh", "scripts/commands/doctor.sh",
    "scripts/test_wheel_install.sh", "scripts/lib/vnx_paths.sh",
    "scripts/nightly_intelligence_pipeline.sh",
)
_METHODS = {"get", "pop", "setdefault", "getenv"}


def _is_environ(node: ast.AST, environ_names: set[str]) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id in environ_names


def _literal(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def python_reads(source: str, names: tuple[str, ...] = DELETED) -> list[tuple[int, str]]:
    """Every read form of a deleted name: get/[]/getenv/setdefault/pop/``in`` and from-imports."""
    tree = ast.parse(source)
    environ_names: set[str] = set()
    getenv_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environ_names.add(alias.asname or alias.name)
                elif alias.name == "getenv":
                    getenv_names.add(alias.asname or alias.name)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call):
            func = node.func
            first = _literal(node.args[0]) if node.args else None
            if isinstance(func, ast.Attribute) and func.attr in _METHODS and (
                _is_environ(func.value, environ_names)
                or (func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os")
            ):
                name = first
            elif isinstance(func, ast.Name) and func.id in getenv_names:
                name = first
        elif isinstance(node, ast.Subscript) and _is_environ(node.value, environ_names):
            name = _literal(node.slice)
        elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            if any(_is_environ(c, environ_names) for c in node.comparators):
                name = _literal(node.left)
        if name in names:
            hits.append((node.lineno, name))
    return hits


def bash_reads(source: str, names: tuple[str, ...] = DELETED) -> list[tuple[int, str]]:
    """Every ``$VNX_X`` / ``${VNX_X...}`` expansion of a deleted name."""
    pattern = re.compile(r"\$\{?(" + "|".join(map(re.escape, names)) + r")\b")
    return [(i, m.group(1)) for i, line in enumerate(source.splitlines(), 1) for m in pattern.finditer(line)]


def _tree_reads() -> list[str]:
    found: list[str] = []
    for root in PY_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            for lineno, name in python_reads(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(REPO)}:{lineno} {name}")
    for rel in BASH_FILES:
        for lineno, name in bash_reads((REPO / rel).read_text(encoding="utf-8")):
            found.append(f"{rel}:{lineno} {name}")
    return found


def test_no_deleted_variable_is_read_anywhere():
    assert _tree_reads() == []


@pytest.mark.parametrize("source", [
    'import os\nx = os.environ.get("{n}")',
    'import os\nx = os.environ["{n}"]',
    'import os\nx = os.getenv("{n}", "1")',
    'import os\nos.environ.setdefault("{n}", "1")',
    'import os\nos.environ.pop("{n}", None)',
    'import os\nx = "{n}" in os.environ',
    'from os import environ\nx = environ.get("{n}")',
    'from os import environ as env\nx = env["{n}"]',
    'from os import getenv\nx = getenv("{n}")',
    'from os import getenv as g\nx = g("{n}")',
])
def test_python_scan_sees_every_read_form(source):
    assert python_reads(source.format(n="VNX_NODE_PATH"))[0][1] == "VNX_NODE_PATH"


def test_python_scan_ignores_other_names_and_plain_strings():
    assert python_reads('import os\nx = os.environ.get("VNX_PROJECT_ID")\ny = "VNX_NODE_PATH"') == []


@pytest.mark.parametrize("line", ['x="$VNX_NODE_PATH"', 'x="${VNX_NODE_PATH:-}"', "echo ${VNX_NODE_PATH}"])
def test_bash_scan_sees_expansions(line):
    assert bash_reads(line) == [(1, "VNX_NODE_PATH")]


def test_bash_scan_ignores_longer_names():
    assert bash_reads('x="${VNX_NODE_PATH_EXTRA:-}"') == []


def test_every_scanned_bash_file_exists():
    assert [rel for rel in BASH_FILES if not (REPO / rel).is_file()] == []


def test_fixed_values_hold():
    """The value each deleted variable stood for when it was unset."""
    sys.path.insert(0, str(REPO / "scripts" / "lib"))
    sys.path.insert(0, str(REPO / "scripts"))
    sys.path.insert(0, str(REPO))
    import process_cleanup
    import vnx_db_maintenance
    import vnx_cli.commands.update as update
    import dashboard.api_register_stream as stream

    assert process_cleanup.IDLE_THRESHOLD_SECONDS == 14400
    assert process_cleanup.CPU_IDLE_MAX == 1.0
    assert vnx_db_maintenance.DEFAULT_RETENTION_DAYS == 180
    assert stream._HEARTBEAT_INTERVAL == 30.0
    assert update._resolve_pin_scan_roots() == [update.DEFAULT_PIN_SCAN_ROOT]
