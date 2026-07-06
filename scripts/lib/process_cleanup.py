#!/usr/bin/env python3
"""process_cleanup — periodic process-hygiene scan (the global-process-cleanup loop).

Classifies every running process into one of three actionable classes plus a
default no-op, so a stray/violating process never races a build again (the 5 stray
`claude -p` incident, 2026-07-06) while legitimate work is left alone:

  - VIOLATION  — a process on the account-protection-blocked headless lane
                 (`claude -p` / `claude --print`; constraint ``claude-headless``).
                 Flagged as an auto-kill candidate regardless of idle time. Killing
                 is still opt-in (``--apply``); dry-run reports only.
  - IDLE       — a *work* process idle past the threshold (>4h, ~0 cpu). SURFACED
                 to the operator (proposal: close y/n) and NEVER auto-killed — the
                 human decides, exactly like the self-learning proposals.
  - PROTECTED  — an INTERACTIVE ``claude`` process (no ``-p``): the fleet sessions
                 (mc/sales/seo + /remote-control). Never flagged, never touched.
  - OK         — everything else.

The producer is pure/testable: ``scan_processes`` takes an optional ``ps_output``
so tests inject fixture lines instead of scanning the host. ``--apply`` sends
SIGTERM to VIOLATION processes only; IDLE and PROTECTED are never killed.

Ties into: pending-proposals-surfacing (idle proposals are appended to
``<state_dir>/process_cleanup_proposals.ndjson`` for the dashboard/pop-up),
provider-constraints (``claude-headless``), and the loop-pattern catalog.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# 4 hours, per the track goal. Configurable so a tick can tighten/loosen it.
IDLE_THRESHOLD_SECONDS = int(os.environ.get("VNX_PROC_IDLE_SECONDS", "14400"))
# A process using more than this %cpu is doing work, not idle.
CPU_IDLE_MAX = float(os.environ.get("VNX_PROC_CPU_IDLE_MAX", "1.0"))
PROPOSALS_FILE = "process_cleanup_proposals.ndjson"

VIOLATION = "violation"
IDLE = "idle"
PROTECTED = "protected"
OK = "ok"

# IDLE only targets stray *work* processes (the real pain: a stray build / benchmark
# / dispatch left running), never system daemons — so an idle proposal is signal,
# not noise. Matched against the full command line.
_WORK_PATTERNS = [
    re.compile(p) for p in (
        r"scripts/lib/\w*dispatch",
        r"scripts/lib/\w*worker",
        r"provider_dispatch\.py",
        r"tmux_interactive_dispatch\.py",
        r"subprocess_dispatch\.py",
        r"benchmark|scout_ab|scout_quality",
        r"pool_worker_runner\.py",
    )
]


@dataclass
class ProcessFinding:
    pid: int
    ppid: int
    etimes: int          # elapsed seconds since start
    pcpu: float          # instantaneous %cpu
    command: str
    klass: str = OK
    reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "etimes": self.etimes,
            "pcpu": self.pcpu,
            "command": self.command,
            "class": self.klass,
            "reason": self.reason,
        }


def _argv0_is_claude(command: str) -> bool:
    """True when the process's OWN executable is ``claude`` — not a shell whose
    script merely CONTAINS the text 'claude' (the false-positive that a naive grep
    hits). Matches argv[0]'s basename only."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    if not argv:
        return False
    return os.path.basename(argv[0]) == "claude"


