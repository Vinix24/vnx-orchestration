"""`vnx providers usage`: one independent row per provider, read-only, no secret in any output.

No real network and no real home: HOME, CODEX_HOME and the provider env file sit inside tmp_path,
and ``urlopen`` is patched wherever a probe would reach the network.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
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
    assert len(pu._kimi_state_dirs(home, env)) == 1
    assert pu.probe_kimi(home, NOW, env)["store"] == "gamma"


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


def _patch_urlopen(monkeypatch, outcome, calls=None):
    def fake(request, timeout=None):
        if calls is not None:
            calls.append((request.full_url, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)
    monkeypatch.setattr(pu.urllib.request, "urlopen", fake)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, f"msg {DEEPSEEK_KEY}", {}, io.BytesIO(b""))


DEEPSEEK_BODY = {"is_available": True, "balance_infos": [
    {"currency": "CNY", "total_balance": "12.50", "granted_balance": "2.50", "topped_up_balance": "10.00"},
    {"currency": "USD", "total_balance": "1.00", "granted_balance": "0.00", "topped_up_balance": "1.00"}]}


def test_deepseek_keeps_every_balance_row_and_bounds_the_call(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, DEEPSEEK_BODY, calls)
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
    _patch_urlopen(monkeypatch, outcome)
    row = pu.probe_deepseek({"DEEPSEEK_API_KEY": DEEPSEEK_KEY}, pu._http_get_json)
    assert row["available"] is False and expected in row["reason"]
    assert DEEPSEEK_KEY not in json.dumps(row)


def test_deepseek_without_a_key_makes_no_call(monkeypatch, home):
    calls = []
    _patch_urlopen(monkeypatch, DEEPSEEK_BODY, calls)
    row = pu.probe_deepseek({}, pu._http_get_json)
    assert row["available"] is False and "DEEPSEEK_API_KEY" in row["reason"] and calls == []


def test_openrouter_remaining_is_the_difference(monkeypatch):
    _patch_urlopen(monkeypatch, {"data": {"total_credits": 25.5, "total_usage": 10.25}})
    row = pu.probe_openrouter({"OPENROUTER_API_KEY": OPENROUTER_KEY}, pu._http_get_json)
    assert row == {"available": True, "total_credits": 25.5, "total_usage": 10.25, "remaining": 15.25}


def test_openrouter_key_found_only_in_the_env_file(monkeypatch, home, tmp_path):
    env_file = tmp_path / "provider.env"
    env_file.write_text(f"# note\nexport OPENROUTER_API_KEY='{OPENROUTER_KEY}'\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("VNX_PROVIDER_ENV_FILE", str(env_file))
    assert pu.resolve_openrouter_api_key({}) == OPENROUTER_KEY
    _patch_urlopen(monkeypatch, {"data": {"total_credits": 2, "total_usage": 1}})
    assert pu.probe_openrouter({}, pu._http_get_json)["remaining"] == 1


@pytest.mark.parametrize("outcome,expected", [
    (_http_error(401), "http 401"), (_http_error(402), "http 402"), (TimeoutError(), "timeout"),
    ({"unexpected": 1}, "onverwacht"), ({"data": {"total_credits": 1}}, "total_usage"),
])
def test_openrouter_failures(monkeypatch, outcome, expected):
    _patch_urlopen(monkeypatch, outcome)
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
    _patch_urlopen(monkeypatch, body)
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
    _patch_urlopen(monkeypatch, body)
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
    _patch_urlopen(monkeypatch, DEEPSEEK_BODY)
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
        _patch_urlopen(monkeypatch, outcome)
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
    _patch_urlopen(monkeypatch, TimeoutError(), calls)
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
