"""config-een-oppervlak N1c: VNX_WORKER_HEARTBEAT_SILENCE_SECONDS is deleted.

The name has zero string-literal reads left in scripts/, vnx_cli/ and dashboard/.
The silence threshold is the module constant; an exported value is ignored.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

NAME = "VNX_WORKER_HEARTBEAT_SILENCE_SECONDS"


def _literal_reads():
    hits = []
    for top in ("scripts", "vnx_cli", "dashboard"):
        for path in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value == NAME:
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        for path in sorted((ROOT / top).rglob("*.sh")):
            text = path.read_text(encoding="utf-8")
            if "$" + NAME in text or "${" + NAME in text:
                hits.append(str(path.relative_to(ROOT)))
    return hits


def test_deleted_name_has_zero_reads():
    assert _literal_reads() == []


@pytest.mark.parametrize("value", ["0", "300", "3600", "-5", "abc"])
def test_exported_value_is_ignored(monkeypatch, value):
    import worker_heartbeat

    monkeypatch.setenv(NAME, value)
    assert worker_heartbeat._resolve_silence_threshold() == 1800
