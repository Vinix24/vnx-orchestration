"""`vnx providers usage`: one independent row per provider, read-only, no secret in any output.

No real network and no real home: HOME, CODEX_HOME and the provider env file sit inside tmp_path,
and the opener is patched wherever a probe would reach the network. The redirect tests run against
real local HTTP servers instead, with the allowed origin widened through the ``origins`` parameter.
"""

from __future__ import annotations

import functools
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import provider_usage as pu  # noqa: E402
import providers_cli  # noqa: E402
import _gate_harness  # noqa: E402

CLI = REPO_ROOT / "scripts" / "providers_cli.py"
DEEPSEEK_KEY = "sk-fake-deepseek-0123456789abcdef"
OPENROUTER_KEY = "sk-or-fake-openrouter-0123456789abcdef"
NOW = 1_792_600_000.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("CODEX_HOME", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "no-state"))
    return home


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------

def _limits_line(used: float, timestamp: str = "2026-10-21T12:00:00Z", secondary=None) -> str:
    return json.dumps({
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 1793098146},
            "secondary": secondary, "plan_type": "prolite"}},
    })


def _rollout(home: Path, name: str, lines, mtime: float) -> Path:
    path = home / ".codex" / "sessions" / "2026" / "10" / "21" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_codex_newest_rollout_and_last_rate_limits_line_win(home):
    _rollout(home, "rollout-old.jsonl", [_limits_line(90.0)], NOW - 1000)
    _rollout(home, "rollout-new.jsonl", [_limits_line(10.0), "not json {", '{"payload": null}', _limits_line(46.0),
                                         "also {broken", json.dumps({"payload": {"type": "x"}})], NOW - 10)
    row = pu.probe_codex({}, home, NOW)
    assert row["available"] is True
    assert row["used_percent"] == 46.0
    assert row["window_minutes"] == 10080
    assert row["resets_at"] == "2026-10-27T10:49:06Z"
    assert row["plan_type"] == "prolite"
    assert "secondary" not in row
    assert row["source"] == "rollout-new.jsonl"


def test_codex_age_comes_from_the_line_timestamp_and_secondary_is_kept(home):
    stamp = _iso(NOW - 125)
    _rollout(home, "rollout-a.jsonl", [_limits_line(5.0, stamp, secondary={"used_percent": 1.0})], NOW)
    row = pu.probe_codex({}, home, NOW)
    assert row["age_seconds"] == 125
    assert row["secondary"] == {"used_percent": 1.0}


def test_codex_reads_the_tail_of_a_large_rollout(home):
    filler = [json.dumps({"payload": {"type": "agent_message", "text": "x" * 500}})] * 600
    _rollout(home, "rollout-big.jsonl", [_limits_line(33.0), *filler], NOW)
    assert pu.probe_codex({}, home, NOW)["used_percent"] == 33.0


def test_codex_respects_codex_home(home, tmp_path):
    other = tmp_path / "elsewhere"
    path = other / "sessions" / "rollout-x.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(_limits_line(12.5) + "\n", encoding="utf-8")
    assert pu.probe_codex({"CODEX_HOME": str(other)}, home, NOW)["used_percent"] == 12.5


def test_codex_unavailable_reasons(home):
    row = pu.probe_codex({}, home, NOW)
    assert row["available"] is False and "sessiemap" in row["reason"]
    (home / ".codex" / "sessions").mkdir(parents=True)
    assert "rollout" in pu.probe_codex({}, home, NOW)["reason"]
    _rollout(home, "rollout-empty.jsonl", [json.dumps({"payload": {"type": "x"}}), "junk"], NOW)
    row = pu.probe_codex({}, home, NOW)
    assert row["available"] is False and "rate_limits" in row["reason"]


def test_codex_primary_without_a_number_is_not_zero(home):
    line = json.dumps({"payload": {"rate_limits": {"primary": None, "plan_type": "x"}}})
    _rollout(home, "rollout-p.jsonl", [line], NOW)
    row = pu.probe_codex({}, home, NOW)
    assert row["available"] is False and "used_percent" not in row


# ---------------------------------------------------------------------------
# kimi
# ---------------------------------------------------------------------------

def _kimi_record(home: Path, project: str, *, state: str, reason: str, detail: str, measured_at: float) -> None:
    path = home / ".vnx-data" / project / "state" / "provider_reachability" / "kimi.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "provider": "kimi", "state": state, "reason": reason, "detail": detail,
        "source": "provider_dispatch:kimi", "measured_at": measured_at, "expires_at": measured_at + 3600,
    }), encoding="utf-8")


