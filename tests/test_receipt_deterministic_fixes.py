#!/usr/bin/env python3
"""Regression tests for receipt deterministic fixes (Dispatch-ID: 20260527-185950-receipt-deterministic-fixes).

Covers:
  Fix 1  provider_dispatch._resolve_codex_model()  — non-empty model for codex
  Fix 1  provider_dispatch._resolve_kimi_model_label() — non-empty label for kimi
  Fix 1  _dispatch_codex uses non-empty model when VNX_CODEX_MODEL unset
  Fix 1  _dispatch_kimi uses non-empty model_label when VNX_KIMI_MODEL unset
  Fix 2  report_parser extracts dispatch_id from filename when content has no id
  Fix 2  report_parser preserves content dispatch_id over filename fallback
  Fix 2  rp_delivery.sh ghost-filter skips bare 'unknown'
  Fix 2  rp_delivery.sh ghost-filter skips empty string
  Fix 2  rp_delivery.sh ghost-filter does NOT skip a real dispatch_id
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))


# ---------------------------------------------------------------------------
# Fix 1 — provider_dispatch model resolution
# ---------------------------------------------------------------------------


class TestResolveCodexModel:
    """_resolve_codex_model() returns a non-empty string in all paths."""

    def test_returns_nonempty_from_registry(self):
        """When registry loads successfully, returns first model key (non-empty)."""
        import provider_dispatch

        result = provider_dispatch._resolve_codex_model()
        assert isinstance(result, str)
        assert result.strip() != "", "_resolve_codex_model must return non-empty string"

    def test_fallback_when_registry_unavailable(self):
        """When registry raises, falls back to hardcoded 'gpt-5.2-codex'."""
        import provider_dispatch

        with patch("providers.provider_registry.load", side_effect=FileNotFoundError("no yaml")):
            result = provider_dispatch._resolve_codex_model()
        assert result == "gpt-5.2-codex"

    def test_fallback_when_openai_missing_from_registry(self):
        """When openai section is absent, returns hardcoded fallback."""
        import provider_dispatch
        from unittest.mock import MagicMock

        mock_registry = {}  # empty — no 'openai' key
        with patch("providers.provider_registry.load", return_value=mock_registry):
            result = provider_dispatch._resolve_codex_model()
        assert result == "gpt-5.2-codex"


class TestResolveKimiModelLabel:
    """_resolve_kimi_model_label() returns a non-empty string in all paths."""

    def test_returns_nonempty_from_registry(self):
        """When registry loads successfully, returns first kimi_cli model key (non-empty)."""
        import provider_dispatch

        result = provider_dispatch._resolve_kimi_model_label()
        assert isinstance(result, str)
        assert result.strip() != "", "_resolve_kimi_model_label must return non-empty string"
        assert result != "default", "should not return generic 'default' — use registry key"

    def test_fallback_when_registry_unavailable(self):
        """When registry raises, falls back to 'kimi-default'."""
        import provider_dispatch

        with patch("providers.provider_registry.load", side_effect=FileNotFoundError("no yaml")):
            result = provider_dispatch._resolve_kimi_model_label()
        assert result == "kimi-default"

    def test_fallback_when_kimi_cli_missing_from_registry(self):
        """When kimi_cli section is absent, returns hardcoded fallback."""
        import provider_dispatch

        mock_registry = {}  # empty — no 'kimi_cli' key
        with patch("providers.provider_registry.load", return_value=mock_registry):
            result = provider_dispatch._resolve_kimi_model_label()
        assert result == "kimi-default"


class TestDispatchCodexModelNonEmpty:
    """_dispatch_codex() emits governance with a non-empty model field."""

    def _make_args(self, **kw):
        args = MagicMock()
        args.provider = "codex"
        args.terminal_id = "T1"
        args.dispatch_id = "test-codex-model-nonempty"
        args.instruction = "noop"
        args.model = "sonnet"
        args.role = "backend-developer"
        args.pr_id = "PR-TEST"
        args.dispatch_paths = ""
        args.max_retries = 1
        args.no_auto_commit = True
        args.gate = ""
        for k, v in kw.items():
            setattr(args, k, v)
        return args

    def test_model_passed_to_emit_governance_is_nonempty_when_env_unset(self):
        """When VNX_CODEX_MODEL is unset, _dispatch_codex resolves a non-empty model."""
        import provider_dispatch

        captured_model = []

        def mock_emit(args, provider, model_used, result, start, end, status):
            captured_model.append(model_used)

        mock_result = MagicMock()
        mock_result.error = None
        mock_result.timed_out = False
        mock_result.returncode = 0
        mock_result.event_writer_failures = 0

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_CODEX_MODEL", None)
            with patch("provider_spawns.codex_spawn.spawn_codex", return_value=mock_result), \
                 patch("event_store.EventStore"), \
                 patch("provider_dispatch._emit_governance", mock_emit), \
                 patch("provider_dispatch._enrich_instruction", return_value="noop"):
                provider_dispatch._dispatch_codex(self._make_args())

        assert len(captured_model) == 1, "emit_governance was not called"
        assert captured_model[0], "model_used must be non-empty"
        assert captured_model[0].strip() != "", "model_used must not be whitespace-only"


class TestDispatchKimiModelLabelNonEmpty:
    """_dispatch_kimi() emits governance with a non-generic model_label field."""

    def _make_args(self, **kw):
        args = MagicMock()
        args.provider = "kimi"
        args.terminal_id = "T1"
        args.dispatch_id = "test-kimi-label-nonempty"
        args.instruction = "noop"
        args.model = "sonnet"
        args.role = "backend-developer"
        args.pr_id = "PR-TEST"
        args.dispatch_paths = ""
        args.max_retries = 1
        args.no_auto_commit = True
        args.gate = ""
        for k, v in kw.items():
            setattr(args, k, v)
        return args

    def test_model_label_nonempty_when_env_unset(self):
        """When VNX_KIMI_MODEL is unset, model_label must not be 'default'."""
        import provider_dispatch

        captured_model = []

        def mock_emit(args, provider, model_used, result, start, end, status):
            captured_model.append(model_used)

        mock_result = MagicMock()
        mock_result.error = None
        mock_result.timed_out = False
        mock_result.returncode = 0
        mock_result.event_writer_failures = 0

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_KIMI_MODEL", None)
            with patch("provider_spawns.kimi_spawn.spawn_kimi", return_value=mock_result), \
                 patch("event_store.EventStore"), \
                 patch("provider_dispatch._emit_governance", mock_emit):
                provider_dispatch._dispatch_kimi(self._make_args())

        assert len(captured_model) == 1, "emit_governance was not called"
        assert captured_model[0], "model_label must be non-empty"
        assert captured_model[0] != "default", (
            "model_label should be a registry key like 'kimi-default', not generic 'default'"
        )


# ---------------------------------------------------------------------------
# Fix 2 — dispatch_id extraction from filename
# ---------------------------------------------------------------------------


def _env_for_parser(tmp_path: Path) -> dict:
    """Build env dict for report_parser.py subprocess."""
    env = os.environ.copy()
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["VNX_DATA_DIR"] = str(data_dir)
    env["VNX_STATE_DIR"] = str(state_dir)
    env["VNX_HOME"] = str(REPO_ROOT)
    env["PROJECT_ROOT"] = str(REPO_ROOT)
    return env


def _run_parser(tmp_path: Path, report_path: Path) -> dict:
    """Run report_parser.py and return parsed JSON."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "report_parser.py"), str(report_path)],
        cwd=REPO_ROOT,
        env=_env_for_parser(tmp_path),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"report_parser.py failed:\n{result.stderr}"
    return json.loads(result.stdout)


