"""Prompt attribution (ADR-056, zelfst PR-8b): the class of a prompt and, only when unambiguous, its delivery key.

The prompt is the sixth coverage source of ADR-056 (§ Tenant-veilige sleutel, row "prompt attribution"):
``project_id`` and ``delivery_id`` only when exactly one delivery belongs to the session, otherwise
``unknown`` with ``reason = ambiguous_session``. No session-to-delivery index exists, so the only
unambiguous case is a dispatch marker in the environment of the worker that received the prompt.

The class of a prompt is NOT ``actor_class.derive()``: in a T0 session the environment says ``t0``
(after PR-7c), but a prompt typed into that session is the operator's. So:

  - a dispatch marker (``VNX_DISPATCH_ID`` / ``VNX_CURRENT_DISPATCH_ID``) is ``worker``;
  - a prompt equal to ``/kickoff rotatie <run>`` for a rotation run directory ``<run>`` younger than 30
    minutes (the spawner's fixed first message, ``t0_rotate_spawn.sh``), or to the content of that run's
    ``goal.txt``, is ``automation``. Only what exists before launch is read: ``delivery.json`` is written
    after the first prompt and plays no part;
  - every other prompt is ``operator``.

Pure functions, no network, no git subprocess, no DB. The prompt text is hashed and measured, never stored.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from delivery_identity import delivery_identity_fields
from governance_receipts import UNKNOWN_IDENTITY
from project_root import resolve_central_data_dir
from vnx_ids import PROJECT_ID_RE
from t0_rotation_state import LATCH_TTL_SECONDS, STATE_DIR_ENV

DISPATCH_MARKERS = ("VNX_DISPATCH_ID", "VNX_CURRENT_DISPATCH_ID")
STORE_FILENAME = "prompt_attribution.ndjson"
CLASS_WORKER, CLASS_AUTOMATION, CLASS_OPERATOR = "worker", "automation", "operator"
STATUS_ATTRIBUTED, STATUS_AMBIGUOUS = "attributed", "ambiguous_session"
RUN_DIR_GLOB = "rotation-*"
PROJECT_MARKER = ".vnx-project-id"
KICKOFF_PREFIX = "kickoff rotatie "


def valid_project_id(value: str) -> str:
    """``value`` when it is a single-component project slug, else ``unknown`` (never joined into a path)."""
    return value if PROJECT_ID_RE.fullmatch(value or "") else UNKNOWN_IDENTITY


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
        return valid_project_id(explicit)
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
            return valid_project_id(first)
    return UNKNOWN_IDENTITY


def central_state_dir(project_id: str) -> Optional[Path]:
    """``~/.vnx-data/<project_id>/state``, or ``None`` when the project is unknown."""
    if project_id == UNKNOWN_IDENTITY or valid_project_id(project_id) == UNKNOWN_IDENTITY:
        return None
    return resolve_central_data_dir(project_id) / "state"


def rotation_state_dir(project_id: str, env: Mapping[str, str]) -> Optional[Path]:
    """The rotation state dir: the override of ``t0_rotation_state`` when set, else the central one."""
    override = (env.get(STATE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    state = central_state_dir(project_id)
    if state is None:
        return None
    sdir = state / "t0_rotation"
    return sdir if is_contained(sdir, resolve_central_data_dir(project_id)) else None


def is_contained(path: Path, root: Path) -> bool:
    """True when ``path`` resolves (symlinks followed) to ``root`` or below it."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _normal(text: object) -> str:
    """A prompt compared without surrounding whitespace and without the slash of a slash command."""
    return text.strip().lstrip("/") if isinstance(text, str) else ""


def _expected_texts(run_dir: Path) -> list:
    """What the spawner types for this run, from files that exist before launch."""
    goal = run_dir / "goal.txt"
    texts = [KICKOFF_PREFIX + run_dir.name, _read_text(goal) if is_contained(goal, run_dir) else None]
    return [t for t in (_normal(x) for x in texts) if t]


def _fresh_run_dirs(sdir: Path, now: float) -> list:
    try:
        candidates = [p for p in sdir.glob(RUN_DIR_GLOB) if p.is_dir()]
        inside = [p for p in candidates if is_contained(p, sdir)]
        if len(inside) < len(candidates):
            print("prompt_attribution: rotation run outside the state dir skipped", file=sys.stderr)
        return [p for p in inside if 0 <= now - p.stat().st_mtime < LATCH_TTL_SECONDS]
    except OSError:
        return []


def is_automation(prompt: str, sdir: Optional[Path], now: Optional[float] = None) -> bool:
    """True when ``prompt`` equals the typed text of a rotation run younger than 30 minutes."""
    wanted = _normal(prompt)
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


LOCK_BUDGET_SECONDS = 0.05
LOCK_RETRY_SECONDS = 0.005


def _acquire_bounded(fd: int) -> bool:
    """Take the exclusive lock without ever waiting longer than ``LOCK_BUDGET_SECONDS`` in total."""
    import fcntl

    deadline = time.monotonic() + LOCK_BUDGET_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_RETRY_SECONDS)


def append_record(state_dir: Path, record: dict, root: Optional[Path] = None) -> bool:
    """Append one line under an exclusive lock. A missing state dir is not created: then ``False``.

    The hook runs on every prompt submission and must never delay it, so the lock is bounded to 50 ms.
    When it is still held after that, nothing is written, ``prompt_attribution: ledger busy, line
    dropped`` goes to stderr and the result is ``False``. Trade-off: a dropped line is a measured gap in
    the prompt source, not a hung prompt.

    With ``root``, the resolved store file must lie under the resolved ``root``; otherwise nothing is
    written and ``ValueError`` is raised.
    """
    import sys

    if not state_dir.is_dir():
        return False
    if root is not None and not is_contained(state_dir / STORE_FILENAME, root):
        raise ValueError(f"store {state_dir / STORE_FILENAME} resolves outside {root}")
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with (state_dir / STORE_FILENAME).open("a", encoding="utf-8") as fh:
        if not _acquire_bounded(fh.fileno()):
            sys.stderr.write("prompt_attribution: ledger busy, line dropped\n")
            return False
        fh.write(line)
    return True
