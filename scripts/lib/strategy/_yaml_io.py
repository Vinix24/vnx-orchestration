"""YAML round-trip helper.

Prefers ruamel.yaml (preserves comments) when available; otherwise falls
back to PyYAML and emits a one-time stderr warning noting the comment-loss
limitation. Callers should treat both as opaque: pass the file content as
text/bytes, get back a Python dict.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any

_BACKEND = "pyyaml"
_RUAMEL = None
_PYYAML = None

try:  # pragma: no cover - exercised only when ruamel is vendored
    import ruamel.yaml as _ruamel_module  # type: ignore[import-not-found]

    _RUAMEL = _ruamel_module
    _BACKEND = "ruamel"
except Exception:  # noqa: BLE001 — we want any import error to fall through
    _RUAMEL = None

import yaml as _yaml  # PyYAML is a hard dep of the project

_PYYAML = _yaml

_WARNED = False


def _warn_pyyaml_once() -> None:
    global _WARNED
    if _WARNED or _BACKEND == "ruamel":
        return
    _WARNED = True
    print(
        "[strategy._yaml_io] ruamel.yaml not installed; using PyYAML fallback "
        "(comments and key ordering may not round-trip).",
        file=sys.stderr,
    )


def backend_name() -> str:
    """Return 'ruamel' or 'pyyaml' so callers / tests can introspect."""
    return _BACKEND


def load_yaml(path: Path) -> Any:
    """Read a YAML file and return its parsed Python object."""
    _warn_pyyaml_once()
    text = Path(path).read_text(encoding="utf-8")
    if _BACKEND == "ruamel":
        yaml_obj = _RUAMEL.YAML(typ="rt")  # type: ignore[union-attr]
        yaml_obj.preserve_quotes = True
        return yaml_obj.load(text)
    return _PYYAML.safe_load(text)


def dump_yaml(data: Any, path: Path) -> None:
    """Write `data` to `path` as YAML.

    With PyYAML fallback, comments are not preserved. The writer enforces
    `sort_keys=False` and `default_flow_style=False` for stable output.
    """
    _warn_pyyaml_once()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _BACKEND == "ruamel":
        yaml_obj = _RUAMEL.YAML(typ="rt")  # type: ignore[union-attr]
        yaml_obj.default_flow_style = False
        with path.open("w", encoding="utf-8") as fh:
            yaml_obj.dump(data, fh)
        return
    with path.open("w", encoding="utf-8") as fh:
        _PYYAML.safe_dump(
            data,
            fh,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )


__all__ = ["backend_name", "load_yaml", "dump_yaml"]


# Suppress benign PyYAML deprecation chatter triggered by older versions
# during import on some platforms.
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module=r"yaml.*"
)
