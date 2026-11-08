"""A new review provider is one registry row (review-lane P8, ADR-046 Amendment 1).

Goal point 5 of track provider-agnostic-review-lane: a new provider is a registry
row plus a runner, and nothing in the engine changes. P1 to P6 each claimed a
piece of that alone; this file proves the pieces together.

A synthetic harness-lane gate is registered through the seam the merged rows use:
a row in ``gate_lane_rows.GATE_LANE_ROWS`` plus its ``gate_recorder.GATE_PROVIDERS``
kind. The views the modules derive from the rows at import time are re-derived
with the modules' own derivation functions, so the row stays the only input.
Six stations then answer for it: billing, lane config, request, availability,
closure and readiness.

The second half is a ratchet. The number of production files under ``scripts/``
that hold a literal table of gate names may not grow. The detector reads the
syntax tree, so a docstring or a comment is not a table.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Set

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (REPO_ROOT / "scripts" / "lib", REPO_ROOT / "scripts", REPO_ROOT / "tests"):
    sys.path.insert(0, str(_p))

import closure_verifier as cv
import dispatch_spec
import gate_billing_table
import gate_lane_contract
import gate_lane_rows
import gate_recorder
import gate_request_handler
import pr_ready
import pr_readiness
import review_stack_support
from review_contract import ReviewContract

GATE = "conformance_gate"
LANE = "conformance-lane"
LABEL = "conformance"
MODEL_ENV = "VNX_CONFORMANCE_GATE_MODEL"
MODEL = "conformance-1"
PR = 5108
BRANCH = "feat/p8-conformance"
HEAD = "d" * 40


def _synthetic_row() -> gate_lane_rows.GateLaneRow:
    return gate_lane_rows.GateLaneRow(
        gate=GATE, lane=LANE, request_provider=LABEL, dispatcher="provider_dispatch",
        billing=gate_lane_rows.BILLING_METERED, model_env=MODEL_ENV, model_default=MODEL,
        single_shot=False, timeout=777, diff_chars=12_345,
    )


@pytest.fixture
def registered(monkeypatch):
    """The synthetic row, registered for this test only."""
    rows = gate_lane_rows.validate_rows((*gate_lane_rows.GATE_LANE_ROWS, _synthetic_row()))
    monkeypatch.setattr(gate_lane_rows, "GATE_LANE_ROWS", rows)
    monkeypatch.setattr(gate_request_handler, "GATE_LANE_ROWS", rows)
    monkeypatch.setitem(
        gate_recorder.GATE_PROVIDERS, GATE, (gate_recorder.GATE_PROVIDER_HARNESS_LANE, LANE)
    )
    monkeypatch.setattr(gate_billing_table, "GATE_BILLING", gate_billing_table.billing_from_rows(rows))
    monkeypatch.setattr(gate_lane_contract, "MODEL_DEFAULTS", gate_lane_rows.model_defaults(rows))
    monkeypatch.setattr(gate_lane_contract, "SINGLE_SHOT_GATES", gate_lane_rows.single_shot_gates(rows))
    monkeypatch.setattr(gate_lane_contract, "TIMEOUT_CONFIG", gate_lane_rows.timeout_config(rows))
    monkeypatch.setattr(gate_lane_contract, "DIFF_CHAR_CONFIG", gate_lane_rows.diff_char_config(rows))
    known = frozenset(
        value for value in dispatch_spec.gate_members(rows).values()
        if value not in cv._GATES_NOT_IMPLEMENTED_BY_CLOSURE
    ) | dispatch_spec.RETIRED_GATE_NAMES
    monkeypatch.setattr(cv, "_KNOWN_GATES", known)
    return rows


@pytest.fixture
def store(tmp_path, monkeypatch):
    isolation = review_stack_support.isolate_config_registry(monkeypatch)
    next(isolation)
    monkeypatch.setattr(gate_request_handler, "get_pr_base_branch", lambda _pr: "")
    monkeypatch.setattr(gate_request_handler, "get_pr_head_sha", lambda _pr: HEAD)
    yield review_stack_support.build_project_store(tmp_path, monkeypatch)
    next(isolation, None)


def _state_dir() -> Path:
    import os
    return Path(os.environ["VNX_STATE_DIR"])


def _manager():
    import review_gate_manager
    return review_gate_manager.ReviewGateManager()


def _request_the_row(available: bool = True) -> dict:
    return _manager()._request_harness_seat(
        GATE, available, pr_number=PR, branch=BRANCH, risk_class="high",
        changed_files=["scripts/a.py"], mode="standard", dispatch_id="dlv-p8",
    )


# ---------------------------------------------------------------------------
# The six stations
# ---------------------------------------------------------------------------


def test_station_1_billing_gives_the_row_a_billing_entry(registered):
    assert gate_billing_table.gate_billing(GATE) == gate_lane_rows.BILLING_METERED
    assert gate_billing_table.GATE_BILLING[GATE] == gate_lane_rows.BILLING_METERED
    # A metered gate ahead of a subscription gate is the violation the table exists to see.
    assert gate_billing_table.metered_before_subscription([GATE, "codex_gate"]) == (GATE, "codex_gate")
    assert gate_billing_table.metered_before_subscription(["codex_gate", GATE]) is None


def test_station_2_lane_config_views_all_hold_the_row(registered):
    assert gate_lane_contract.MODEL_DEFAULTS[GATE] == (MODEL_ENV, MODEL)
    assert gate_lane_contract.gate_model(GATE) == MODEL
    assert GATE not in gate_lane_contract.SINGLE_SHOT_GATES
    assert gate_lane_contract.reviews_with_tools(GATE) is True
    assert gate_lane_contract.TIMEOUT_CONFIG[GATE] == (gate_lane_rows.timeout_config_key(GATE), 777)
    assert gate_lane_contract.DIFF_CHAR_CONFIG[GATE] == (gate_lane_rows.diff_chars_config_key(GATE), 12_345)
    assert gate_lane_contract.max_diff_chars(GATE) == 12_345
    assert dispatch_spec.Gate is not None and GATE in dispatch_spec.gate_members(registered).values()


def test_station_3_request_builds_a_payload_through_the_one_seat_path(registered, store):
    payload = _request_the_row()
    kimi = _manager()._request_harness_seat(
        "kimi_gate", True, pr_number=PR, branch=BRANCH, risk_class="high",
        changed_files=["scripts/a.py"], mode="standard", dispatch_id="dlv-p8",
    )

    assert payload["gate"] == GATE
    assert payload["provider"] == LABEL
    assert payload["status"] == "requested"
    assert payload["report_path"] and GATE in payload["report_path"]
    assert sorted(payload) == sorted(kimi)
    on_disk = json.loads(_manager()._request_path(GATE, PR).read_text(encoding="utf-8"))
    assert on_disk["origin"] == "door"


def test_station_3_an_unavailable_row_has_no_report_path(registered, store):
    payload = _request_the_row(available=False)

    assert payload["status"] == "not_executable"
    assert payload["report_path"] == ""
    assert payload["provider"] == LABEL


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OI: provider_switch.family_of has no path from a gate row to a family, so a new "
        "gate is refused as an unknown identifier and gate_recorder.gate_is_available "
        "answers False for a registered harness-lane row (scripts/lib/provider_switch.py "
        "_EXACT/_PREFIXES). Acceptance: the family comes from a column of the row."
    ),
)
def test_station_4_availability_answers_true_for_a_registered_row(registered, store):
    assert gate_recorder.gate_is_available(GATE, state_dir=_state_dir()) is True


def test_station_4_availability_answers_for_the_row_and_never_names_another_gate(registered, store):
    answer = gate_recorder.gate_is_available(GATE, state_dir=_state_dir())

    assert isinstance(answer, bool)
    refusal = gate_recorder.gate_switch_refusal(GATE, state_dir=_state_dir())
    assert refusal is None or refusal.identifier == GATE


def test_station_4_an_unregistered_gate_still_fails_loud(registered, store):
    with pytest.raises(gate_billing_table.UnknownGateProvider):
        gate_recorder.gate_is_available("never_registered_gate", state_dir=_state_dir())


def test_station_5_closure_returns_a_check_result_through_the_generic_handler(registered, tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# report\nNothing to flag.\n", encoding="utf-8")
    record = {
        "gate": GATE, "status": "pass", "pr_id": "PR-1", "branch": BRANCH, "commit_sha": HEAD,
        "contract_hash": "abcdef1234567890", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [],
    }
    contract = ReviewContract(
        pr_id="PR-1", branch=BRANCH, risk_class="medium", review_stack=[GATE],
        content_hash="abcdef1234567890",
    )

    assert GATE not in cv._GATE_HANDLERS
    check = cv._check_single_gate(GATE, contract, record, tmp_path, BRANCH)

    assert isinstance(check, cv.CheckResult)
    assert check.name == f"gate_{GATE}"
    assert check.status == "PASS", check.detail


@pytest.mark.parametrize(
    "mutation",
    [{"contract_hash": None}, {"report_path": ""}, {"status": "unknown"}, {"status": None}],
    ids=["hash-none", "report-missing", "status-unknown", "status-none"],
)
def test_station_5_closure_stays_blocking_on_weak_evidence(registered, tmp_path, mutation):
    report = tmp_path / "report.md"
    report.write_text("# report\n", encoding="utf-8")
    record = {
        "gate": GATE, "status": "pass", "pr_id": "PR-1", "branch": BRANCH, "commit_sha": HEAD,
        "contract_hash": "abcdef1234567890", "report_path": str(report),
        "blocking_findings": [], "advisory_findings": [], **mutation,
    }
    contract = ReviewContract(
        pr_id="PR-1", branch=BRANCH, risk_class="medium", review_stack=[GATE],
        content_hash="abcdef1234567890",
    )

    assert cv._check_single_gate(GATE, contract, record, tmp_path, BRANCH).status != "PASS"


def test_station_6_readiness_counts_the_door_request_as_declared(registered, store, monkeypatch):
    import review_seat_evaluation

    _request_the_row()

    def fill(seat):
        return SimpleNamespace(
            seat=seat, filled=True, reviewer=seat, route="own", message="ok", record=None,
            evidence_via="",
        )

    monkeypatch.setattr(
        pr_readiness, "fetch_pr_facts",
        lambda *a, **k: {"headRefOid": HEAD, "headRefName": BRANCH, "state": "OPEN"},
    )
    monkeypatch.setattr(pr_readiness.ci_contexts, "evaluate_commit", lambda *a, **k: [])
    monkeypatch.setattr(
        review_seat_evaluation, "evaluate_review_seats",
        lambda *a, **k: SimpleNamespace(
            required=SimpleNamespace(error=None), fills=[fill(GATE)], blocking=[],
        ),
    )

    report = pr_readiness.assess(PR, _state_dir().parent, _state_dir())

    assert report.door_requested_gates == [GATE]
    assert {g.gate: g.declared for g in report.gates} == {GATE: True}
    assert "[off the door" not in pr_ready._gate_line(report)


def test_station_6_a_request_without_the_door_stamp_stays_off_the_door(registered, store):
    _request_the_row()
    path = _manager()._request_path(GATE, PR)
    record = json.loads(path.read_text(encoding="utf-8"))
    for origin in (None, "Door", "shim", 1):
        record["origin"] = origin
        path.write_text(json.dumps(record), encoding="utf-8")
        assert pr_readiness.door_requested_gates_for_pr(path.parent, PR) == []
    del record["origin"]
    path.write_text(json.dumps(record), encoding="utf-8")
    assert pr_readiness.door_requested_gates_for_pr(path.parent, PR) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OI: gate_dispatch_table.LANE_DISPATCHER_KEYS is a literal lane -> dispatcher table "
        "(scripts/lib/gate_dispatch_table.py); the row's dispatcher column is data only. "
        "Acceptance: unresolved_detail(gate, lane) reads the dispatcher from the row."
    ),
)
def test_station_7_the_dispatch_table_resolves_the_rows_lane(registered):
    import gate_dispatch_table

    assert gate_dispatch_table.unresolved_detail(GATE, LANE) is None


# ---------------------------------------------------------------------------
# The ratchet: files with a literal table of gate names
# ---------------------------------------------------------------------------

GATE_NAMES = frozenset({
    "codex_gate", "kimi_gate", "glm_gate", "deepseek_gate", "claude_gate", "gemini_review",
})

# Measured on origin/main (see the dispatch report). A lower count passes and
# the failure message says to lower these; a file that is not in the list fails.
LITERAL_TABLE_CEILING = 16
LITERAL_TABLE_FILES: List[str] = [
    "scripts/closure_verifier.py",
    "scripts/gate_reanchor_cli.py",
    "scripts/gate_runner.py",
    "scripts/lib/domain_plan_validator.py",
    "scripts/lib/gate_billing_table.py",
    "scripts/lib/gate_lane_rows.py",
    "scripts/lib/gate_recorder.py",
    "scripts/lib/gate_result_parser.py",
    "scripts/lib/ghost_receipt_filter.py",
    "scripts/lib/governance_profiles.py",
    "scripts/lib/governed_lane.py",
    "scripts/lib/headless_adapter.py",
    "scripts/lib/prior_round_injector.py",
    "scripts/lib/provider_switch.py",
    "scripts/lib/smart_router.py",
    "scripts/lib/stop_conditions.py",
]


def _names_in(node: ast.AST) -> Set[str]:
    return {
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in GATE_NAMES
    }


def _compared_name(test: ast.AST) -> Set[str]:
    """Gate names a comparison or a boolean of comparisons tests for equality."""
    found: Set[str] = set()
    for n in ast.walk(test):
        if isinstance(n, ast.Compare):
            for side in (n.left, *n.comparators):
                if isinstance(side, ast.Constant) and side.value in GATE_NAMES:
                    found.add(side.value)
    return found


def _chain_names(node: ast.If) -> Set[str]:
    names = _compared_name(node.test)
    if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        names |= _chain_names(node.orelse[0])
    return names


def holds_literal_gate_table(source: str) -> bool:
    """True when ``source`` has a container or if/elif chain naming two or more gates.

    Raises ``SyntaxError`` on source that does not parse: an unreadable file is
    never counted as clean.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple)) and len(_names_in(node)) >= 2:
            return True
        if isinstance(node, ast.BoolOp) and len(_compared_name(node)) >= 2:
            return True
        if isinstance(node, ast.If) and len(_chain_names(node)) >= 2:
            return True
    return False


