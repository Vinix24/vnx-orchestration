"""OI-2066: the harness-lane review deadline fits the diff, from one resolver.

Measured 2026-10-15. ``gate_lane_contract.TIMEOUT_SECONDS = 900`` was applied
as-is by gate_runner, kimi_gate and glm_gate, with no config key and no env
var. On PR #2058 (head ae1e193e, +2288/-207) kimi_gate and deepseek_gate both
stopped at 900.0 s, so a big PR could not get a second review seat.

``gate_lane_contract.harness_timeout_seconds(gate, diff_chars)`` is now the one
place that knows the per-gate config keys, the default, the diff extension and
the ceiling, the same one-resolver pattern as ``max_diff_chars`` (OI-1874).

The second half of the same incident: ``str(subprocess.TimeoutExpired)`` is
the whole argv, the diff under review included, and it landed in
``reason_detail``. ``describe_dispatch_exception`` is the bounded, argv-free
description every harness-lane gate now uses.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VNX_ROOT / "scripts"))
sys.path.insert(0, str(VNX_ROOT / "scripts" / "lib"))

import config_registry
import config_runtime
import gate_lane_contract
import vnx_paths
from gate_lane_contract import (
    DISPATCH_ERROR_DETAIL_MAX_CHARS,
    TIMEOUT_CONFIG,
    describe_dispatch_exception,
    harness_dispatcher_timeouts,
    harness_timeout_seconds,
)

GATES = ("kimi_gate", "glm_gate", "deepseek_gate")
_LOGGER = "gate_lane_contract"

# The two provider phrases the 15-10 quota scan found inside PR #2058's diff:
# test fixtures, quoted by the code under review, never said by a provider.
QUOTA_FIXTURE_TEXT = (
    "+    body = 'API Error: 402 insufficient balance'\n"
    "+    other = \"You've reached your usage limit for this billing cycle\"\n"
)


@pytest.fixture
def isolated_timeout_config(monkeypatch):
    """Hold the resolver to its registry defaults: no env value, no operator
    override, and no real project_config store (config_registry's DB layer
    ranks ABOVE env vars, so deleting the env vars alone is not enough on a
    machine with a populated store). Same isolation as
    ``tests/test_gate_lane_contract.py::isolate_config_runtime``, for the
    timeout keys."""
    for key, _default in TIMEOUT_CONFIG.values():
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"VNX_OVERRIDE_{config_registry._bare(key)}", raising=False)
    monkeypatch.delenv("VNX_STATE_DIR", raising=False)
    monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
    monkeypatch.setattr(vnx_paths, "resolve_paths", lambda: {})
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)
    yield
    config_runtime._wired_for.clear()
    config_registry.set_db_resolver(None)
    config_registry.set_default_project_id(None)


def _warnings(caplog):
    return [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestWorkedExamples:
    """The four values the dispatch pins: 900 s base, +600 s for every
    STARTED 100,000 chars above the first 100,000, bounded by 3600 s."""

    @pytest.mark.parametrize("gate", GATES)
    @pytest.mark.parametrize("diff_chars,expected", [
        (50_000, 900),
        (100_000, 900),
        (163_891, 1500),   # PR #2058's diff on 15-10
        (800_000, 3600),   # 900 + 4200 = 5100, clamped
    ])
    def test_worked_example(self, isolated_timeout_config, gate, diff_chars, expected):
        assert harness_timeout_seconds(gate, diff_chars) == expected

    def test_a_started_step_counts_whole(self, isolated_timeout_config):
        assert harness_timeout_seconds("kimi_gate", 100_001) == 1500
        assert harness_timeout_seconds("kimi_gate", 200_000) == 1500
        assert harness_timeout_seconds("kimi_gate", 200_001) == 2100

    def test_the_clamp_logs_exactly_one_warning(self, isolated_timeout_config, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("kimi_gate", 800_000) == 3600
        warnings = _warnings(caplog)
        assert len(warnings) == 1, [w.getMessage() for w in warnings]
        assert "clamped to 3600" in warnings[0].getMessage()

    def test_no_warning_below_the_ceiling(self, isolated_timeout_config, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("glm_gate", 163_891) == 1500
        assert _warnings(caplog) == []


class TestConfigOverride:
    @pytest.mark.parametrize("gate", GATES)
    def test_each_gate_reads_its_own_key(self, isolated_timeout_config, monkeypatch, gate):
        key, _default = TIMEOUT_CONFIG[gate]
        monkeypatch.setenv(key, "1200")
        assert harness_timeout_seconds(gate, 50_000) == 1200
        # The diff extension applies on top of a configured base.
        assert harness_timeout_seconds(gate, 163_891) == 1800
        # The other gates are untouched by this gate's key.
        for other in GATES:
            if other != gate:
                assert harness_timeout_seconds(other, 50_000) == 900

    def test_the_ceiling_bounds_a_configured_base_too(
        self, isolated_timeout_config, monkeypatch, caplog,
    ):
        monkeypatch.setenv("VNX_GLM_GATE_TIMEOUT_SECONDS", "5000")
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("glm_gate", 0) == 3600
        assert len(_warnings(caplog)) == 1

    def test_the_operator_override_brake_wins(self, isolated_timeout_config, monkeypatch):
        monkeypatch.setenv("VNX_KIMI_GATE_TIMEOUT_SECONDS", "1200")
        monkeypatch.setenv(
            f"VNX_OVERRIDE_{config_registry._bare('VNX_KIMI_GATE_TIMEOUT_SECONDS')}", "600",
        )
        assert harness_timeout_seconds("kimi_gate", 0) == 600


class TestFallbacks:
    @pytest.mark.parametrize("raw", ["abc", "", "12.5", "15m"])
    def test_an_invalid_value_falls_back_with_a_warning(
        self, isolated_timeout_config, monkeypatch, caplog, raw,
    ):
        monkeypatch.setenv("VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS", raw)
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("deepseek_gate", 50_000) == 900
        assert len(_warnings(caplog)) == 1
        assert "invalid config value" in _warnings(caplog)[0].getMessage()

    @pytest.mark.parametrize("raw", ["0", "-5"])
    def test_a_non_positive_value_falls_back_with_a_warning(
        self, isolated_timeout_config, monkeypatch, caplog, raw,
    ):
        monkeypatch.setenv("VNX_KIMI_GATE_TIMEOUT_SECONDS", raw)
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("kimi_gate", 163_891) == 1500
        assert len(_warnings(caplog)) == 1
        assert "non-positive" in _warnings(caplog)[0].getMessage()

    def test_an_unknown_gate_falls_back_with_a_warning(self, isolated_timeout_config, caplog):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("codex_gate", 50_000) == 900
        assert "unknown gate" in _warnings(caplog)[0].getMessage()
        # The fallback base still grows with the diff: never a fixed 900.
        assert harness_timeout_seconds("no_such_gate", 163_891) == 1500

    @pytest.mark.parametrize("diff_chars", [None, "abc", object()])
    def test_a_diff_length_that_is_not_a_number_counts_as_zero(
        self, isolated_timeout_config, caplog, diff_chars,
    ):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_timeout_seconds("kimi_gate", diff_chars) == 900
        assert "not a number" in _warnings(caplog)[0].getMessage()

    def test_a_negative_diff_length_counts_as_zero(self, isolated_timeout_config):
        assert harness_timeout_seconds("kimi_gate", -1) == 900

    @pytest.mark.parametrize("raw", ["abc", "0", "-1", "999999", "1"])
    @pytest.mark.parametrize("diff_chars", [0, 99_999, 163_891, 10_000_000])
    def test_never_unbounded_never_zero(
        self, isolated_timeout_config, monkeypatch, raw, diff_chars,
    ):
        monkeypatch.setenv("VNX_GLM_GATE_TIMEOUT_SECONDS", raw)
        value = harness_timeout_seconds("glm_gate", diff_chars)
        assert isinstance(value, int)
        assert 0 < value <= gate_lane_contract.TIMEOUT_CEILING_SECONDS


class TestRegistry:
    @pytest.mark.parametrize("gate", GATES)
    def test_config_registry_carries_the_same_default(self, gate):
        key, default = TIMEOUT_CONFIG[gate]
        entry = config_registry.CONFIG_REGISTRY[key]
        assert entry.default == str(default) == "900"
        assert entry.subsystem == "harness-lane-review-gates"

    def test_the_diff_cap_flag_stays_the_subsystems_last_inserted_flag(self):
        # vnx_cli's ledger generator shows a subsystem's LAST-INSERTED flag
        # (OI-1874 r4); the three new keys must not displace the canonical one.
        keys = [
            key for key, entry in config_registry.CONFIG_REGISTRY.items()
            if entry.subsystem == "harness-lane-review-gates"
        ]
        assert keys[-1] == "VNX_KIMI_GATE_MAX_DIFF_CHARS"
        assert {k for k, _ in TIMEOUT_CONFIG.values()} <= set(keys)


# ---------------------------------------------------------------------------
# The pair handed to the dispatcher
# ---------------------------------------------------------------------------


class TestDispatcherTimeouts:
    def test_outer_timeout_is_the_lane_deadline_plus_the_margin(self, isolated_timeout_config):
        assert harness_dispatcher_timeouts("kimi_gate", 163_891) == {
            "timeout_seconds": 1560,
            "total_deadline_seconds": 1500,
        }

    def test_an_explicit_timeout_replaces_the_resolved_deadline(self, isolated_timeout_config):
        assert harness_dispatcher_timeouts("glm_gate", 800_000, explicit=1200) == {
            "timeout_seconds": 1260,
            "total_deadline_seconds": 1200,
        }

    @pytest.mark.parametrize("explicit", [0, -30])
    def test_a_non_positive_explicit_timeout_is_ignored(
        self, isolated_timeout_config, caplog, explicit,
    ):
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        assert harness_dispatcher_timeouts("kimi_gate", 50_000, explicit=explicit) == {
            "timeout_seconds": 960,
            "total_deadline_seconds": 900,
        }
        assert "non-positive explicit" in _warnings(caplog)[0].getMessage()


# ---------------------------------------------------------------------------
# describe_dispatch_exception: no argv, no prompt, bounded
# ---------------------------------------------------------------------------


def _argv(prompt: str) -> list:
    return [
        "/opt/homebrew/bin/python3.12", "/repo/scripts/lib/provider_dispatch.py",
        "--provider", "kimi", "--dispatch-id", "kimi-gate-pr2058-1792000000",
        "--instruction", prompt, "--no-auto-commit",
    ]


def _big_prompt() -> str:
    return "Review the PR diff below.\n" + ("+x = 1\n" * 20_000) + QUOTA_FIXTURE_TEXT


class TestDescribeDispatchException:
    def test_timeout_expired_keeps_program_and_timeout_never_argv(self):
        prompt = _big_prompt()
        exc = subprocess.TimeoutExpired(_argv(prompt), 1560, output=prompt, stderr=prompt)
        assert len(str(exc)) > 100_000  # the 15-10 shape: str() is the whole argv
        text = describe_dispatch_exception(exc, prompt=prompt)
        assert text == "TimeoutExpired: provider_dispatch.py timed out after 1560 s"
        assert "insufficient balance" not in text
        assert "--instruction" not in text

    def test_called_process_error_keeps_program_and_return_code(self):
        prompt = _big_prompt()
        exc = subprocess.CalledProcessError(3, _argv(prompt), output=prompt, stderr=prompt)
        text = describe_dispatch_exception(exc, prompt=prompt)
        assert text == "CalledProcessError: provider_dispatch.py exited with code 3"

    def test_a_string_command_keeps_only_its_program(self):
        exc = subprocess.TimeoutExpired("kimi --print -p 'secret prompt'", 30)
        assert describe_dispatch_exception(exc) == "TimeoutExpired: kimi timed out after 30 s"

    def test_another_exception_keeps_type_and_message(self):
        exc = RuntimeError("no report for kimi-gate-pr7-1 (rc=1): stdout:  | stderr: boom")
        assert describe_dispatch_exception(exc) == (
            "RuntimeError: no report for kimi-gate-pr7-1 (rc=1): stdout:  | stderr: boom"
        )

    def test_the_prompt_is_redacted_raw_and_inside_a_list_repr(self):
        prompt = "line one\nYou've reached your usage limit for this billing cycle\n"
        wrapped = RuntimeError(f"dispatch failed: {prompt} | argv={_argv(prompt)!r}")
        text = describe_dispatch_exception(wrapped, prompt=prompt)
        assert "usage limit" not in text
        assert text.count(f"<prompt: {len(prompt)} chars>") == 2

    def test_the_description_is_bounded(self):
        text = describe_dispatch_exception(ValueError("y" * 50_000))
        assert len(text) == DISPATCH_ERROR_DETAIL_MAX_CHARS == 2000
        assert text.endswith(" ... [truncated]")

    def test_an_exception_without_a_message_still_names_its_type(self):
        assert describe_dispatch_exception(KeyError()) == "KeyError: "
