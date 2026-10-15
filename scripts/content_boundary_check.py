#!/usr/bin/env python3
"""Nightly content-boundary measurement over the transcripts of external lanes.

Restricted content (client and personal folders, see ``lib/content_class.py``) must never
reach a harness lane (deepseek, glm) or kimi. This check reads the transcripts those lanes
leave behind and counts four signals per transcript:

  config_leak        an ``instructions`` entry loaded from ``~/.claude/`` or ``~/.claude-*/``
  restricted_cwd     the session ran in a restricted folder
  restricted_access  a tool call or user prompt names a concrete path below a restricted root
                     (tool results and attachments are excluded: repo files name such paths)
  canary             the canary token occurs anywhere in a record

Roots (all injectable): ``<home>/.claude/projects/*/*.jsonl`` (harness transcripts only),
``<stores>/*/harness-config/*/projects/*/*.jsonl`` (every transcript) and
``<home>/.kimi/sessions/*/*/context.jsonl``.

Only files with an mtime at or after ``since`` gate. Older hits are reported as ``historic``.
The beacon (``health/content_boundary.json``), the ledger line and the log carry counts,
lane names and session ids, never a path, a token or transcript text.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import content_class  # noqa: E402

COMPONENT = "content_boundary"
LEDGER_NAME = "content_boundary_findings.ndjson"
BEACON_INTERVAL_SECONDS = 86400
SESSION_ID_LIMIT = 20

SIGNALS = ("config_leak", "restricted_cwd", "restricted_access", "canary")
KIMI_SIGNALS = ("canary", "restricted_access")
KIMI_SYSTEM_REPORT = "system_prompt_names_restricted_root"

ROOT_CLAUDE = "claude_projects"
ROOT_LANE = "harness_config"
ROOT_KIMI = "kimi"

LANE_DEEPSEEK = "deepseek"
LANE_GLM = "glm"
LANE_KIMI = "kimi"


def _model_lane(model: Any) -> Optional[str]:
    name = str(model or "").strip().lower()
    if name.startswith("deepseek"):
        return LANE_DEEPSEEK
    if name.startswith("z-ai/") or name.startswith("glm"):
        return LANE_GLM
    return None


def _strings(value: Any) -> Iterator[str]:
    """Every string inside a JSON value, keys excluded."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


@dataclasses.dataclass
class Transcript:
    """What one transcript showed: the signals it raised and the lane it belongs to."""

    session_id: str
    lane: Optional[str] = None
    signals: set = dataclasses.field(default_factory=set)
    system_reported: bool = False


@dataclasses.dataclass
class Context:
    boundary: content_class.Boundary
    text_boundary: content_class.Boundary
    home: Path
    config_roots: Tuple[Path, ...]
    cwd_cache: Dict[str, bool] = dataclasses.field(default_factory=dict)


def _config_roots(home: Path) -> Tuple[Path, ...]:
    roots = [home / ".claude"]
    try:
        roots.extend(sorted(home.glob(".claude-*")))
    except OSError:
        pass
    return tuple(Path(os.path.realpath(r)) for r in roots)


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _is_config_path(raw: Any, ctx: Context) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    resolved = Path(os.path.realpath(os.path.expanduser(raw)))
    return any(_under(resolved, root) for root in ctx.config_roots)


def _cwd_restricted(cwd: Any, ctx: Context) -> bool:
    if not isinstance(cwd, str) or not cwd.strip():
        return False
    cached = ctx.cwd_cache.get(cwd)
    if cached is None:
        origin = content_class.classify_path(cwd, ctx.boundary)
        cached = origin.cls in content_class.DISPATCH_RESTRICTED
        ctx.cwd_cache[cwd] = cached
    return cached


def _names_restricted_path(text: str, ctx: Context) -> bool:
    return bool(text) and content_class.classify_text(text, ctx.text_boundary) is not None


