"""Daemon register — the single source of truth for VNX's supervised daemons (D2).

D2 dispatch measured four candidate registers before picking one:

  - ``scripts/vnx_supervisor_simple.sh``'s ``start_all()`` — ten ``start_process``
    calls, one commented out, two behind a ``VNX_QUEUE_POPUP_ENABLED`` branch,
    two resolved through shell variables. **This is the source**: it is the
    only place that actually decides which processes VNX starts.
  - the same file's ``status()`` — a second, hand-duplicated list derived from
    the first. Rejected: a derived copy, not the source.
  - ``scripts/commands/start.sh`` — carries two more hand-duplicated fallback
    lists. Rejected for the same reason.
  - ``subsystem_health.known_subsystems()`` — 28 cockpit subsystem names that
    do not know about daemons at all (overlap with the nine daemons: two).
    Rejected: wrong register entirely.

``read_daemon_register()`` parses ``start_all()`` directly instead of
hand-maintaining a Python list — a hand-kept list drifts from the shell
function exactly the way ``docs/core/ARCHITECTURE.md`` already had (the
D2 dispatch's own finding). D6 (architecture-doc generation) reads this same
module so there is exactly one register, not two.

``measure_daemon_liveness()`` is the second half: for each register entry,
determine whether a matching process is actually running right now, using a
single ``psutil.process_iter()`` pass (already a project dependency — see
``scripts/intelligence_daemon_monitor.py``) rather than one ``pgrep`` subprocess
per daemon. A daemon's state is one of three, not two: ``"running"``,
``"absent"``, or ``"unknown"`` — "unknown" is its own branch for when
liveness genuinely cannot be measured (psutil unavailable, or process
enumeration itself raised), not a silent default toward either of the other
two. Conflating "could not check" with "not running" would be exactly the
kind of loud-but-wrong claim this dispatch exists to prevent.

This module has three distinct ways of failing to produce a register, and
each gets its own loud handling instead of being lumped together (D2b):

  1. **The source is gone** — ``start_all()`` itself cannot be found in the
     supervisor script (missing function, renamed, file moved).
     ``read_daemon_register()`` raises ``ValueError``.
  2. **The source exists but the parse finds nothing** — ``start_all()`` is
     there, but zero ``start_process`` lines matched (e.g. the shell switched
     from double to single quotes and the regex silently stopped matching).
     This is NOT the same as an intentionally empty register: it is a failed
     parse wearing an empty register's clothes. ``read_daemon_register()``
     raises ``ValueError`` here too — a structurally-found-but-semantically-
     empty result is exactly as untrustworthy as a missing function, and
     collapsing it into "zero daemons expected" is the bug this dispatch
     closes (a real register going from nine entries to zero, silently,
     reported ``overall: "ok"``).
  3. **The measurement itself cannot run** — process enumeration raises, or
     psutil is unavailable. ``measure_daemon_liveness()`` returns
     ``overall: "unknown"`` via ``_all_unknown()`` rather than raising,
     because by this point a caller already has a real register and wants a
     best-effort answer, not an exception.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import project_root  # noqa: E402

_SUPERVISOR_RELATIVE_PATH = Path("scripts") / "vnx_supervisor_simple.sh"

_VAR_ASSIGN_RE = re.compile(r'^([A-Z_][A-Z0-9_]*)="([^"]*)"\s*$')
_START_PROCESS_RE = re.compile(r'^start_process\s+"([^"]*)"\s+"([^"]*)"')
_FUNC_START_RE = re.compile(r'^start_all\s*\(\)\s*\{')
_IF_RE = re.compile(r'^if\b')


@dataclass(frozen=True)
class DaemonSpec:
    """One daemon as declared by ``start_all()``.

    ``scripts`` carries every candidate script name observed for this daemon
    name — normally one, but ``queue_watcher`` has two (the
    ``VNX_QUEUE_POPUP_ENABLED`` branch selects between
    ``queue_popup_watcher.sh`` and ``queue_auto_accept.sh``). Liveness
    matching treats these as alternatives: the daemon is "running" if a
    process matching ANY of its scripts is found.
    """

    name: str
    scripts: Tuple[str, ...]
    conditional: bool = False
    line: Optional[int] = None


def _default_supervisor_script() -> Path:
    # No __file__ anchor (central-mode path gate, shape 3a): a central install
    # runs this module from the read-only keystone checkout, so anchoring on
    # __file__ would resolve the keystone's supervisor script, not the
    # project's. CWD-first resolution matches every caller of this default —
    # build_t0_state.py and generate_daemon_liveness_md.py both run with the
    # project checkout as CWD.
    root = project_root.resolve_project_root()
    return root / _SUPERVISOR_RELATIVE_PATH


def read_daemon_register(supervisor_script: Optional[Path] = None) -> Tuple[DaemonSpec, ...]:
    """Parse ``start_all()`` in ``vnx_supervisor_simple.sh`` into daemon specs.

    Raises ``ValueError`` if the file is unreadable, ``start_all()`` cannot be
    found, OR ``start_all()`` is found but parses to zero entries — the third
    failure mode (see module docstring): a structural change to the shell
    (e.g. a quote-style edit that the ``start_process`` regex no longer
    matches) can silently turn a real, populated register into an empty one
    without raising anything on its own. An empty parse result is
    indistinguishable from "this project runs no daemons" unless it is
    treated as a failure — so it is. Callers that need a best-effort/
    non-raising read (e.g. ``measure_daemon_liveness``) catch ``ValueError``
    themselves; they do not receive a legitimately-empty register through
    this path.
    """
    path = Path(supervisor_script) if supervisor_script else _default_supervisor_script()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    variables: Dict[str, str] = {}
    for line in lines:
        m = _VAR_ASSIGN_RE.match(line.strip())
        if m:
            variables[m.group(1)] = m.group(2)

    def _resolve(token: str) -> str:
        if token.startswith("$"):
            return variables.get(token[1:], token)
        return token

    body_start = None
    for i, line in enumerate(lines):
        if _FUNC_START_RE.match(line.strip()):
            body_start = i + 1
            break
    if body_start is None:
        raise ValueError(f"start_all() not found in {path}")

    # Bash functions in this file never nest braces inside start_all() (the
    # if/else/fi conditional does not use braces), so the first bare "}" line
    # reliably closes the function.
    specs: "dict[str, Dict[str, Any]]" = {}
    if_depth = 0
    for offset, line in enumerate(lines[body_start:]):
        stripped = line.strip()
        if stripped == "}":
            break
        if not stripped or stripped.startswith("#"):
            continue
        if _IF_RE.match(stripped):
            if_depth += 1
            continue
        if stripped == "fi":
            if_depth = max(0, if_depth - 1)
            continue
        m = _START_PROCESS_RE.match(stripped)
        if not m:
            continue
        name = _resolve(m.group(1))
        script = _resolve(m.group(2))
        line_no = body_start + offset + 1
        entry = specs.setdefault(name, {"scripts": [], "conditional": False, "line": line_no})
        if script not in entry["scripts"]:
            entry["scripts"].append(script)
        if if_depth > 0:
            entry["conditional"] = True

    if not specs:
        raise ValueError(
            f"start_all() found in {path} but yielded zero start_process "
            "entries — a failed parse (e.g. a quote-style change the regex "
            "no longer matches), not an empty register"
        )

    return tuple(
        DaemonSpec(
            name=name,
            scripts=tuple(entry["scripts"]),
            conditional=entry["conditional"],
            line=entry["line"],
        )
        for name, entry in specs.items()
    )


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _all_unknown(register: Sequence[DaemonSpec], *, reason: str) -> Dict[str, Any]:
    daemons = {
        spec.name: {"expected": True, "state": "unknown", "pid": None, "since": None}
        for spec in register
    }
    return {"overall": "unknown", "daemons": daemons, "reason": reason}


def measure_daemon_liveness(
    register: Optional[Sequence[DaemonSpec]] = None,
    *,
    supervisor_script: Optional[Path] = None,
) -> Dict[str, Any]:
    """Measure whether each registered daemon currently has a running process.

    Returns ``{"overall": "ok"|"fail"|"unknown", "daemons": {name: {...}}}``.
    ``overall`` is ``"fail"`` when at least one expected daemon is confirmed
    absent (a real, measured problem — the exact "nine daemons show 0
    processes and nothing says so" case this dispatch closes), ``"unknown"``
    when liveness could not be measured at all (psutil unavailable, or
    process enumeration itself raised — never silently reported as "ok").
    """
    if register is None:
        try:
            register = read_daemon_register(supervisor_script)
        except (OSError, ValueError) as exc:
            return {"overall": "unknown", "daemons": {}, "reason": f"register unreadable: {exc}"}

    try:
        import psutil
    except ImportError as exc:
        return _all_unknown(register, reason=f"psutil unavailable: {exc}")

    matches: Dict[str, Tuple[Optional[int], Optional[float]]] = {}
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
            try:
                info = proc.info
                cmdline = info.get("cmdline") or []
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            if not cmdline:
                continue
            # Exact basename match per argv token, NOT a substring search over
            # the joined command line: a `claude -p <prompt>` worker process
            # (this dispatch's own process, or a sibling worker's) carries its
            # full instruction text as a single argv element, and that prose
            # routinely NAMES these very script filenames (e.g. this file's
            # own docstring/dispatch instructions). A substring match against
            # that blob self-matches every daemon as "running" on the wrong
            # PID -- measured directly while building this module (all nine
            # matched the current `claude -p` worker PID). Basename equality
            # on a single token only matches the real
            # `nohup python /.../heartbeat_ack_monitor.py &` invocation shape.
            basenames = {Path(tok).name for tok in cmdline if tok}
            for spec in register:
                if spec.name in matches:
                    continue
                if any(script in basenames for script in spec.scripts):
                    matches[spec.name] = (info.get("pid"), info.get("create_time"))
    except Exception as exc:  # vnx-silent-except: process enumeration is best-effort; a failure mid-scan must still yield "unknown", not a false "absent"
        return _all_unknown(register, reason=f"process enumeration failed: {exc}")

    daemons: Dict[str, Any] = {}
    absent = False
    for spec in register:
        hit = matches.get(spec.name)
        if hit is not None:
            pid, create_time = hit
            daemons[spec.name] = {
                "expected": True,
                "state": "running",
                "pid": pid,
                "since": _iso(create_time) if create_time else None,
            }
        else:
            absent = True
            daemons[spec.name] = {
                "expected": True,
                "state": "absent",
                "pid": None,
                "since": None,
            }

    return {"overall": "fail" if absent else "ok", "daemons": daemons}


__all__ = ["DaemonSpec", "read_daemon_register", "measure_daemon_liveness"]