class TestDispatchIdFromFilename:
    """Dispatch-ID is extracted from filename when content has no id field."""

    def test_dispatch_id_from_filename_when_content_empty(self, tmp_path: Path):
        """A report file with no dispatch_id in content → id extracted from filename."""
        dispatch_id = "20260527-132804-pip-repackage-namespace"
        report = tmp_path / f"{dispatch_id}_report.md"
        report.write_text(
            textwrap.dedent("""\
                # REPORT: Implementation

                **Terminal**: T1
                **Status**: success

                ## Summary
                No dispatch_id field in this report.
            """),
            encoding="utf-8",
        )

        payload = _run_parser(tmp_path, report)
        assert payload["dispatch_id"] == dispatch_id, (
            f"Expected dispatch_id={dispatch_id!r}, got {payload['dispatch_id']!r}"
        )

    def test_dispatch_id_from_filename_strips_report_md_suffix(self, tmp_path: Path):
        """Suffix '_report.md' is stripped cleanly from the filename."""
        dispatch_id = "20260527-185950-receipt-deterministic-fixes"
        report = tmp_path / f"{dispatch_id}_report.md"
        report.write_text(
            "# REPORT: test\n\n**Terminal**: T2\n**Status**: success\n",
            encoding="utf-8",
        )

        payload = _run_parser(tmp_path, report)
        assert payload["dispatch_id"] == dispatch_id

    def test_content_dispatch_id_takes_precedence_over_filename(self, tmp_path: Path):
        """When content has a valid dispatch_id, filename fallback is NOT applied."""
        content_id = "20260525-120000-content-wins"
        filename_id = "20260525-120000-filename-id"
        report = tmp_path / f"{filename_id}_report.md"
        report.write_text(
            textwrap.dedent(f"""\
                # REPORT: test

                **Terminal**: T1
                **Dispatch-ID**: {content_id}
                **Status**: success
            """),
            encoding="utf-8",
        )

        payload = _run_parser(tmp_path, report)
        assert payload["dispatch_id"] == content_id, (
            f"Content id {content_id!r} should override filename id {filename_id!r}"
        )

    def test_unknown_in_content_falls_back_to_filename(self, tmp_path: Path):
        """When content has 'unknown' dispatch_id, filename fallback kicks in."""
        dispatch_id = "20260527-094500-real-dispatch"
        report = tmp_path / f"{dispatch_id}_report.md"
        report.write_text(
            textwrap.dedent("""\
                # REPORT: test

                **Terminal**: T1
                **Dispatch-ID**: unknown
                **Status**: success
            """),
            encoding="utf-8",
        )

        payload = _run_parser(tmp_path, report)
        assert payload["dispatch_id"] == dispatch_id, (
            f"'unknown' in content should trigger filename fallback, got {payload['dispatch_id']!r}"
        )


