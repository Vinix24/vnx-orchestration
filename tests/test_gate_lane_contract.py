"""The harness-lane verdict contract is ONE object, not three copies (C6 step 3).

``gate_runner``, ``glm_gate`` and ``kimi_gate`` each used to carry their own
byte-identical copy of the verdict contract and the model/timeout/diff-cap
defaults. A copy drifts silently: an edit to one shape leaves the other two
behind and no test fails, because every module reads its own copy. The three
readers now alias the SAME objects in ``gate_lane_contract``. These tests pin
that identity — a byte-identical copy smuggled back in would be a different
object, so ``is`` fails and the copy can no longer go unnoticed.
"""
from __future__ import annotations

import sys
from pathlib import Path

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_lane_contract
import gate_runner
import glm_gate
import kimi_gate


def test_verdict_contract_is_one_object_across_all_three_readers():
    assert glm_gate._VERDICT_CONTRACT is gate_lane_contract.VERDICT_CONTRACT
    assert kimi_gate._VERDICT_CONTRACT is gate_lane_contract.VERDICT_CONTRACT
    assert gate_runner._HARNESS_LANE_VERDICT_CONTRACT is gate_lane_contract.VERDICT_CONTRACT
    # The two gates must read the same object as each other, not merely two
    # equal aliases of the source.
    assert glm_gate._VERDICT_CONTRACT is kimi_gate._VERDICT_CONTRACT


def test_verdict_contract_is_not_the_codex_reviewer_template():
    # gate_runner's codex/gemini reviewer asks for a different, richer findings
    # shape. Aliasing that template here would silently change what the
    # harness-lane gates ask for, so the identity must NOT hold.
    assert gate_runner._HARNESS_LANE_VERDICT_CONTRACT is not gate_runner._REVIEWER_VERDICT_TEMPLATE


def test_model_timeout_and_diff_cap_are_one_source():
    assert gate_runner._HARNESS_LANE_MODEL is gate_lane_contract.MODEL_DEFAULTS
    assert glm_gate.DEFAULT_MODEL == gate_lane_contract.MODEL_DEFAULTS["glm_gate"][1]
    assert kimi_gate.DEFAULT_MODEL == gate_lane_contract.MODEL_DEFAULTS["kimi_gate"][1]
    assert glm_gate.DEFAULT_TIMEOUT == gate_lane_contract.TIMEOUT_SECONDS
    assert kimi_gate.DEFAULT_TIMEOUT == gate_lane_contract.TIMEOUT_SECONDS
    assert gate_runner._HARNESS_LANE_TIMEOUT_SECONDS == gate_lane_contract.TIMEOUT_SECONDS
    assert glm_gate.MAX_DIFF_CHARS == gate_lane_contract.MAX_DIFF_CHARS
    assert kimi_gate.MAX_DIFF_CHARS == gate_lane_contract.MAX_DIFF_CHARS
    assert gate_runner._HARNESS_LANE_MAX_DIFF_CHARS == gate_lane_contract.MAX_DIFF_CHARS


def test_model_env_var_names_come_from_the_shared_map():
    assert glm_gate._MODEL_ENV == "VNX_GLM_GATE_MODEL"
    assert kimi_gate._MODEL_ENV == "VNX_KIMI_GATE_MODEL"
