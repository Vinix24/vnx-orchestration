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

import logging
import sys
from pathlib import Path
from typing import Dict

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import config_runtime  # noqa: E402  (after the scripts/lib path guard above)

logger = logging.getLogger(__name__)

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
#
# OI-1874: one fixed cap shared by all three harness-lane gates was never
# chosen per model. kimi-k3 carries a 1M-token context (wave7_models.yaml) and
# runs on the kimi CLI OAuth subscription, so raising its cap costs nothing;
# glm_gate (OpenRouter) and deepseek_gate (deepseek-harness) are API-credit
# fallback seats and stay at the original conservative cap. gate name -> (config
# key, default chars). config_registry carries the same defaults and the
# per-gate rationale; a project may raise or lower either via config or env.
DIFF_CHAR_CONFIG: Dict[str, tuple] = {
    "kimi_gate": ("VNX_KIMI_GATE_MAX_DIFF_CHARS", 400000),
    "glm_gate": ("VNX_GLM_GATE_MAX_DIFF_CHARS", 50000),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS", 50000),
}

# The conservative fallback for a gate name this table does not know at all —
# never unlimited, never a crash.
_UNKNOWN_GATE_MAX_DIFF_CHARS = 50000


def max_diff_chars(gate: str) -> int:
    """Resolve the diff-char cap for *gate*, the ONLY place that knows both the
    per-gate config keys and their defaults (OI-1874).

    Read at runtime via ``config_runtime.get`` so a project config (or an env
    var, or the operator override brake) can raise or lower it per gate — see
    ``config_runtime``'s precedence chain. A gate this table does not carry
    (``_UNKNOWN_GATE_MAX_DIFF_CHARS``), a missing/unset config value (falls
    back to that gate's own default), and an invalid or non-positive config
    value (same fallback, plus a logged warning) all resolve to a bounded,
    positive cap — never to "no limit", and never by raising.
    """
    entry = DIFF_CHAR_CONFIG.get(gate)
    if entry is None:
        logger.warning(
            "gate_lane_contract.max_diff_chars: unknown gate %r, falling back "
            "to the conservative default %d", gate, _UNKNOWN_GATE_MAX_DIFF_CHARS,
        )
        return _UNKNOWN_GATE_MAX_DIFF_CHARS
    key, default = entry
    raw = config_runtime.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "gate_lane_contract.max_diff_chars: invalid config value %r for "
            "%s, falling back to the %s default %d", raw, key, gate, default,
        )
        return default
    if value <= 0:
        logger.warning(
            "gate_lane_contract.max_diff_chars: non-positive config value %d "
            "for %s, falling back to the %s default %d", value, key, gate, default,
        )
        return default
    return value


# The verdict the gate must end its report with. Verbatim-identical across
# glm_gate, kimi_gate and gate_runner's harness-lane strategy; this is the one
# copy. Not gate_runner._REVIEWER_VERDICT_TEMPLATE — the codex/gemini reviewer
# asks for a different, richer findings shape.
VERDICT_CONTRACT = (
    "When done, end your report with a structured JSON verdict ONLY, in a fenced block:\n"
    "```json\n"
    "{\n"
    '  "verdict": "pass|fail|blocked",\n'
    '  "findings": [{"severity": "error|warning|info", "message": "...", "file_path": "...", "line": 0}],\n'
    '  "residual_risk": "remaining risk or null"\n'
    "}\n"
    "```\n"
    "verdict=fail/blocked ONLY for a real, blocking correctness/security/governance issue "
    "introduced by THIS diff. Style nits are severity=info, never blocking.\n"
    'file_path and line point at the ONE new line a finding is about, repo-relative. Leave '
    'file_path="" and line=0 for a finding about the PR as a whole, a missing file, or '
    "anything that does not point at a single line — a guessed line number is worse than an "
    "empty one.\n"
)

# The verdict values VERDICT_CONTRACT's own "verdict" field allows. A report
# that echoes the contract's placeholder text verbatim ("pass|fail|blocked",
# the literal template string above) must never be mistaken for a real
# decision — glm_gate, kimi_gate and codex_parser.extract_verdict_block all
# validate a candidate verdict block against this SAME set (previously three
# independent literals; OI-1767 fix-forward, this module already being the
# one source for the contract those values gate).
VALID_VERDICTS = frozenset({"pass", "fail", "blocked"})
