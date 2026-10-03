#!/usr/bin/env python3
"""event_analyzer.py — Deterministic behavioral analysis of dispatch NDJSON event archives.

No LLM required. Pure Python analysis of tool_use/tool_result event streams.

Usage:
    python3 scripts/lib/event_analyzer.py --dispatch 20260414-090200-f58-pr3-layered-prompt-A
    python3 scripts/lib/event_analyzer.py --all
    python3 scripts/lib/event_analyzer.py --summary
    python3 scripts/lib/event_analyzer.py --all --output "$VNX_STATE_DIR/dispatch_behaviors.json"
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LIB_DIR = str(Path(__file__).resolve().parent)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

# Import after the sys.path guard above: event_store lives next to this module.
from event_store import (  # noqa: E402
    archive_id,
    find_archive_file,
    list_archive_files,
    open_archive_text,
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class DispatchBehavior:
    dispatch_id: str
    terminal: str
    role: str
    duration_seconds: float

    total_events: int = 0
    reads: int = 0
    writes: int = 0
    edits: int = 0
    bash_calls: int = 0
    grep_calls: int = 0
    glob_calls: int = 0

    reads_before_first_write: int = 0
    edit_cycles_same_file: int = 0
    test_fail_edit_cycles: int = 0

    unique_files_read: int = 0
    unique_files_written: int = 0

    phase_sequence: list = field(default_factory=list)
    bash_errors: list = field(default_factory=list)
    test_results: dict = field(default_factory=dict)

    files_read: list = field(default_factory=list)
    files_written: list = field(default_factory=list)

    committed: bool = False
    pushed: bool = False
    wrote_report: bool = False

    first_timestamp: str = ""
    last_timestamp: str = ""


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO 8601 timestamp (with +00:00 or Z suffix)."""
    ts_str = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(ts_str)


