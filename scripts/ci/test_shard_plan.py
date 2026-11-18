#!/usr/bin/env python3
"""Project-independent test shard planner (stdlib only, no project paths).

``plan``: files + N (+ JUnit-XML) -> JSON; greedy on duration, hash for the rest.
``select-source``: JUnit-XML of the newest green run before a timestamp. A failed
source gives ``"source": "hash"`` plus the reason, exit 0; N < 1 or no files exit 2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

ApiCaller = Callable[[str], object]
Downloader = Callable[[str, int, str, Path], None]


class SourceError(Exception):
    """The duration source is unusable; the plan falls back to hash."""


def hash_shard(path: str, shards: int) -> int:
    return int(hashlib.sha256(path.encode("utf-8")).hexdigest(), 16) % shards + 1


def plan(files: Iterable[str], shards: int, durations: Optional[dict] = None) -> tuple[dict, list]:
    if shards < 1:
        raise ValueError("shards must be >= 1")
    unique = sorted(set(files))
    if not unique:
        raise ValueError("no test files")
    known = durations or {}
    measured = sorted((f for f in unique if f in known), key=lambda f: (-known[f], f))
    loads = [0.0] * shards
    assignment: dict[str, int] = {}
    for f in measured:
        idx = min(range(shards), key=lambda i: (loads[i], i))
        loads[idx] += known[f]
        assignment[f] = idx + 1
    for f in unique:
        if f not in known:
            assignment[f] = hash_shard(f, shards)
    return dict(sorted(assignment.items())), [round(x, 3) for x in loads]


def _module_index(files: Iterable[str]) -> dict[str, str]:
    return {f[:-3].replace("/", "."): f for f in files if f.endswith(".py")}


def _map_classname(classname: str, index: dict[str, str]) -> Optional[str]:
    parts = classname.split(".")
    while parts:
        hit = index.get(".".join(parts))
        if hit is not None:
            return hit
        parts.pop()
    return None


def _parse_time(raw: Optional[str], where: str) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise SourceError(f"{where}: testcase time is missing, malformed or negative")
    return value


def durations_from_junit(xml_paths: Sequence[Path], files: Iterable[str]) -> tuple[dict, int]:
    """Sum testcase ``time`` per input file: ``(durations, unmapped)``; ``SourceError`` if unusable."""
    file_set = set(files)
    index = _module_index(file_set)
    totals: dict[str, float] = {}
    unmapped = 0
    for path in xml_paths:
        try:
            raw = Path(path).read_bytes()
            if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                raise SourceError(f"{path}: DOCTYPE/ENTITY is not accepted")
            root = ET.fromstring(raw)
        except (OSError, ET.ParseError) as exc:
            raise SourceError(f"{path}: unreadable XML ({exc})") from exc
        for case in root.iter("testcase"):
            seconds = _parse_time(case.get("time"), str(path))
            target = case.get("file")
            if target not in file_set:
                target = _map_classname(case.get("classname") or "", index)
            if target is None:
                unmapped += 1
            else:
                totals[target] = totals.get(target, 0.0) + seconds
    if not totals:
        raise SourceError("no testcase mapped to an input file")
    return totals, unmapped


def _parse_ts(raw: object) -> datetime:
    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))  # type: ignore[union-attr]
    if stamp.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return stamp.astimezone(timezone.utc)


def _gh_api(path: str) -> object:
    proc = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False, timeout=120)
    if proc.returncode != 0:
        raise SourceError(f"gh api {path} failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def _gh_download(repo: str, run_id: int, artifact: str, dest: Path) -> None:
    cmd = ["gh", "run", "download", str(run_id), "-R", repo, "-n", artifact, "-D", str(dest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    if proc.returncode != 0:
        raise SourceError(f"artifact download failed: {proc.stderr.strip()[:200]}")


def _newest_run_before(api: ApiCaller, repo: str, workflow: str, branch: str, before: datetime) -> int:
    body = api(f"repos/{repo}/actions/workflows/{workflow}/runs?branch={branch}&status=success&per_page=100"
               f"&created=%3C%3D{before:%Y-%m-%dT%H:%M:%SZ}")  # created<=cutoff: page 1 starts before it (OI-2979)
    runs = body.get("workflow_runs") if isinstance(body, dict) else None
    if not isinstance(runs, list):
        raise SourceError("runs response has no workflow_runs list")
    best: Optional[tuple[datetime, int]] = None
    for run in runs:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        run_id = run.get("id")
        try:
            created = _parse_ts(run.get("created_at"))
        except (ValueError, AttributeError):  # no usable created_at: this run cannot be proven earlier
            continue
        try:  # completed before the cutoff too: a later re-run moves updated_at, so a run only drops out, never in
            completed = _parse_ts(run.get("updated_at"))
        except (ValueError, AttributeError):  # no usable updated_at: completion cannot be proven earlier
            continue
        if isinstance(run_id, int) and not isinstance(run_id, bool) and created < before and completed <= before:
            best = max(best or (created, run_id), (created, run_id))
    if best is None:
        raise SourceError("no successful run before the timestamp")
    return best[1]


def _artifact_present(api: ApiCaller, repo: str, run_id: int, artifact: str) -> None:
    body = api(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
    items = body.get("artifacts") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise SourceError("artifacts response has no artifacts list")
    if not any(isinstance(i, dict) and i.get("name") == artifact and i.get("expired") is False for i in items):
        raise SourceError(f"artifact {artifact} of run {run_id} is missing or expired")


def select_source(repo: str, workflow: str, branch: str, before: str, artifact: str, dest: Path,
                  api: Optional[ApiCaller] = None, download: Optional[Downloader] = None) -> tuple[int, list[Path]]:
    call = api or _gh_api
    fetch = download or _gh_download
    try:
        cutoff = _parse_ts(before)
        run_id = _newest_run_before(call, repo, workflow, branch, cutoff)
        _artifact_present(call, repo, run_id, artifact)
        Path(dest).mkdir(parents=True, exist_ok=True)
        fresh = Path(tempfile.mkdtemp(prefix="download-", dir=str(dest)))  # never XML of an earlier download (OI-2979)
        fetch(repo, run_id, artifact, fresh)
    except SourceError:
        raise
    except Exception as exc:  # the source is advisory: any failure is a hash fallback with a reason
        raise SourceError(f"{type(exc).__name__}: {exc}") from exc
    xmls = sorted(fresh.rglob("*.xml"))
    if not xmls:
        raise SourceError(f"artifact {artifact} of run {run_id} holds no XML")
    return run_id, xmls


def read_files(spec: str) -> list[str]:
    text = sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
    lines = (line.strip() for line in text.splitlines())
    return [line[2:] if line.startswith("./") else line for line in lines if line]


def build_result(files: list[str], shards: int, xml_paths: Sequence[Path], source_id: str, reason: Optional[str]) -> dict:
    durations: Optional[dict] = None
    unmapped = 0
    if xml_paths and reason is None:
        try:
            durations, unmapped = durations_from_junit(xml_paths, files)
        except SourceError as exc:
            reason = str(exc)
    elif reason is None:
        reason = "no JUnit-XML given"
    assignment, loads = plan(files, shards, durations)
    return {
        "source": f"junit:{source_id}" if durations else "hash",
        "source_reason": None if durations else reason,
        "shards": shards,
        "assignment": assignment,
        "load_seconds": loads,
        "unmapped": unmapped,
        "files_without_duration": sum(1 for f in assignment if f not in (durations or {})),
    }


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        files = read_files(args.files)
        plan(files, args.shards)
    except (ValueError, OSError) as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    result = build_result(files, args.shards, [Path(p) for p in args.junit], args.source_id or "local", args.source_reason)
    print(json.dumps(result, sort_keys=True))
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    dest = Path(args.dest) if args.dest else Path(tempfile.mkdtemp(prefix="shard-plan-"))
    out: dict = {"run_id": None, "junit": [], "reason": None}
    try:
        run_id, xmls = select_source(args.repo, args.workflow, args.branch, args.before, args.artifact, dest)
        out.update(run_id=run_id, junit=[str(p) for p in xmls])
    except SourceError as exc:
        out["reason"] = str(exc)
    print(json.dumps(out, sort_keys=True))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="assign test files to shards")
    p.add_argument("--files", required=True, help="file with one test path per line, or - for stdin")
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--junit", nargs="*", default=[], help="JUnit-XML files for durations")
    p.add_argument("--source-id", help="run id of the JUnit source (output: junit:<id>)")
    p.add_argument("--source-reason", help="why there is no source (a failed select-source)")
    p.set_defaults(func=_cmd_plan)
    s = sub.add_parser("select-source", help="pick the JUnit-XML of the last green run")
    for name in ("repo", "workflow", "branch", "before", "artifact"):
        s.add_argument(f"--{name}", required=True)
    s.add_argument("--dest", help="download directory (default: a temp dir)")
    s.set_defaults(func=_cmd_select)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