def test_kimi_newer_record_wins_and_has_no_percentage(home):
    _kimi_record(home, "alpha", state="unreachable", reason="quota_exhausted", detail="429 weekly", measured_at=NOW - 500)
    _kimi_record(home, "beta", state="reachable", reason="", detail="", measured_at=NOW - 50)
    row = pu.probe_kimi(home, NOW)
    assert row["store"] == "beta" and row["status"] == "bereikbaar"
    assert row["age_seconds"] == 50 and row["resets_at"] == "onbekend"
    assert not any("percent" in key for key in row)


def test_kimi_weekly_limit_booked_as_auth_401_reads_as_full(home):
    detail = ("kimi-cli: raw='Error code: 429 - {\\'error\\': {\\'message\\': \"You\\'ve reached your weekly "
              "(7-day) usage limit.\"}}'")
    _kimi_record(home, "alpha", state="unreachable", reason="auth_401", detail=detail, measured_at=NOW - 7200)
    row = pu.probe_kimi(home, NOW)
    assert row["status"] == pu.KIMI_FULL and row["reason"] == "auth_401"
    assert row["measured_at"].endswith("Z") and row["store"] == "alpha"


WEEKLY = ("kimi-cli: [quota_or_auth] provider=kimi reason=quota_or_auth raw='Error code: 403 - "
          "You\\'ve reached your weekly (7-day) usag'")
DAY = 86400.0


def _kimi_receipts(home: Path, project: str, events: list) -> None:
    """Kimi dispatch receipts as the converter writes them: (epoch, 'weekly' | 'success' | 'fivehour')."""
    path = home / ".vnx-data" / project / "state" / "t0_receipts.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for epoch, kind in events:
        receipt = {"receipt_kind": "dispatch", "event_type": "task_complete", "provider": "kimi",
                   "dispatch_id": f"d{int(epoch)}", "timestamp": _iso(epoch)}
        if kind == "weekly":
            receipt.update(status="failure", failure_reason=WEEKLY)
        elif kind == "fivehour":
            receipt.update(status="failure", failure_reason="kimi-cli: You've reached your 5-hour usage limit.")
        else:
            receipt.update(status="success")
        lines.append(json.dumps(receipt))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _full_kimi(home: Path) -> None:
    _kimi_record(home, "alpha", state="unreachable", reason="auth_401", detail=WEEKLY, measured_at=NOW - 60)


def test_kimi_reset_bound_is_first_weekly_failure_of_unbroken_streak_plus_seven_days(home):
    _full_kimi(home)
    first = NOW - 3 * DAY
    _kimi_receipts(home, "alpha", [(NOW - 9 * DAY, "success"), (first, "weekly"),
                                   (NOW - 2 * DAY, "fivehour"), (NOW - DAY, "weekly")])
    row = pu.probe_kimi(home, NOW)
    assert row["status"] == pu.KIMI_FULL
    assert row["resets_at_latest"] == _iso(first + 7 * DAY)
    assert row["resets_at"] == "onbekend"


def test_kimi_reset_bound_streak_is_broken_by_a_later_success(home):
    _full_kimi(home)
    _kimi_receipts(home, "alpha", [(NOW - 6 * DAY, "weekly"), (NOW - 4 * DAY, "success"),
                                   (NOW - 2 * DAY, "weekly"), (NOW - DAY, "weekly")])
    assert pu.probe_kimi(home, NOW)["resets_at_latest"] == _iso(NOW - 2 * DAY + 7 * DAY)


def test_kimi_reset_bound_success_in_another_store_breaks_the_streak(home):
    _full_kimi(home)
    _kimi_receipts(home, "alpha", [(NOW - 5 * DAY, "weekly"), (NOW - DAY, "weekly")])
    _kimi_receipts(home, "beta", [(NOW - 3 * DAY, "success")])
    assert pu.probe_kimi(home, NOW)["resets_at_latest"] == _iso(NOW - DAY + 7 * DAY)


def test_kimi_reset_bound_unknown_without_records(home):
    _full_kimi(home)
    row = pu.probe_kimi(home, NOW)
    assert row["resets_at_latest"] == "onbekend"
    assert pu.render_usage_text({"providers": {"kimi": row}}).endswith("reset onbekend")


