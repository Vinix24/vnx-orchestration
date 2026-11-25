"""config_global — the global configuration layer (ADR-087 decision 1): ``~/.vnx/config.json``.

``VNX_GLOBAL_CONFIG_FILE`` moves the file with its ``config_audit.ndjson`` and ``config.json.lock``.
Writes use one lock for recovery, re-read, txn audit, atomic replace and commit (ADR-093).
Unfinished transactions are recovered by the next write; reads only warn. Only the registry
check lives here: the shared writer (G3) owns enum and scope checks and calls into this module or
mirrors the same check, so there is no second, divergent validator. Values are stored as strings.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_registry
from atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
OP_SET, OP_UNSET, OP_ABORT, OP_COMMIT = "set", "unset", "abort", "commit"
_LOGGED_IGNORED: set = set()
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def config_file_path() -> Path:
    """``$VNX_GLOBAL_CONFIG_FILE`` when set, else ``~/.vnx/config.json``."""
    override = os.environ.get("VNX_GLOBAL_CONFIG_FILE", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".vnx" / "config.json"


def audit_log_path(path: Optional[Path] = None) -> Path:
    return (path or config_file_path()).parent / "config_audit.ndjson"


def lock_file_path(path: Optional[Path] = None) -> Path:
    return (path or config_file_path()).with_name((path or config_file_path()).name + ".lock")


class GlobalConfigUnreadable(RuntimeError):
    """The global file exists but cannot be used; names the file, the reason and the audit log."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"global config {path} is unreadable: {reason} (audit log: {audit_log_path(path)})")
        self.path, self.reason = path, reason


def _log_ignored_once(what: str) -> None:
    if what not in _LOGGED_IGNORED:
        _LOGGED_IGNORED.add(what)
        logger.info("config_global: %s ignored (a newer writer may have added it)", what)


