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

Roots (all injectable), each walked recursively so subagent transcripts and rotated contexts
count too: ``<home>/.claude/projects/**/*.jsonl`` (harness transcripts only),
``<stores>/*/harness-config/<lane>/projects/**/*.jsonl`` (every transcript) and
``<home>/.kimi/sessions/**/context*.jsonl``.

A line that is no JSON object, or a record the structured inspection cannot handle, is
inspected as raw text: canary and restricted path names over the whole line (tool results
included, nothing tells them apart any more), model, cwd and instructions paths from their
``"key": "value"`` pairs. It counts as ``malformed_records`` and does not on its own make
the run incomplete.

Only files with an mtime at or after ``since`` gate. Older hits are reported as ``historic``.
The checkpoint (``scanned_through``) moves only after a complete run: the boundary was
configured, every directory below the roots could be listed and every transcript could be
read. Any other run is incomplete: its status is ``fail``, ``incomplete_reasons`` and
``incomplete_where`` say what failed where, and it keeps the checkpoint it started from, so
the next run scans the same window again.

The beacon (``health/content_boundary.json``), the ledger line and the log carry counts,
lane names, session ids and error types, never a path, a token or transcript text.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import content_class  # noqa: E402

COMPONENT = "content_boundary"
LEDGER_NAME = "content_boundary_findings.ndjson"
BEACON_INTERVAL_SECONDS = 86400
SESSION_ID_LIMIT = 20
WHERE_LIMIT = 20

SIGNALS = ("config_leak", "restricted_cwd", "restricted_access", "canary")
KIMI_SIGNALS = ("canary", "restricted_access")
KIMI_SYSTEM_REPORT = "system_prompt_names_restricted_root"

ROOT_CLAUDE = "claude_projects"
ROOT_LANE = "harness_config"
ROOT_KIMI = "kimi"
ROOT_CONFIG = "config_dirs"

LANE_DEEPSEEK = "deepseek"
LANE_GLM = "glm"
LANE_KIMI = "kimi"
LANE_UNRECOGNIZED = "unrecognized_model"

TRAVERSAL_ERROR = "traversal_error"
UNREADABLE_FILE = "unreadable_file"

REASON_UNCONFIGURED = "boundary_unconfigured"
REASON_TRAVERSAL = "traversal_errors"
REASON_UNREADABLE = "unreadable_files"

SINCE_ARGUMENT = "argument"
SINCE_BEACON = "beacon"
SINCE_LEDGER = "ledger"
SINCE_FULL = "full"
SINCE_FIRST_RUN = "first_run"

KIMI_CONTEXT = re.compile(r"context(?:_\d+)?\.jsonl")
# ``"key": "value"`` in a line that is no JSON object; the value may be cut off at the end.
_RAW_PAIR = {key: re.compile(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)' % key)
             for key in ("model", "cwd", "path", "role")}


def _model_lane(model: Any) -> Optional[str]:
    """External lane of an assistant response; None for Claude, a synthetic entry or no model.

    Any other model is external too (``unrecognized_model``): a lane this check does not know
    yet must not make its transcripts pass as pure Claude ones.
    """
    name = str(model or "").strip().lower()
    if name.startswith("deepseek"):
        return LANE_DEEPSEEK
    if name.startswith("z-ai/") or name.startswith("glm"):
        return LANE_GLM
    if not name or name == "<synthetic>" or "claude" in name:
        return None
    return LANE_UNRECOGNIZED


