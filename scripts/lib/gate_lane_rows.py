"""The harness-lane gate rows: one registration per gate, stdlib only (review-lane P2).

ADR-046 Amendment 1, section A3: every ``harness_lane`` gate is ONE row here,
and the tables that used to be written out by hand per gate
(``gate_lane_contract.MODEL_DEFAULTS``, ``SINGLE_SHOT_GATES``,
``TIMEOUT_CONFIG`` and ``DIFF_CHAR_CONFIG``) are views derived from the rows.

This module imports the standard library and nothing else. ``dispatch_spec``
and ``gate_billing_table`` are stdlib-only and must stay so, which means they
can only read the rows from a module that is stdlib-only itself.
``gate_lane_contract`` imports ``config_runtime``; it reads from here, never
the other way round.

A row is validated when it is built, so a bad row stops the import of this
module and not the first review that happens to use it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Tuple

# The billing classes a gate can have. ``gate_billing_table`` carries the same
# three values; tests/test_gate_lane_rows.py pins that the two sets are equal.
# They are defined here and not imported, because P2b makes gate_billing_table
# read these rows.
BILLING_SUBSCRIPTION = "subscription"
BILLING_METERED = "provider_metered"
BILLING_NONE = "none"
VALID_BILLING: FrozenSet[str] = frozenset(
    {BILLING_SUBSCRIPTION, BILLING_METERED, BILLING_NONE}
)

# The base lane deadline of a harness-lane review gate, in seconds.
DEFAULT_TIMEOUT_SECONDS = 900

# Operator decision 2026-10-03: every reviewer may read up to 500,000 tokens of
# diff by default. 3.8 characters per token was measured on this repo's diffs.
# The argv delivery route bounds it: the prompt travels as ONE command-line
# argument and macOS ARG_MAX is 1,048,576 bytes (OI-1961).
REVIEW_READ_TOKENS_TARGET = 500_000
DIFF_CHARS_PER_TOKEN = 3.8
ARGV_SAFE_MAX_DIFF_CHARS = 800_000
DEFAULT_MAX_DIFF_CHARS = min(
    int(REVIEW_READ_TOKENS_TARGET * DIFF_CHARS_PER_TOKEN),
    ARGV_SAFE_MAX_DIFF_CHARS,
)


class GateLaneRowError(ValueError):
    """A gate lane row that cannot be registered. The message names row and column."""


@dataclass(frozen=True)
class GateLaneRow:
    """One harness-lane gate, every column of the A3 table.

    ``request_provider`` is the label on a gate request (``kimi``, ``glm``,
    ``deepseek``, ``claude``). ``stop_conditions`` and ``provider_costs`` match
    on it byte for byte, and the registry lane is named differently
    (``glm-harness``), so it is a column of its own and never derived from
    ``lane``. ``dispatcher`` is the dispatcher key; it is data in this PR.
    ``family`` is the provider family the switch decides on (``provider_switch``);
    it has no default, so a row that does not name one is refused when built.
    """

    gate: str
    lane: str
    request_provider: str
    dispatcher: str
    billing: str
    model_env: str
    model_default: str
    single_shot: bool
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    diff_chars: int = DEFAULT_MAX_DIFF_CHARS
    family: str = ""

    def __post_init__(self) -> None:
        _validate_row(self)


def _fail(row: "GateLaneRow", column: str, why: str) -> GateLaneRowError:
    return GateLaneRowError(
        f"gate lane row {row.gate!r} (lane {row.lane!r}): column {column!r} {why}"
    )


def _validate_text_columns(row: "GateLaneRow") -> None:
    for column in (
        "gate", "lane", "request_provider", "dispatcher", "model_env", "model_default",
        "family",
    ):
        value = getattr(row, column)
        if not isinstance(value, str) or not value.strip():
            raise _fail(row, column, f"must be a non-empty string, got {value!r}")


def _known_families() -> FrozenSet[str]:
    """The families a row may carry: the switchable ones plus claude and codex.

    Imported when a row is built and not at the top: provider_switch reads this module
    too, and the shipped rows are built while this module is still being imported.
    """
    import provider_switch

    return frozenset(provider_switch.SWITCHABLE_FAMILIES) | {
        provider_switch.CLAUDE, provider_switch.CODEX,
    }


def _validate_family(row: "GateLaneRow") -> None:
    known = _known_families()
    if row.family not in known:
        raise _fail(row, "family", f"must be one of {sorted(known)}, got {row.family!r}")


def _validate_number_columns(row: "GateLaneRow") -> None:
    for column in ("timeout", "diff_chars"):
        value = getattr(row, column)
        # bool is an int subclass; True would pass as 1 second.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise _fail(row, column, f"must be a positive integer, got {value!r}")
    if not isinstance(row.single_shot, bool):
        raise _fail(row, "single_shot", f"must be a bool, got {row.single_shot!r}")


def _validate_row(row: "GateLaneRow") -> None:
    _validate_text_columns(row)
    if row.billing not in VALID_BILLING:
        raise _fail(
            row, "billing",
            f"must be one of {sorted(VALID_BILLING)}, got {row.billing!r}",
        )
    _validate_family(row)
    _validate_number_columns(row)


def validate_rows(rows: Iterable[GateLaneRow]) -> Tuple[GateLaneRow, ...]:
    """The rows as a tuple, refused when two of them share a ``gate``.

    Each row validated itself when it was built; what only the whole set can
    know, a duplicate gate, is checked here.
    """
    rows = tuple(rows)
    seen: Dict[str, GateLaneRow] = {}
    for row in rows:
        if not isinstance(row, GateLaneRow):
            raise GateLaneRowError(f"gate lane rows hold GateLaneRow only, got {row!r}")
        if row.gate in seen:
            raise _fail(row, "gate", "is registered twice")
        seen[row.gate] = row
    return rows


# The four harness-lane gates today. glm_gate runs glm-5.2 (deprecated-glm-models
# allowlist, operator directive 2026-08-03) through the glm-harness lane;
# kimi_gate runs kimi-k3 through the kimi CLI; deepseek_gate runs deepseek-flash
# (V4.1-Flash, operator decision 2026-09-30, OI-1939), never the phased-out alias
# deepseek-v4-flash; claude_gate runs sonnet on the operator's subscription (R7,
# operator decision 2026-10-22) through claude_gate_lane. glm_gate and kimi_gate
# are the single-shot gates (R12): the diff in the prompt is what the run is held
# to. deepseek_gate and claude_gate review with tools in a checkout of the PR head.
GATE_LANE_ROWS: Tuple[GateLaneRow, ...] = validate_rows((
    GateLaneRow(
        gate="kimi_gate", lane="kimi", request_provider="kimi",
        dispatcher="provider_dispatch", billing=BILLING_SUBSCRIPTION,
        model_env="VNX_KIMI_GATE_MODEL", model_default="kimi-k3",
        single_shot=True,
        family="kimi",
    ),
    GateLaneRow(
        gate="glm_gate", lane="glm-harness", request_provider="glm",
        dispatcher="provider_dispatch", billing=BILLING_METERED,
        model_env="VNX_GLM_GATE_MODEL", model_default="glm-5.2",
        single_shot=True,
        family="glm",
    ),
    GateLaneRow(
        gate="deepseek_gate", lane="deepseek-harness", request_provider="deepseek",
        dispatcher="provider_dispatch", billing=BILLING_METERED,
        model_env="VNX_DEEPSEEK_GATE_MODEL", model_default="deepseek-flash",
        single_shot=False,
        family="deepseek",
    ),
    GateLaneRow(
        gate="claude_gate", lane="claude", request_provider="claude",
        dispatcher="claude_gate_lane", billing=BILLING_SUBSCRIPTION,
        model_env="VNX_CLAUDE_GATE_MODEL", model_default="sonnet",
        single_shot=False,
        family="claude",
    ),
))


def timeout_config_key(gate: str) -> str:
    """The config key of the base lane deadline of *gate*."""
    return f"VNX_{gate.upper()}_TIMEOUT_SECONDS"


def diff_chars_config_key(gate: str) -> str:
    """The config key of the diff-char cap of *gate*."""
    return f"VNX_{gate.upper()}_MAX_DIFF_CHARS"


def model_defaults(rows: Iterable[GateLaneRow]) -> Dict[str, tuple]:
    """gate -> (model env var, default model)."""
    return {r.gate: (r.model_env, r.model_default) for r in rows}


def single_shot_gates(rows: Iterable[GateLaneRow]) -> FrozenSet[str]:
    """The gates measured as one shot (R12)."""
    return frozenset(r.gate for r in rows if r.single_shot)


def timeout_config(rows: Iterable[GateLaneRow]) -> Dict[str, tuple]:
    """gate -> (config key, default base seconds)."""
    return {r.gate: (timeout_config_key(r.gate), r.timeout) for r in rows}


def diff_char_config(rows: Iterable[GateLaneRow]) -> Dict[str, tuple]:
    """gate -> (config key, default chars)."""
    return {r.gate: (diff_chars_config_key(r.gate), r.diff_chars) for r in rows}


def family_of_gate(gate: str, rows: Iterable[GateLaneRow]) -> "str | None":
    """The family column of the row registered for *gate*, or ``None`` when no row is."""
    for row in rows:
        if row.gate == gate:
            return row.family
    return None