def _record_access_text(record: Dict[str, Any]) -> str:
    """Tool-call input and user prompt text of one Claude record; results and attachments left out."""
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    kind = record.get("type")
    content = message.get("content")
    parts: List[str] = []
    if isinstance(content, str):
        if kind == "user":
            parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                parts.extend(_strings(block.get("input")))
            elif btype == "text" and kind == "user":
                parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _read_lines(path: Path) -> Iterator[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip():
                yield line


def scan_claude_transcript(path: Path, ctx: Context) -> Transcript:
    result = Transcript(session_id=path.stem)
    token = ctx.boundary.canary_token
    first_model_seen = False
    for line in _read_lines(path):
        if token and token in line:
            result.signals.add("canary")
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if not first_model_seen and record.get("type") == "assistant":
            message = record.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            if model:
                first_model_seen = True
                result.lane = _model_lane(model)
        if "restricted_cwd" not in result.signals and _cwd_restricted(record.get("cwd"), ctx):
            result.signals.add("restricted_cwd")
        attachment = record.get("attachment")
        if (isinstance(attachment, dict) and attachment.get("type") == "instructions"
                and "config_leak" not in result.signals):
            for entry in attachment.get("files") or []:
                if isinstance(entry, dict) and _is_config_path(entry.get("path"), ctx):
                    result.signals.add("config_leak")
                    break
        if "restricted_access" not in result.signals:
            if _names_restricted_path(_record_access_text(record), ctx):
                result.signals.add("restricted_access")
    return result


def scan_kimi_transcript(path: Path, ctx: Context) -> Transcript:
    result = Transcript(session_id=path.parent.name, lane=LANE_KIMI)
    token = ctx.boundary.canary_token
    for line in _read_lines(path):
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        role = record.get("role") if isinstance(record, dict) else None
        if role == "_system_prompt":
            if _names_restricted_path("\n".join(_strings(record.get("content"))), ctx):
                result.system_reported = True
            continue
        if token and token in line:
            result.signals.add("canary")
        if role in ("user", "assistant") and "restricted_access" not in result.signals:
            text = "\n".join(_strings(record.get("content")))
            calls = record.get("tool_calls")
            if calls:
                text += "\n" + "\n".join(_strings(calls))
            if _names_restricted_path(text, ctx):
                result.signals.add("restricted_access")
    return result


def _unique(paths: List[str]) -> List[Path]:
    seen: Dict[str, Path] = {}
    for raw in paths:
        seen.setdefault(os.path.realpath(raw), Path(raw))
    return [seen[key] for key in sorted(seen)]


def discover(home: Path, stores_root: Path, data_dir: Optional[Path]) -> Dict[str, List[Tuple[Path, str]]]:
    """Transcript files per root as ``(file, lane-dir-name)`` pairs."""
    claude = [(p, "") for p in _unique(glob.glob(str(home / ".claude" / "projects" / "*" / "*.jsonl")))]
    lane_files: Dict[str, Tuple[Path, str]] = {}
    patterns = [str(stores_root / "*" / "harness-config" / "*" / "projects" / "*" / "*.jsonl")]
    if data_dir is not None:
        patterns.append(str(data_dir / "harness-config" / "*" / "projects" / "*" / "*.jsonl"))
    for pattern in patterns:
        for raw in glob.glob(pattern):
            lane = Path(raw).parents[2].name
            lane_files.setdefault(os.path.realpath(raw), (Path(raw), lane))
    kimi = [(p, "") for p in _unique(glob.glob(str(home / ".kimi" / "sessions" / "*" / "*" / "context.jsonl")))]
    return {ROOT_CLAUDE: claude, ROOT_LANE: [lane_files[k] for k in sorted(lane_files)], ROOT_KIMI: kimi}


def _armed(boundary: content_class.Boundary) -> Tuple[bool, str]:
    """Whether the canary is armed, and a path-free reason when it is not."""
    token_file = boundary.canary_token_file
    if token_file is None:
        return False, "no_token_file_configured"
    token = boundary.canary_token
    if token is None:
        return False, "token_file_unreadable_or_invalid"
    try:
        if token_file.stat().st_mode & 0o077:
            return False, "token_file_mode_not_0600"
    except OSError:
        return False, "token_file_unreadable_or_invalid"
    if not boundary.canary_armed_in:
        return False, "no_armed_files_configured"
    for target in boundary.canary_armed_in:
        try:
            if token not in target.read_text(encoding="utf-8", errors="replace"):
                return False, "token_missing_in_armed_file"
        except OSError:
            return False, "armed_file_unreadable"
    return True, "armed"


def _empty_counts() -> Dict[str, Dict[str, int]]:
    return {signal: {} for signal in SIGNALS}


def _bump(counts: Dict[str, Dict[str, int]], signal: str, lane: str) -> None:
    counts[signal][lane] = counts[signal].get(lane, 0) + 1


def _first_run_since(now: float) -> float:
    day = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()


def read_previous_scanned_through(data_dir: Path) -> Optional[float]:
    try:
        beacon = json.loads((data_dir / "health" / f"{COMPONENT}.json").read_text(encoding="utf-8"))
        value = beacon["details"]["scanned_through"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def run_check(*, home: Path, stores_root: Path, data_dir: Path, state_dir: Path,
              boundary: content_class.Boundary, since: Optional[float] = None,
              now: Optional[float] = None, write: bool = True) -> Dict[str, Any]:
    """Scan every root, write the beacon and the ledger line, return the details."""
    now = time.time() if now is None else now
    if since is None:
        since = read_previous_scanned_through(data_dir)
    if since is None:
        since = _first_run_since(now)

    home = Path(os.path.realpath(home))
    ctx = Context(
        boundary=boundary,
        text_boundary=dataclasses.replace(boundary, canary_token=None),
        home=home,
        config_roots=_config_roots(home),
    )
    files = discover(home, Path(stores_root), Path(data_dir))
    armed, armed_reason = _armed(boundary)

    counts = _empty_counts()
    historic = _empty_counts()
    sessions: Dict[str, List[str]] = {signal: [] for signal in SIGNALS}
    scanned = {root: 0 for root in files}
    kimi_system_reported = 0
    unreadable = 0

    for root, entries in files.items():
        for path, lane_dir in entries:
            try:
                in_window = path.stat().st_mtime >= since
                if root == ROOT_KIMI:
                    found = scan_kimi_transcript(path, ctx)
                else:
                    found = scan_claude_transcript(path, ctx)
            except OSError:
                unreadable += 1
                continue
            if root == ROOT_LANE:
                found.lane = lane_dir
            elif root == ROOT_CLAUDE and found.lane is None:
                continue
            scanned[root] += 1
            if root == ROOT_KIMI:
                found.signals &= set(KIMI_SIGNALS)
                if found.system_reported and in_window:
                    kimi_system_reported += 1
            for signal in sorted(found.signals):
                _bump(counts if in_window else historic, signal, found.lane or "unknown")
                if in_window and len(sessions[signal]) < SESSION_ID_LIMIT:
                    sessions[signal].append(found.session_id)

    gating = sum(sum(per_lane.values()) for per_lane in counts.values())
    details: Dict[str, Any] = {
        "configured": boundary.configured,
        "unconfigured": not boundary.configured,
        "counts": counts,
        "gating_total": gating,
        "historic": historic,
        "historic_total": sum(sum(per_lane.values()) for per_lane in historic.values()),
        "scanned": scanned,
        "unreadable_files": unreadable,
        "kimi_system_prompt_reported": kimi_system_reported,
        "canary_armed": armed,
        "canary_armed_reason": armed_reason,
        "sessions": sessions,
        "since": int(since),
        "scanned_through": int(now),
    }
    status = "fail" if (gating > 0 or not boundary.configured) else "ok"

    if write:
        _write_outputs(data_dir, state_dir, status, details, now)
    return {"status": status, "details": details}


def _write_outputs(data_dir: Path, state_dir: Path, status: str, details: Dict[str, Any], now: float) -> None:
    from health_beacon import HealthBeacon
    HealthBeacon(Path(data_dir), "content_boundary",
                 expected_interval_seconds=BEACON_INTERVAL_SECONDS).heartbeat_strict(status, details)
    ledger = Path(state_dir) / LEDGER_NAME
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": int(now), "status": status, "details": details}, sort_keys=True) + "\n"
    fd = os.open(str(ledger), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _summary(result: Dict[str, Any]) -> str:
    d = result["details"]
    per_signal = {s: sum(v.values()) for s, v in d["counts"].items()}
    return (f"content_boundary status={result['status']} "
            f"configured={d['configured']} gating={d['gating_total']} {per_signal} "
            f"historic={d['historic_total']} scanned={d['scanned']} canary_armed={d['canary_armed']}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--home", help="home directory holding .claude and .kimi (default: $HOME)")
    parser.add_argument("--stores-root", help="directory of central stores (default: <home>/.vnx-data)")
    parser.add_argument("--data-dir", help="VNX data dir for the beacon (default: resolved)")
    parser.add_argument("--state-dir", help="VNX state dir for the ledger (default: resolved)")
    parser.add_argument("--boundary-file", help="content boundary file (default: the operator file)")
    parser.add_argument("--since", type=float, help="epoch seconds; default previous scanned_through")
    args = parser.parse_args(argv)

    from vnx_paths import resolve_paths
    paths = resolve_paths()
    home = Path(args.home).expanduser() if args.home else Path.home()
    data_dir = Path(args.data_dir or paths["VNX_DATA_DIR"])
    state_dir = Path(args.state_dir or paths["VNX_STATE_DIR"])
    stores_root = Path(args.stores_root).expanduser() if args.stores_root else home / ".vnx-data"
    boundary = content_class.load_boundary(args.boundary_file) if args.boundary_file \
        else content_class.load_boundary()

    result = run_check(home=home, stores_root=stores_root, data_dir=data_dir, state_dir=state_dir,
                       boundary=boundary, since=args.since)
    print(_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