def _strings(value: Any) -> Iterator[str]:
    """Every string inside a JSON value, keys included: a tool input can name a path as a key."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


@dataclasses.dataclass
class Transcript:
    """What one transcript showed: the signals it raised and the lanes it belongs to.

    A harness-config or kimi transcript has one lane. A ``.claude/projects`` transcript can
    hold responses of several models: it counts once for every external lane seen in any
    assistant entry, so an external-lane signal is never dropped because a Claude response
    came first. A pure Claude transcript has no lane and stays out.
    """

    session_id: str
    lane: Optional[str] = None
    lanes: set = dataclasses.field(default_factory=set)
    signals: set = dataclasses.field(default_factory=set)
    system_reported: bool = False
    malformed: int = 0


@dataclasses.dataclass
class Coverage:
    """What a run could not reach. ``where`` goes into the beacon, so it holds root names,
    lane-dir names, session ids and error types, never a path."""

    traversal_errors: int = 0
    unreadable_files: int = 0
    non_regular_files: int = 0
    where: List[Dict[str, str]] = dataclasses.field(default_factory=list)

    def failed(self, condition: str, root: str, exc: OSError, lane: str = "", session: str = "") -> None:
        if condition == TRAVERSAL_ERROR:
            self.traversal_errors += 1
        else:
            self.unreadable_files += 1
        if len(self.where) < WHERE_LIMIT:
            entry = {"condition": condition, "root": root, "error": type(exc).__name__}
            if lane:
                entry["lane"] = lane
            if session:
                entry["session"] = session
            self.where.append(entry)


@dataclasses.dataclass
class Context:
    boundary: content_class.Boundary
    text_boundary: content_class.Boundary
    home: Path
    config_roots: Tuple[Path, ...]
    cwd_cache: Dict[str, bool] = dataclasses.field(default_factory=dict)


def _config_roots(home: Path, cov: Coverage) -> Tuple[Path, ...]:
    """``~/.claude`` and every ``~/.claude-*``. A home that cannot be listed is a traversal
    error: instructions loaded from a config dir the run cannot see would not count."""
    roots = [home / ".claude"]
    try:
        with os.scandir(home) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as exc:
        cov.failed(TRAVERSAL_ERROR, ROOT_CONFIG, exc)
        names = []
    roots.extend(home / name for name in names if name.startswith(".claude-"))
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
    """Tool-call input and user prompt text of one Claude record; results and attachments left out.

    Prompt text in a shape other than a string or a list of typed blocks still counts.
    """
    message = record.get("message")
    kind = record.get("type")
    if not isinstance(message, dict):
        return "\n".join(_strings(message)) if kind == "user" else ""
    content = message.get("content")
    parts: List[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "tool_use":
                    parts.extend(_strings(block.get("input")))
                elif btype == "text" and kind == "user":
                    parts.append(str(block.get("text") or ""))
            elif kind == "user":
                parts.extend(_strings(block))
    elif kind == "user":
        parts.extend(_strings(content))
    return "\n".join(parts)


def _read_lines(path: Path) -> Iterator[str]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.strip():
                yield line


def _raw_values(line: str, key: str) -> List[str]:
    """String values of ``"key": "..."`` in a line that is no JSON object; the last may be cut off."""
    values = []
    for match in _RAW_PAIR[key].finditer(line):
        raw = match.group(1)
        try:
            values.append(json.loads('"' + raw + '"'))
        except ValueError:
            values.append(raw)
    return values


def _any_raw_value(line: str, key: str, check: Callable[[str], bool]) -> bool:
    for value in _raw_values(line, key):
        try:
            if check(value):
                return True
        except ValueError:
            # A value that is no valid path (an embedded NUL) names no directory. The line as
            # a whole is still checked for restricted path names as text.
            continue
    return False


def _inspect_structured(line: str, inspect: Callable[[Dict[str, Any]], None]) -> bool:
    """Run ``inspect`` on the record in ``line``. False when the line is no JSON object or the
    record broke the inspection (an embedded NUL in a path, nesting past the recursion limit):
    the caller then inspects the line as raw text."""
    try:
        record = json.loads(line)
        if not isinstance(record, dict):
            return False
        inspect(record)
    except (ValueError, RecursionError):
        return False
    return True


def _inspect_claude_record(record: Dict[str, Any], result: Transcript, ctx: Context) -> None:
    if record.get("type") == "assistant":
        message = record.get("message")
        lane = _model_lane(message.get("model")) if isinstance(message, dict) else None
        if lane:
            result.lanes.add(lane)
    if "restricted_cwd" not in result.signals and _cwd_restricted(record.get("cwd"), ctx):
        result.signals.add("restricted_cwd")
    attachment = record.get("attachment")
    if (isinstance(attachment, dict) and attachment.get("type") == "instructions"
            and "config_leak" not in result.signals):
        files = attachment.get("files")
        for entry in files if isinstance(files, list) else ():
            if _is_config_path(entry.get("path") if isinstance(entry, dict) else entry, ctx):
                result.signals.add("config_leak")
                break
    if "restricted_access" not in result.signals:
        if _names_restricted_path(_record_access_text(record), ctx):
            result.signals.add("restricted_access")


def _inspect_raw_claude(line: str, result: Transcript, ctx: Context) -> None:
    """A Claude line without a usable record: the whole line is checked for restricted path
    names; model, cwd and instructions paths come from their raw pairs, so a cut-off record
    still sets its lane and still raises restricted_cwd and config_leak."""
    for model in _raw_values(line, "model"):
        lane = _model_lane(model)
        if lane:
            result.lanes.add(lane)
    if _any_raw_value(line, "cwd", lambda cwd: _cwd_restricted(cwd, ctx)):
        result.signals.add("restricted_cwd")
    if '"instructions"' in line and _any_raw_value(line, "path", lambda p: _is_config_path(p, ctx)):
        result.signals.add("config_leak")
    if _names_restricted_path(line, ctx):
        result.signals.add("restricted_access")


def scan_claude_transcript(path: Path, ctx: Context) -> Transcript:
    result = Transcript(session_id=path.stem)
    token = ctx.boundary.canary_token
    for line in _read_lines(path):
        if token and token in line:
            result.signals.add("canary")
        if not _inspect_structured(line, lambda record: _inspect_claude_record(record, result, ctx)):
            result.malformed += 1
            _inspect_raw_claude(line, result, ctx)
    return result


def _inspect_kimi_record(record: Dict[str, Any], result: Transcript, ctx: Context) -> None:
    role = record.get("role")
    if role == "_system_prompt":
        # Paths in the system prompt come from kimi's own configuration: reported, not gated.
        if _names_restricted_path("\n".join(_strings(record.get("content"))), ctx):
            result.system_reported = True
        return
    # Tool results stay out (repo files name restricted paths). Every other role counts,
    # one this check does not know yet included.
    if role == "tool" or "restricted_access" in result.signals:
        return
    if _names_restricted_path("\n".join(_strings(record)), ctx):
        result.signals.add("restricted_access")


def _inspect_raw_kimi(line: str, result: Transcript, ctx: Context) -> None:
    """A kimi line without a usable record: a cut-off system prompt is still only reported,
    any other line that names a restricted path gates, tool results included."""
    if _names_restricted_path(line, ctx):
        if "_system_prompt" in _raw_values(line, "role"):
            result.system_reported = True
        else:
            result.signals.add("restricted_access")


def scan_kimi_transcript(path: Path, ctx: Context) -> Transcript:
    result = Transcript(session_id=path.parent.name, lane=LANE_KIMI)
    token = ctx.boundary.canary_token
    for line in _read_lines(path):
        if token and token in line:
            result.signals.add("canary")
        if not _inspect_structured(line, lambda record: _inspect_kimi_record(record, result, ctx)):
            result.malformed += 1
            _inspect_raw_kimi(line, result, ctx)
    return result


def _subdirs(path: Path, root: str, cov: Coverage) -> List[Path]:
    """Directories directly below ``path``. An absent ``path`` holds nothing and is no error;
    any other failure to list it, or to read the type of an entry, is a traversal error."""
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        cov.failed(TRAVERSAL_ERROR, root, exc)
        return []
    found = []
    for entry in entries:
        try:
            if entry.is_dir():
                found.append(Path(entry.path))
        except OSError as exc:
            cov.failed(TRAVERSAL_ERROR, root, exc)
    return sorted(found)


def _walk(top: Path, root: str, wanted: Callable[[str], bool], cov: Coverage, lane: str = "") -> List[Path]:
    """Every regular file below ``top`` whose name is wanted, directory links followed once.

    An absent ``top`` holds nothing yet and is no error. A directory below it that cannot be
    listed (also one that vanished after its parent was listed: it may have been renamed),
    or an entry whose type cannot be read, is a traversal error. A wanted name that is no
    regular file (a dangling link, a fifo) holds no content and is only counted.
    """
    found: List[Path] = []
    visited = set()
    pending = [top]
    while pending:
        current = pending.pop()
        real = os.path.realpath(current)
        if real in visited:
            continue
        visited.add(real)
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except (FileNotFoundError, NotADirectoryError) as exc:
            if current is not top:
                cov.failed(TRAVERSAL_ERROR, root, exc, lane=lane)
            continue
        except OSError as exc:
            cov.failed(TRAVERSAL_ERROR, root, exc, lane=lane)
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    pending.append(Path(entry.path))
                elif wanted(entry.name):
                    if entry.is_file():
                        found.append(Path(entry.path))
                    else:
                        cov.non_regular_files += 1
            except OSError as exc:
                cov.failed(TRAVERSAL_ERROR, root, exc, lane=lane)
    return found


def _unique(paths: List[Path]) -> List[Path]:
    seen: Dict[str, Path] = {}
    for path in paths:
        seen.setdefault(os.path.realpath(path), path)
    return [seen[key] for key in sorted(seen)]


def _is_jsonl(name: str) -> bool:
    return name.endswith(".jsonl")


def _is_kimi_context(name: str) -> bool:
    return KIMI_CONTEXT.fullmatch(name) is not None


def discover(home: Path, stores_root: Path, data_dir: Optional[Path],
             cov: Optional[Coverage] = None) -> Dict[str, List[Tuple[Path, str]]]:
    """Transcript files per root as ``(file, lane-dir-name)`` pairs; failures land in ``cov``."""
    cov = Coverage() if cov is None else cov
    claude = _unique(_walk(home / ".claude" / "projects", ROOT_CLAUDE, _is_jsonl, cov))
    configs = [store / "harness-config" for store in _subdirs(stores_root, ROOT_LANE, cov)]
    if data_dir is not None:
        configs.append(data_dir / "harness-config")
    lane_files: Dict[str, Tuple[Path, str]] = {}
    for config in configs:
        for lane_dir in _subdirs(config, ROOT_LANE, cov):
            for path in _walk(lane_dir / "projects", ROOT_LANE, _is_jsonl, cov, lane=lane_dir.name):
                lane_files.setdefault(os.path.realpath(path), (path, lane_dir.name))
    kimi = _unique(_walk(home / ".kimi" / "sessions", ROOT_KIMI, _is_kimi_context, cov))
    return {
        ROOT_CLAUDE: [(p, "") for p in claude],
        ROOT_LANE: [lane_files[k] for k in sorted(lane_files)],
        ROOT_KIMI: [(p, "") for p in kimi],
    }


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


def _first_run_since(now: float) -> float:
    day = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _read_checkpoint(data_dir: Path) -> Tuple[Optional[float], bool]:
    """The beacon's ``scanned_through``, and whether the beacon file is absent."""
    try:
        text = (data_dir / "health" / f"{COMPONENT}.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    try:
        return _number(json.loads(text)["details"]["scanned_through"]), False
    except (ValueError, KeyError, TypeError):
        return None, False


def read_previous_scanned_through(data_dir: Path) -> Optional[float]:
    return _read_checkpoint(data_dir)[0]


def _last_ledger_since(state_dir: Path, now: float) -> Optional[float]:
    """``since`` of the last ledger line that carries a usable one."""
    try:
        lines = (state_dir / LEDGER_NAME).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            value = _number(json.loads(line)["details"]["since"])
        except (ValueError, KeyError, TypeError):
            # A torn or foreign line: an earlier line gives an earlier start, never a later one.
            continue
        if value is not None and value <= now:
            return value
    return None


def resolve_since(data_dir: Path, state_dir: Path, now: float) -> Tuple[float, str]:
    """Where this run's window starts, and why. A lost checkpoint never shrinks the window.

    ``beacon``     the checkpoint the last run left in its beacon
    ``ledger``     the beacon has no usable checkpoint (unreadable, corrupt, absent, or after
                   ``now``): the ``since`` of the last ledger line. That run started from the
                   checkpoint before it, so this never lies after the lost one
    ``full``       the beacon file exists but neither it nor the ledger has a usable value:
                   everything is in the window
    ``first_run``  no beacon file and no ledger line: midnight UTC of today
    """
    checkpoint, beacon_absent = _read_checkpoint(data_dir)
    if checkpoint is not None and checkpoint <= now:
        return checkpoint, SINCE_BEACON
    ledger = _last_ledger_since(state_dir, now)
    if ledger is not None:
        return ledger, SINCE_LEDGER
    if beacon_absent:
        return _first_run_since(now), SINCE_FIRST_RUN
    return 0.0, SINCE_FULL


@dataclasses.dataclass
class Tally:
    """Counts of one run: ``counts`` for files in the window, ``historic`` for older ones."""

    scanned: Dict[str, int]
    counts: Dict[str, Dict[str, int]] = dataclasses.field(default_factory=_empty_counts)
    historic: Dict[str, Dict[str, int]] = dataclasses.field(default_factory=_empty_counts)
    sessions: Dict[str, List[str]] = dataclasses.field(default_factory=lambda: {s: [] for s in SIGNALS})
    kimi_system_reported: int = 0
    malformed_records: int = 0
    malformed_sessions: List[str] = dataclasses.field(default_factory=list)

    def add(self, root: str, found: Transcript, lanes: List[str], in_window: bool) -> None:
        self.scanned[root] += 1
        if found.malformed:
            self.malformed_records += found.malformed
            if len(self.malformed_sessions) < SESSION_ID_LIMIT:
                self.malformed_sessions.append(found.session_id)
        if root == ROOT_KIMI:
            found.signals &= set(KIMI_SIGNALS)
            if found.system_reported and in_window:
                self.kimi_system_reported += 1
        target = self.counts if in_window else self.historic
        for signal in sorted(found.signals):
            for lane in lanes:
                target[signal][lane] = target[signal].get(lane, 0) + 1
            if in_window and len(self.sessions[signal]) < SESSION_ID_LIMIT:
                self.sessions[signal].append(found.session_id)


def _lanes(root: str, found: Transcript, lane_dir: str) -> List[str]:
    if root == ROOT_LANE:
        return [lane_dir]
    if root == ROOT_CLAUDE:
        return sorted(found.lanes)
    return [found.lane or "unknown"]


def _scan(files: Dict[str, List[Tuple[Path, str]]], ctx: Context, since: float, cov: Coverage) -> Tally:
    tally = Tally(scanned={root: 0 for root in files})
    for root, entries in files.items():
        for path, lane_dir in entries:
            try:
                in_window = path.stat().st_mtime >= since
                scan = scan_kimi_transcript if root == ROOT_KIMI else scan_claude_transcript
                found = scan(path, ctx)
            except OSError as exc:
                session = path.parent.name if root == ROOT_KIMI else path.stem
                cov.failed(UNREADABLE_FILE, root, exc, lane=lane_dir, session=session)
                continue
            lanes = _lanes(root, found, lane_dir)
            # A .claude/projects transcript without any non-Claude response is pure Claude.
            if lanes:
                tally.add(root, found, lanes, in_window)
    return tally


def _incomplete_reasons(boundary: content_class.Boundary, cov: Coverage) -> List[str]:
    reasons = []
    if not boundary.configured:
        reasons.append(REASON_UNCONFIGURED)
    if cov.traversal_errors:
        reasons.append(REASON_TRAVERSAL)
    if cov.unreadable_files:
        reasons.append(REASON_UNREADABLE)
    return reasons


def _build_details(boundary: content_class.Boundary, tally: Tally, cov: Coverage,
                   since: float, since_source: str, now: float) -> Tuple[str, Dict[str, Any]]:
    armed, armed_reason = _armed(boundary)
    gating = sum(sum(per_lane.values()) for per_lane in tally.counts.values())
    reasons = _incomplete_reasons(boundary, cov)
    details: Dict[str, Any] = {
        "configured": boundary.configured,
        "unconfigured": not boundary.configured,
        "counts": tally.counts,
        "gating_total": gating,
        "historic": tally.historic,
        "historic_total": sum(sum(per_lane.values()) for per_lane in tally.historic.values()),
        "scanned": tally.scanned,
        "unreadable_files": cov.unreadable_files,
        "traversal_errors": cov.traversal_errors,
        "non_regular_files": cov.non_regular_files,
        "malformed_records": tally.malformed_records,
        "malformed_sessions": tally.malformed_sessions,
        "kimi_system_prompt_reported": tally.kimi_system_reported,
        "canary_armed": armed,
        "canary_armed_reason": armed_reason,
        "sessions": tally.sessions,
        "since": int(since),
        "since_source": since_source,
        "scan_incomplete": bool(reasons),
        "incomplete_reasons": reasons,
        "incomplete_where": cov.where,
        "scanned_through": int(now),
    }
    # An incomplete run (no boundary, a directory or transcript it could not reach) does not
    # move the checkpoint: the next run starts from the same ``since`` and sees that window again.
    if reasons:
        details["scanned_through"] = int(since)
        details["scanned_through_kept"] = "previous_since"
    status = "fail" if (gating > 0 or reasons) else "ok"
    return status, details


def run_check(*, home: Path, stores_root: Path, data_dir: Path, state_dir: Path,
              boundary: content_class.Boundary, since: Optional[float] = None,
              now: Optional[float] = None, write: bool = True) -> Dict[str, Any]:
    """Scan every root, write the beacon and the ledger line, return the details."""
    now = time.time() if now is None else now
    if since is None:
        since, since_source = resolve_since(Path(data_dir), Path(state_dir), now)
    else:
        since_source = SINCE_ARGUMENT

    home = Path(os.path.realpath(home))
    cov = Coverage()
    ctx = Context(
        boundary=boundary,
        text_boundary=dataclasses.replace(boundary, canary_token=None),
        home=home,
        config_roots=_config_roots(home, cov),
    )
    files = discover(home, Path(stores_root), Path(data_dir), cov)
    tally = _scan(files, ctx, since, cov)
    status, details = _build_details(boundary, tally, cov, since, since_source, now)

    if write:
        _write_outputs(data_dir, state_dir, status, details, now)
    return {"status": status, "details": details}


def _write_outputs(data_dir: Path, state_dir: Path, status: str, details: Dict[str, Any], now: float) -> None:
    from health_beacon import HealthBeacon
    ledger = Path(state_dir) / LEDGER_NAME
    ledger.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": int(now), "status": status, "details": details}, sort_keys=True) + "\n"
    fd = os.open(str(ledger), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    # The ledger is primary (ADR-005): the beacon, and the checkpoint it carries, only move
    # after the append succeeded. A failed append raises, the run exits non-zero and the old
    # checkpoint stays, so the next run re-scans the same window.
    HealthBeacon(Path(data_dir), "content_boundary",
                 expected_interval_seconds=BEACON_INTERVAL_SECONDS).heartbeat_strict(status, details)


def _summary(result: Dict[str, Any]) -> str:
    d = result["details"]
    per_signal = {s: sum(v.values()) for s, v in d["counts"].items()}
    return (f"content_boundary status={result['status']} "
            f"configured={d['configured']} gating={d['gating_total']} {per_signal} "
            f"incomplete={d['incomplete_reasons']} malformed={d['malformed_records']} "
            f"since_source={d['since_source']} historic={d['historic_total']} "
            f"scanned={d['scanned']} canary_armed={d['canary_armed']}")


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