def _read_document(path: Path) -> Optional[Dict[str, Any]]:
    """The validated document, or None for a missing file. Raises GlobalConfigUnreadable."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:  # ValueError covers JSON and UnicodeDecodeError
        raise GlobalConfigUnreadable(path, f"{type(exc).__name__}: {exc}") from exc
    version = document.get("version") if isinstance(document, dict) else None
    if isinstance(version, bool) or not isinstance(version, int):
        raise GlobalConfigUnreadable(path, f"version {version!r} is missing or not an integer")
    if not 1 <= version <= SCHEMA_VERSION:
        raise GlobalConfigUnreadable(path, f"version {version} is outside majors 1..{SCHEMA_VERSION}")
    if not isinstance(document.get("values"), dict):
        raise GlobalConfigUnreadable(path, "'values' is not an object")
    for field in set(document) - {"version", "values", "last_txn"}:
        _log_ignored_once(f"unknown field {field!r}")
    for key, entry in document["values"].items():
        if key not in config_registry.CONFIG_REGISTRY:
            _log_ignored_once(f"unknown key {key!r}")
        elif not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            # A known key whose value cannot be read is never "no value" (fail-closed).
            raise GlobalConfigUnreadable(path, f"values.{key} has no string 'value'")
    return document


def load() -> Dict[str, Dict[str, Any]]:
    """The entries of the known keys; empty for a missing file. Raises GlobalConfigUnreadable."""
    path = config_file_path()
    try:
        pending = _unfinished(path)
    except GlobalConfigUnreadable as exc:
        logger.warning("%s", exc)  # Audit trouble is visible, but never prevents reads of the file.
    else:
        if pending:
            logger.warning("global config %s: unfinished txn %s at %s line %s; next write recovers",
                           path, pending[1]["txn"], audit_log_path(path), pending[0])
    document = _read_document(path) or {"values": {}}
    return {k: copy.deepcopy(v) for k, v in document["values"].items() if k in config_registry.CONFIG_REGISTRY}


def get_with_meta(key: str) -> Optional[Dict[str, Any]]:
    """The entry of ``key`` (value, changed_at, changed_by, approval_id, reason), or None."""
    return load().get(key)


def get(key: str) -> Optional[str]:
    """The global value of ``key``, or None when none is set. Raises GlobalConfigUnreadable."""
    entry = get_with_meta(key)
    return None if entry is None else entry["value"]


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    lock = lock_file_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _append_audit(path: Path, record: Dict[str, Any]) -> None:
    line = {"ts": datetime.now(timezone.utc).strftime(_TS_FORMAT), "cwd": os.getcwd(), "pid": os.getpid(),
            "argv": list(sys.argv), **record}
    with audit_log_path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, allow_nan=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _unfinished(path: Path):
    """Return (line, intent, previous committed txn), or None; invalid audit evidence raises."""
    number, record, pending, previous, seen = 0, {}, None, None, False
    try:
        try:
            lines = audit_log_path(path).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None  # Pre-upgrade installations need not have an audit log.
        for number, line in enumerate(lines, 1):
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("audit record is not an object")
            if (not seen and "txn" not in record and "new_sha256" not in record
                    and record.get("operation") in (OP_SET, OP_UNSET, OP_ABORT)):
                continue  # Legacy prefix, before the first transaction.
            txn, operation = record.get("txn"), record.get("operation")
            if not isinstance(txn, str) or UUID(txn).version != 4:
                raise ValueError("invalid txn")
            seen = True
            if operation in (OP_SET, OP_UNSET):
                digest = record.get("new_sha256")
                if (pending or not isinstance(digest, str) or len(digest) != 64
                        or any(c not in "0123456789abcdef" for c in digest)):
                    raise ValueError("unfinished predecessor or invalid new_sha256")
                pending = (number, record, previous)
            elif operation in (OP_COMMIT, OP_ABORT) and pending and txn == pending[1]["txn"]:
                if operation == OP_COMMIT:
                    previous = txn
                pending = None
            else:
                raise ValueError("unknown operation or unmatched terminal txn")
    except (OSError, ValueError) as exc:
        identity = record.get("txn") if isinstance(record, dict) else "<invalid>"
        raise GlobalConfigUnreadable(path, f"audit line {number}, txn {identity}: {exc}") from exc
    return pending


def _recover(path: Path, actor: str) -> None:
    """Resolve an unfinished txn under _locked; refuse ambiguous evidence, leaving reads usable."""
    pending = _unfinished(path)
    if pending is None:
        return
    number, record, previous = pending
    txn = record["txn"]
    try:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = b"{}"  # A missing file proves non-replacement only on the first transaction.
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("document is not an object")
        if document.get("last_txn") == txn and hashlib.sha256(raw).hexdigest() == record["new_sha256"]:
            terminal = {"operation": OP_COMMIT}
        elif (document.get("last_txn") == previous if previous is not None else "last_txn" not in document):
            terminal = {"operation": OP_ABORT, "error": "crash_recovery: replace never landed"}
        else:
            raise ValueError("file changed outside config_global; operator reconciliation required")
    except (OSError, ValueError) as exc:
        raise GlobalConfigUnreadable(path, f"txn {txn}, audit line {number}: {exc}") from exc
    _append_audit(path, {"actor": actor, "txn": txn, **terminal})


def _write(operation: str, key: str, value: Optional[str], *, actor: str, reason: str,
           approval_id: Optional[str]) -> Dict[str, Any]:
    entry = config_registry.CONFIG_REGISTRY.get(key)
    if entry is None:
        raise ValueError(f"unknown config key: {key}")
    if not entry.writable_from_ui:
        raise PermissionError(f"{key} is not writable")
    if entry.requires_approval and not approval_id:
        raise PermissionError(f"{key} requires an approval_id")
    for name, given in (("reason", reason), ("actor", actor)):
        if not isinstance(given, str) or not given.strip():
            raise ValueError(f"a non-empty {name} is required")
    path = config_file_path()
    with _locked(path):
        _recover(path, actor)
        document = _read_document(path) or {"values": {}}
        values = document["values"]
        old = copy.deepcopy(values.get(key))
        if operation == OP_SET:
            values[key] = {**(old or {}), "value": value, "changed_by": actor, "approval_id": approval_id,
                           "reason": reason, "changed_at": datetime.now(timezone.utc).strftime(_TS_FORMAT)}
        else:
            values.pop(key, None)
        new = copy.deepcopy(values.get(key))
        document["version"] = SCHEMA_VERSION
        txn = document["last_txn"] = str(uuid4())
        text = json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        _append_audit(path, {"actor": actor, "operation": operation, "key": key, "old": old,
                             "new": new, "approval_id": approval_id, "reason": reason,
                             "txn": txn, "new_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
        try:
            atomic_write_text(path, text)
        except BaseException as exc:
            # The audit line already holds the change: mark it as never landed, then re-raise.
            _append_audit(path, {"actor": actor, "operation": OP_ABORT, "key": key, "txn": txn,
                                 "aborted_operation": operation, "error": f"{type(exc).__name__}: {exc}"})
            raise
        _append_audit(path, {"actor": actor, "operation": OP_COMMIT, "txn": txn})
    return {"key": key, "operation": operation, "old": old, "new": new, "path": path}


def set_global(key: str, value: str, *, actor: str, reason: str,
               approval_id: Optional[str] = None) -> Dict[str, Any]:
    """Set the global value of ``key`` (a string) with one audit line before the replace."""
    if not isinstance(value, str):
        raise TypeError(f"value must be a string, got {type(value).__name__}")
    return _write(OP_SET, key, value, actor=actor, reason=reason, approval_id=approval_id)


def unset_global(key: str, *, actor: str, reason: str,
                 approval_id: Optional[str] = None) -> Dict[str, Any]:
    """Remove the global value of ``key`` with one audit line (also when none was set)."""
    return _write(OP_UNSET, key, None, actor=actor, reason=reason, approval_id=approval_id)
