"""Nightly analyzer on DeepSeek Flash: model default, key loading, balance
pre-flight, key-auth hardening and the cache-hit cost tariff (D4e).

No test starts a network call or a claude process: subprocess.run and
urllib.request.urlopen are patched wherever the code under test would reach them.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "lib"))

_import_env = {
    "VNX_HOME": tempfile.mkdtemp(),
    "VNX_STATE_DIR": tempfile.mkdtemp(),
    "PROJECT_ROOT": tempfile.mkdtemp(),
}
with patch.dict(os.environ, _import_env):
    import conversation_analyzer.deep_analyzer as da_module
    from conversation_analyzer import (
        DeepAnalyzer, DigestGenerator, RunStats, SessionFlags, SessionMetrics,
    )
    from conversation_analyzer.deep_analyzer import LLMOutcome

import provider_dispatch  # noqa: E402  (scripts/lib on sys.path above)

LOADER = SCRIPTS / "lib" / "load_deepseek_key.sh"
NIGHTLY = SCRIPTS / "conversation_analyzer_nightly.sh"
SECRET = "sk-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def _fresh_preflight(monkeypatch):
    """The pre-flight verdict is cached per process; every test starts uncached."""
    monkeypatch.setattr(DeepAnalyzer, "_deepseek_preflight_cache", None, raising=False)


def _balance_body(total="5.00", currency="USD", available=True):
    return {
        "is_available": available,
        "balance_infos": [{
            "currency": currency, "total_balance": total,
            "granted_balance": "0.00", "topped_up_balance": total,
        }],
    }


def _ok_completion():
    return MagicMock(returncode=0, stdout=json.dumps({"result": '{"suggestions": []}'}), stderr="")


# ---------------------------------------------------------------------------
# (a) model default
# ---------------------------------------------------------------------------

def test_analyzer_defaults_to_deepseek_flash_without_env_var():
    env = {k: v for k, v in os.environ.items() if k != "VNX_ANALYZER_DEEPSEEK_MODEL"}
    env.update(_import_env)
    out = subprocess.run(
        [sys.executable, "-c",
         "from conversation_analyzer.models import DEEPSEEK_HARNESS_MODEL as M; print(M)"],
        cwd=SCRIPTS, env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.stdout.strip() == "deepseek-flash", out.stderr


def test_harness_call_names_the_model_and_is_key_auth_hardened():
    """The claude call carries --model deepseek-flash, MCP off, key-auth only."""
    hostile = {
        "DEEPSEEK_API_KEY": SECRET,
        "ANTHROPIC_API_KEY": "sk-ant-should-be-scrubbed",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-should-be-scrubbed",
    }
    with patch.dict(os.environ, hostile), \
         patch.object(DeepAnalyzer, "_fetch_deepseek_balance", return_value=_balance_body(), create=True), \
         patch.object(da_module, "DEEPSEEK_HARNESS_MODEL", "deepseek-flash"), \
         patch("subprocess.run", return_value=_ok_completion()) as run:
        outcome = DeepAnalyzer._try_deepseek_harness("prompt")

    assert outcome.status == "ok"
    run.assert_called_once()
    args = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert args[args.index("--model") + 1] == "deepseek-flash"
    assert "--strict-mcp-config" in args
    assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


# ---------------------------------------------------------------------------
# (b) nightly prelude: exactly one variable, value never logged
# ---------------------------------------------------------------------------

def _run_loader(env_file_text, tmp_path, extra_env=None):
    env_file = tmp_path / "provider-usage.env"
    env_file.write_text(env_file_text)
    marker = tmp_path / "sourced-marker"
    script = f'source "{LOADER}"; vnx_load_deepseek_key; echo "rc=$?" >&2; env'
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "VNX_PROVIDER_ENV_FILE": str(env_file)}
    env.update(extra_env or {})
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                         text=True, timeout=30)
    return out, marker


def test_prelude_exports_only_the_deepseek_key_from_the_env_file(tmp_path):
    marker = tmp_path / "sourced-marker"
    text = (
        "# provider keys\n"
        f"touch {marker}\n"
        "OPENROUTER_API_KEY=or-should-not-load\n"
        "export MOONSHOT_API_KEY=ms-should-not-load\n"
        f'export DEEPSEEK_API_KEY="{SECRET}"\n'
    )
    out, _ = _run_loader(text, tmp_path)

    env_lines = dict(l.split("=", 1) for l in out.stdout.splitlines() if "=" in l)
    assert env_lines.get("DEEPSEEK_API_KEY") == SECRET
    assert "OPENROUTER_API_KEY" not in env_lines
    assert "MOONSHOT_API_KEY" not in env_lines
    assert not marker.exists(), "the env file was executed, not parsed"
    assert "rc=0" in out.stderr


def test_prelude_prints_nothing_of_the_value(tmp_path):
    script = (f'source "{LOADER}"; vnx_load_deepseek_key')
    env_file = tmp_path / "e.env"
    env_file.write_text(f"DEEPSEEK_API_KEY={SECRET}\n")
    out = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "VNX_PROVIDER_ENV_FILE": str(env_file)},
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0
    assert SECRET not in out.stdout + out.stderr


def test_prelude_reports_missing_key_with_nonzero_status(tmp_path):
    out, _ = _run_loader("OPENROUTER_API_KEY=x\n", tmp_path)
    assert "rc=1" in out.stderr
    assert "DEEPSEEK_API_KEY" not in out.stdout


def test_prelude_reports_missing_file_with_nonzero_status(tmp_path):
    script = f'source "{LOADER}"; vnx_load_deepseek_key; echo "rc=$?"'
    out = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
             "VNX_PROVIDER_ENV_FILE": str(tmp_path / "absent.env")},
        capture_output=True, text=True, timeout=30,
    )
    assert "rc=1" in out.stdout


def test_nightly_script_uses_the_loader_and_never_echoes_the_key():
    text = NIGHTLY.read_text()
    assert 'lib/load_deepseek_key.sh' in text
    assert "vnx_load_deepseek_key" in text
    assert "$DEEPSEEK_API_KEY" not in text and "${DEEPSEEK_API_KEY" not in text
    check = subprocess.run(["bash", "-n", str(NIGHTLY)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    check = subprocess.run(["bash", "-n", str(LOADER)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


# ---------------------------------------------------------------------------
# (c) balance pre-flight
# ---------------------------------------------------------------------------

def test_balance_probe_hits_user_balance_and_never_v1_models():
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return io.BytesIO(json.dumps(_balance_body()).encode())

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        body = DeepAnalyzer._fetch_deepseek_balance(SECRET)

    assert seen == ["https://api.deepseek.com/user/balance"]
    assert not any("/v1/models" in u for u in seen)
    assert body["is_available"] is True


@pytest.mark.parametrize("body,status", [
    (_balance_body(total="0.10"), "balance_low"),
    (_balance_body(available=False), "balance_unavailable"),
    (_balance_body(currency="CNY"), "balance_unavailable"),
    ({"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "n/a"}]},
     "balance_unavailable"),
])
def test_balance_that_cannot_pay_skips_loudly_without_starting_claude(body, status, capsys):
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}), \
         patch.object(DeepAnalyzer, "_fetch_deepseek_balance", return_value=body, create=True), \
         patch("subprocess.run") as run:
        outcome = DeepAnalyzer._try_deepseek_harness("prompt")

    assert outcome.status == status
    assert outcome.attempted is True
    run.assert_not_called()
    logged = capsys.readouterr().out
    assert "[ERROR]" in logged
    assert SECRET not in logged


def test_balance_endpoint_failure_skips_loudly(capsys):
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}), \
         patch.object(DeepAnalyzer, "_fetch_deepseek_balance",
                      side_effect=OSError("connection refused"), create=True), \
         patch("subprocess.run") as run:
        outcome = DeepAnalyzer._try_deepseek_harness("prompt")

    assert outcome.status == "balance_unavailable"
    run.assert_not_called()
    assert "[ERROR]" in capsys.readouterr().out


def test_balance_is_checked_once_per_process():
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}), \
         patch.object(DeepAnalyzer, "_fetch_deepseek_balance",
                      return_value=_balance_body(), create=True) as fetch, \
         patch("subprocess.run", return_value=_ok_completion()):
        DeepAnalyzer._try_deepseek_harness("one")
        DeepAnalyzer._try_deepseek_harness("two")
    assert fetch.call_count == 1


def test_low_balance_is_a_failed_attempt_in_the_digest(tmp_path):
    analyzer = DeepAnalyzer()
    metrics = SessionMetrics(session_id="s1", total_output_tokens=5000, tool_calls_total=20)
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}), \
         patch.object(DeepAnalyzer, "_fetch_deepseek_balance",
                      return_value=_balance_body(total="0.01"), create=True), \
         patch.object(da_module, "LLM_STRATEGY", "deepseek-harness"), \
         patch("subprocess.run") as run:
        result = analyzer.analyze_session(jsonl, metrics, SessionFlags())

    assert result is None
    run.assert_not_called()
    assert analyzer.deep_attempts == 1
    assert analyzer.deep_failures == 1
    assert analyzer.deep_config_skips == 0
    assert analyzer.deep_failure_reasons == {"balance_low": 1}

    stats = RunStats(deep_attempts=analyzer.deep_attempts,
                     deep_failures=analyzer.deep_failures,
                     deep_failure_reasons=dict(analyzer.deep_failure_reasons))
    digest = DigestGenerator().generate("2026-09-28", stats, [], tmp_path / "none.db")
    assert "DEGRADED" in digest
    assert "balance_low" in digest


def test_missing_key_is_a_failed_attempt_in_the_digest(tmp_path):
    analyzer = DeepAnalyzer()
    metrics = SessionMetrics(session_id="s2", total_output_tokens=5000, tool_calls_total=20)
    jsonl = tmp_path / "s.jsonl"
    jsonl.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')

    with patch.dict(os.environ, {}, clear=True), \
         patch.object(da_module, "LLM_STRATEGY", "deepseek-harness"), \
         patch("subprocess.run") as run:
        result = analyzer.analyze_session(jsonl, metrics, SessionFlags())

    assert result is None
    run.assert_not_called()
    assert analyzer.deep_failure_reasons == {"key_missing": 1}
    stats = RunStats(deep_attempts=analyzer.deep_attempts,
                     deep_failures=analyzer.deep_failures,
                     deep_failure_reasons=dict(analyzer.deep_failure_reasons))
    assert "key_missing" in DigestGenerator().generate("2026-09-28", stats, [], tmp_path / "n.db")


def test_failed_harness_call_logs_at_error_level(capsys):
    failed = MagicMock(returncode=1, stdout="", stderr="402 Insufficient Balance")
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": SECRET}), \
         patch.object(DeepAnalyzer, "_fetch_deepseek_balance",
                      return_value=_balance_body(), create=True), \
         patch("subprocess.run", return_value=failed):
        outcome = DeepAnalyzer._try_deepseek_harness("prompt")
    assert outcome.status == "cli_failed"
    out = capsys.readouterr().out
    assert "[ERROR]" in out and "[WARNING]" not in out


# ---------------------------------------------------------------------------
# (d) cost tariff with cache hits
# ---------------------------------------------------------------------------

def test_registry_entry_carries_the_official_flash_tariff_and_source():
    raw = yaml.safe_load((SCRIPTS / "lib" / "providers" / "wave7_models.yaml").read_text())
    flash = raw["providers"]["deepseek_harness"]["models"]["deepseek-flash"]
    assert flash["cost_input_per_mtok"] == 0.30
    assert flash["cost_output_per_mtok"] == 1.20
    assert flash["cost_cache_read_per_mtok"] == 0.006
    assert flash["price_source"] == "https://api-docs.deepseek.com/quick_start/pricing"
    assert str(flash["price_checked_at"]) == "2026-09-28"
    # the gates keep their phased-out alias untouched
    legacy = raw["providers"]["deepseek_harness"]["models"]["deepseek-v4-flash"]
    assert (legacy["cost_input_per_mtok"], legacy["cost_output_per_mtok"]) == (0.14, 0.28)


def test_cost_with_cache_hits_uses_the_tariff():
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 1_000_000}
    cost = provider_dispatch._compute_cost("deepseek-harness", "deepseek-flash", usage)
    assert cost == pytest.approx(0.30 + 0.006 + 1.20)


def test_cost_of_a_cache_heavy_review_run():
    # 20k fresh input, 480k cache-read, 6k output
    usage = {"input": 20_000, "output": 6_000, "cache_hit": 480_000}
    cost = provider_dispatch._compute_cost("deepseek-harness", "deepseek-flash", usage)
    expected = 20_000 * 0.30 / 1e6 + 480_000 * 0.006 / 1e6 + 6_000 * 1.20 / 1e6
    assert cost == pytest.approx(expected)
    assert cost == pytest.approx(0.01608)


def test_models_without_a_cache_read_price_do_not_bill_cache_tokens():
    usage = {"input": 1_000_000, "output": 1_000_000, "cache_hit": 5_000_000}
    cost = provider_dispatch._compute_cost("deepseek-harness", "deepseek-v4-flash", usage)
    assert cost == pytest.approx(0.14 + 0.28)
