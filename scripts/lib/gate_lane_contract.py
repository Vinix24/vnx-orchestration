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
from unified_report_schema import SchemaViolation, parse_frontmatter  # after the same path guard

logger = logging.getLogger(__name__)

# gate name -> (model env var, default model).
#
# glm_gate runs glm-5.2 (deprecated-glm-models allowlist, operator directive
# 2026-08-03) via the glm-harness lane; kimi_gate runs kimi-k3 via the kimi
# CLI; deepseek_gate runs deepseek-flash (DeepSeek V4.1-Flash, registry
# wave7_models.yaml `deepseek_harness.deepseek-flash`, operator decision
# 2026-09-30, OI-1939) via the deepseek-harness lane. That default deliberately
# differs from the build lane's DEFAULT_DEEPSEEK_HARNESS_MODEL
# (provider_spawns/deepseek_harness_spawn.py, deepseek-v4-pro). The name
# `deepseek-v4-flash` is a phased-out alias the vendor reroutes to V4.1-Flash
# and must not be used as the default. The env var is the per-gate override an
# operator sets before the run; the default is what the governed lane
# dispatches when it is unset.
MODEL_DEFAULTS: Dict[str, tuple] = {
    "glm_gate": ("VNX_GLM_GATE_MODEL", "glm-5.2"),
    "kimi_gate": ("VNX_KIMI_GATE_MODEL", "kimi-k3"),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_MODEL", "deepseek-flash"),
}

# The BASE lane deadline of a harness-lane review gate, in seconds.
# headless_adapter.gate_timeout() has no entry for these gates and would fall
# back to 600, cutting a run short that the standalone gate would have let
# finish. Since OI-2066 no reader applies this constant directly: every reader
# (gate_runner's harness lane, glm_gate, kimi_gate) resolves the deadline
# through harness_timeout_seconds() below, which starts from this base and
# extends it with the size of the diff the lane has to read.
TIMEOUT_SECONDS = 900

# OI-2066, measured 2026-10-15: one fixed 900 s did not fit a large PR. On PR
# #2058 (head ae1e193e, +2288/-207, 19 files) kimi_gate and deepseek_gate both
# stopped at 900.0 s; on the earlier head 71f92de1 (+1719) kimi finished in
# 779 s, and on PR #2057 (+1026) kimi timed out once at 900 s and finished in
# 757 s on another run. The deadline therefore grows with the diff: the first
# TIMEOUT_FREE_DIFF_CHARS cost nothing extra, every started
# TIMEOUT_STEP_DIFF_CHARS above them adds TIMEOUT_STEP_SECONDS, and
# TIMEOUT_CEILING_SECONDS bounds the result (a configured base included).
TIMEOUT_FREE_DIFF_CHARS = 100_000
TIMEOUT_STEP_DIFF_CHARS = 100_000
TIMEOUT_STEP_SECONDS = 600
TIMEOUT_CEILING_SECONDS = 3600

# The governed dispatcher runs the lane as a subprocess under an OUTER timeout
# of its own, while the lane carries an INNER deadline (the streaming drainer's
# total_deadline, set through VNX_TOTAL_DEADLINE in the child environment). The
# outer one gets this margin on top, so the lane's own deadline fires first and
# the lane still writes its own report before anything kills it from outside.
TIMEOUT_OUTER_MARGIN_SECONDS = 60

# gate name -> (config key, default base seconds). config_registry carries the
# same defaults, exactly as it does for DIFF_CHAR_CONFIG below.
TIMEOUT_CONFIG: Dict[str, tuple] = {
    "kimi_gate": ("VNX_KIMI_GATE_TIMEOUT_SECONDS", TIMEOUT_SECONDS),
    "glm_gate": ("VNX_GLM_GATE_TIMEOUT_SECONDS", TIMEOUT_SECONDS),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_TIMEOUT_SECONDS", TIMEOUT_SECONDS),
}

# Diff cap applied by gate_prompt.build_review_prompt: the diff is delimited,
# untrusted DATA, and this bounds how much of it enters the prompt.
#
# Operator decision 2026-10-03: every reviewer may read up to 500,000 tokens of
# diff by default, configurable per gate. A cut diff books partial_review,
# billed and worth nothing (PR #2032: 107,797 chars against the old 50,000 cap).
# The model windows allow it (kimi-k3 1,000,000, glm-5.2 1,048,576,
# deepseek-flash 1,000,000; wave7_models.yaml). The delivery route does not
# yet: see ARGV_SAFE_MAX_DIFF_CHARS. So all three gates share one default, the
# operator's reading budget bounded by that ceiling, and a project may lower it
# per gate. gate name -> (config key, default chars). config_registry carries
# the same defaults.

