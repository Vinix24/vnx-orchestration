"""Governance and dispatch detail API handlers.

Extracted from api_intelligence.py (OI-1085 file-size split).
Covers: governance enforcement/overrides/audit/config, dispatch detail,
        dispatch events, dispatch result, SSE events stream.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

_UTC = timezone.utc

_SSE_KEEPALIVE_INTERVAL = 15  # seconds
_SSE_POLL_INTERVAL = 0.5       # seconds


def _sd():
    """Lazy accessor for serve_dashboard constants (avoids circular import)."""
    import serve_dashboard
    return serve_dashboard


# ---------------------------------------------------------------------------
# /api/governance/* — Governance audit trail endpoints
# ---------------------------------------------------------------------------

def _governance_scripts_lib() -> str:
    """Return scripts/lib path for lazy governance_audit import."""
    return str(Path(__file__).resolve().parent.parent / "scripts" / "lib")


def _import_governance_audit():
    """Lazy-import governance_audit from scripts/lib. Returns module or None."""
    lib = _governance_scripts_lib()
    if lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import governance_audit  # noqa: PLC0415
        return governance_audit
    except ImportError:
        return None


def _governance_get_enforcement(params: dict) -> dict:
    """GET /api/governance/enforcement — recent enforcement check results."""
    try:
        raw_limit = (params.get("limit") or [None])[0]
        limit = max(1, min(int(raw_limit), 500)) if raw_limit else 50
    except (ValueError, TypeError):
        limit = 50

    mod = _import_governance_audit()
    entries = mod.get_recent(limit) if mod else []

    checks = [
        {
            "timestamp": e.get("timestamp", ""),
            "check_name": e.get("check_name", ""),
            "level": e.get("level"),
            "passed": e.get("passed"),
            "message": e.get("message", ""),
        }
        for e in entries
        if e.get("event_type", "enforcement_check") == "enforcement_check"
    ]
    return {"checks": checks}


def _governance_get_overrides(params: dict) -> dict:
    """GET /api/governance/overrides — overrides in last 7 days."""
    try:
        raw_days = (params.get("days") or [None])[0]
        days = max(1, min(int(raw_days), 90)) if raw_days else 7
    except (ValueError, TypeError):
        days = 7

    mod = _import_governance_audit()
    entries = mod.get_overrides(days) if mod else []

    overrides = [
        {
            "timestamp": e.get("timestamp", ""),
            "check_name": e.get("check_name", ""),
            "override_reason": e.get("override", ""),
            "operator": e.get("operator") or "",
        }
        for e in entries
    ]
    return {"overrides": overrides}


def _governance_get_audit(params: dict) -> dict:
    """GET /api/governance/audit — full audit trail (paginated)."""
    try:
        raw_limit = (params.get("limit") or [None])[0]
        limit = max(1, min(int(raw_limit), 500)) if raw_limit else 50
    except (ValueError, TypeError):
        limit = 50

    try:
        raw_offset = (params.get("offset") or [None])[0]
        offset = max(0, int(raw_offset)) if raw_offset else 0
    except (ValueError, TypeError):
        offset = 0

    mod = _import_governance_audit()
    all_entries = mod.get_recent(limit=10000) if mod else []

    total = len(all_entries)
    page = all_entries[offset: offset + limit]
    return {"entries": page, "total": total}


def _governance_get_config() -> tuple[dict, int]:
    """GET /api/governance/config — current enforcement config and check levels."""
    lib = _governance_scripts_lib()
    if lib not in sys.path:
        sys.path.insert(0, lib)

    try:
        from governance_enforcer import GovernanceEnforcer, DEFAULT_CONFIG_PATH  # noqa: PLC0415
    except ImportError as exc:
        return {"error": f"governance_enforcer not available: {exc}"}, 500

    if not DEFAULT_CONFIG_PATH.exists():
        return {"error": f"governance_enforcement.yaml not found at {DEFAULT_CONFIG_PATH}"}, 404

    try:
        enforcer = GovernanceEnforcer()
        enforcer.load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:
        return {"error": f"failed to load governance config: {exc}"}, 500

    checks = [
        {
            "name": cfg.name,
            "level": cfg.level,
            "description": cfg.description,
        }
        for cfg in enforcer._checks.values()
    ]
    return {"mode": enforcer._mode, "checks": checks}, 200


# ---------------------------------------------------------------------------
# /api/dispatches/<id>  (GET) — detail
# ---------------------------------------------------------------------------

def _dispatch_get_detail(dispatch_id: str) -> tuple[dict, int]:
    """Return full detail for a single dispatch by ID."""
    if not dispatch_id or "/" in dispatch_id or "\\" in dispatch_id:
        return {"error": "invalid dispatch_id"}, 400

    sd = _sd()
    dispatches_dir: Path = sd.DISPATCHES_DIR

    for stage in ("completed", "active", "pending", "staging", "rejected"):
        stage_dir = dispatches_dir / stage
        if not stage_dir.exists():
            continue
        for path in stage_dir.glob("*.md"):
            if dispatch_id in path.stem or path.stem == dispatch_id:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                    metadata = _parse_dispatch_metadata(text)
                    return {
                        "dispatch_id": dispatch_id,
                        "stage": stage,
                        "file": path.name,
                        "instruction": text,
                        "metadata": metadata,
                    }, 200
                except OSError as exc:
                    return {"error": f"failed to read dispatch: {exc}"}, 500

    return {"error": f"dispatch not found: {dispatch_id}"}, 404


def _parse_dispatch_metadata(text: str) -> dict:
    """Extract metadata fields from dispatch markdown footer."""
    meta: dict = {}
    meta_section_re = re.compile(
        r"###\s+Dispatch Metadata\s*\n(.*?)(?:\n#{1,3}\s|\Z)", re.DOTALL
    )
    field_re = re.compile(r"^[-*]\s+\*{0,2}([A-Za-z][A-Za-z0-9 _-]*?)\*{0,2}\s*:\s*(.+)$",
                          re.MULTILINE)
    m = meta_section_re.search(text)
    section = m.group(1) if m else text
    for fm in field_re.finditer(section):
        key = fm.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        meta[key] = fm.group(2).strip()
    return meta


# ---------------------------------------------------------------------------
# /api/dispatches/<id>/events  (GET)
# ---------------------------------------------------------------------------

def _dispatch_get_events(dispatch_id: str) -> tuple[dict, int]:
    """Return formatted tool events from the dispatch archive NDJSON."""
    if not dispatch_id or "/" in dispatch_id or "\\" in dispatch_id:
        return {"error": "invalid dispatch_id"}, 400

    sd = _sd()
    events_dir = sd.VNX_DATA_DIR / "events" / "archive"
    archive_path = _find_archive(dispatch_id, events_dir)

    if archive_path is None:
        return {"error": f"event archive not found for dispatch: {dispatch_id}"}, 404

    try:
        raw_lines = archive_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"error": f"failed to read archive: {exc}"}, 500

    events_out: list[dict] = []
    first_ts: float | None = None
    last_phase: str | None = None

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        ev_type = ev.get("type", "")
        if ev_type not in ("tool_use", "tool_result"):
            continue

        ts_str = ev.get("timestamp", "")
        ts_offset: float | None = None
        if ts_str:
            try:
                ts_val = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if first_ts is None:
                    first_ts = ts_val
                ts_offset = round(ts_val - first_ts, 1)
            except Exception:
                pass

        if ev_type == "tool_use":
            data = ev.get("data", {})
            tool_name = data.get("name", "")
            inp = data.get("input", {})
            file_path = inp.get("file_path") or inp.get("path") or ""
            cmd = (inp.get("command") or "")[:120] if tool_name == "Bash" else ""

            phase = _classify_phase(tool_name, cmd)
            if phase != last_phase:
                events_out.append({"type": "phase_marker", "phase": phase})
                last_phase = phase

            events_out.append({
                "type": "tool_use",
                "timestamp_offset": ts_offset,
                "tool_name": tool_name,
                "file_path": file_path,
                "summary": cmd or file_path or tool_name,
            })

    return {"dispatch_id": dispatch_id, "events": events_out}, 200


def _classify_phase(tool_name: str, cmd: str) -> str:
    if tool_name in ("Read", "Grep", "Glob"):
        return "explore"
    if tool_name in ("Write", "Edit", "MultiEdit"):
        return "implement"
    if tool_name == "Bash":
        if "git commit" in cmd or "git push" in cmd:
            return "commit"
        if "pytest" in cmd:
            return "test"
        return "implement"
    return "other"


def _find_archive(dispatch_id: str, archive_dir: Path) -> Path | None:
    """Locate NDJSON archive for a dispatch_id."""
    if not archive_dir.exists():
        return None
    for path in archive_dir.rglob("*.ndjson"):
        if path.stem == dispatch_id or dispatch_id in path.stem:
            return path
    return None


# ---------------------------------------------------------------------------
# /api/dispatches/<id>/result  (GET)
# ---------------------------------------------------------------------------

def _dispatch_get_result(dispatch_id: str) -> tuple[dict, int]:
    """Return receipt entry and report content for a dispatch."""
    if not dispatch_id or "/" in dispatch_id or "\\" in dispatch_id:
        return {"error": "invalid dispatch_id"}, 400

    sd = _sd()
    result: dict = {"dispatch_id": dispatch_id}

    receipts_path: Path = sd.RECEIPTS_PATH
    receipt: dict | None = None
    if receipts_path.exists():
        try:
            for line in reversed(receipts_path.read_text(encoding="utf-8",
                                                          errors="replace").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("dispatch_id") == dispatch_id:
                    receipt = rec
                    break
        except OSError:
            pass
    result["receipt"] = receipt

    reports_dir: Path = sd.REPORTS_DIR
    report_text: str | None = None
    if reports_dir.exists():
        for path in reports_dir.glob("*.md"):
            if dispatch_id in path.stem:
                try:
                    report_text = path.read_text(encoding="utf-8", errors="replace")
                    result["report_file"] = path.name
                    break
                except OSError:
                    pass
    result["report"] = report_text

    if receipt is None and report_text is None:
        return {"error": f"no result found for dispatch: {dispatch_id}"}, 404

    return result, 200


# ---------------------------------------------------------------------------
# /api/events/stream  (GET, SSE)
# ---------------------------------------------------------------------------

def handle_events_stream(handler: "BaseHTTPRequestHandler", terminal: str) -> None:
    """Stream raw NDJSON events from .vnx-data/events/{terminal}.ndjson as SSE."""
    valid_terminals = frozenset({"T0", "T1", "T2", "T3"})
    if terminal not in valid_terminals:
        _send_sse_error(handler, f"invalid terminal: {terminal}")
        return

    sd = _sd()
    events_file = sd.VNX_DATA_DIR / "events" / f"{terminal}.ndjson"

    if not events_file.exists():
        _send_sse_error(handler, f"events file not found: {events_file.name}")
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    try:
        with open(events_file, "rb") as fh:
            fh.seek(0, 2)
            last_keepalive = time.monotonic()

            while True:
                line = fh.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            json.loads(line)
                            handler.wfile.write(f"data: {line.decode('utf-8', errors='replace')}\n\n".encode("utf-8"))
                            handler.wfile.flush()
                        except json.JSONDecodeError:
                            pass
                else:
                    now = time.monotonic()
                    if now - last_keepalive >= _SSE_KEEPALIVE_INTERVAL:
                        handler.wfile.write(b": keepalive\n\n")
                        handler.wfile.flush()
                        last_keepalive = now
                    time.sleep(_SSE_POLL_INTERVAL)

    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _send_sse_error(handler: "BaseHTTPRequestHandler", message: str) -> None:
    payload = json.dumps({"error": message}).encode("utf-8")
    handler.send_response(400)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)
