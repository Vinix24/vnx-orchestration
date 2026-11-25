"""The evidence check of ``applied`` for a ``permissie`` proposal (ADR-086 decision 2, permissieblokkades-leren L7a).

``applied_ref`` is ``settings:<path>#<first 16 hex of the sha256 of the exact rule text>``. ``verify`` reads the
settings file once, read-only (``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``), and returns the evidence the ``applied``
record carries, or raises ``Refused`` with one line that starts with the name of the check that failed:
``applied_ref``, ``proposal``, ``path``, ``hash``, ``file``, ``json``, ``list`` or ``rule``. It never writes a
file (ADR-065, ADR-086: a T0 never changes its own permissions, and the check writes no settings file).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from permission_account_dirs import account_dirs

REF = re.compile(r"settings:(?P<path>[^#\n]+)#(?P<digest>[0-9a-f]{16})")
OORZAAK = re.compile(r"permission_form:permissie:(?P<account>vnx|sm):(?P<scope>project|user):[0-9a-f]{16}")
RULE = re.compile(r"Add `(?P<rule>[^`\n]+)` via /permissions, scope ")  # the text of permission_proposals._voorstel
PROJECT_FILES = (".claude/settings.json", ".claude/settings.local.json")  # under the repo of the project


class Refused(ValueError):
    """One check failed; nothing may be written."""


def allowed_paths(scope: str, account: str, repo: Path, home: Path) -> Tuple[str, ...]:
    """ADR-090 user paths, ADR-086 project paths; missing or invalid user mappings raise Refused."""
    if scope == "project":
        return tuple(str(Path(repo) / name) for name in PROJECT_FILES)
    try:
        folders = account_dirs(home)
    except ValueError as exc:
        raise Refused(f"path: {exc}") from exc
    if account not in ("vnx", "sm") or account not in folders:
        raise Refused(f"path: no mapped folder for account {account}")
    return (str(folders[account] / "settings.json"),)


def _target(entry: Mapping[str, Any]) -> Tuple[str, str, str]:
    """(account, scope, rule) of the approved revision; refused when the proposal does not name all three."""
    who = OORZAAK.fullmatch(entry.get("oorzaak") or "") if isinstance(entry.get("oorzaak"), str) else None
    revisions, bound = entry.get("revisions") or [], entry.get("decided_revision")
    approved = revisions[bound - 1] if isinstance(bound, int) and 1 <= bound <= len(revisions) else {}
    voorstel = approved.get("voorstel") if isinstance(approved, Mapping) else None
    rule = RULE.match(voorstel) if isinstance(voorstel, str) else None
    if who is None or rule is None:
        raise Refused("proposal: no account, scope or rule to check in its oorzaak and approved revision")
    return who["account"], who["scope"], rule["rule"]


def _read(path: str) -> Tuple[bytes, os.stat_result]:
    """The bytes and stat of one regular file, opened read-only; a symlink or a FIFO is refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Refused(f"file: {path} cannot be opened read-only ({exc.strerror})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise Refused(f"file: {path} is not a regular file")
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks), info
    except OSError as exc:
        raise Refused(f"file: {path} cannot be read ({exc.strerror})") from exc
    finally:
        os.close(fd)


def verify(entry: Mapping[str, Any], applied_ref: Any, repo: Path, home: Path) -> Dict[str, Any]:
    """The evidence for the ``applied`` record of an approved ``permissie`` proposal, after every check passed."""
    ref = REF.fullmatch(applied_ref) if isinstance(applied_ref, str) else None
    if ref is None:
        raise Refused("applied_ref: not settings:<path>#<first 16 hex of the sha256 of the rule>")
    account, scope, rule = _target(entry)
    path = str(home) + ref["path"][1:] if ref["path"].startswith("~/") else ref["path"]
    if path not in allowed_paths(scope, account, repo, home):
        raise Refused(f"path: {ref['path']} is not a settings file of scope {scope}, account {account}")
    rule_sha = hashlib.sha256(rule.encode("utf-8")).hexdigest()
    if rule_sha[:16] != ref["digest"]:
        raise Refused(f"hash: {ref['digest']} is not the sha256 prefix of the rule {rule}")
    body, info = _read(path)
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError as exc:  # UnicodeDecodeError is a ValueError too
        raise Refused(f"json: {path} does not parse as JSON ({exc})") from exc
    permissions = data.get("permissions") if isinstance(data, dict) else None
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        raise Refused(f"list: {path} has no permissions.allow list")
    if rule not in allow:
        raise Refused(f"rule: permissions.allow in {path} does not contain {rule}")
    return {"path": path, "rule": rule, "rule_sha256": rule_sha, "file_sha256": hashlib.sha256(body).hexdigest(),
            "mtime": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()}