# Operator decision 2026-10-03: the reading budget per review, in tokens.
REVIEW_READ_TOKENS_TARGET = 500_000

# Characters per token on this repo's diffs, measured 2026-10-03 with tiktoken
# (cl100k_base and o200k_base) on four real diffs (#2030, #2029, #2025, #2032):
# 322,709 chars = 84,989 and 85,400 tokens, 3.78 to 3.80.
DIFF_CHARS_PER_TOKEN = 3.8

# The ceiling of the current delivery route (OI-1961). The harness-lane prompt,
# diff included, travels as ONE command-line argument at three places:
# plan_gate_panel.py ("--instruction" to provider_dispatch.py),
# subprocess_adapter.py (claude -p <instruction>) and
# provider_spawns/kimi_spawn.py (-p <prompt>). macOS ARG_MAX is 1,048,576 bytes
# for argv plus environment, and a live glm review measured about 11 KB of
# scaffold plus 4.7 KB of environment around the diff. 800,000 chars leaves
# about 23% of ARG_MAX for multi-byte text, that scaffold and the environment.
# OI-1961 (deliver the prompt by stdin or file) lifts this ceiling.
ARGV_SAFE_MAX_DIFF_CHARS = 800_000

# 800,000 today; 1,900,000 the day OI-1961 removes the argv ceiling.
DEFAULT_MAX_DIFF_CHARS = min(
    int(REVIEW_READ_TOKENS_TARGET * DIFF_CHARS_PER_TOKEN),
    ARGV_SAFE_MAX_DIFF_CHARS,
)

