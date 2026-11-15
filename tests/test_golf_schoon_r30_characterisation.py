"""Characterisation of ``_check_reachability`` (golf schoon R30): verdicts, exact stderr lines, the probe
request (URL, auth header, timeout) and the order of the ledger side effects. The network is patched at
``urllib.request.urlopen``, the ledger collaborators on ``dispatch_cli`` (where they are looked up)."""
from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

import dispatch_cli  # noqa: E402

_P = "[dispatch_cli] "
_LL = "http://127.0.0.1:4141/v1/models"
_DS = "https://api.deepseek.com/v1/models"


def _plan(provider, lane="provider"):
    return NS(provider=NS(value=provider), lane=lane, dispatch_id="d-r30")


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body.encode("utf-8")


def _http_error(url, code, body="denied", read_fails=False):
    exc = urllib.error.HTTPError(url, code, f"msg {code}", {}, io.BytesIO(body.encode("utf-8")))
    if read_fails:
        def _boom():
            raise RuntimeError("no body")
        exc.read = _boom
    return exc


@pytest.fixture
def net(monkeypatch):
    for name in ("LITELLM_PROXY_BASE", "LITELLM_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    box = NS(outcome=None)

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_header("Authorization"), timeout))
        if isinstance(box.outcome, BaseException):
            raise box.outcome
        return box.outcome
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    box.calls = calls
    return box


def _run(plan, capsys, state_dir=None):
    verdict = dispatch_cli._check_reachability(plan, NS(dispatch_id="d-r30"), state_dir=state_dir)
    return verdict, capsys.readouterr()


def test_litellm_ok_counts_models(net, capsys):
    net.outcome = _Resp(200, '{"data": [1, 2, 3]}')
    v, out = _run(_plan("litellm:deepseek"), capsys)
    assert (v.block, v.reason) == (False, dispatch_cli.ReachabilityVerdict(block=False).reason)
    assert out.out == ""
    assert out.err == f"{_P}[reachability] litellm proxy OK: 3 models at {_LL}\n"
    assert net.calls == [(_LL, None, 5)]


def test_litellm_zero_models_warns(net, capsys, monkeypatch):
    monkeypatch.setenv("LITELLM_PROXY_BASE", "http://proxy:9/")
    monkeypatch.setenv("LITELLM_API_KEY", "  k1  ")
    net.outcome = _Resp(200, '{"other": 1}')
    v, out = _run(_plan("litellm"), capsys)
    assert v.block is False
    assert out.err == (f"{_P}[WARN] litellm proxy returned 0 models from http://proxy:9/v1/models — the lane "
                       "may be stale. Evidence: empty model list.\n")
    assert net.calls == [("http://proxy:9/v1/models", "Bearer k1", 5)]


def test_litellm_bad_json_warns_with_truncated_body(net, capsys):
    net.outcome = _Resp(200, "x" * 300)
    v, out = _run(_plan("litellm:zai"), capsys)
    assert v.block is False
    assert out.err == f"{_P}[WARN] litellm proxy response is not valid JSON from {_LL} — response: {'x' * 200}\n"


def test_litellm_non_200_status_is_silent(net, capsys):
    net.outcome = _Resp(204, "")
    v, out = _run(_plan("litellm:moonshot"), capsys)
    assert v.block is False
    assert out.err == ""


@pytest.mark.parametrize("code,key,hint", [
    (401, None, "LITELLM_API_KEY is not set — ANY dispatch on this lane will fail silently."),
    (403, "k", "The proxy API key (LITELLM_API_KEY) is missing or invalid — ANY dispatch on this lane will "
               "fail silently."),
])
def test_litellm_auth_rejected(net, capsys, monkeypatch, code, key, hint):
    if key:
        monkeypatch.setenv("LITELLM_API_KEY", key)
    net.outcome = _http_error(_LL, code, "y" * 250)
    v, out = _run(_plan("litellm:deepseek"), capsys)
    assert v.block is False
    assert out.err == (f"{_P}[WARN] litellm proxy AUTH REJECTED (HTTP {code}) at {_LL}. Evidence: {'y' * 200}. "
                       f"{hint}\n")


def test_litellm_other_http_error_body_unreadable_uses_str(net, capsys):
    exc = _http_error(_LL, 500, read_fails=True)
    net.outcome = exc
    v, out = _run(_plan("litellm:deepseek"), capsys)
    assert v.block is False
    assert out.err == f"{_P}[WARN] litellm proxy returned HTTP 500 from {_LL}. Evidence: {exc}\n"


@pytest.mark.parametrize("exc", [urllib.error.URLError("refused"), OSError("boom")])
def test_litellm_unreachable(net, capsys, exc):
    net.outcome = exc
    v, out = _run(_plan("litellm:deepseek"), capsys)
    assert v.block is False
    assert out.err == (f"{_P}[WARN] litellm proxy unreachable at {_LL} ({exc}). The lane may be down — "
                       "dispatch will likely fail.\n")