# ---------------------------------------------------------------------------
# Fix 2 — ghost-filter in rp_delivery.sh
# ---------------------------------------------------------------------------


def _bash_case_check(dispatch_id: str) -> str:
    """Run a minimal bash snippet that mirrors the ghost-filter and returns 'skipped' or 'delivered'."""
    script = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        dispatch_id={dispatch_id!r}
        case "$dispatch_id" in
            unknown-*|unknown|no-id|"")
                echo "skipped"
                ;;
            *)
                echo "delivered"
                ;;
        esac
    """)
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"bash snippet failed:\n{result.stderr}"
    return result.stdout.strip()


class TestGhostFilterCaseStatement:
    """The ghost-filter case pattern skips only genuine ghosts."""

    @pytest.mark.parametrize("dispatch_id", [
        "unknown",
        "unknown-stop-hook-123",
        "unknown-abc",
        "no-id",
        "",
    ])
    def test_ghost_ids_are_skipped(self, dispatch_id: str):
        """All ghost/no-id patterns must be skipped (not delivered)."""
        assert _bash_case_check(dispatch_id) == "skipped", (
            f"dispatch_id={dispatch_id!r} should be skipped by ghost-filter"
        )

    @pytest.mark.parametrize("dispatch_id", [
        "20260527-132804-pip-repackage-namespace",
        "20260527-185950-receipt-deterministic-fixes",
        "some-real-dispatch-id",
        "PR-RECEIPT-FIX",
    ])
    def test_real_ids_are_delivered(self, dispatch_id: str):
        """Real dispatch IDs must NOT be filtered out."""
        assert _bash_case_check(dispatch_id) == "delivered", (
            f"dispatch_id={dispatch_id!r} should pass the ghost-filter and be delivered"
        )

    def test_rp_delivery_sh_contains_updated_filter(self):
        """rp_delivery.sh source contains the extended ghost-filter pattern."""
        delivery_sh = REPO_ROOT / "scripts" / "lib" / "receipt_processor" / "rp_delivery.sh"
        content = delivery_sh.read_text(encoding="utf-8")
        # Verify the vangnet covers bare 'unknown' and empty string
        assert "unknown|no-id" in content, (
            "rp_delivery.sh ghost-filter must include bare 'unknown' alongside no-id"
        )
        assert '|"")' in content or "|\"\")".replace("\\", "") in content, (
            "rp_delivery.sh ghost-filter must include empty string case"
        )
