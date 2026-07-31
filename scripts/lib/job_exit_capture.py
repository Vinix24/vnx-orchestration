#!/usr/bin/env python3
"""job_exit_capture.py — exit-status capture for launchd / cron / nohup jobs.

OI-850 ran for months on exit 127 and nobody read it: launchd plists redirect
stdout/stderr to a log file and the exit code evaporates. This module gives
every scheduled job two capture paths into ``<state_dir>/job_exits.ndjson``:

  1. wrapper  — run the job THROUGH this module:
        python3 scripts/lib/job_exit_capture.py --state-dir ... --job nightly-pipeline -- \
            /bin/bash scripts/nightly_intelligence_pipeline.sh
     The child exit code is recorded (with duration) and returned unchanged,
     so wrapping never alters job semantics. Works for cron and nohup lines.

  2. harvest  — ``harvest_launchd()`` reads ``launchctl list`` and records the
     LastExitStatus of every ``com.vnx.*`` job. No plist changes needed; this
     is exactly how an exit-127 loop like OI-850 surfaces. A small cache file
     (``job_exits_launchd_state.json``) dedups unchanged statuses so a harvest
     per sweep does not flood the NDJSON stream; a persistent non-zero status
     is re-recorded once per RE_RECORD_SECONDS so an ongoing failure stays
     visible instead of firing once and going quiet.

Record shape (NDJSON, one per line):
    {"timestamp", "event_type": "job_exit", "job", "exit_code",
     "duration_seconds" (wrapper only), "source": "wrapper"|"launchd_harvest"}
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_LOG = logging.getLogger(__name__)

JOB_EXITS_FILENAME = "job_exits.ndjson"
HARVEST_CACHE_FILENAME = "job_exits_launchd_state.json"
LAUNCHD_LABEL_PREFIX = "com.vnx."
# A persistently-failing job is re-recorded at most once per day per label.
RE_RECORD_SECONDS = 86400


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_job_exit(
    state_dir: Path,
    *,
    job: str,
    exit_code: int,
    source: str,
    duration_seconds: Optional[float] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Path:
    """Append one job-exit record (locked append + fsync). Returns the path."""
    path = Path(state_dir) / JOB_EXITS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    record: Dict[str, Any] = {
        "timestamp": _now_iso(),
        "event_type": "job_exit",
        "job": job,
        "exit_code": int(exit_code),
        "source": source,
    }
    if duration_seconds is not None:
        record["duration_seconds"] = round(float(duration_seconds), 3)
    if detail:
        record["detail"] = detail
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record, ensure_ascii=False, default=str))
            fh.write("\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                _LOG.warning("job_exit_capture: fsync failed for %s: %s", path, exc)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return path


def run_and_record(state_dir: Path, *, job: str, argv: Sequence[str]) -> int:
    """Run ``argv``, record its exit code, and return it unchanged.

    The wrapper is transparent: whatever the child exits is what the caller
    (launchd/cron/nohup) sees, so wrapping never changes job semantics.
    """
    if not argv:
        raise ValueError("run_and_record requires a command after '--'")
    start = time.monotonic()
    try:
        proc = subprocess.run(list(argv), check=False)
        code = proc.returncode
    except OSError as exc:
        # The job itself could not even start (e.g. interpreter missing —
        # the OI-850/OI-852 class). Record 127, the shell's "command not
        # found", so the failure is visible in the same stream.
        _LOG.warning("job_exit_capture: could not start %s: %s", argv[0], exc)
        code = 127
    duration = time.monotonic() - start
    record_job_exit(
        state_dir,
        job=job,
        exit_code=code,
        duration_seconds=duration,
        source="wrapper",
    )
    return code


def _parse_launchctl_list(output: str) -> List[Dict[str, Any]]:
    """Parse ``launchctl list`` output: 'PID<TAB>Status<TAB>Label' per line."""
    jobs: List[Dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pid_raw, status_raw, label = parts
        try:
            status = int(status_raw)
        except ValueError:
            continue
        jobs.append(
            {
                "label": label.strip(),
                "pid": None if pid_raw.strip() == "-" else int(pid_raw),
                "last_exit_status": status,
            }
        )
    return jobs


def _load_harvest_cache(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("job_exit_capture: harvest cache %s unreadable, starting fresh: %s", path, exc)
        return {}


def harvest_launchd(
    state_dir: Path,
    *,
    runner: Any = None,
    label_prefix: str = LAUNCHD_LABEL_PREFIX,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Record LastExitStatus for every launchd job whose label starts with
    ``label_prefix``. Dedup via a cache file: a status identical to the last
    harvest is only re-recorded after RE_RECORD_SECONDS when non-zero.

    ``runner`` is injectable for tests; defaults to subprocess.run.
    """
    run = runner or subprocess.run
    now = time.time() if now is None else now
    state_dir = Path(state_dir)
    try:
        proc = run(["launchctl", "list"], capture_output=True, text=True, check=False)
    except OSError as exc:
        _LOG.warning("job_exit_capture: launchctl unavailable: %s", exc)
        return {"harvested": 0, "recorded": 0, "error": str(exc)}
    if proc.returncode != 0:
        _LOG.warning("job_exit_capture: launchctl list exited %s", proc.returncode)
        return {"harvested": 0, "recorded": 0, "error": f"launchctl exit {proc.returncode}"}

    cache_path = state_dir / HARVEST_CACHE_FILENAME
    cache = _load_harvest_cache(cache_path)
    harvested = 0
    recorded = 0
    for job in _parse_launchctl_list(proc.stdout):
        label = job["label"]
        if not label.startswith(label_prefix):
            continue
        harvested += 1
        status = job["last_exit_status"]
        cached = cache.get(label) or {}
        unchanged = cached.get("last_exit_status") == status
        recently_recorded = (now - float(cached.get("recorded_ts", 0))) < RE_RECORD_SECONDS
        if unchanged and (status == 0 or recently_recorded):
            continue
        record_job_exit(
            state_dir,
            job=label,
            exit_code=status,
            source="launchd_harvest",
            detail={"pid": job["pid"]},
        )
        recorded += 1
        cache[label] = {"last_exit_status": status, "recorded_ts": now}

    try:
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        _LOG.warning("job_exit_capture: could not write harvest cache %s: %s", cache_path, exc)
    return {"harvested": harvested, "recorded": recorded}


def _default_state_dir() -> Path:
    env = os.environ.get("VNX_STATE_DIR") or (
        os.path.join(os.environ["VNX_DATA_DIR"], "state") if os.environ.get("VNX_DATA_DIR") else None
    )
    if not env:
        raise SystemExit("job_exit_capture: pass --state-dir or set VNX_STATE_DIR / VNX_DATA_DIR")
    return Path(env)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", default=None, help="VNX state dir (default: $VNX_STATE_DIR)")
    parser.add_argument("--job", default=None, help="Job name for wrapper mode")
    parser.add_argument("--harvest-launchd", action="store_true", help="Harvest launchctl LastExitStatus")
    parser.add_argument("--label-prefix", default=LAUNCHD_LABEL_PREFIX)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Wrapper mode: command after '--'")
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir) if args.state_dir else _default_state_dir()

    if args.harvest_launchd:
        result = harvest_launchd(state_dir, label_prefix=args.label_prefix)
        print(json.dumps(result))
        return 0

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not args.job or not command:
        parser.error("wrapper mode needs --job NAME -- COMMAND...")
    return run_and_record(state_dir, job=args.job, argv=command)


if __name__ == "__main__":
    raise SystemExit(main())
