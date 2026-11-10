"""The one audit writer: one fsynced NDJSON line per record, outside the permit dir, never swallowed.

Invariant: every audited change writes its intent line before the first write and its outcome line after it, both
with the stage of the change. The intent has status "intent"; the outcome "ok" (committed), "rolled_back" (written,
then undone after a failed proof or an error, with reason) or "refused" (a check before the first write refused,
with reason). A reader counts a stage as done only on "ok"; an intent without an outcome for the same permit_sha256
is a change whose result must be measured, never assumed. `quarantine` predates it: its seal writes only "ok".
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

AUDIT_FILE = "intelligence_corpus_quarantine_audit.ndjson"
STAGES = ("baseline", "snapshot", "quarantined", "verified", "deleted", "vacuumed", "restored", "rehearsed")
STATUSES = ("intent", "ok", "rolled_back", "refused")


class AfterCommitError(Exception):
    """The change is committed or sealed; only its record failed (exit 4)."""


def prepare(state_dir, permit_dir):
    """Return the permit's manifest.json; refuse an audit dir inside the permit dir (B4) or a missing manifest."""
    state, permit = Path(state_dir).expanduser().resolve(), Path(permit_dir).expanduser().resolve()
    if state == permit or permit in state.parents:
        raise ValueError(f"audit state dir {state} lies inside the permit dir {permit} (B4)")
    manifest = permit / "manifest.json"
    if not manifest.is_file():
        raise ValueError(f"no manifest.json in {permit}: run baseline first, the audit line binds it")
    return manifest


def permit_sha256(manifest_path, quarantine_path):
    """sha256 over the bytes of manifest.json followed by those of quarantine.json."""
    return hashlib.sha256(Path(manifest_path).read_bytes() + Path(quarantine_path).read_bytes()).hexdigest()


def append(state_dir, stage, permit_sha256, status, **numbers):
    """Append one line with O_APPEND + fsync; an invalid record or a write error raises, nothing is swallowed."""
    if stage not in STAGES:
        raise ValueError(f"unknown audit stage: {stage!r}")
    if type(permit_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", permit_sha256) is None:
        raise ValueError("invalid permit_sha256")
    if type(status) is not str or status not in STATUSES:
        raise ValueError(f"unknown audit status: {status!r}")
    record = dict(ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), stage=stage,  # a number named
                  permit_sha256=permit_sha256, status=status, **numbers)  # like a fixed field is a TypeError
    line = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    fd = os.open(Path(state_dir) / AUDIT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        if os.write(fd, line) != len(line):
            raise OSError(f"short write to {Path(state_dir) / AUDIT_FILE}")
        os.fsync(fd)
    finally:
        os.close(fd)
    return record
