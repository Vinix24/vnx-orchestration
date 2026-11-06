"""Characterisation tests for the opt-in ``--auto-route`` step of provider_dispatch.main.

These pin the observable behaviour of the smart-router step BEFORE it moves into its
own private helper (golf 2, G2-12). The contract under test is what a caller can see:

* with a primary decision: ``provider`` and ``args.model`` are replaced, both
  ``VNX_ROUTE_STRATEGY`` / ``VNX_TASK_CLASS`` are set, the decision is written into
  the state dir, and the info line is logged;
* with a decision that has no primary: the provider and model stay as passed,
  neither env var is set, the decision is still written and the info line logged;
* when ``decide`` raises: nothing raises out of ``main``, the warning is logged and
  the ``--provider`` / ``--model`` seeds are used;
* without ``--auto-route``: ``smart_router`` is not imported and ``decide`` never runs.

The warning text is a frozen output shape: ``_EXPECTED_WARNING`` is module-level so the
sensitivity of that assertion can be demonstrated at runtime.

Dispatch-ID: dlv-33d4916379b0
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from smart_router import RouteDecision  # noqa: E402

# Frozen output shape of the fallback warning. Module-level so a run can assert the
# test's own sensitivity to the text (the dispatch's red-run demonstration).
_EXPECTED_WARNING = "auto-route failed"


@pytest.fixture
def recs_kimi_wins(tmp_path):
    """kimi-k3 ranked first with an explicit low cost, claude second."""
    data = {
        "routing_by_task": {
            "01_code_generation": [
                {"model_id": "kimi-k3", "composite_score": 9.0,
                 "avg_duration_seconds": 200.0, "cost_usd_per_call": 0.001},
                {"model_id": "claude-sonnet-4-6", "composite_score": 8.0,
                 "avg_duration_seconds": 512.0, "cost_usd_per_call": None},
            ],
        },
    }
    path = tmp_path / "routing_recommendations.yaml"
    path.write_text(yaml.dump(data))
    return path


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def _swap_recommendations(src_yaml, monkeypatch):
    """Point smart_router at the fixture registry without touching the shipped YAML."""
    import smart_router as _sr

    # decide() reads the path from its own globals; patch the function globals too so
    # the shim under tests/smart_router cannot shadow the module attribute.
    monkeypatch.setitem(_sr.decide.__globals__, "_RECOMMENDATIONS_PATH", Path(src_yaml))
    monkeypatch.setattr(_sr, "_RECOMMENDATIONS_PATH", Path(src_yaml))


def _main_args(**overrides):
    values = {
        "provider": "gemini",
        "terminal-id": "T1",
        "dispatch-id": "g2-12-auto-route",
        "instruction": "implement feature X",
        "model": "sonnet",
    }
    values.update({k.replace("_", "-"): v for k, v in overrides.items()})
    argv: list[str] = []
    for key, value in values.items():
        argv.extend([f"--{key}", value])
    argv.append("--auto-route")
    return argv


class TestAutoRouteStepWithPrimary:
    def test_primary_replaces_provider_model_and_env(self, recs_kimi_wins, state_dir, monkeypatch, caplog):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        for var in ("VNX_ROUTE_STRATEGY", "VNX_TASK_CLASS"):
            monkeypatch.delenv(var, raising=False)
        _swap_recommendations(recs_kimi_wins, monkeypatch)

        captured = {}

        def _fake_kimi(args, **_kwargs):
            captured["provider"] = args.provider
            captured["model"] = args.model
            return 0

        with patch("provider_dispatch._dispatch_kimi", side_effect=_fake_kimi) as kimi_mock, \
                patch("provider_dispatch._dispatch_gemini", return_value=99) as gemini_mock, \
                caplog.at_level(logging.INFO, logger="provider_dispatch"):
            result = provider_dispatch.main(
                _main_args(dispatch_id="g2-12-primary"),
            )

        assert result == 0
        assert kimi_mock.called, "the routed provider handler must run"
        assert not gemini_mock.called, "the seed provider must have been replaced"
        assert captured == {"provider": "kimi", "model": "kimi-k3"}
        assert os.environ["VNX_ROUTE_STRATEGY"] == "smart_router"
        assert os.environ["VNX_TASK_CLASS"] == "01_code_generation"

        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists()
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "g2-12-primary"
        assert record["chosen_route"]["model_id"] == "kimi-k3"

        info_lines = [
            rec.getMessage() for rec in caplog.records
            if rec.levelno == logging.INFO and "smart_router: auto-route" in rec.getMessage()
        ]
        assert info_lines == [
            "smart_router: auto-route provider=kimi model=kimi-k3 (task_class=01_code_generation)"
        ]


class TestAutoRouteStepWithoutPrimary:
    def test_no_primary_keeps_provider_model_and_skips_env(self, state_dir, monkeypatch, caplog):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        for var in ("VNX_ROUTE_STRATEGY", "VNX_TASK_CLASS"):
            monkeypatch.delenv(var, raising=False)

        decision = RouteDecision(
            task_class="01_code_generation",
            primary=None,
            fallback=None,
            reason="no candidate survived constraints",
        )
        captured = {}

        def _fake_kimi(args, **_kwargs):
            captured["provider"] = args.provider
            captured["model"] = args.model
            return 0

        with patch("smart_router.decide", return_value=decision), \
                patch("provider_dispatch._dispatch_kimi", side_effect=_fake_kimi), \
                caplog.at_level(logging.INFO, logger="provider_dispatch"):
            result = provider_dispatch.main(
                _main_args(provider="kimi", model="kimi-k3", dispatch_id="g2-12-no-primary"),
            )

        assert result == 0
        # The seed provider/model survive an empty decision untouched.
        assert captured == {"provider": "kimi", "model": "kimi-k3"}
        assert "VNX_ROUTE_STRATEGY" not in os.environ
        assert "VNX_TASK_CLASS" not in os.environ

        # The no-primary decision is still persisted.
        ndjson_path = state_dir / "route_decisions.ndjson"
        assert ndjson_path.exists()
        record = json.loads(ndjson_path.read_text(encoding="utf-8").strip())
        assert record["dispatch_id"] == "g2-12-no-primary"
        assert record["chosen_route"] is None

        info_lines = [
            rec.getMessage() for rec in caplog.records
            if rec.levelno == logging.INFO and "smart_router: auto-route" in rec.getMessage()
        ]
        assert info_lines == [
            "smart_router: auto-route provider=kimi model=kimi-k3 (task_class=01_code_generation)"
        ]


class TestAutoRouteStepFailure:
    def test_decide_raising_falls_back_with_warning(self, state_dir, monkeypatch, caplog):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        for var in ("VNX_ROUTE_STRATEGY", "VNX_TASK_CLASS"):
            monkeypatch.delenv(var, raising=False)

        captured = {}

        def _fake_kimi(args, **_kwargs):
            captured["provider"] = args.provider
            captured["model"] = args.model
            return 0

        with patch("smart_router.decide", side_effect=RuntimeError("registry exploded")), \
                patch("provider_dispatch._dispatch_kimi", side_effect=_fake_kimi), \
                caplog.at_level(logging.WARNING, logger="provider_dispatch"):
            result = provider_dispatch.main(
                _main_args(provider="kimi", model="kimi-k3", dispatch_id="g2-12-failure"),
            )

        assert result == 0, "an auto-route failure must never raise out of main"
        assert captured == {"provider": "kimi", "model": "kimi-k3"}
        assert "VNX_ROUTE_STRATEGY" not in os.environ
        assert "VNX_TASK_CLASS" not in os.environ
        assert not (state_dir / "route_decisions.ndjson").exists()

        warnings = [
            rec.getMessage() for rec in caplog.records
            if rec.levelno == logging.WARNING and _EXPECTED_WARNING in rec.getMessage()
        ]
        assert len(warnings) == 1
        assert "registry exploded" in warnings[0]
        assert "falling back to --provider=kimi --model=kimi-k3" in warnings[0]


class TestAutoRouteDisabled:
    def test_without_flag_smart_router_is_never_imported_or_called(self, state_dir, monkeypatch):
        import provider_dispatch

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))

        # Load the module only so it can be patched; the assertion below is that MAIN
        # does not import it. The pre-call snapshot is taken after the import.
        import smart_router  # noqa: F401

        pre_call = set(sys.modules)
        with patch("smart_router.decide", side_effect=AssertionError("decide must not run")) as decide_mock, \
                patch("provider_dispatch._dispatch_kimi", return_value=0):
            result = provider_dispatch.main([
                "--provider", "kimi",
                "--terminal-id", "T1",
                "--dispatch-id", "g2-12-disabled",
                "--instruction", "implement feature",
                "--model", "kimi-k3",
            ])

        assert result == 0
        decide_mock.assert_not_called()
        assert "smart_router" not in (set(sys.modules) - pre_call)
        assert not (state_dir / "route_decisions.ndjson").exists()
