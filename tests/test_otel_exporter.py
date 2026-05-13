#!/usr/bin/env python3
"""Tests for otel_emitter and otel_exporter — env-gate behaviour and no-op paths."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


class TestOtelEmitterDisabled:
    """When OTEL_EXPORTER_OTLP_ENDPOINT is unset, everything is a no-op."""

    def setup_method(self):
        import importlib
        import otel_emitter
        # Force module reload with _ENABLED=False to ensure isolation
        self._orig_enabled = otel_emitter._ENABLED
        otel_emitter._ENABLED = False
        otel_emitter._meter = None
        otel_emitter._tracer = None

    def teardown_method(self):
        import otel_emitter
        otel_emitter._ENABLED = self._orig_enabled

    def test_emit_metric_is_noop_when_disabled(self):
        import otel_emitter
        # Must not raise and must not attempt opentelemetry import
        with patch.dict("sys.modules", {"opentelemetry": None}):
            otel_emitter.emit_metric("smoke_test", value=1.0)

    def test_emit_span_is_noop_when_disabled(self):
        import otel_emitter
        with patch.dict("sys.modules", {"opentelemetry": None}):
            with otel_emitter.emit_span("smoke_span") as span:
                assert span is None

    def test_emit_metric_no_import_error_when_pkg_absent(self):
        """Absence of opentelemetry package must never raise."""
        import otel_emitter
        otel_emitter._ENABLED = False
        otel_emitter.emit_metric("no_pkg_test")

    def test_emit_dispatch_completion_noop_when_disabled(self):
        from otel_exporter import emit_dispatch_completion
        import otel_emitter
        otel_emitter._ENABLED = False
        # Must not raise
        emit_dispatch_completion("d-001", "T1", "done", 42.0)


class TestOtelEmitterEnabled:
    """When enabled, emit_metric and emit_span use in-memory exporters."""

    def setup_method(self):
        import otel_emitter
        self._orig_enabled = otel_emitter._ENABLED
        self._orig_meter = otel_emitter._meter
        self._orig_tracer = otel_emitter._tracer
        otel_emitter._meter = None
        otel_emitter._tracer = None
        otel_emitter._ENABLED = True

    def teardown_method(self):
        import otel_emitter
        otel_emitter._ENABLED = self._orig_enabled
        otel_emitter._meter = self._orig_meter
        otel_emitter._tracer = self._orig_tracer

    def test_emit_metric_calls_create_counter_and_add(self):
        import otel_emitter
        mock_meter = MagicMock()
        mock_counter = MagicMock()
        mock_meter.create_counter.return_value = mock_counter
        otel_emitter._meter = mock_meter

        otel_emitter.emit_metric("dispatch_completion_count", value=1.0, attrs={"status": "done"})

        mock_meter.create_counter.assert_called_once_with("dispatch_completion_count")
        mock_counter.add.assert_called_once_with(1.0, attributes={"status": "done"})

    def test_emit_metric_default_attrs_empty_dict(self):
        import otel_emitter
        mock_meter = MagicMock()
        mock_counter = MagicMock()
        mock_meter.create_counter.return_value = mock_counter
        otel_emitter._meter = mock_meter

        otel_emitter.emit_metric("test_metric")

        mock_counter.add.assert_called_once_with(1.0, attributes={})

    def test_emit_span_enters_span_context(self):
        import otel_emitter
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = lambda s: mock_span
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
        otel_emitter._tracer = mock_tracer

        with otel_emitter.emit_span("my_span", attrs={"k": "v"}) as span:
            assert span is mock_span

        mock_tracer.start_as_current_span.assert_called_once_with("my_span", attributes={"k": "v"})

    def test_emit_span_default_attrs(self):
        import otel_emitter
        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = lambda s: MagicMock()
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
        otel_emitter._tracer = mock_tracer

        with otel_emitter.emit_span("span_no_attrs"):
            pass

        mock_tracer.start_as_current_span.assert_called_once_with("span_no_attrs", attributes={})

    def test_init_graceful_when_opentelemetry_missing(self):
        """_init() must not raise even if the package is absent."""
        import otel_emitter
        otel_emitter._meter = None
        otel_emitter._tracer = None
        with patch.dict("sys.modules", {
            "opentelemetry": None,
            "opentelemetry.metrics": None,
            "opentelemetry.trace": None,
        }):
            otel_emitter._init()
        # meter/tracer remain None — that's the graceful no-op path
        assert otel_emitter._meter is None


class TestEmitDispatchCompletion:
    """emit_dispatch_completion passes correct attrs to both metric and span."""

    def test_calls_emit_metric_and_span(self):
        import otel_emitter
        from otel_exporter import emit_dispatch_completion

        orig_enabled = otel_emitter._ENABLED
        otel_emitter._ENABLED = True
        mock_meter = MagicMock()
        mock_counter = MagicMock()
        mock_meter.create_counter.return_value = mock_counter
        otel_emitter._meter = mock_meter

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span.return_value.__enter__ = lambda s: MagicMock()
        mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
        otel_emitter._tracer = mock_tracer

        try:
            emit_dispatch_completion("d-999", "T2", "failed", 30.5)
        finally:
            otel_emitter._ENABLED = orig_enabled

        mock_meter.create_counter.assert_called_once_with("dispatch_completion_count")
        add_attrs = mock_counter.add.call_args[1]["attributes"]
        assert add_attrs["dispatch_id"] == "d-999"
        assert add_attrs["terminal"] == "T2"
        assert add_attrs["status"] == "failed"

        span_attrs = mock_tracer.start_as_current_span.call_args[1]["attributes"]
        assert span_attrs["duration_seconds"] == 30.5