DIFF_CHAR_CONFIG: Dict[str, tuple] = {
    "kimi_gate": ("VNX_KIMI_GATE_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS),
    "glm_gate": ("VNX_GLM_GATE_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS),
    "deepseek_gate": ("VNX_DEEPSEEK_GATE_MAX_DIFF_CHARS", DEFAULT_MAX_DIFF_CHARS),
}

# A gate name this table does not know at all gets the same default: never
# unlimited, never a crash.
_UNKNOWN_GATE_MAX_DIFF_CHARS = DEFAULT_MAX_DIFF_CHARS


def max_diff_chars(gate: str) -> int:
    """Resolve the diff-char cap for *gate*, the ONLY place that knows both the
    per-gate config keys and their defaults (OI-1874).

    Read at runtime via ``config_runtime.get`` so a project config (or an env
    var, or the operator override brake) can lower it per gate — see
    ``config_runtime``'s precedence chain. A gate this table does not carry
    (``_UNKNOWN_GATE_MAX_DIFF_CHARS``), a missing/unset config value, and an
    invalid or non-positive config value (plus a logged warning) all resolve
    to the shared default. A valid value above ``ARGV_SAFE_MAX_DIFF_CHARS`` is
    clamped to it with one warning, because the argv route cannot spawn a
    larger prompt (OI-1961). The result is always bounded and positive — never
    "no limit", and never by raising.
    """
    entry = DIFF_CHAR_CONFIG.get(gate)
    if entry is None:
        logger.warning(
            "gate_lane_contract.max_diff_chars: unknown gate %r, falling back "
            "to the default %d", gate, _UNKNOWN_GATE_MAX_DIFF_CHARS,
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
    if value > ARGV_SAFE_MAX_DIFF_CHARS:
        logger.warning(
            "gate_lane_contract.max_diff_chars: config value %d for %s exceeds "
            "the argv ceiling %d, clamped to it (the prompt travels as one "
            "command-line argument, OI-1961)", value, key, ARGV_SAFE_MAX_DIFF_CHARS,
        )
        return ARGV_SAFE_MAX_DIFF_CHARS
    return value


def _timeout_base_seconds(gate: str) -> int:
    """The base lane deadline for *gate*: its configured value, or the default.

    Same fallbacks as :func:`max_diff_chars`: an unknown gate, an invalid value
    and a non-positive value each log one warning and resolve to
    ``TIMEOUT_SECONDS``; an unset value resolves to the gate's default quietly.
    """
    entry = TIMEOUT_CONFIG.get(gate)
    if entry is None:
        logger.warning(
            "gate_lane_contract.harness_timeout_seconds: unknown gate %r, base "
            "falls back to the default %d", gate, TIMEOUT_SECONDS,
        )
        return TIMEOUT_SECONDS
    key, default = entry
    raw = config_runtime.get(key)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning(
            "gate_lane_contract.harness_timeout_seconds: invalid config value %r "
            "for %s, falling back to the %s default %d", raw, key, gate, default,
        )
        return default
    if value <= 0:
        logger.warning(
            "gate_lane_contract.harness_timeout_seconds: non-positive config value "
            "%d for %s, falling back to the %s default %d", value, key, gate, default,
        )
        return default
    return value


def harness_timeout_seconds(gate: str, diff_chars: int) -> int:
    """Resolve the lane deadline, in seconds, for a *gate* run over *diff_chars* (OI-2066).

    The ONLY place that knows the per-gate config keys, the default, the
    diff-size extension and the ceiling — the same one-resolver pattern as
    :func:`max_diff_chars` (OI-1874). *diff_chars* is the length of the diff
    that actually enters the prompt, after the diff cap.

    The base comes from ``config_runtime.get`` (``TIMEOUT_CONFIG``), so a
    project config, an env var or the operator override brake can set it per
    gate. Every started ``TIMEOUT_STEP_DIFF_CHARS`` above the first
    ``TIMEOUT_FREE_DIFF_CHARS`` adds ``TIMEOUT_STEP_SECONDS``, also on top of a
    fallback base. ``TIMEOUT_CEILING_SECONDS`` bounds the result, a configured
    base included, with one warning when it clamps. A diff length that is not
    a number counts as 0, with a warning.

    The result is always a positive int — never "no limit", and never by
    raising.
    """
    base = _timeout_base_seconds(gate)
    try:
        chars = max(0, int(diff_chars))
    except (TypeError, ValueError):
        logger.warning(
            "gate_lane_contract.harness_timeout_seconds: diff length %r for %s is "
            "not a number, counted as 0", diff_chars, gate,
        )
        chars = 0
    over = max(0, chars - TIMEOUT_FREE_DIFF_CHARS)
    steps = -(-over // TIMEOUT_STEP_DIFF_CHARS)  # every STARTED step counts
    value = base + steps * TIMEOUT_STEP_SECONDS
    if value > TIMEOUT_CEILING_SECONDS:
        logger.warning(
            "gate_lane_contract.harness_timeout_seconds: %s deadline %d s (base %d s, "
            "diff %d chars) exceeds the ceiling, clamped to %d s",
            gate, value, base, chars, TIMEOUT_CEILING_SECONDS,
        )
        return TIMEOUT_CEILING_SECONDS
    return value


def harness_dispatcher_timeouts(
    gate: str, diff_chars: int, *, explicit: "int | None" = None,
) -> Dict[str, int]:
    """The two deadlines a review gate hands ``plan_gate_panel._make_default_dispatcher``.

    Returned as keyword arguments: ``total_deadline_seconds`` is the lane's own
    deadline (the drainer's, set in the child environment), ``timeout_seconds``
    the outer subprocess timeout with ``TIMEOUT_OUTER_MARGIN_SECONDS`` on top.
    *explicit* — an operator's ``--timeout`` on a standalone gate — replaces
    the resolved lane deadline when given; it is not clamped, because the
    operator typed it for this one run. A non-positive *explicit* is ignored
    with a warning and the resolver decides.
    """
    if explicit is not None and explicit <= 0:
        logger.warning(
            "gate_lane_contract.harness_dispatcher_timeouts: non-positive explicit "
            "timeout %r for %s ignored, resolving from the diff instead", explicit, gate,
        )
        explicit = None
    lane = explicit if explicit is not None else harness_timeout_seconds(gate, diff_chars)
    return {
        "timeout_seconds": lane + TIMEOUT_OUTER_MARGIN_SECONDS,
        "total_deadline_seconds": lane,
    }


# OI-2066: the most a dispatcher-failure description may put in a result
# record. Measured 2026-10-15 on PR #2058: str(subprocess.TimeoutExpired) is
# the whole argv, and the `--instruction` value in it carries the diff under
# review (163,891 chars, a 513 KB result file). gate_recorder copies
# reason_detail into summary and failure_reason, and scans it for provider
# quota markers — it found two test fixtures inside that diff and booked the
# timeout as a spent quota. A description is a sentence, never a payload.
DISPATCH_ERROR_DETAIL_MAX_CHARS = 2000


def _program_name(cmd: object) -> str:
    """The program a subprocess command ran — the script when a python
    interpreter ran one — and never any of its arguments."""
    parts = list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd or "").split()
    names = [Path(str(part)).name for part in parts[:2]]
    if not names:
        return "<unknown program>"
    if len(names) > 1 and names[0].startswith("python"):
        return names[1]
    return names[0]


def describe_dispatch_exception(exc: BaseException, *, prompt: str = "") -> str:
    """Describe a governed-dispatcher exception without its argv or the prompt.

    An exception that carries a ``cmd`` (``subprocess.TimeoutExpired``,
    ``subprocess.CalledProcessError``, any ``SubprocessError`` like them) is
    described from its fields: the type, the program name, and the timeout or
    the return code — never ``exc.cmd`` in full, never its captured output.
    Any other exception keeps its type and message, with every occurrence of
    *prompt* (raw, and as it appears inside a list repr) replaced by its
    length. The result is bounded to ``DISPATCH_ERROR_DETAIL_MAX_CHARS``.
    """
    kind = type(exc).__name__
    cmd = getattr(exc, "cmd", None)
    if cmd is not None:
        text = f"{kind}: {_program_name(cmd)}"
        timeout = getattr(exc, "timeout", None)
        if timeout is not None:
            text += f" timed out after {timeout} s"
        returncode = getattr(exc, "returncode", None)
        if returncode is not None:
            text += f" exited with code {returncode}"
    else:
        message = str(exc)
        if prompt:
            marker = f"<prompt: {len(prompt)} chars>"
            message = message.replace(prompt, marker).replace(repr(prompt)[1:-1], marker)
        text = f"{kind}: {message}"
    if len(text) > DISPATCH_ERROR_DETAIL_MAX_CHARS:
        suffix = " ... [truncated]"
        text = text[: DISPATCH_ERROR_DETAIL_MAX_CHARS - len(suffix)] + suffix
    return text


def _frontmatter_number(value: object) -> "float | None":
    """A frontmatter value as a float when it is a plain number, else None (a bool is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def lane_deadline_detail(report_text: str, elapsed: "float | None" = None) -> "str | None":
    """One line saying the lane's OWN deadline stopped the run, or None (OI-2066).

    The ONE reader of that signal. gate_runner's harness lane, glm_gate,
    kimi_gate, the plan-gate seat reader, its tiebreaker and the companion
    report search all call it before they read a verdict: a report the lane cut
    off can still hold a partial verdict block, and a review that never
    finished must not count as one. Measured on 4ac2669a (codex): glm_gate and
    kimi_gate booked ``pass`` with exit 0 on a deadline-killed report whose
    body held a partial pass block.

    The signal is the lane's explicit statement, never an inference:
    ``deadline_exceeded: true`` in the report frontmatter, which
    provider_dispatch stamps (``_deadline_frontmatter``, with
    ``deadline_seconds``) only when the code that killed the run did so
    because its total deadline ran out — the drainer for kimi, the claude
    harness adapter for glm and deepseek. Unreadable frontmatter, or any
    other value than ``True``, returns None and the report keeps its old
    reading, quota detection included.

    Duration is never the deciding signal (codex on 595c3e8e): deepseek's
    ``duration_seconds`` includes prompt and worktree setup, while its timer
    starts after delivery — a quota failure after 20 s of setup and 890 s of
    work "lasted" a 900 s deadline that never fired. The lane's
    ``duration_seconds`` (or *elapsed* when the report carries none) goes into
    the line as information only. The line is built from numbers only, never
    from the report's text.
    """
    if not report_text:
        return None
    try:
        frontmatter = parse_frontmatter(report_text)
    except SchemaViolation:
        return None
    if frontmatter.get("deadline_exceeded") is not True:
        return None
    deadline = _frontmatter_number(frontmatter.get("deadline_seconds"))
    ran = _frontmatter_number(frontmatter.get("duration_seconds"))
    if ran is None:
        ran = elapsed
    exit_code = frontmatter.get("exit_code")
    of_deadline = f" of {deadline:.0f} s" if deadline is not None else ""
    ran_note = f"; it ran {ran:.0f} s" if ran is not None else ""
    exit_note = (
        f" (report exit_code={exit_code})"
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) else ""
    )
    return f"the lane reported its own deadline{of_deadline} stopped the run{ran_note}{exit_note}"


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

# Review gates also meet a verdict word that is not in VALID_VERDICTS. Operator
# decision 2026-09-30 (OI-1938): a REVISE is a fail. The map is closed: it holds
# only the word measured on real deepseek_gate reports. `approve`, `block`,
# `changes_requested` and anything else stay refused. VALID_VERDICTS itself is
# unchanged (glm_gate/kimi_gate and the plan gate read their own vocabulary).
REVIEW_VERDICT_ALIASES: Dict[str, str] = {"revise": "fail"}


def canonical_review_verdict(value: object) -> str:
    """Trim and lowercase *value* and return a member of :data:`VALID_VERDICTS`.

    A word in :data:`REVIEW_VERDICT_ALIASES` is mapped first. Returns ``""`` for
    anything else (the echoed placeholder ``pass|fail|blocked``, an unknown word,
    a non-string value).
    """
    word = str(value if value is not None else "").strip().lower()
    word = REVIEW_VERDICT_ALIASES.get(word, word)
    return word if word in VALID_VERDICTS else ""
