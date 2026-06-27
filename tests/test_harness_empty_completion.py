#!/usr/bin/env python3
"""Harness lanes coerce an empty rc=0 completion into a retryable, loud failure.

Dispatch-ID: 20260627-harness-empty-completion

The glm/deepseek harness lanes (claude CLI → litellm/OpenRouter) occasionally
return a successful exit with no assistant text. Left as-is that is a silent
empty report with no retry. `_coerce_empty_completion_to_retryable` turns it into
returncode=1 + a clear error so the adapter retries and the flake is visible.
Timeouts and non-zero exits are passed through unchanged.
"""

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from provider_spawns.glm_harness_spawn import _coerce_empty_completion_to_retryable as glm_coerce  # noqa: E402
from provider_spawns.deepseek_harness_spawn import _coerce_empty_completion_to_retryable as ds_coerce  # noqa: E402

_BOTH = pytest.mark.parametrize("coerce,lane", [(glm_coerce, "glm-harness"), (ds_coerce, "deepseek-harness")])


@_BOTH
def test_empty_rc0_becomes_retryable(coerce, lane):
    rc, err = coerce(0, False, {"text": ""}, None, lane)
    assert rc == 1
    assert err and "empty completion" in err and "retryable" in err


@_BOTH
def test_whitespace_only_rc0_becomes_retryable(coerce, lane):
    rc, err = coerce(0, False, {"text": "   \n  "}, None, lane)
    assert rc == 1


@_BOTH
def test_missing_text_key_becomes_retryable(coerce, lane):
    rc, err = coerce(0, False, {}, None, lane)
    assert rc == 1


@_BOTH
def test_non_dict_completion_becomes_retryable(coerce, lane):
    rc, err = coerce(0, False, None, None, lane)
    assert rc == 1


@_BOTH
def test_real_text_passes_through(coerce, lane):
    rc, err = coerce(0, False, {"text": "a real answer"}, None, lane)
    assert rc == 0
    assert err is None


@_BOTH
def test_nonzero_rc_passes_through(coerce, lane):
    # An existing failure is left exactly as-is (don't mask its error/code).
    rc, err = coerce(2, False, {"text": ""}, "boom", lane)
    assert rc == 2
    assert err == "boom"


@_BOTH
def test_timeout_passes_through(coerce, lane):
    # A timeout keeps its own signal; we don't relabel it as an empty-completion flake.
    rc, err = coerce(0, True, {"text": ""}, "timeout after 600s", lane)
    assert rc == 0
    assert err == "timeout after 600s"


@_BOTH
def test_preexisting_error_preserved_on_empty(coerce, lane):
    rc, err = coerce(0, False, {"text": ""}, "upstream said X", lane)
    assert rc == 1
    assert err == "upstream said X"  # don't clobber a more-specific error
