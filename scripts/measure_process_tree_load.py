#!/usr/bin/env python3
"""Sample CPU, RSS, swap and compressor load of one process tree (RS-1).

One `ps` snapshot per interval, split into xdist workers (execnet/pytest-xdist)
and all other subprocesses, beside swap (`sysctl vm.swapusage`) and the
compressor (`vm_stat`). Read-only: never signals or kills a process. Each sample
is one NDJSON line; `--summary` adds count, mean, p50, p95 and max per field plus
`cores_p95`, the candidate slot number for one run (a measurement, not a budget).
Exit codes: 0 completed run, 1 every snapshot failed, 2 bad arguments or an
existing `--out`, 3 the root cannot be found.

The pure parsers, the tree walk, the classifier and the summary live in
`scripts/lib/process_tree_load.py` (split 1 of RS-1); this module adds only the
sampler, the exclusive NDJSON writer and the CLI on top of them.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_LIB_DIR = str(Path(__file__).resolve().parent / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from process_tree_load import (  # noqa: E402
    SAMPLE_FIELDS,
    parse_ps,
    parse_swapusage,
    parse_vm_stat,
    sample_tree,
    summarize,
)

PS_CMD = ("ps", "-A", "-o", "pid=,ppid=,pgid=,pcpu=,rss=,command=")
SWAP_CMD = ("sysctl", "vm.swapusage")
VM_STAT_CMD = ("vm_stat",)


def _run(cmd: tuple[str, ...], timeout: float = 10) -> tuple[str | None, str | None]:
    """One read-only command: (stdout, None), or (None, why it produced nothing)."""
    try:
        result = subprocess.run(list(cmd), capture_output=True, text=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc) or type(exc).__name__
    if result.returncode != 0:
        return None, f"exit {result.returncode}"
    if not result.stdout.strip():
        return None, "empty output"
    return result.stdout, None


def take_sample(root_pid: int) -> tuple[dict, str]:
    """One sample and its outcome: "sample", "root_gone" or "snapshot_failed".

    A failed `ps` snapshot is not a vanished root: the count, CPU and rss fields
    stay null with a `ps_failed:` reason and the caller keeps sampling. An
    unreadable value is null with a reason, never 0.
    """
    sample = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "root_pid": root_pid}
    sample.update(dict.fromkeys(SAMPLE_FIELDS))
    reasons = []
    ps_text, ps_error = _run(PS_CMD)
    procs = parse_ps(ps_text) if ps_text is not None else []
    if ps_error is not None or not procs:
        outcome = "snapshot_failed"
        reasons.append(f"ps_failed: {ps_error or 'ps output has no parsable line'}")
    elif not any(p["pid"] == root_pid for p in procs):
        outcome = "root_gone"
        reasons.append("tree: root pid not in snapshot")
    else:
        outcome = "sample"
        sample.update(sample_tree(procs, root_pid))
    swap_text, _ = _run(SWAP_CMD)
    swap = parse_swapusage(swap_text) if swap_text is not None else None
    if swap is None:
        reasons.append("swap_used: sysctl vm.swapusage gave no usable value")
    else:
        sample["swap_used"] = swap
    vm_text, _ = _run(VM_STAT_CMD)
    compressor = parse_vm_stat(vm_text) if vm_text is not None else None
    if compressor is None:
        reasons.append("compressor: vm_stat has no compressor line")
    else:
        sample["compressor"] = compressor
    if reasons:
        sample["reasons"] = reasons
    return sample, outcome


def run_measurement(root_pid: int, interval: float, duration: float | None,
                    out_path: Path) -> tuple[dict | None, int]:
    """Sample until the duration elapses or the root disappears; write NDJSON.

    `out_path` is created exclusively and only after the first successful
    snapshot, so an earlier measurement is never truncated and a missing root
    writes nothing. Lines sampled before that point are buffered, never lost.
    """
    samples, failed, buffered, handle = [], 0, [], None
    started = time.monotonic()
    deadline = started + duration if duration is not None else None
    while True:
        sample, outcome = take_sample(root_pid)
        if outcome == "root_gone":
            if handle is None:
                return None, 3
            return summarize(samples, time.monotonic() - started, failed), 0
        if outcome == "snapshot_failed":
            failed += 1
        else:
            samples.append(sample)
        buffered.append(json.dumps(sample) + "\n")
        if handle is None and outcome == "sample":
            handle = out_path.open("x", encoding="utf-8")
        if handle is not None:
            handle.writelines(buffered)
            handle.flush()
            buffered.clear()
        if deadline is None:
            time.sleep(interval)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (summarize(samples, time.monotonic() - started, failed),
                    0 if samples else 1)
        time.sleep(min(interval, remaining))


def resolve_root_pid(args: argparse.Namespace) -> tuple[int | None, str | None]:
    if args.root_pid is not None:
        return args.root_pid, None
    try:
        pattern = re.compile(args.match)
    except re.error as exc:
        return None, f"bad --match regex: {exc}"
    ps_text, ps_error = _run(PS_CMD)
    if ps_text is None:
        return None, f"ps snapshot unreadable: {ps_error}"
    for proc in parse_ps(ps_text):
        if pattern.search(proc["command"]):
            return proc["pid"], None
    return None, f"no process matches --match {args.match!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample the load of one process tree.")
    root = parser.add_mutually_exclusive_group(required=True)
    root.add_argument("--root-pid", type=int, help="pid of the tree's root process")
    root.add_argument("--match", help="REGEX: first process whose command matches")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between samples")
    parser.add_argument("--duration", type=float, default=None,
                        help="seconds to sample; default: until the root is gone")
    parser.add_argument("--out", required=True, type=Path, help="NDJSON output path")
    parser.add_argument("--summary", action="store_true", help="print the JSON summary")
    args = parser.parse_args(argv)
    if args.interval <= 0 or (args.duration is not None and args.duration < 0):
        print("error: --interval must be > 0 and --duration >= 0", file=sys.stderr)
        return 2
    if args.out.exists():
        print(f"error: --out {args.out} already exists", file=sys.stderr)
        return 2
    root_pid, error = resolve_root_pid(args)
    if root_pid is None:
        print(f"error: {error}", file=sys.stderr)
        return 2 if error.startswith("bad --match") else 3
    try:
        summary, code = run_measurement(root_pid, args.interval, args.duration, args.out)
    except FileExistsError:
        print(f"error: --out {args.out} already exists", file=sys.stderr)
        return 2
    if summary is None:
        print(f"error: root pid {root_pid} not found", file=sys.stderr)
        return 3
    if args.summary:
        print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
