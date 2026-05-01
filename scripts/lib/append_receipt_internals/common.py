"""Shared utilities, exceptions, and facade proxy for append_receipt internals."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

_PACKAGE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = _PACKAGE_DIR.parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

EXIT_OK = 0
EXIT_INVALID_INPUT = 10
EXIT_VALIDATION_ERROR = 11
EXIT_IO_ERROR = 12
EXIT_LOCK_ERROR = 13
EXIT_UNEXPECTED_ERROR = 20


@dataclass(frozen=True)
class AppendResult:
    status: str
    receipts_file: Path
    idempotency_key: str


class AppendReceiptError(RuntimeError):
    def __init__(self, code: str, exit_code: int, message: str):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.message = message


_open_items_manager = None


def _get_open_items_manager():
    global _open_items_manager
    if _open_items_manager is None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import open_items_manager as _oim
        _open_items_manager = _oim
    return _open_items_manager


# Facade registry: lets submodules look up patchable symbols on the active
# facade module so test-side ``patch.object(ar, "X")`` interception still
# works after the split. Tests load the facade under different module names
# (canonical ``append_receipt`` and isolated ``append_receipt_testmodule``);
# we track ALL of them and resolve attributes by preferring the most recent
# patched (Mock) value, so cross-test patches survive registration order.
_facade_modules: List[Any] = []


def register_facade(mod) -> None:
    if mod in _facade_modules:
        _facade_modules.remove(mod)
    _facade_modules.append(mod)


def get_facade_module():
    """Return the most-recently-registered facade module (or canonical fallback)."""
    if _facade_modules:
        return _facade_modules[-1]
    return sys.modules.get("append_receipt")


def _is_mock(value) -> bool:
    cls_name = type(value).__name__
    return cls_name in ("MagicMock", "Mock", "AsyncMock", "NonCallableMagicMock", "NonCallableMock")


class _FacadeProxy:
    """Attribute-access proxy that resolves names across all registered facades.

    For each lookup, prefer a Mock-typed attribute (test-patched) over a
    real one; otherwise return the most-recently-registered match.
    """

    def __getattr__(self, name: str) -> Any:
        candidates: List[Any] = []
        for mod in reversed(_facade_modules):
            if hasattr(mod, name):
                candidates.append(getattr(mod, name))
        canonical = sys.modules.get("append_receipt")
        if canonical is not None and canonical not in _facade_modules and hasattr(canonical, name):
            candidates.append(getattr(canonical, name))
        if not candidates:
            raise AttributeError(f"facade not registered (looking for {name!r})")
        for value in candidates:
            if _is_mock(value):
                return value
        return candidates[0]


facade = _FacadeProxy()


def is_headless_t0() -> bool:
    """Return True when T0 is configured to run via subprocess adapter."""
    return os.environ.get("VNX_ADAPTER_T0", "tmux").lower() == "subprocess"


def _emit(level: str, code: str, **fields: Any) -> None:
    payload = {
        "level": level,
        "code": code,
        "timestamp": int(time.time()),
    }
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stderr)


def _safe_subprocess(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 5) -> Optional[str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
