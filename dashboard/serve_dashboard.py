#!/usr/bin/env python3
"""
Dual-stack HTTP server for the VNX dashboard.

Why:
- `python -m http.server` often binds to only IPv4 or only IPv6 depending on OS defaults.
- Many systems resolve `localhost` to `::1` first, which makes an IPv4-only server look "down".

This server binds to `::` and attempts to accept IPv4-mapped connections by disabling IPV6_V6ONLY.
It serves `.claude/vnx-system` so these paths work:
- `/` (redirects to `/dashboard/index.html` via `index.html`)
- `/dashboard/index.html`
- `/state/dashboard_status.json`
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import os
import socket
import subprocess
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# Make scripts/lib importable for conversation_read_model
_SCRIPTS_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, _SCRIPTS_LIB)

# Make sibling modules (api_token_stats, api_operator) importable when
# serve_dashboard.py is run directly (e.g. `python dashboard/serve_dashboard.py`).
_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, _DASHBOARD_DIR)


class DualStackHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        with contextlib.suppress(Exception):
            # Accept IPv4-mapped connections on the IPv6 socket (platform-dependent).
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


_SERVER_START_TIME = datetime.now(timezone.utc)

VNX_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = VNX_DIR
SCRIPTS_DIR = VNX_DIR / "scripts"
LOGS_DIR = VNX_DIR / "logs"
CANONICAL_STATE_DIR = Path(os.environ.get("VNX_STATE_DIR", str(PROJECT_ROOT / ".vnx-data" / "state")))
LEGACY_STATE_DIR = VNX_DIR / "state"

PROCESS_COMMANDS = {
    "smart_tap": ["bash", "smart_tap_v7_json_translator.sh"],
    "dispatcher": ["bash", "dispatcher_v8_minimal.sh"],
    "queue_watcher": ["bash", "queue_popup_watcher.sh"],
    "receipt_processor": ["bash", "receipt_processor_v4.sh"],
    "supervisor": ["bash", "vnx_supervisor_simple.sh"],
    "ack_dispatcher": ["bash", "dispatch_ack_watcher.sh"],
    "intelligence_daemon": ["python3", "intelligence_daemon.py"],
    "report_watcher": ["bash", "report_watcher.sh"],
    "receipt_notifier": ["bash", "receipt_notifier.sh"],
}

PROCESS_KILL_PATTERNS = {
    "smart_tap": "smart_tap_v7_json_translator",
    "dispatcher": "dispatcher_v8_minimal|dispatcher_v7_compilation",
    "queue_watcher": "queue_popup_watcher",
    "receipt_processor": "receipt_processor_v4",
    "report_watcher": "report_watcher",
    "receipt_notifier": "receipt_notifier",
    "supervisor": "vnx_supervisor_simple",
    "ack_dispatcher": "dispatch_ack_watcher|ack_dispatcher_v2",
    "intelligence_daemon": "intelligence_daemon.py",
}

TERMINAL_TRACK_MAP = {
    "T1": "A",
    "T2": "B",
    "T3": "C",
}

VALID_TERMINALS = frozenset({"T0", "T1", "T2", "T3"})

VNX_DATA_DIR = CANONICAL_STATE_DIR.parent  # .vnx-data/
DISPATCHES_DIR = VNX_DATA_DIR / "dispatches"
REPORTS_DIR = VNX_DATA_DIR / "unified_reports"

DISPATCH_DIR = Path(os.environ.get("VNX_DISPATCH_DIR", str(PROJECT_ROOT / ".vnx-data" / "dispatches")))
RECEIPTS_PATH = CANONICAL_STATE_DIR / "t0_receipts.ndjson"

DB_PATH = CANONICAL_STATE_DIR / "quality_intelligence.db"

GATE_CONFIG_PATH = VNX_DIR / "configs" / "governance_gates.yaml"

DISPATCH_STAGES = ("staging", "pending", "active", "completed", "rejected")

# ---------- Import handler modules ----------
# Deferred to avoid circular imports at module level — the submodules import
# constants from this file.  Importing here (after constants are defined)
# makes all handler functions available as serve_dashboard.X for backward
# compatibility with existing tests.

from api_token_stats import (  # noqa: E402
    _query_events,
    _query_token_stats,
    _query_token_sessions,
)

from api_operator import (  # noqa: E402
    _api_health,
    _jump_terminal,
    _operator_get_agents,
    _operator_get_gate_config,
    _operator_get_governance_digest,
    _operator_get_kanban,
    _operator_get_open_items,
    _operator_get_open_items_aggregate,
    _operator_get_projects,
    _operator_get_report_content,
    _operator_get_reports,
    _operator_get_session,
    _operator_get_system_health,
    _operator_get_terminal,
    _operator_get_terminals,
    _operator_post_action,
    _operator_post_gate_toggle,
    _query_conversations,
    _resume_conversation,
    _scan_dispatches,
    _unlock_terminal,
)

from api_agent_stream import (  # noqa: E402
    handle_agent_stream,
    handle_agent_stream_archive,
    handle_agent_stream_archive_list,
    handle_agent_stream_status,
)

from api_register_stream import (  # noqa: E402
    handle_register_stream,
    handle_register_stream_archive,
)

from api_intelligence import (  # noqa: E402
    _intelligence_get_patterns,
    _intelligence_get_injections,
    _intelligence_get_classifications,
    _intelligence_get_dispatch_outcomes,
    _intelligence_get_transcript,
    _intelligence_get_proposals,
    _intelligence_accept_proposal,
    _intelligence_reject_proposal,
    _intelligence_apply_proposals,
    _intelligence_get_confidence_trends,
    _intelligence_get_weekly_digest,
    _intelligence_generate_weekly_digest,
    _intelligence_get_learning_summary,
    _governance_get_enforcement,
    _governance_get_overrides,
    _governance_get_audit,
    _governance_get_config,
    _intelligence_get_behavioral_summary,
    _dispatch_get_detail,
    _dispatch_get_events,
    _dispatch_get_result,
    handle_events_stream,
)


def _json_response(handler: "DashboardHandler", status: HTTPStatus, payload_obj: dict) -> None:
    payload = json.dumps(payload_obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


class DashboardHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        """
        Serve `/state/*` from canonical state first, with legacy fallback.
        Keeps dashboard UI stable while state ownership moved to `.vnx-data/state`.
        """
        parsed_path = unquote(urlsplit(path).path)
        if parsed_path.startswith("/state/"):
            rel = parsed_path[len("/state/") :]
            rel_parts = [part for part in Path(rel).parts if part not in ("", ".", "..")]
            canonical_path = CANONICAL_STATE_DIR.joinpath(*rel_parts)
            if canonical_path.exists():
                return str(canonical_path)
            return str(LEGACY_STATE_DIR.joinpath(*rel_parts))
        return super().translate_path(path)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)

        if path == "/api/health":
            _json_response(self, HTTPStatus.OK, _api_health())
            return

        if path == "/api/events":
            try:
                result = _query_events(params)
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "events": []})
                return
            _json_response(self, HTTPStatus.OK, result)
            return

        if path == "/api/token-stats":
            result = _query_token_stats(params)
            _json_response(self, HTTPStatus.OK, {"data": result, "count": len(result)})
            return

        if path == "/api/token-stats/sessions":
            result = _query_token_sessions(params)
            _json_response(self, HTTPStatus.OK, {"data": result, "count": len(result)})
            return

        if path == "/api/conversations":
            try:
                result = _query_conversations(params)
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc), "sessions": []})
                return
            _json_response(self, HTTPStatus.OK, result)
            return

        if path == "/api/dispatches":
            try:
                result = _scan_dispatches()
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            _json_response(self, HTTPStatus.OK, result)
            return

        if path.startswith("/api/dispatches/"):
            rest = path[len("/api/dispatches/"):]
            # /api/dispatches/<id>/events
            if rest.endswith("/events"):
                dispatch_id = rest[:-len("/events")]
                payload, status_int = _dispatch_get_events(dispatch_id)
                _json_response(self, HTTPStatus(status_int), payload)
                return
            # /api/dispatches/<id>/result
            if rest.endswith("/result"):
                dispatch_id = rest[:-len("/result")]
                payload, status_int = _dispatch_get_result(dispatch_id)
                _json_response(self, HTTPStatus(status_int), payload)
                return
            # /api/dispatches/<id>
            dispatch_id = rest
            payload, status_int = _dispatch_get_detail(dispatch_id)
            _json_response(self, HTTPStatus(status_int), payload)
            return

        # Events SSE stream — /api/events/stream?terminal=T1
        if path == "/api/events/stream":
            terminal = params.get("terminal", [None])[0] or ""
            handle_events_stream(self, terminal)
            return

        # Operator Dashboard API
        if path == "/api/operator/projects":
            _json_response(self, HTTPStatus.OK, _operator_get_projects())
            return

        if path == "/api/operator/session":
            _json_response(self, HTTPStatus.OK, _operator_get_session(params))
            return

        if path == "/api/operator/terminals":
            _json_response(self, HTTPStatus.OK, _operator_get_terminals())
            return

        if path.startswith("/api/operator/terminal/"):
            tid = path[len("/api/operator/terminal/"):]
            _json_response(self, HTTPStatus.OK, _operator_get_terminal(tid))
            return

        if path == "/api/operator/open-items/aggregate":
            _json_response(self, HTTPStatus.OK, _operator_get_open_items_aggregate(params))
            return

        if path == "/api/operator/open-items":
            _json_response(self, HTTPStatus.OK, _operator_get_open_items(params))
            return

        if path == "/api/operator/kanban":
            _json_response(self, HTTPStatus.OK, _operator_get_kanban())
            return

        if path == "/api/operator/gate/config":
            _json_response(self, HTTPStatus.OK, _operator_get_gate_config(params))
            return

        if path == "/api/operator/governance-digest":
            _json_response(self, HTTPStatus.OK, _operator_get_governance_digest())
            return

        if path == "/api/operator/system-health":
            _json_response(self, HTTPStatus.OK, _operator_get_system_health())
            return

        if path == "/api/operator/reports":
            _json_response(self, HTTPStatus.OK, _operator_get_reports(params))
            return

        if path.startswith("/api/operator/reports/"):
            filename = path[len("/api/operator/reports/"):]
            result = _operator_get_report_content(filename)
            if result is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found", "filename": filename})
            else:
                _json_response(self, HTTPStatus.OK, result)
            return

        if path == "/api/operator/agents":
            _json_response(self, HTTPStatus.OK, _operator_get_agents())
            return

        # Register stream SSE endpoints — pass CANONICAL_STATE_DIR so the
        # handler uses the same state dir as all other /state/* APIs, honoring
        # VNX_STATE_DIR in non-default worktree / runtime configurations.
        _register_file = CANONICAL_STATE_DIR / "dispatch_register.ndjson"

        if path == "/api/register-stream/archive":
            handle_register_stream_archive(self, register_file=_register_file)
            return

        if path == "/api/register-stream":
            since_ts = params.get("since_ts", [None])[0]
            event_type_filter = params.get("event_type", [None])[0]
            handle_register_stream(self, since_ts, event_type_filter, register_file=_register_file)
            return

        # Agent stream SSE endpoints
        if path == "/api/agent-stream/status":
            handle_agent_stream_status(self)
            return

        if path.startswith("/api/agent-stream/") and path.endswith("/archives"):
            terminal = path[len("/api/agent-stream/"):-len("/archives")]
            handle_agent_stream_archive_list(self, terminal)
            return

        _ARCHIVE_PREFIX = "/api/agent-stream/"
        _ARCHIVE_INFIX = "/archive/"
        if path.startswith(_ARCHIVE_PREFIX) and _ARCHIVE_INFIX in path:
            rest = path[len(_ARCHIVE_PREFIX):]
            if _ARCHIVE_INFIX in rest:
                terminal, dispatch_id = rest.split(_ARCHIVE_INFIX, 1)
                handle_agent_stream_archive(self, terminal, dispatch_id)
                return

        if path.startswith("/api/agent-stream/"):
            terminal = path[len("/api/agent-stream/"):]
            since = params.get("since", [None])[0]
            handle_agent_stream(self, terminal, since)
            return

        # Intelligence API
        if path == "/api/intelligence/patterns":
            _json_response(self, HTTPStatus.OK, _intelligence_get_patterns(params))
            return

        if path == "/api/intelligence/injections":
            _json_response(self, HTTPStatus.OK, _intelligence_get_injections(params))
            return

        if path == "/api/intelligence/classifications":
            _json_response(self, HTTPStatus.OK, _intelligence_get_classifications(params))
            return

        if path == "/api/intelligence/dispatch-outcomes":
            _json_response(self, HTTPStatus.OK, _intelligence_get_dispatch_outcomes(params))
            return

        if path == "/api/intelligence/proposals":
            _json_response(self, HTTPStatus.OK, _intelligence_get_proposals(params))
            return

        if path == "/api/intelligence/confidence-trends":
            _json_response(self, HTTPStatus.OK, _intelligence_get_confidence_trends(params))
            return

        if path == "/api/intelligence/weekly-digest":
            payload, status_int = _intelligence_get_weekly_digest()
            _json_response(self, HTTPStatus(status_int), payload)
            return

        if path == "/api/intelligence/learning-summary":
            payload, status_int = _intelligence_get_learning_summary()
            _json_response(self, HTTPStatus(status_int), payload)
            return

        if path == "/api/intelligence/behavioral":
            payload, status_int = _intelligence_get_behavioral_summary()
            _json_response(self, HTTPStatus(status_int), payload)
            return

        # Governance API
        if path == "/api/governance/enforcement":
            _json_response(self, HTTPStatus.OK, _governance_get_enforcement(params))
            return

        if path == "/api/governance/overrides":
            _json_response(self, HTTPStatus.OK, _governance_get_overrides(params))
            return

        if path == "/api/governance/audit":
            _json_response(self, HTTPStatus.OK, _governance_get_audit(params))
            return

        if path == "/api/governance/config":
            payload, status_int = _governance_get_config()
            _json_response(self, HTTPStatus(status_int), payload)
            return

        if path.startswith("/api/conversations/") and path.endswith("/transcript"):
            session_id = path[len("/api/conversations/"):-len("/transcript")]
            payload, status_int = _intelligence_get_transcript(session_id)
            _json_response(self, HTTPStatus(status_int), payload)
            return

        # Return JSON 404 for unrecognised /api/* paths so callers get
        # structured errors instead of HTML from static file serving.
        if path.startswith("/api/"):
            _json_response(
                self,
                HTTPStatus.NOT_FOUND,
                {"error": "not_found", "path": path},
            )
            return

        # Fall through to static file serving
        super().do_GET()

    def end_headers(self) -> None:
        """Add no-cache headers for JSON state files to ensure live updates."""
        if self.path and (".json" in self.path):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_POST(self) -> None:
        parsed_path = unquote(urlsplit(self.path).path)

        # /api/jump/{terminal} — switch tmux focus to terminal
        if parsed_path.startswith("/api/jump/"):
            terminal_id = parsed_path[len("/api/jump/"):]
            if terminal_id not in VALID_TERMINALS:
                self.send_error(HTTPStatus.BAD_REQUEST, f"Unknown terminal: {terminal_id}")
                return
            try:
                response = _jump_terminal(terminal_id)
            except RuntimeError as exc:
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
                return
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or b"").decode(errors="replace").strip()
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"tmux error: {stderr or exc}")
                return
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Jump failed: {exc}")
                return
            _json_response(self, HTTPStatus.OK, response)
            return

        # /api/conversations/resume — validate and return resume command
        if parsed_path == "/api/conversations/resume":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            try:
                result = _resume_conversation(data)
            except Exception as exc:
                _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc), "session_id": data.get("session_id", "")})
                return
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT
            _json_response(self, status, result)
            return

        # Operator control actions
        _OPERATOR_ACTIONS = {
            "/api/operator/session/start": "session/start",
            "/api/operator/session/stop": "session/stop",
            "/api/operator/terminal/attach": "terminal/attach",
            "/api/operator/projections/refresh": "projections/refresh",
            "/api/operator/reconcile": "reconcile",
            "/api/operator/open-item/inspect": "open-item/inspect",
        }
        if parsed_path == "/api/operator/gate/toggle":
            length = int(self.headers.get("Content-Length", "0") or "0")
            body_bytes = self.rfile.read(length) if length else b"{}"
            try:
                body_data = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            result, status_int = _operator_post_gate_toggle(body_data)
            _json_response(self, HTTPStatus(status_int), result)
            return

        if parsed_path in _OPERATOR_ACTIONS:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body_bytes = self.rfile.read(length) if length else b"{}"
            try:
                body_data = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
                return
            result, status_int = _operator_post_action(_OPERATOR_ACTIONS[parsed_path], body_data)
            _json_response(self, HTTPStatus(status_int), result)
            return

        # Proposals: accept / reject / apply
        if parsed_path == "/api/intelligence/proposals/apply":
            result, status_int = _intelligence_apply_proposals()
            _json_response(self, HTTPStatus(status_int), result)
            return

        if parsed_path.startswith("/api/intelligence/proposals/") and parsed_path.endswith("/accept"):
            proposal_id = parsed_path[len("/api/intelligence/proposals/"):-len("/accept")]
            result, status_int = _intelligence_accept_proposal(proposal_id)
            _json_response(self, HTTPStatus(status_int), result)
            return

        if parsed_path.startswith("/api/intelligence/proposals/") and parsed_path.endswith("/reject"):
            proposal_id = parsed_path[len("/api/intelligence/proposals/"):-len("/reject")]
            length = int(self.headers.get("Content-Length", "0") or "0")
            body_bytes = self.rfile.read(length) if length else b"{}"
            try:
                body_data = json.loads(body_bytes.decode("utf-8"))
            except json.JSONDecodeError:
                body_data = {}
            result, status_int = _intelligence_reject_proposal(proposal_id, body_data)
            _json_response(self, HTTPStatus(status_int), result)
            return

        if parsed_path == "/api/intelligence/weekly-digest/generate":
            result, status_int = _intelligence_generate_weekly_digest()
            _json_response(self, HTTPStatus(status_int), result)
            return

        if parsed_path not in ("/api/restart-process", "/api/unlock-terminal"):
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return

        if parsed_path == "/api/unlock-terminal":
            terminal_id = data.get("terminal")
            if terminal_id not in TERMINAL_TRACK_MAP:
                self.send_error(HTTPStatus.BAD_REQUEST, f"Unknown terminal: {terminal_id}")
                return
            try:
                response = _unlock_terminal(terminal_id)
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unlock failed: {stderr or exc}")
                return
            except Exception as exc:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unlock failed: {exc}")
                return
            _json_response(self, HTTPStatus.OK, response)
            return

        process_name = data.get("process")
        if process_name not in PROCESS_COMMANDS:
            self.send_error(HTTPStatus.BAD_REQUEST, f"Unknown process: {process_name}")
            return

        kill_pattern = PROCESS_KILL_PATTERNS.get(process_name, process_name)
        subprocess.run(["pkill", "-f", kill_pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / f"{process_name}.log"
        log_handle = open(log_path, "ab", buffering=0)

        try:
            subprocess.Popen(
                PROCESS_COMMANDS[process_name],
                cwd=str(SCRIPTS_DIR),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        except Exception as exc:
            log_handle.close()
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Failed to start: {exc}")
            return

        response = {"status": "ok", "process": process_name}
        _json_response(self, HTTPStatus.OK, response)


def main() -> None:
    port = int(os.environ.get("PORT", "4173"))

    # Serve from `.claude/vnx-system` regardless of where the script is launched from.
    service_dir = Path(__file__).resolve().parents[1]
    handler = partial(DashboardHandler, directory=str(service_dir))

    server = DualStackHTTPServer(("::", port), handler)
    print(
        f"Serving dashboard from {service_dir} on http://localhost:{port} (dashboard at /dashboard/index.html)",
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