def production_files() -> Iterable[Path]:
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def measured_tables() -> List[str]:
    found: List[str] = []
    for path in production_files():
        if holds_literal_gate_table(path.read_text(encoding="utf-8")):
            found.append(path.relative_to(REPO_ROOT).as_posix())
    return found


@pytest.mark.parametrize(
    "source,expected",
    [
        ('X = {"codex_gate": 1, "kimi_gate": 2}', True),
        ('X = ("glm_gate", "claude_gate")', True),
        ('X = {"codex_gate", "gemini_review"}', True),
        ('X = [("a", "codex_gate"), ("b", "deepseek_gate")]', True),
        ('if g == "codex_gate":\n    pass\nelif g == "kimi_gate":\n    pass', True),
        ('ok = g == "codex_gate" or g == "glm_gate"', True),
        ('"""codex_gate and kimi_gate in a docstring"""\nX = 1', False),
        ('# codex_gate, kimi_gate\nX = 1', False),
        ('X = ("codex_gate", "codex_gate")', False),
        ('X = {"codex_gate": 1, "other": 2}', False),
        ('if g == "codex_gate":\n    pass\nif g == "kimi_gate":\n    pass', False),
        ('X = ["codex_gate"]', False),
    ],
    ids=[
        "dict", "tuple", "set", "nested-pairs", "if-elif", "boolop", "docstring", "comment",
        "same-name-twice", "one-name", "separate-ifs", "single-member",
    ],
)
def test_detector_counts_tables_and_ignores_prose(source, expected):
    assert holds_literal_gate_table(source) is expected


@pytest.mark.parametrize("source", ["def broken(:\n", "X = {", "\x00"], ids=["def", "brace", "nul"])
def test_detector_refuses_source_it_cannot_parse(source):
    with pytest.raises((SyntaxError, ValueError)):
        holds_literal_gate_table(source)


def test_files_with_a_literal_gate_name_table_do_not_grow():
    found = measured_tables()
    new = sorted(set(found) - set(LITERAL_TABLE_FILES))

    assert not new, (
        f"{len(new)} file(s) gained a literal table of gate names: {new}. "
        f"A new provider is a registry row (gate_lane_rows.GATE_LANE_ROWS); read the name from the row."
    )
    assert len(found) <= LITERAL_TABLE_CEILING, (
        f"{len(found)} files hold a literal gate-name table, the ceiling is {LITERAL_TABLE_CEILING}"
    )
    if len(found) < LITERAL_TABLE_CEILING or set(found) != set(LITERAL_TABLE_FILES):
        pytest.fail(
            f"the count fell to {len(found)} (ceiling {LITERAL_TABLE_CEILING}): lower "
            f"LITERAL_TABLE_CEILING and LITERAL_TABLE_FILES to {found}"
        )