@pytest.mark.parametrize("provider", ["deepseek-harness", "deepseek_harness"])
def test_deepseek_ok(net, capsys, monkeypatch, provider):
    monkeypatch.setenv("DEEPSEEK_API_KEY", " dk ")
    net.outcome = _Resp(200, "")
    v, out = _run(_plan(provider), capsys)
    assert v.block is False
    assert out.err == f"{_P}[reachability] deepseek API OK: {_DS}\n"
    assert net.calls == [(_DS, "Bearer dk", 5)]


@pytest.mark.parametrize("code,key,hint", [
    (401, None, "DEEPSEEK_API_KEY is not set."),
    (403, None, "DEEPSEEK_API_KEY is not set."),
    (403, "k", "the API key is missing permissions."),
    (401, "k", "the API key is missing, invalid, or expired."),
])
def test_deepseek_auth_rejected(net, capsys, monkeypatch, code, key, hint):
    if key:
        monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    net.outcome = _http_error(_DS, code)
    v, out = _run(_plan("deepseek-harness"), capsys)
    assert v.block is False
    assert out.err == f"{_P}[WARN] deepseek API AUTH REJECTED (HTTP {code}) at {_DS}. {hint}\n"
    assert net.calls == [(_DS, "Bearer k" if key else None, 5)]


def test_deepseek_other_http_error(net, capsys):
    net.outcome = _http_error(_DS, 502)
    v, out = _run(_plan("deepseek-harness"), capsys)
    assert v.block is False
    assert out.err == f"{_P}[WARN] deepseek API returned HTTP 502 from {_DS}.\n"


@pytest.mark.parametrize("exc", [urllib.error.URLError("refused"), OSError("boom")])
def test_deepseek_unreachable(net, capsys, exc):
    net.outcome = exc
    v, out = _run(_plan("deepseek-harness"), capsys)
    assert v.block is False
    assert out.err == f"{_P}[WARN] deepseek API unreachable at {_DS} ({exc}).\n"


def _ledger(monkeypatch, receipt):
    order = []

    def recent(provider, state_dir):
        order.append(("recent", provider, state_dir))
        return receipt

    def record(provider, rec, *, dispatch_id, state_dir):
        order.append(("record", provider, rec, dispatch_id, state_dir))

    def fmt(provider, rec):
        order.append(("format", provider, rec))
        return "REASON"
    monkeypatch.setattr(dispatch_cli, "_recent_blocking_rejection", recent)
    monkeypatch.setattr(dispatch_cli, "_record_provider_lane_exhausted", record)
    monkeypatch.setattr(dispatch_cli, "_format_known_rejection", fmt)
    return order


def test_ledger_block_records_then_formats_and_never_probes(net, capsys, monkeypatch, tmp_path):
    order = _ledger(monkeypatch, {"r": 1})
    v, out = _run(_plan("litellm:deepseek"), capsys, state_dir=tmp_path)
    assert (v.block, v.reason) == (True, "REASON")
    assert order == [("recent", "litellm:deepseek", tmp_path),
                     ("record", "litellm:deepseek", {"r": 1}, "d-r30", tmp_path),
                     ("format", "litellm:deepseek", {"r": 1})]
    assert net.calls == [] and out.err == ""


def test_ledger_clear_falls_through_to_probe(net, capsys, monkeypatch, tmp_path):
    order = _ledger(monkeypatch, None)
    net.outcome = _Resp(200, '{"data": []}')
    v, out = _run(_plan("litellm:deepseek"), capsys, state_dir=tmp_path)
    assert v.block is False and order == [("recent", "litellm:deepseek", tmp_path)]
    assert len(net.calls) == 1 and "0 models" in out.err


def test_no_state_dir_skips_ledger(net, capsys, monkeypatch):
    order = _ledger(monkeypatch, {"r": 1})
    net.outcome = _Resp(200, '{"data": [1]}')
    v, _ = _run(_plan("deepseek-harness"), capsys)
    assert v.block is False and order == []


@pytest.mark.parametrize("plan", [_plan("kimi"), _plan("litellm:deepseek", lane="claude_headless"),
                                  _plan("claude", lane="claude_headless")])
def test_other_lanes_print_not_checkable(net, capsys, monkeypatch, tmp_path, plan):
    _ledger(monkeypatch, None)
    seen = []

    def note(lane, provider, state_dir):
        seen.append((lane, provider, state_dir))
        return "NOTE"
    monkeypatch.setattr(dispatch_cli, "_not_checkable_note", note)
    v, out = _run(plan, capsys, state_dir=tmp_path)
    assert v.block is False and out.err == "NOTE\n" and net.calls == []
    assert seen == [(plan.lane, plan.provider.value, tmp_path)]
