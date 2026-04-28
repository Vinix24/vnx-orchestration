"""Best-effort codex_gate register emit — shared by gate_artifacts and gate_recorder.

Writes directly to dispatch_register.ndjson using env-var path resolution
(mirrors dispatch_register._register_path fallback chain, but without calling
vnx_paths.resolve_paths — which triggers a git subprocess that can interfere
with tests that mock subprocess.Popen).
"""
from __future__ import annotations

import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def _resolve_register_path() -> Path:
    """Resolve dispatch_register.ndjson path using env vars only (no git subprocess)."""
    state_dir_env = os.environ.get("VNX_STATE_DIR")
    if state_dir_env:
        return Path(state_dir_env) / "dispatch_register.ndjson"
    if os.environ.get("VNX_DATA_DIR_EXPLICIT") == "1" and os.environ.get("VNX_DATA_DIR"):
        return Path(os.environ["VNX_DATA_DIR"]) / "state" / "dispatch_register.ndjson"
    return _REPO_ROOT / ".vnx-data" / "state" / "dispatch_register.ndjson"


def emit_codex_gate_to_register(
    event: str,
    *,
    dispatch_id: str,
    pr_number: Optional[int],
    pr_id: str,
    gate: str,
) -> bool:
    """Resolve identifiers and append codex_gate event to register. Best-effort."""
    if gate != "codex_gate":
        return False
    try:
        pr_number_resolved = pr_number if pr_number is not None else None
        feature_id_resolved = ""
        if pr_number_resolved is None and pr_id:
            try:
                pr_number_resolved = int(pr_id)
            except (ValueError, TypeError):
                feature_id_resolved = str(pr_id)

        if not dispatch_id and pr_number_resolved is None and not feature_id_resolved:
            logger.warning(
                "gate_register_emit: register emit dropped — no identifying field "
                "(gate=%s, pr_id=%s, pr_number=%s)",
                gate, pr_id, pr_number,
            )
            return False

        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        record: dict = {"timestamp": now, "event": event, "gate": gate}
        if dispatch_id:
            record["dispatch_id"] = dispatch_id
        if pr_number_resolved is not None:
            record["pr_number"] = pr_number_resolved
        if feature_id_resolved:
            record["feature_id"] = feature_id_resolved

        path = _resolve_register_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False