def _load_events(archive_path: Path) -> list[dict[str, Any]]:
    """Read every parseable JSON line of an archive; raise when none is left."""
    events: list[dict[str, Any]] = []
    with open_archive_text(archive_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not events:
        raise ValueError(f"No events in {archive_path}")
    return events


def _init_metadata(events: list[dict[str, Any]], archive_path: Path) -> tuple[str, str]:
    """Return (dispatch_id, terminal), taken from the first init event when present."""
    dispatch_id = archive_id(archive_path)
    terminal = "unknown"
    for ev in events:
        if ev.get("type") == "init":
            dispatch_id = ev.get("dispatch_id", dispatch_id)
            terminal = ev.get("terminal", terminal)
            break
    return dispatch_id, terminal


def _timing(events: list[dict[str, Any]]) -> tuple[str, str, float]:
    """Return (first_ts, last_ts, duration_seconds) over the events' timestamps."""
    timestamps = [ev["timestamp"] for ev in events if "timestamp" in ev]
    first_ts = timestamps[0] if timestamps else ""
    last_ts = timestamps[-1] if timestamps else ""

    duration = 0.0
    if first_ts and last_ts:
        try:
            duration = (_parse_ts(last_ts) - _parse_ts(first_ts)).total_seconds()
        except Exception:
            duration = 0.0
    return first_ts, last_ts, duration


def _index_tool_events(
    events: list[dict[str, Any]],
) -> tuple[list[dict], dict[str, dict]]:
    """Return (tool_use events in sequence order, tool_result events by tool_use_id)."""
    ordered_uses: list[dict] = []
    results_by_id: dict[str, dict] = {}
    for ev in events:
        if ev.get("type") == "tool_use":
            ordered_uses.append(ev)
        elif ev.get("type") == "tool_result":
            tid = ev["data"].get("tool_use_id", "")
            results_by_id[tid] = ev
    return ordered_uses, results_by_id


@dataclass
class _ToolScan:
    """Mutable accumulator for the per-tool-call pass in analyze_dispatch."""

    reads: int = 0
    writes: int = 0
    edits: int = 0
    bash_calls: int = 0
    grep_calls: int = 0
    glob_calls: int = 0
    files_read: list = field(default_factory=list)
    files_written: list = field(default_factory=list)
    first_write_idx: int | None = None
    reads_before_first_write: int = 0
    edit_counts_per_file: Counter = field(default_factory=Counter)
    phases: list = field(default_factory=list)
    # pytest fail->edit cycle detection. States: idle | pytest_ran | pytest_failed
    pytest_state: str = "idle"
    test_fail_edit_cycles: int = 0
    test_results: dict = field(default_factory=dict)
    bash_errors: list = field(default_factory=list)
    committed: bool = False
    pushed: bool = False
    wrote_report: bool = False


def _scan_read(scan: _ToolScan, inp: dict) -> None:
    scan.reads += 1
    fp = inp.get("file_path", "")
    if fp:
        scan.files_read.append(fp)
    if scan.first_write_idx is None:
        scan.reads_before_first_write += 1
    _append_phase(scan.phases, "explore")


def _scan_write(scan: _ToolScan, name: str, inp: dict, idx: int) -> None:
    if name == "Write":
        scan.writes += 1
    else:
        scan.edits += 1
    fp = inp.get("file_path", "")
    if fp:
        scan.files_written.append(fp)
        if name in ("Edit", "MultiEdit"):
            scan.edit_counts_per_file[fp] += 1
    if scan.first_write_idx is None:
        scan.first_write_idx = idx
    # Test-fail -> edit cycle detection
    if scan.pytest_state == "pytest_failed":
        scan.test_fail_edit_cycles += 1
        scan.pytest_state = "idle"
    _append_phase(scan.phases, "implement")


def _scan_pytest_result(scan: _ToolScan, result_content: str) -> None:
    """Parse pytest totals from a Bash result and advance the fail->edit state."""
    passed_m = re.search(r"(\d+)\s+passed", result_content)
    failed_m = re.search(r"(\d+)\s+failed", result_content)
    if passed_m or failed_m:
        p = int(passed_m.group(1)) if passed_m else 0
        fa = int(failed_m.group(1)) if failed_m else 0
        # Keep the last run's totals
        scan.test_results["passed"] = p
        scan.test_results["failed"] = fa
        scan.pytest_state = "pytest_failed" if fa > 0 else "pytest_ran"
    else:
        scan.pytest_state = "idle"


def _scan_bash(scan: _ToolScan, inp: Any, result_content: str) -> None:
    scan.bash_calls += 1
    cmd = inp.get("command", "") if isinstance(inp, dict) else str(inp)
    if "git commit" in cmd or "git push" in cmd:
        _append_phase(scan.phases, "commit")
    elif "pytest" in cmd or "python3 -m pytest" in cmd or "python -m pytest" in cmd:
        _append_phase(scan.phases, "test")
    else:
        _append_phase(scan.phases, "implement")
    if "git commit" in cmd:
        scan.committed = True
    if "git push" in cmd:
        scan.pushed = True
    if "unified_reports" in cmd:
        scan.wrote_report = True
    if "pytest" in cmd and result_content:
        _scan_pytest_result(scan, result_content)
    if result_content:
        for line in result_content.splitlines():
            if any(kw in line for kw in ("Error", "Exception", "FAILED", "Traceback")):
                cleaned = line.strip()
                if cleaned and cleaned not in scan.bash_errors:
                    scan.bash_errors.append(cleaned)


def _scan_tool_calls(
    ordered_uses: list[dict], results_by_id: dict[str, dict]
) -> _ToolScan:
    """Walk the tool_use events once and accumulate counters, files and phases."""
    scan = _ToolScan()
    for idx, use_ev in enumerate(ordered_uses):
        name = use_ev["data"].get("name", "")
        inp = use_ev["data"].get("input", {})
        result_ev = results_by_id.get(use_ev["data"].get("id", ""))
        result_content = ""
        if result_ev:
            rc = result_ev["data"].get("content", "")
            result_content = rc if isinstance(rc, str) else json.dumps(rc)

        if name == "Read":
            _scan_read(scan, inp)
        elif name in ("Write", "Edit", "MultiEdit"):
            _scan_write(scan, name, inp, idx)
        elif name == "Bash":
            _scan_bash(scan, inp, result_content)
        elif name == "Grep":
            scan.grep_calls += 1
            _append_phase(scan.phases, "explore")
        elif name == "Glob":
            scan.glob_calls += 1
            _append_phase(scan.phases, "explore")

        # Write tool: also check file_path for report detection
        if name == "Write" and "unified_reports" in inp.get("file_path", ""):
            scan.wrote_report = True
    return scan


def analyze_dispatch(archive_path: Path) -> DispatchBehavior:
    """Parse a single NDJSON archive file and extract behavioral metrics."""
    events = _load_events(archive_path)
    dispatch_id, terminal = _init_metadata(events, archive_path)
    first_ts, last_ts, duration = _timing(events)
    ordered_uses, results_by_id = _index_tool_events(events)
    scan = _scan_tool_calls(ordered_uses, results_by_id)

    # Edit rework: files edited more than once
    edit_cycles_same_file = sum(
        count - 1 for count in scan.edit_counts_per_file.values() if count > 1
    )

    total_events = sum(
        1 for ev in events
        if ev.get("type") in ("tool_use", "tool_result", "thinking", "text", "result", "init", "system")
    )

    return DispatchBehavior(
        dispatch_id=dispatch_id,
        terminal=terminal,
        role="unknown",
        duration_seconds=round(duration, 1),
        total_events=total_events,
        reads=scan.reads,
        writes=scan.writes,
        edits=scan.edits,
        bash_calls=scan.bash_calls,
        grep_calls=scan.grep_calls,
        glob_calls=scan.glob_calls,
        reads_before_first_write=scan.reads_before_first_write,
        edit_cycles_same_file=edit_cycles_same_file,
        test_fail_edit_cycles=scan.test_fail_edit_cycles,
        unique_files_read=len(set(scan.files_read)),
        unique_files_written=len(set(scan.files_written)),
        phase_sequence=scan.phases,
        bash_errors=scan.bash_errors[:50],  # cap at 50
        test_results=scan.test_results,
        files_read=list(dict.fromkeys(scan.files_read)),   # dedup preserving order
        files_written=list(dict.fromkeys(scan.files_written)),
        committed=scan.committed,
        pushed=scan.pushed,
        wrote_report=scan.wrote_report,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
    )


def _append_phase(phases: list[str], phase: str) -> None:
    """Append phase only when it differs from the last entry (run-length encode)."""
    if not phases or phases[-1] != phase:
        phases.append(phase)


# ---------------------------------------------------------------------------
# Batch analysis
# ---------------------------------------------------------------------------

def analyze_all(archive_dir: Path) -> list[DispatchBehavior]:
    """Scan all .ndjson and .ndjson.gz archives under archive_dir recursively, return sorted by timestamp."""
    behaviors: list[DispatchBehavior] = []
    for ndjson_path in list_archive_files(archive_dir, recursive=True):
        try:
            b = analyze_dispatch(ndjson_path)
            behaviors.append(b)
        except Exception as exc:
            sys.stderr.write(f"[warn] skipping {ndjson_path.name}: {exc}\n")
    # Sort by first_timestamp
    behaviors.sort(key=lambda b: b.first_timestamp or "")
    return behaviors


# ---------------------------------------------------------------------------
# Summary aggregation
# ---------------------------------------------------------------------------

def get_summary(behaviors: list[DispatchBehavior]) -> dict:
    """Aggregate cross-dispatch statistics."""
    if not behaviors:
        return {"total_dispatches": 0}

    # Group by role
    by_role: dict[str, list[DispatchBehavior]] = defaultdict(list)
    for b in behaviors:
        by_role[b.role].append(b)

    role_stats: dict[str, dict] = {}
    for role, bs in by_role.items():
        durations = [b.duration_seconds for b in bs]
        rfw = [b.reads_before_first_write for b in bs]
        role_stats[role] = {
            "count": len(bs),
            "duration_avg_s": round(sum(durations) / len(durations), 1),
            "duration_min_s": round(min(durations), 1),
            "duration_max_s": round(max(durations), 1),
            "reads_before_write_avg": round(sum(rfw) / len(rfw), 2),
        }

    # Total rework
    total_rework = sum(b.edit_cycles_same_file + b.test_fail_edit_cycles for b in behaviors)

    # Most common bash errors
    all_errors: list[str] = []
    for b in behaviors:
        all_errors.extend(b.bash_errors)
    error_counts = Counter(all_errors).most_common(20)

    # Most frequently read/written files
    all_reads: list[str] = []
    all_writes: list[str] = []
    for b in behaviors:
        all_reads.extend(b.files_read)
        all_writes.extend(b.files_written)

    top_reads = Counter(all_reads).most_common(20)
    top_writes = Counter(all_writes).most_common(20)

    return {
        "total_dispatches": len(behaviors),
        "terminals_seen": sorted({b.terminal for b in behaviors}),
        "roles_seen": sorted({b.role for b in behaviors}),
        "total_rework_events": total_rework,
        "role_stats": role_stats,
        "most_common_bash_errors": [{"error": e, "count": c} for e, c in error_counts],
        "top_files_read": [{"file": f, "count": c} for f, c in top_reads],
        "top_files_written": [{"file": f, "count": c} for f, c in top_writes],
        "commits_pct": round(
            100 * sum(1 for b in behaviors if b.committed) / len(behaviors), 1
        ),
        "push_pct": round(
            100 * sum(1 for b in behaviors if b.pushed) / len(behaviors), 1
        ),
        "report_pct": round(
            100 * sum(1 for b in behaviors if b.wrote_report) / len(behaviors), 1
        ),
    }


# ---------------------------------------------------------------------------
# Default archive dir resolution
# ---------------------------------------------------------------------------

def _default_archive_dir() -> Path:
    """Locate the events archive dir via canonical vnx_paths (central-mode aware)."""
    here = Path(__file__).resolve().parent
    try:
        lib_dir = str(here)
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        from vnx_paths import resolve_paths
        candidate = Path(resolve_paths()["VNX_DATA_DIR"]) / "events" / "archive"
        if candidate.exists():
            return candidate
    except Exception:
        logging.getLogger(__name__).debug(
            "vnx_paths canonical resolver unavailable; using __file__ last-resort path fallback",
            exc_info=True,
        )
    # scripts/lib -> scripts -> repo root — a raw __file__ two-up walk resolves
    # the KEYSTONE (not the project's ~/.vnx-data/<project>) in a central
    # install. Kept only as a last-resort fallback. See #1023.
    repo_root = here.parent.parent
    candidate = repo_root / ".vnx-data" / "events" / "archive"
    if candidate.exists():
        return candidate
    # Fallback: try CWD
    cwd_candidate = Path.cwd() / ".vnx-data" / "events" / "archive"
    if cwd_candidate.exists():
        return cwd_candidate
    return candidate  # Return even if missing; callers will handle


def _find_dispatch_archive(dispatch_id: str, archive_dir: Path) -> Path | None:
    """Locate the ndjson(.gz) file for a dispatch_id anywhere under archive_dir."""
    return find_archive_file(archive_dir, dispatch_id)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_behavior(b: DispatchBehavior) -> None:
    print(f"\n{'='*60}")
    print(f"Dispatch : {b.dispatch_id}")
    print(f"Terminal : {b.terminal}  Role: {b.role}")
    print(f"Duration : {b.duration_seconds}s   Events: {b.total_events}")
    print(f"Tools    : Read={b.reads} Write={b.writes} Edit={b.edits} "
          f"Bash={b.bash_calls} Grep={b.grep_calls} Glob={b.glob_calls}")
    print(f"Explore  : reads_before_first_write={b.reads_before_first_write}")
    print(f"Rework   : edit_cycles={b.edit_cycles_same_file}  "
          f"test_fail_edit_cycles={b.test_fail_edit_cycles}")
    print(f"Files    : read={b.unique_files_read} unique  "
          f"written={b.unique_files_written} unique")
    print(f"Phases   : {' → '.join(b.phase_sequence)}")
    print(f"Tests    : {b.test_results or 'none detected'}")
    print(f"Outcome  : committed={b.committed}  pushed={b.pushed}  "
          f"wrote_report={b.wrote_report}")
    if b.bash_errors:
        print(f"Errors   : {len(b.bash_errors)} captured")
        for e in b.bash_errors[:5]:
            print(f"  - {e[:120]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze dispatch event archives for behavioral patterns."
    )
    parser.add_argument(
        "--dispatch", metavar="DISPATCH_ID",
        help="Analyze a single dispatch by ID",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Analyze all archives",
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print summary statistics across all archives",
    )
    parser.add_argument(
        "--archive-dir", metavar="PATH",
        help="Path to archive root (default: .vnx-data/events/archive)",
    )
    parser.add_argument(
        "--output", metavar="PATH",
        help="Write JSON output to this file (for --all or --summary)",
    )
    args = parser.parse_args()

    archive_dir = Path(args.archive_dir) if args.archive_dir else _default_archive_dir()

    if args.dispatch:
        path = _find_dispatch_archive(args.dispatch, archive_dir)
        if path is None:
            # Try treating as a direct path
            direct = Path(args.dispatch)
            if direct.exists():
                path = direct
            else:
                sys.exit(f"[error] dispatch archive not found: {args.dispatch}")
        b = analyze_dispatch(path)
        _print_behavior(b)
        if args.output:
            Path(args.output).write_text(json.dumps(asdict(b), indent=2))

    elif args.all or args.summary:
        behaviors = analyze_all(archive_dir)
        print(f"[info] Analyzed {len(behaviors)} dispatch archives from {archive_dir}")

        if args.all:
            for b in behaviors:
                _print_behavior(b)

        if args.summary:
            summary = get_summary(behaviors)
            print(f"\n{'='*60}")
            print("SUMMARY")
            print(f"{'='*60}")
            print(json.dumps(summary, indent=2))

        if args.output:
            if args.summary:
                data = get_summary(behaviors)
            else:
                data = [asdict(b) for b in behaviors]
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data, indent=2))
            print(f"\n[ok] Output written to {args.output}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
