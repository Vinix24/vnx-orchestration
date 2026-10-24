"""A test that points provider_registry at a tmp registry must not leave the
model normalizer's alias tables behind for later tests in the same process.

Dispatch dlv-df8c5d8f5ed0. VNX CI run 39804864104 reported
``test_normalizer_keeps_5_and_5_5_apart[anthropic/claude-sonnet-5-5-sonnet-5-5]``
failing only under xdist. The polluter is
``tests/test_codex_model_registry.py::test_result_payload_carries_reasoning_effort_next_to_the_model``:
it runs a codex gate under a tmp registry that holds only ``openai``, the
receipt path calls ``normalize_model_name`` for the first time in that worker,
and the alias table is built from the tmp registry. Under that table
``claude-sonnet-5-5`` still resolves through the ``claude-<family>`` prefix
fallback, but ``anthropic/claude-sonnet-5-5`` has no registry alias and no
``claude-`` prefix, so it came back unchanged.

The reproduction runs the polluter and the victim in one process in a fixed
order, which is the only way to make the order dependency deterministic.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent

POLLUTER = (
    "tests/test_codex_model_registry.py"
    "::test_result_payload_carries_reasoning_effort_next_to_the_model"
)
VICTIM = "tests/test_sonnet_5_5_registry_and_model_resolved.py::test_normalizer_keeps_5_and_5_5_apart"


def test_normalizer_survives_a_tmp_registry_test_that_ran_before_it():
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:randomly",
         "-p", "no:cacheprovider", POLLUTER, VICTIM],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
