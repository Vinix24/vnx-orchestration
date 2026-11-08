"""Prompt attribution (ADR-056, zelfst PR-8b): the class of a prompt and, only when unambiguous, its delivery key.

The prompt is the sixth coverage source of ADR-056 (§ Tenant-veilige sleutel, row "prompt attribution"):
``project_id`` and ``delivery_id`` only when exactly one delivery belongs to the session, otherwise
``unknown`` with ``reason = ambiguous_session``. No session-to-delivery index exists, so the only
unambiguous case is a dispatch marker in the environment of the worker that received the prompt.

The class of a prompt is NOT ``actor_class.derive()``: in a T0 session the environment says ``t0``
(after PR-7c), but a prompt typed into that session is the operator's. So:

  - a dispatch marker (``VNX_DISPATCH_ID`` / ``VNX_CURRENT_DISPATCH_ID``) is ``worker``;
  - a prompt equal to the ``expected`` first message in a rotation run's ``delivery.json``, or to the
    content of that run's ``goal.txt``, for a run directory younger than 30 minutes, is ``automation``
    (the rotation spawner typed it);
  - every other prompt is ``operator``.

Pure functions, no network, no git subprocess, no DB. The prompt text is hashed and measured, never stored.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from delivery_identity import delivery_identity_fields
from governance_receipts import UNKNOWN_IDENTITY
from project_root import resolve_central_data_dir
from t0_rotation_state import LATCH_TTL_SECONDS, STATE_DIR_ENV

DISPATCH_MARKERS = ("VNX_DISPATCH_ID", "VNX_CURRENT_DISPATCH_ID")
STORE_FILENAME = "prompt_attribution.ndjson"
CLASS_WORKER, CLASS_AUTOMATION, CLASS_OPERATOR = "worker", "automation", "operator"
STATUS_ATTRIBUTED, STATUS_AMBIGUOUS = "attributed", "ambiguous_session"
RUN_DIR_GLOB = "rotation-*"
PROJECT_MARKER = ".vnx-project-id"


@dataclass(frozen=True)
class Attribution:
    """``status`` is ``attributed`` or ``ambiguous_session``; the keys are ``unknown`` when not attributed."""
    status: str
    actor_class: str
    project_id: str
    delivery_id: str

    @property
    def reason(self) -> Optional[str]:
        return None if self.status == STATUS_ATTRIBUTED else self.status


def dispatch_marker(env: Mapping[str, str]) -> str:
    """The first non-blank dispatch marker in ``env``, or ``""``."""
    for name in DISPATCH_MARKERS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_project_id_light(cwd: str, env: Mapping[str, str]) -> str:
    """``VNX_PROJECT_ID``, else the nearest ``.vnx-project-id`` above ``cwd``; ``unknown`` otherwise.

    Same first two steps as ``project_root.resolve_project_id``, without its git-remote step: the
    hook may not spawn a subprocess. A failed lookup is ``unknown``, never a default project.
    """
    explicit = (env.get("VNX_PROJECT_ID") or "").strip()
    if explicit:
        return explicit
    try:
        start = Path(cwd).resolve()
    except (OSError, ValueError, RuntimeError):
        return UNKNOWN_IDENTITY
    for ancestor in (start, *start.parents):
        marker = ancestor / PROJECT_MARKER
        try:
            lines = marker.read_text(encoding="utf-8").splitlines() if marker.is_file() else None
        except OSError:
            return UNKNOWN_IDENTITY  # a marker that exists but cannot be read: do not keep walking
        if lines is not None:
            first = lines[0].strip() if lines else ""
            return first or UNKNOWN_IDENTITY
    return UNKNOWN_IDENTITY


def central_state_dir(project_id: str) -> Optional[Path]:
    """``~/.vnx-data/<project_id>/state``, or ``None`` when the project is unknown."""
    if project_id == UNKNOWN_IDENTITY or not project_id:
        return None
    return resolve_central_data_dir(project_id) / "state"


def rotation_state_dir(project_id: str, env: Mapping[str, str]) -> Optional[Path]:
    """The rotation state dir: the override of ``t0_rotation_state`` when set, else the central one."""
    override = (env.get(STATE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    state = central_state_dir(project_id)
    return state / "t0_rotation" if state else None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _expected_texts(run_dir: Path) -> list:
    texts = [_read_text(run_dir / "goal.txt")]
    raw = _read_text(run_dir / "delivery.json")
    try:
        parsed = json.loads(raw) if raw is not None else None
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        texts.append(parsed.get("expected"))
    return [t.strip() for t in texts if isinstance(t, str) and t.strip()]


def _fresh_run_dirs(sdir: Path, now: float) -> list:
    try:
        candidates = [p for p in sdir.glob(RUN_DIR_GLOB) if p.is_dir()]
        return [p for p in candidates if 0 <= now - p.stat().st_mtime < LATCH_TTL_SECONDS]
    except OSError:
        return []


def is_automation(prompt: str, sdir: Optional[Path], now: Optional[float] = None) -> bool:
    """True when ``prompt`` equals the typed text of a rotation run younger than 30 minutes."""
    wanted = (prompt or "").strip()
    if not wanted or sdir is None:
        return False
    stamp = time.time() if now is None else now
    return any(wanted in _expected_texts(run) for run in _fresh_run_dirs(sdir, stamp))


def classify(prompt: str, env: Mapping[str, str], sdir: Optional[Path], now: Optional[float] = None) -> str:
    """``worker`` on a dispatch marker, ``automation`` on a rotation text, else ``operator``."""
    if dispatch_marker(env):
        return CLASS_WORKER
    return CLASS_AUTOMATION if is_automation(prompt, sdir, now) else CLASS_OPERATOR


def attribute(prompt: str, env: Mapping[str, str], cwd: str, now: Optional[float] = None) -> Attribution:
    """Class and delivery key of one prompt. Ambiguous (``delivery_id`` unknown) without a dispatch marker."""
    project = resolve_project_id_light(cwd, env)
    actor = classify(prompt, env, rotation_state_dir(project, env), now)
    marker = dispatch_marker(env)
    if not marker:
        return Attribution(STATUS_AMBIGUOUS, actor, project, UNKNOWN_IDENTITY)
    keys = delivery_identity_fields(marker, project)
    return Attribution(STATUS_ATTRIBUTED, actor, keys["project_id"], keys["delivery_id"])


def build_record(attribution: Attribution, *, session_id: Any, prompt: str, ts_utc: str) -> dict:
    """The stored line: length and sha256 of the prompt, never its text."""
    data = (prompt or "").encode("utf-8")
    record = {"ts_utc": ts_utc, "session_id": session_id if isinstance(session_id, str) else UNKNOWN_IDENTITY,
              "prompt_len": len(prompt or ""), "prompt_sha256": hashlib.sha256(data).hexdigest(),
              "actor_class": attribution.actor_class, "project_id": attribution.project_id,
              "delivery_id": attribution.delivery_id}
    if attribution.reason:
        record["reason"] = attribution.reason
    return record


def append_record(state_dir: Path, record: dict) -> bool:
    """Append one line under an exclusive lock. A missing state dir is not created: then ``False``."""
    import fcntl

    if not state_dir.is_dir():
        return False
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with (state_dir / STORE_FILENAME).open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.write(line)
    return True
