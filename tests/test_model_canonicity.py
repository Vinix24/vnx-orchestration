"""dispatch-20260814k-a-model-canonicity — no ``model: unknown`` default (OI-1184).

Pins the write-time behaviour: an undeterminable model is left EMPTY (so the
existing fail-closed ``_validate_model_present`` refuses the dispatch receipt
loudly) instead of being silently defaulted to the fake literal ``"unknown"``.

The four write sites under test:
1. ``session_resolver._resolve_model_provider`` — default ``""``, not ``"unknown"``.
2. ``enrichment._enrich_session_metadata`` — never stamps a sentinel model.
3. ``tmux_interactive_dispatch._build_completion_protocol`` — receipt JSON default ``""``.
4. ``worker_heartbeat`` failure reports — ``**Model**: `` default ``""``, not ``"unknown"``.

Each test fails against the pre-fix code and passes against the fix.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import append_receipt_internals.session_resolver as sr  # noqa: E402
from append_receipt_internals.common import _facade_modules, register_facade  # noqa: E402
from append_receipt_internals import enrichment as en  # noqa: E402


# ---------------------------------------------------------------------------
# 1. session_resolver: no "unknown" default
# ---------------------------------------------------------------------------

class TestResolverNoUnknownDefault:
    def test_claude_terminal_defaults_to_empty_model(self, tmp_path):
        """A plain T1 terminal with no panes.json resolves model="" — the
        caller (enrichment + fail-closed validation) decides how to treat an
        undeterminable model, it is never silently named "unknown"."""
        result = sr._resolve_model_provider("T1", tmp_path)
        assert result["model"] == "", f"expected empty model, got {result['model']!r}"
        assert result["provider"] == "claude_code"

    @pytest.mark.parametrize("terminal", ["T0", "T1", "T2", "T3", "T-MANAGER"])
    def test_all_claude_terminals_default_to_empty_model(self, tmp_path, terminal):
        result = sr._resolve_model_provider(terminal, tmp_path)
        assert result["model"] == "", f"{terminal}: expected empty model, got {result['model']!r}"

    def test_gemini_heuristic_still_resolves_real_model(self, tmp_path):
        """The terminal-name heuristic keeps supplying a real model — the
        sentinel->heuristic replacement must not regress."""
        assert sr._resolve_model_provider("GEMINI-T2", tmp_path)["model"] == "gemini-pro"

    def test_codex_heuristic_still_resolves_real_model(self, tmp_path):
        assert sr._resolve_model_provider("CODEX-T3", tmp_path)["model"] == "gpt-5.2-codex"

    def test_panes_json_real_model_is_kept(self, tmp_path):
        (tmp_path / "panes.json").write_text(
            json.dumps({"T1": {"model": "sonnet-5", "provider": "claude_code"}}),
            encoding="utf-8",
        )
        result = sr._resolve_model_provider("T1", tmp_path)
        assert result["model"] == "sonnet-5"

    def test_panes_json_sentinel_model_is_blanked_for_heuristic(self, tmp_path):
        """A stale panes.json carrying model:"unknown" with no provider is
        treated as undetermined by the terminal-name heuristic (gemini)."""
        (tmp_path / "panes.json").write_text(
            json.dumps({"GEMINI-T2": {"model": "unknown"}}),
            encoding="utf-8",
        )
        assert sr._resolve_model_provider("GEMINI-T2", tmp_path)["model"] == "gemini-pro"


# ---------------------------------------------------------------------------
# 2. enrichment: never stamps a sentinel model
# ---------------------------------------------------------------------------

class TestEnrichmentNoUnknownDefault:
    @pytest.fixture(autouse=True)
    def register_sr(self):
        register_facade(sr)
        yield
        if sr in _facade_modules:
            _facade_modules.remove(sr)

    def _enrich(self, tmp_path, monkeypatch, receipt):
        # Keep session-id resolution off the real home directory: the model
        # assertion below does not depend on it, and scanning ~/.claude is
        # neither deterministic nor cheap in a unit test.
        monkeypatch.setattr(sr, "_resolve_session_id", lambda r, state_dir=None: "test-session")
        monkeypatch.setattr(sr, "_extract_session_token_usage", lambda sid, terminal: None)
        enriched = dict(receipt)
        en._enrich_session_metadata(enriched, tmp_path)
        return enriched

    def test_undeterminable_model_is_not_stamped(self, tmp_path, monkeypatch):
        """No panes.json -> no "model" key lands on the receipt. The pre-fix
        code stamped model="unknown" here."""
        enriched = self._enrich(tmp_path, monkeypatch, {"terminal": "T1"})
        assert "model" not in enriched, (
            f"an undeterminable model must stay absent, got {enriched.get('model')!r}"
        )
        assert enriched.get("provider") == "claude_code"

    def test_real_model_is_stamped(self, tmp_path, monkeypatch):
        (tmp_path / "panes.json").write_text(
            json.dumps({"T1": {"model": "sonnet-5", "provider": "claude_code"}}),
            encoding="utf-8",
        )
        enriched = self._enrich(tmp_path, monkeypatch, {"terminal": "T1"})
        assert enriched["model"] == "sonnet-5"

    def test_caller_supplied_model_is_never_overwritten(self, tmp_path, monkeypatch):
        """A caller-supplied real model wins over resolution (setdefault semantics)."""
        (tmp_path / "panes.json").write_text(
            json.dumps({"T1": {"model": "sonnet-5", "provider": "claude_code"}}),
            encoding="utf-8",
        )
        enriched = self._enrich(tmp_path, monkeypatch, {"terminal": "T1", "model": "opus-5"})
        assert enriched["model"] == "opus-5"


# ---------------------------------------------------------------------------
# 3. completion protocol: no "unknown" default in the worker receipt JSON
# ---------------------------------------------------------------------------

class TestCompletionProtocolNoUnknownDefault:
    @staticmethod
    def _protocol_model(protocol: str) -> str:
        m = re.search(r'--receipt\s+"((?:[^"\\]|\\.)*)"', protocol)
        assert m, "no --receipt argument found in protocol"
        raw = m.group(1).replace('\\"', '"').replace("$_VNX_TS", "2099-01-01T00:00:00Z")
        return json.loads(raw)["model"]

    def _lane(self, tmp_path):
        from tmux_interactive_dispatch import TmuxInteractiveDispatch
        return TmuxInteractiveDispatch(
            tmp_path,
            receipts_file=tmp_path / "t0_receipts.ndjson",
            project_root=tmp_path,
        )

    def test_default_model_is_empty_not_unknown(self, tmp_path):
        protocol = self._lane(tmp_path)._build_completion_protocol("disp-oi1184", "T1")
        assert self._protocol_model(protocol) == ""

    def test_explicit_model_still_baked_in(self, tmp_path):
        protocol = self._lane(tmp_path)._build_completion_protocol(
            "disp-oi1184", "T1", model="sonnet-5"
        )
        assert self._protocol_model(protocol) == "sonnet-5"


# ---------------------------------------------------------------------------
# 4. worker_heartbeat failure reports: no "unknown" model default
# ---------------------------------------------------------------------------

class TestHeartbeatReportNoUnknownDefault:
    def _verdict(self):
        from worker_heartbeat import SilenceVerdict
        return SilenceVerdict(is_silent=True, silence_seconds=1800.0, threshold_seconds=1800.0)

    def test_heartbeat_failure_report_default_model_is_empty(self):
        from worker_heartbeat import build_heartbeat_failure_report
        report = build_heartbeat_failure_report(
            "disp-oi1184", self._verdict(), terminal_id="T1"
        )
        assert "**Model**: \n" in report, report
        assert "**Model**: unknown" not in report, report

    def test_process_gone_report_default_model_is_empty(self):
        from worker_heartbeat import build_process_gone_failure_report
        report = build_process_gone_failure_report(
            "disp-oi1184", liveness_reason="tmux_session_gone", terminal_id="T1"
        )
        assert "**Model**: \n" in report, report
        assert "**Model**: unknown" not in report, report

    def test_explicit_model_is_baked_in(self):
        from worker_heartbeat import build_heartbeat_failure_report
        report = build_heartbeat_failure_report(
            "disp-oi1184", self._verdict(), model="sonnet-5", terminal_id="T1"
        )
        assert "**Model**: sonnet-5" in report
