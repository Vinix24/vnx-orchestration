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
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class DualStackHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        with contextlib.suppress(Exception):
            # Accept IPv4-mapped connections on the IPv6 socket (platform-dependent).
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


VNX_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = VNX_DIR.parents[1]
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


def _json_response(handler: "DashboardHandler", status: HTTPStatus, payload_obj: dict) -> None:
    payload = json.dumps(payload_obj).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(payload)


def _unlock_terminal(terminal_id: str) -> dict:
    if terminal_id not in TERMINAL_TRACK_MAP:
        raise ValueError(f"Unknown terminal: {terminal_id}")

    now = datetime.now(timezone.utc).isoformat()
    terminal_shadow_script = SCRIPTS_DIR / "terminal_state_shadow.py"
    progress_update_script = SCRIPTS_DIR / "update_progress_state.py"

    shadow_result = subprocess.run(
        [
            "python3",
            str(terminal_shadow_script),
            "--terminal-id",
            terminal_id,
            "--status",
            "idle",
            "--clear-claim",
            "--last-activity",
            now,
            "--state-dir",
            str(CANONICAL_STATE_DIR),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    track = TERMINAL_TRACK_MAP[terminal_id]
    subprocess.run(
        [
            "python3",
            str(progress_update_script),
            "--track",
            track,
            "--status",
            "idle",
            "--dispatch-id",
            "",
            "--updated-by",
            "dashboard_unlock",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    shadow_record = {}
    stdout = shadow_result.stdout.strip()
    if stdout:
        with contextlib.suppress(Exception):
            shadow_record = json.loads(stdout)

    # Force-refresh dashboard_status.json so the next UI refresh sees the update
    dashboard_update_script = SCRIPTS_DIR / "update_dashboard_status.sh"
    if dashboard_update_script.exists():
        with contextlib.suppress(Exception):
            subprocess.run(
                ["bash", str(dashboard_update_script)],
                cwd=str(SCRIPTS_DIR),
                capture_output=True,
                timeout=5,
            )

    return {
        "status": "ok",
        "terminal": terminal_id,
        "track": track,
        "unlocked_at": now,
        "terminal_state": shadow_record,
    }


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

    def end_headers(self) -> None:
        """Add no-cache headers for JSON state files to ensure live updates."""
        if self.path and (".json" in self.path):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def do_POST(self) -> None:
        if self.path not in ("/api/restart-process", "/api/unlock-terminal"):
            self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON body")
            return

        if self.path == "/api/unlock-terminal":
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