def test_kimi_reset_bound_unknown_when_the_streak_is_older_than_a_window(home):
    _full_kimi(home)
    _kimi_receipts(home, "alpha", [(NOW - 8 * DAY, "weekly"), (NOW - DAY, "weekly")])
    assert pu.probe_kimi(home, NOW)["resets_at_latest"] == "onbekend"


def test_kimi_reset_bound_ignores_malformed_lines_and_review_gate_receipts(home):
    _full_kimi(home)
    _kimi_receipts(home, "alpha", [(NOW - 2 * DAY, "weekly")])
    path = home / ".vnx-data" / "alpha" / "state" / "t0_receipts.ndjson"
    gate = {"receipt_kind": "review_gate", "provider": "kimi", "status": "completed", "timestamp": _iso(NOW - DAY)}
    path.write_text(path.read_text() + "not json \"kimi\"\n" + json.dumps(gate) + "\n", encoding="utf-8")
    assert pu.probe_kimi(home, NOW)["resets_at_latest"] == _iso(NOW + 5 * DAY)


def test_kimi_reset_bound_text_says_uiterlijk(home):
    _full_kimi(home)
    _kimi_receipts(home, "alpha", [(NOW - DAY, "weekly")])
    text = pu.render_usage_text({"providers": {"kimi": pu.probe_kimi(home, NOW)}})
    assert f"reset uiterlijk {_iso(NOW + 6 * DAY)}" in text


def test_kimi_reachable_row_has_no_reset_bound(home):
    _kimi_record(home, "alpha", state="reachable", reason="", detail="", measured_at=NOW - 10)
    _kimi_receipts(home, "alpha", [(NOW - DAY, "weekly")])
    assert pu.probe_kimi(home, NOW)["resets_at_latest"] == "onbekend"


def test_kimi_real_auth_failure_is_unreachable_with_the_reason(home):
    _kimi_record(home, "alpha", state="unreachable", reason="auth_401", detail="401 invalid api key", measured_at=NOW)
    assert pu.probe_kimi(home, NOW)["status"] == "onbereikbaar: auth_401"


def test_kimi_no_record_is_geen_meting(home):
    row = pu.probe_kimi(home, NOW)
    assert row == {"available": False, "reason": "geen meting"}


def test_kimi_reads_the_state_dir_the_current_project_resolves_to(home, tmp_path, monkeypatch):
    local = tmp_path / "project" / "state"
    path = local / "provider_reachability" / "kimi.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "provider": "kimi", "state": "reachable", "reason": "", "detail": "",
        "source": "provider_dispatch:kimi", "measured_at": NOW - 10, "expires_at": NOW + 3600,
    }), encoding="utf-8")
    monkeypatch.setenv("VNX_STATE_DIR", str(local))
    row = pu.probe_kimi(home, NOW, {})
    assert row["available"] is True and row["status"] == "bereikbaar" and row["store"] == "project"


def test_kimi_reads_the_stores_under_vnx_data_home_once_per_real_path(home, tmp_path, monkeypatch):
    data_home = tmp_path / "datahome"
    path = data_home / "gamma" / "state" / "provider_reachability" / "kimi.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "provider": "kimi", "state": "reachable", "reason": "", "detail": "",
        "source": "provider_dispatch:kimi", "measured_at": NOW - 10, "expires_at": NOW + 3600,
    }), encoding="utf-8")
    (home / ".vnx-data").mkdir()
    (home / ".vnx-data" / "gamma").symlink_to(data_home / "gamma")
    monkeypatch.setenv("VNX_STATE_DIR", str(data_home / "gamma" / "state"))
    env = {"VNX_DATA_HOME": str(data_home)}
    assert len(pu._kimi_state_dirs(home, env)[0]) == 1
    assert pu.probe_kimi(home, NOW, env)["store"] == "gamma"


def test_kimi_unresolvable_state_dir_is_named_on_the_row_without_path_or_message(home, monkeypatch):
    import vnx_paths

    def unresolvable(*args, **kwargs):
        raise RuntimeError(f"no project below {home}/private/client-x")
    monkeypatch.setattr(vnx_paths, "resolve_state_dir", unresolvable)
    row = pu.probe_kimi(home, NOW, {})
    assert row == {"available": False, "reason": "geen meting (state dir niet op te lossen: RuntimeError)"}
    _kimi_record(home, "alpha", state="reachable", reason="", detail="", measured_at=NOW - 30)
    row = pu.probe_kimi(home, NOW, {})
    assert row["available"] is True and row["store"] == "alpha" and row["status"] == "bereikbaar"
    assert row["warning"] == "state dir niet op te lossen: RuntimeError"
    text = pu.render_usage_text({"providers": {"kimi": row}})
    assert text.endswith("reset onbekend; state dir niet op te lossen: RuntimeError")
    for output in (json.dumps(row), text):
        assert str(home) not in output and "client-x" not in output and "no project" not in output