def _is_forbidden_headless_claude(command: str) -> bool:
    """True for the blocked headless lane: the executable is ``claude`` AND it was
    invoked with ``-p`` / ``--print`` (constraint ``claude-headless``). A ``-p`` that
    is only a substring of another argument does not count — it must be its own token."""
    if not _argv0_is_claude(command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return any(tok == "-p" or tok == "--print" for tok in argv[1:])


def _looks_like_work(command: str) -> bool:
    return any(p.search(command) for p in _WORK_PATTERNS)


def _classify(
    f: ProcessFinding,
    *,
    idle_threshold: int,
    cpu_idle_max: float,
    protected_pids: Optional[set] = None,
    self_pid: Optional[int] = None,
) -> ProcessFinding:
    protected_pids = protected_pids or set()
    # Never classify the scanner itself (or its shell parent) as anything actionable.
    if self_pid is not None and f.pid in (self_pid, os.getppid()):
        f.klass, f.reason = OK, "scanner"
        return f
    if _is_forbidden_headless_claude(f.command):
        f.klass = VIOLATION
        f.reason = "forbidden headless lane (claude -p / --print) — constraint claude-headless"
        return f
    if f.pid in protected_pids or _argv0_is_claude(f.command):
        # Interactive claude (no -p): the fleet sessions + /remote-control. Protected.
        f.klass = PROTECTED
        f.reason = "interactive claude (fleet session) — never touched"
        return f
    if (
        f.etimes >= idle_threshold
        and f.pcpu <= cpu_idle_max
        and _looks_like_work(f.command)
    ):
        f.klass = IDLE
        hours = f.etimes // 3600
        f.reason = f"work process idle ~{hours}h (>= {idle_threshold // 3600}h, cpu {f.pcpu}%)"
        return f
    f.klass, f.reason = OK, ""
    return f


def _read_ps() -> str:
    """Snapshot of the process table: pid ppid etimes pcpu command."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etimes=,pcpu=,command="],
        capture_output=True, text=True, timeout=15,
    )
    return out.stdout


def _parse_ps(ps_output: str) -> List[ProcessFinding]:
    findings: List[ProcessFinding] = []
    for line in ps_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 4)  # pid ppid etimes pcpu command(rest)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, etimes = int(parts[0]), int(parts[1]), int(parts[2])
            pcpu = float(parts[3])
        except ValueError:
            continue
        findings.append(ProcessFinding(pid, ppid, etimes, pcpu, parts[4]))
    return findings


def scan_processes(
    ps_output: Optional[str] = None,
    *,
    idle_threshold: int = IDLE_THRESHOLD_SECONDS,
    cpu_idle_max: float = CPU_IDLE_MAX,
    protected_pids: Optional[set] = None,
) -> List[ProcessFinding]:
    """Scan + classify. Pure over ``ps_output`` when supplied (tests inject fixtures)."""
    raw = ps_output if ps_output is not None else _read_ps()
    self_pid = os.getpid()
    return [
        _classify(
            f,
            idle_threshold=idle_threshold,
            cpu_idle_max=cpu_idle_max,
            protected_pids=protected_pids,
            self_pid=self_pid,
        )
        for f in _parse_ps(raw)
    ]


def emit_proposals(findings: List[ProcessFinding], state_dir: "Path | str") -> Path:
    """Append IDLE + VIOLATION findings to the proposals NDJSON the operator surface
    (pending-proposals-surfacing) reads. Best-effort; never raises past the caller."""
    path = Path(state_dir) / PROPOSALS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    actionable = [f for f in findings if f.klass in (VIOLATION, IDLE)]
    with open(path, "a", encoding="utf-8") as fh:
        for f in actionable:
            rec = f.to_dict()
            rec["proposed_action"] = "kill" if f.klass == VIOLATION else "operator_confirm_close"
            rec["operator_gated"] = f.klass == IDLE  # violations may auto-kill; idle needs a human
            rec["timestamp"] = ts
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return path


def _kill_violations(findings: List[ProcessFinding]) -> List[int]:
    """SIGTERM the VIOLATION processes only. IDLE and PROTECTED are never killed."""
    killed: List[int] = []
    for f in findings:
        if f.klass != VIOLATION:
            continue
        try:
            os.kill(f.pid, signal.SIGTERM)
            killed.append(f.pid)
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scan + classify processes (global cleanup loop).")
    ap.add_argument("--apply", action="store_true",
                    help="SIGTERM the forbidden-lane VIOLATIONS (never idle/protected). Default: dry-run.")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--state-dir", default=os.environ.get("VNX_STATE_DIR", ""),
                    help="where to append the operator proposals NDJSON")
    args = ap.parse_args(argv)

    findings = scan_processes()
    violations = [f for f in findings if f.klass == VIOLATION]
    idle = [f for f in findings if f.klass == IDLE]

    if args.state_dir:
        try:
            emit_proposals(findings, args.state_dir)
        except OSError as exc:
            print(f"warning: could not write proposals: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps([f.to_dict() for f in findings if f.klass != OK], indent=2))
    else:
        print(f"process-cleanup scan: {len(violations)} violation(s), {len(idle)} idle candidate(s)")
        for f in violations:
            print(f"  [VIOLATION] pid={f.pid} {f.reason}\n      {f.command[:100]}")
        for f in idle:
            print(f"  [idle→operator] pid={f.pid} {f.reason}\n      {f.command[:100]}")

    if args.apply and violations:
        killed = _kill_violations(findings)
        print(f"applied: SIGTERM sent to {len(killed)} violation(s): {killed}")
    elif violations and not args.apply:
        print("(dry-run: re-run with --apply to SIGTERM the violations; idle stays operator-gated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
