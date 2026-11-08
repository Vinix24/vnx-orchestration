#!/usr/bin/env python3
"""Reader for the T0 playbook loading evidence (ADR-058 decision 7). Stdlib only, read-only.

`<state dir>/playbook_loading.ndjson` holds one JSON record per observation that a
provider really read the playbook. A pointer block says the playbook is *present*;
only a valid record here may say it was *loaded*. Every doubt is `unverified`.

A record counts only for its own provider, and only when project_id, source_id,
path and sha256 all equal the measured project and today's resolution. Nothing in
this module writes the file: its producer is the follow-up track F1.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import t0_playbook

EVIDENCE_FILENAME = "playbook_loading.ndjson"
PROVIDERS = ("claude", "codex", "kimi")
LOADED = "loaded"
UNVERIFIED = "unverified"

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = 1


def resolve_project_state(project_root) -> "tuple[Optional[str], Optional[Path]]":
    """(project_id, state dir) of one project through the existing path helpers.

    Either is None when it cannot be resolved; the caller then reads `unverified`."""
    try:
        import vnx_paths

        root = Path(project_root).expanduser().resolve()
        pid = vnx_paths._resolve_state_project_id(root)
        if not pid:
            return None, None
        return pid, vnx_paths.resolve_project_data_root(pid, root) / "state"
    except Exception:  # vnx-silent-except: an unresolvable store means no evidence, which reads unverified
        return None, None


def _parse_observed_at(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_valid_shape(rec: Any) -> bool:
    """A record is usable only with the full schema; anything else is not evidence."""
    if not isinstance(rec, dict) or rec.get("schema") != _SCHEMA:
        return False
    if rec.get("provider") not in PROVIDERS:
        return False
    for key in ("project_id", "source_id", "path"):
        if not isinstance(rec.get(key), str) or not rec[key]:
            return False
    sha = rec.get("sha256")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        return False
    return _parse_observed_at(rec.get("observed_at"))


def _matches(rec: dict, project_id: str, current: "t0_playbook.Resolution") -> bool:
    return (
        rec["project_id"] == project_id
        and rec["source_id"] == current.source_id
        and rec["path"] == current.path
        and rec["sha256"] == current.sha256
    )


def _read_lines(path: Path) -> list:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):  # vnx-silent-except: an unreadable evidence file is no evidence
        return []


def _valid_records(state_dir: Path) -> list:
    out = []
    for line in _read_lines(Path(state_dir) / EVIDENCE_FILENAME):
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):  # vnx-silent-except: a malformed line is skipped, it can only withhold `loaded`
            continue
        if _is_valid_shape(rec):
            out.append(rec)
    return out


def loading_by_provider(
    project_id: Optional[str],
    state_dir: Optional[Path],
    current: "t0_playbook.Resolution",
) -> dict:
    """{provider: "loaded" | "unverified"} for every known provider, never a missing key."""
    result = {p: UNVERIFIED for p in PROVIDERS}
    if not project_id or state_dir is None or current.state != t0_playbook.STATE_OK:
        return result
    for rec in _valid_records(state_dir):
        if _matches(rec, project_id, current):
            result[rec["provider"]] = LOADED
    return result