# ---------------------------------------------------------------------------
# deepseek and openrouter
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, body):
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, limit=-1):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, outcome, calls):
        self._outcome, self._calls = outcome, calls

    def open(self, request, timeout=None):
        if self._calls is not None:
            self._calls.append((request.full_url, timeout))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _Response(self._outcome)


def _patch_opener(monkeypatch, outcome, calls=None):
    monkeypatch.setattr(pu.urllib.request, "build_opener", lambda *handlers: _Opener(outcome, calls))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, f"msg {DEEPSEEK_KEY}", {}, io.BytesIO(b""))


DEEPSEEK_BODY = {"is_available": True, "balance_infos": [
    {"currency": "CNY", "total_balance": "12.50", "granted_balance": "2.50", "topped_up_balance": "10.00"},
    {"currency": "USD", "total_balance": "1.00", "granted_balance": "0.00", "topped_up_balance": "1.00"}]}


def test_deepseek_keeps_every_balance_row_and_bounds_the_call(monkeypatch):
    calls = []
    _patch_opener(monkeypatch, DEEPSEEK_BODY, calls)
    row = pu.probe_deepseek({"DEEPSEEK_API_KEY": DEEPSEEK_KEY}, pu._http_get_json)
    assert row["available"] is True and row["is_available"] is True
    assert row["balances"][0] == {"currency": "CNY", "total": "12.50", "granted": "2.50", "topped_up": "10.00"}
    assert len(row["balances"]) == 2
    assert calls == [(pu.DEEPSEEK_BALANCE_URL, 5)]


@pytest.mark.parametrize("outcome,expected", [
    (_http_error(401), "http 401"),
    (_http_error(402), "http 402"),
    (TimeoutError("timed out"), "timeout"),
    (urllib.error.URLError(TimeoutError("t")), "timeout"),
    (urllib.error.URLError(ConnectionRefusedError(DEEPSEEK_KEY)), "netwerkfout: ConnectionRefusedError"),
    (b"<html>not json</html>", "geen JSON"),
])
def test_deepseek_failures_are_unavailable_with_a_reason(monkeypatch, outcome, expected):
    _patch_opener(monkeypatch, outcome)
    row = pu.probe_deepseek({"DEEPSEEK_API_KEY": DEEPSEEK_KEY}, pu._http_get_json)
    assert row["available"] is False and expected in row["reason"]
    assert DEEPSEEK_KEY not in json.dumps(row)


def test_deepseek_without_a_key_makes_no_call(monkeypatch, home):
    calls = []
    _patch_opener(monkeypatch, DEEPSEEK_BODY, calls)
    row = pu.probe_deepseek({}, pu._http_get_json)
    assert row["available"] is False and "DEEPSEEK_API_KEY" in row["reason"] and calls == []


def test_openrouter_remaining_is_the_difference(monkeypatch):
    _patch_opener(monkeypatch, {"data": {"total_credits": 25.5, "total_usage": 10.25}})
    row = pu.probe_openrouter({"OPENROUTER_API_KEY": OPENROUTER_KEY}, pu._http_get_json)
    assert row == {"available": True, "total_credits": 25.5, "total_usage": 10.25, "remaining": 15.25}


