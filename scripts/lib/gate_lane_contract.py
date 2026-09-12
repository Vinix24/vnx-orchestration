"""Single source for the harness-lane gate constants (C6 step 3).

``gate_runner``, ``glm_gate`` and ``kimi_gate`` each carried their own copy of
the verdict contract and the model/timeout/diff-cap defaults. Three copies
drift silently: a writer edits one shape, misses the other two, and no test
fails because every module reads its own copy. This module is the ONE source
all three read from — the names in the consumers are aliases to these SAME
objects (``is``-identical, not merely equal), and
``tests/test_gate_lane_contract.py`` pins that identity so a byte-identical
copy can no longer sneak back in unnoticed.
"""

from __future__ import annotations

from typing import Dict

# gate name -> (model env var, default model).
#
# glm_gate runs glm-5.2 (deprecated-glm-models allowlist, operator directive
# 2026-08-03) via the glm-harness lane; kimi_gate runs kimi-k3 via the kimi
# CLI; deepseek_gate runs deepseek-v4-pro via the deepseek-harness lane
# (DEFAULT_DEEPSEEK_HARNESS_MODEL, provider_spawns/deepseek_harness_spawn.py
# — the SAME default the build lane dispatches, registry wave7_models.yaml
# `deepseek_harness.deepseek-v4-pro` with dispatch_allowed: true). The env var
# is the per-gate override an operator sets before the run; the default is
# what the governed lane dispatches when it is unset.
MODEL_DEFAULTS: Dict[str, tuple] = {
    "glm_gate": ("VNX_GLM_GATE_MODEL", "glm-5.2"),
    "kimi_gate": ("VNX_KIMI_GATE_MODEL", "kimi-k3"),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_MODEL", "deepseek-v4-pro"),
}

# glm_gate.py/kimi_gate.py drive the governed lane with DEFAULT_TIMEOUT=900.
# headless_adapter.gate_timeout() has no entry for these gates and would fall
# back to 600, cutting a run short that the standalone gate would have let
# finish. The dispatcher's timeout_seconds is the lane's own deadline, so it
# must match the standalone gate, not the runner's PATH-binary default.
TIMEOUT_SECONDS = 900

# Diff cap applied by gate_prompt.build_review_prompt: the diff is delimited,
# untrusted DATA, and this bounds how much of it enters the prompt.
MAX_DIFF_CHARS = 50000

# The verdict the gate must end its report with. Verbatim-identical across
# glm_gate, kimi_gate and gate_runner's harness-lane strategy; this is the one
# copy. Not gate_runner._REVIEWER_VERDICT_TEMPLATE — the codex/gemini reviewer
# asks for a different, richer findings shape.
VERDICT_CONTRACT = (
    "When done, end your report with a structured JSON verdict ONLY, in a fenced block:\n"
    "```json\n"
    "{\n"
    '  "verdict": "pass|fail|blocked",\n'
    '  "findings": [{"severity": "error|warning|info", "message": "..."}],\n'
    '  "residual_risk": "remaining risk or null"\n'
    "}\n"
    "```\n"
    "verdict=fail/blocked ONLY for a real, blocking correctness/security/governance issue "
    "introduced by THIS diff. Style nits are severity=info, never blocking.\n"
)
