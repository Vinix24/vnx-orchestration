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
import gate_recorder
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


def _harness_lane_gates():
    """The set of harness-lane gates, DERIVED from the single registry.

    ``gate_recorder.GATE_PROVIDERS`` is the one place that says which gates are
    harness lanes; ``gate_lane_contract.MODEL_DEFAULTS`` is the second table
    that must carry the model for each of them. Deriving the expectation here
    (instead of hardcoding a list of three names) means a fourth harness gate
    added to ``GATE_PROVIDERS`` is automatically demanded of ``MODEL_DEFAULTS``
    by the same test that caught ``deepseek_gate`` — the problem can no longer
    shift into the test.
    """
    return {
        gate
        for gate, (kind, _name) in gate_recorder.GATE_PROVIDERS.items()
        if kind == gate_recorder.GATE_PROVIDER_HARNESS_LANE
    }


def test_every_harness_lane_gate_has_a_model_default():
    """Every harness-lane gate must have a MODEL_DEFAULTS rule with a model.

    The two tables carry one fact in two places: ``GATE_PROVIDERS`` says which
    gates route through the governed dispatcher, ``MODEL_DEFAULTS`` says which
    model they dispatch on. When a harness gate is missing from the second
    table, ``gate_runner._run_harness_lane_path`` falls back to
    ``("", "")`` and hands ``model=""`` to the dispatcher. This is exactly the
    OI-1714 gap that let ``deepseek_gate`` reach the lane with no model.
    """
    harness_gates = _harness_lane_gates()
    assert harness_gates, (
        "GATE_PROVIDERS has no harness-lane gates — the derivation is empty, "
        "so this test would pass vacuously"
    )
    missing = sorted(
        gate for gate in harness_gates if gate not in gate_lane_contract.MODEL_DEFAULTS
    )
    assert not missing, (
        "every harness-lane gate in GATE_PROVIDERS must have a MODEL_DEFAULTS "
        f"entry, or gate_runner hands model='' to the dispatcher: {missing}"
    )
    empty_models = sorted(
        gate
        for gate in harness_gates
        if not (gate_lane_contract.MODEL_DEFAULTS.get(gate, ("", ""))[1] or "").strip()
    )
    assert not empty_models, (
        "a MODEL_DEFAULTS entry whose default model is empty is the same defect "
        f"as a missing entry — model='' reaches the dispatcher: {empty_models}"
    )


def test_model_defaults_cover_only_harness_lane_gates():
    """A MODEL_DEFAULTS rule for a gate that is not a harness lane is dead.

    ``gate_runner`` only reads ``MODEL_DEFAULTS`` from
    ``_run_harness_lane_path``, which is reached exclusively for harness-lane
    gates. An entry keyed on a path-binary gate (codex_gate, gemini_review) or
    a renamed gate could never fire, and would be the inverse drift of the
    missing-entry case: a rule nobody consumes while a real gate silently runs
    model-less.
    """
    harness_gates = _harness_lane_gates()
    orphan = sorted(set(gate_lane_contract.MODEL_DEFAULTS) - harness_gates)
    assert not orphan, (
        "a MODEL_DEFAULTS entry for a gate that is not a harness lane in "
        "GATE_PROVIDERS is dead config — gate_runner never reads it: {orphan}"
    )