def test_openrouter_key_found_only_in_the_env_file(monkeypatch, home, tmp_path):
    env_file = tmp_path / "provider.env"
    env_file.write_text(f"# note\nexport OPENROUTER_API_KEY='{OPENROUTER_KEY}'\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(env_file))
    assert pu.resolve_openrouter_api_key({}) == OPENROUTER_KEY
    _patch_opener(monkeypatch, {"data": {"total_credits": 2, "total_usage": 1}})
    assert pu.probe_openrouter({}, pu._http_get_json)["remaining"] == 1


@pytest.mark.parametrize("outcome,expected", [
    (_http_error(401), "http 401"), (_http_error(402), "http 402"), (TimeoutError(), "timeout"),
    ({"unexpected": 1}, "onverwacht"), ({"data": {"total_credits": 1}}, "total_usage"),
])
def test_openrouter_failures(monkeypatch, outcome, expected):
    _patch_opener(monkeypatch, outcome)
    row = pu.probe_openrouter({"OPENROUTER_API_KEY": OPENROUTER_KEY}, pu._http_get_json)
    assert row["available"] is False and expected in row["reason"]


@pytest.mark.parametrize("body,field", [
    ({}, "is_available"),
    ({"error": "temporary failure"}, "is_available"),
    ({"is_available": "yes", "balance_infos": []}, "is_available"),
    ({"is_available": True}, "balance_infos"),
    ({"is_available": True, "balance_infos": "none"}, "balance_infos"),
    ({"is_available": True, "balance_infos": [{"currency": "USD"}]}, "total_balance"),
    ({"is_available": True, "balance_infos": ["USD"]}, "balance_infos"),
])
def test_deepseek_malformed_answer_is_unavailable_and_names_the_field(monkeypatch, body, field):
    _patch_opener(monkeypatch, body)
    row = pu.probe_deepseek({"DEEPSEEK_API_KEY": DEEPSEEK_KEY}, pu._http_get_json)
    assert row["available"] is False and field in row["reason"]


@pytest.mark.parametrize("body,field", [
    ({"data": {}}, "total_credits"),
    ({"data": {"error": "temporary failure"}}, "total_credits"),
    ({"data": {"total_credits": "5", "total_usage": 1}}, "total_credits"),
    ({"data": {"total_credits": 5}}, "total_usage"),
    ({"data": {"total_credits": 5, "total_usage": None}}, "total_usage"),
])
def test_openrouter_malformed_answer_is_unavailable_and_names_the_field(monkeypatch, body, field):
    _patch_opener(monkeypatch, body)
    row = pu.probe_openrouter({"OPENROUTER_API_KEY": OPENROUTER_KEY}, pu._http_get_json)
    assert row["available"] is False and field in row["reason"]


def test_provider_env_value_reads_one_named_variable_with_the_deepseek_rules(tmp_path):
    import load_deepseek_key as keys
    env_file = tmp_path / "p.env"
    env_file.write_text('A=1\nexport OPENROUTER_API_KEY="first"\r\nOPENROUTER_API_KEY=\'second\'\nDEEPSEEK_API_KEY=ds\n',
                        encoding="utf-8")
    assert keys.load_provider_env_value("OPENROUTER_API_KEY", env_file) == "second"
    assert keys.load_provider_env_value("MISSING", env_file) == ""
    assert keys.load_deepseek_api_key_from_file(env_file) == "ds"


def test_check_runs_from_a_vnx_home_holding_only_the_gate_harness_closure(tmp_path):
    scripts = tmp_path / "vnx_home" / "scripts"
    (scripts / "lib").mkdir(parents=True)
    for name in _gate_harness.PROVIDER_SWITCH_SCRIPTS:
        (scripts / name).write_bytes((REPO_ROOT / "scripts" / name).read_bytes())
    for name in _gate_harness.PROVIDER_SWITCH_LIB_MODULES:
        (scripts / "lib" / name).write_bytes((REPO_ROOT / "scripts" / "lib" / name).read_bytes())
    env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_") and k != "PYTHONPATH"}
    env["VNX_PROVIDER_SWITCH_FILE"] = str(tmp_path / "absent-switch.yaml")
    result = subprocess.run([sys.executable, str(scripts / "providers_cli.py"), "check", "codex_gate",
                             "--consumer", "vnx gate --only"],
                            env=env, capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr


def test_openrouter_without_a_key(home):
    assert pu.probe_openrouter({}, pu._http_get_json)["available"] is False


# ---------------------------------------------------------------------------
# a key never follows a redirect: real local servers, no patched opener
# ---------------------------------------------------------------------------

class _Recorder(http.server.BaseHTTPRequestHandler):
    """Records every request with its headers and answers with the server's planned response."""

    def do_GET(self):
        self.server.received.append({"path": self.path, "headers": dict(self.headers.items())})
        status, headers, body = self.server.answer
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """No request lines on stderr: the stderr assertions see only what the probe writes."""


@pytest.fixture
def serve(monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    started = []

    def start(status=200, headers=None, body=b"{}"):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
        server.received, server.answer = [], (status, headers or {}, body)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _origin(server) -> tuple:
    return ("http", f"127.0.0.1:{server.server_port}")


def _url(server, path: str = "/user/balance") -> str:
    return f"http://127.0.0.1:{server.server_port}{path}"


_REDIRECT_TARGETS = {
    "other-host": lambda target: f"http://localhost:{target.server_port}/collect?token=leak",
    "http-url": lambda target: f"http://127.0.0.1:{target.server_port}/collect?token=leak",
}


@pytest.mark.parametrize("target_kind", sorted(_REDIRECT_TARGETS))
@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_a_redirect_is_refused_and_its_target_never_receives_the_key(home, monkeypatch, capfd, serve, code,
                                                                     target_kind):
    target = serve(body=json.dumps(DEEPSEEK_BODY).encode())
    location = _REDIRECT_TARGETS[target_kind](target)
    source = serve(status=code, headers={"Location": location})
    monkeypatch.setattr(pu, "DEEPSEEK_BALANCE_URL", _url(source))
    http_get = functools.partial(pu._http_get_json, origins=frozenset({_origin(source)}))
    report = pu.build_usage(env={"DEEPSEEK_API_KEY": DEEPSEEK_KEY}, home=home, now=NOW, http_get=http_get)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(pu.render_usage_text(report))
    assert target.received == [], "the redirect target received the request, Authorization header included"
    assert [r["headers"].get("Authorization") for r in source.received] == ["Bearer " + DEEPSEEK_KEY]
    assert report["providers"]["deepseek"] == {"available": False, "reason": f"redirect refused ({code})"}
    captured = capfd.readouterr()
    for output in (captured.out, captured.err, json.dumps(report)):
        assert DEEPSEEK_KEY not in output and DEEPSEEK_KEY[:12] not in output
        assert "token=leak" not in output and "/collect" not in output


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_a_redirect_to_the_same_origin_is_refused_too(serve, code):
    source = serve(status=code, headers={"Location": "/again"})
    body, reason = pu._http_get_json(_url(source), DEEPSEEK_KEY, origins=frozenset({_origin(source)}))
    assert [r["path"] for r in source.received] == ["/user/balance"]
    assert (body, reason) == (None, f"redirect refused ({code})")


def test_a_plain_200_still_answers_through_the_same_opener(home, monkeypatch, serve):
    deepseek = serve(body=json.dumps(DEEPSEEK_BODY).encode())
    openrouter = serve(body=json.dumps({"data": {"total_credits": 4, "total_usage": 1.5}}).encode())
    monkeypatch.setattr(pu, "DEEPSEEK_BALANCE_URL", _url(deepseek))
    monkeypatch.setattr(pu, "OPENROUTER_CREDITS_URL", _url(openrouter, "/api/v1/credits"))
    http_get = functools.partial(pu._http_get_json, origins=frozenset({_origin(deepseek), _origin(openrouter)}))
    env = {"DEEPSEEK_API_KEY": DEEPSEEK_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY}
    providers = pu.build_usage(env=env, home=home, now=NOW, http_get=http_get)["providers"]
    assert providers["deepseek"]["available"] is True and len(providers["deepseek"]["balances"]) == 2
    assert providers["openrouter"] == {"available": True, "total_credits": 4, "total_usage": 1.5, "remaining": 2.5}
    assert [r["headers"]["Authorization"] for r in deepseek.received] == ["Bearer " + DEEPSEEK_KEY]
    assert [r["headers"]["Authorization"] for r in openrouter.received] == ["Bearer " + OPENROUTER_KEY]


def test_a_server_outside_the_allowed_origins_never_receives_the_key(serve):
    server = serve(body=json.dumps(DEEPSEEK_BODY).encode())
    body, reason = pu._http_get_json(_url(server), DEEPSEEK_KEY)
    assert server.received == []
    assert body is None and "geen sleutel verstuurd" in reason and DEEPSEEK_KEY not in reason


@pytest.mark.parametrize("url", [
    "http://api.deepseek.com/user/balance",
    "https://api.deepseek.com.evil.example/user/balance",
    "https://api.deepseek.com@evil.example/user/balance",
    "https://api.deepseek.com:8443/user/balance",
    "http://openrouter.ai/api/v1/credits",
    "https://openrouter.ai.evil.example/api/v1/credits",
])
def test_only_the_two_https_origins_are_sent_a_key(monkeypatch, url):
    calls = []
    _patch_opener(monkeypatch, DEEPSEEK_BODY, calls)
    body, reason = pu._http_get_json(url, DEEPSEEK_KEY)
    assert calls == [] and body is None and "geen sleutel verstuurd" in reason
    assert "evil" not in reason and DEEPSEEK_KEY not in reason
    assert pu.KEY_ORIGINS == frozenset({("https", "api.deepseek.com"), ("https", "openrouter.ai")})


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def test_one_source_raising_does_not_change_the_others(home, monkeypatch):
    _rollout(home, "rollout-a.jsonl", [_limits_line(7.0)], NOW)
    _kimi_record(home, "alpha", state="reachable", reason="", detail="", measured_at=NOW)
    env = {"DEEPSEEK_API_KEY": DEEPSEEK_KEY, "OPENROUTER_API_KEY": OPENROUTER_KEY}

    def get(url, key):
        return ({"data": {"total_credits": 3, "total_usage": 1}}, "") if "openrouter" in url else (DEEPSEEK_BODY, "")

    baseline = pu.build_usage(env=env, home=home, now=NOW, http_get=get)

    def boom(*args, **kwargs):
        raise RuntimeError(f"explodes with {DEEPSEEK_KEY}")
    monkeypatch.setattr(pu, "probe_codex", boom)
    broken = pu.build_usage(env=env, home=home, now=NOW, http_get=get)
    assert broken["providers"]["codex"]["available"] is False
    assert "RuntimeError" in broken["providers"]["codex"]["reason"]
    assert DEEPSEEK_KEY not in json.dumps(broken)
    for name in ("claude", "kimi", "deepseek", "openrouter"):
        assert broken["providers"][name] == baseline["providers"][name]


def test_json_matches_the_documented_shape(home, capsys, monkeypatch):
    _rollout(home, "rollout-a.jsonl", [_limits_line(7.0)], time.time())
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)
    _patch_opener(monkeypatch, DEEPSEEK_BODY)
    assert providers_cli.main(["usage", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "vnx.providers.usage/1"
    assert report["measured_at"].endswith("Z")
    assert list(report["providers"]) == ["claude", "codex", "kimi", "deepseek", "openrouter"]
    for row in report["providers"].values():
        assert isinstance(row["available"], bool)
        assert row["available"] or row["reason"]
    assert report["providers"]["claude"]["reason"] == "alleen in een Claude Code-sessie meetbaar"
    assert report["providers"]["codex"]["used_percent"] == 7.0
    assert report["providers"]["deepseek"]["available"] is True


def test_text_output_has_one_line_per_source(home, capsys):
    assert providers_cli.main(["usage"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[0] for line in lines] == ["claude", "codex", "kimi", "deepseek", "openrouter"]


def test_no_key_in_stdout_or_stderr_also_on_the_401_path(home, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    for outcome in (_http_error(401), {"is_available": True, "balance_infos": []}):
        _patch_opener(monkeypatch, outcome)
        for argv in (["usage"], ["usage", "--json"]):
            assert providers_cli.main(argv) == 0
            captured = capsys.readouterr()
            for secret in (DEEPSEEK_KEY, OPENROUTER_KEY):
                assert secret not in captured.out and secret not in captured.err
                assert secret[:12] not in captured.out and secret[:12] not in captured.err


def test_every_hung_endpoint_is_bounded_by_the_call_timeout(home, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", DEEPSEEK_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    calls = []
    _patch_opener(monkeypatch, TimeoutError(), calls)
    report = pu.build_usage()
    assert [t for _, t in calls] == [5, 5]
    assert all(not report["providers"][n]["available"] for n in ("deepseek", "openrouter"))
    assert pu.HTTP_TIMEOUT_SECONDS * 2 <= 15


def test_command_exits_zero_when_every_source_is_unavailable(home, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "CODEX_HOME")}
    env.update(HOME=str(home), VNX_PROVIDER_ENV_FILE=str(tmp_path / "absent.env"))
    result = subprocess.run([sys.executable, str(CLI), "usage", "--json"], env=env, capture_output=True, text=True,
                            cwd=tmp_path, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert all(row["available"] is False for row in report["providers"].values())


def test_usage_rejects_an_unknown_flag_with_a_usage_error(home, tmp_path):
    result = subprocess.run([sys.executable, str(CLI), "usage", "--nope"], capture_output=True, text=True,
                            cwd=tmp_path, timeout=30)
    assert result.returncode == 2
