"""P1 of provider-agnostic-review-lane: GateRunner picks its dispatcher from one table.

ADR-046 Amendment 1, section A2. A harness-lane gate is carried by the dispatcher
``scripts/lib/gate_dispatch_table.py`` builds for the lane its registry row names.
``scripts/gate_runner.py`` holds no lane or provider name: a synthetic row plus a
sentinel dispatcher is enough to prove the engine calls the sentinel.
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import gate_dispatch_table as table  # noqa: E402
import gate_recorder as rec  # noqa: E402
import gate_runner  # noqa: E402
from gate_runner import GateRunner  # noqa: E402

SENTINEL_GATE = "sentinel_gate"
SENTINEL_LANE = "sentinel-lane"
SENTINEL_KEY = "sentinel_transport"

PASS_REPORT = (
    "Reviewed the diff.\nNo blocking findings.\n\n"
    "```json\n"
    '{"verdict": "pass", "findings": [], "residual_risk": null}\n'
    "```\n"
)

# The provider names that must not appear in executable code of gate_runner.py.
# The codex seat stays: it is a path_binary gate the engine launches itself.
FORBIDDEN_IN_ENGINE = ("kimi", "glm", "deepseek", "claude_gate", "claude_lane")


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    monkeypatch.setattr(gate_runner, "fetch_branch", lambda *a, **kw: None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    data_dir = project_root / ".vnx-data"
    state_dir = data_dir / "state"
    reports_dir = data_dir / "unified_reports"
    (state_dir / "review_gates" / "requests").mkdir(parents=True)
    (state_dir / "review_gates" / "results").mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_REPORTS_DIR", str(reports_dir))
    # The provider switch keeps its own family map; a synthetic gate is not in it.
    monkeypatch.setattr(rec, "gate_switch_refusal", lambda *a, **k: None)
    return {"state_dir": state_dir, "reports_dir": reports_dir}


def _payload(gate: str) -> dict:
    return {
        "gate": gate, "status": "requested", "provider": "x", "branch": "feature/test",
        "pr_number": 1, "review_mode": "per_pr", "risk_class": "medium",
        "changed_files": ["scripts/test.py"], "requested_at": "2026-04-01T14:00:00Z",
        "report_path": "", "prompt": "Review this diff",
    }


def _runner(env) -> GateRunner:
    return GateRunner(state_dir=env["state_dir"], reports_dir=env["reports_dir"])


def _register_synthetic_row(monkeypatch, factory, *, lane=SENTINEL_LANE, key=SENTINEL_KEY):
    monkeypatch.setitem(rec.GATE_PROVIDERS, SENTINEL_GATE, (rec.GATE_PROVIDER_HARNESS_LANE, lane))
    if key is not None:
        monkeypatch.setitem(table.LANE_DISPATCHER_KEYS, lane, key)
    if factory is not None:
        monkeypatch.setitem(table.DISPATCHER_FACTORIES, SENTINEL_KEY, factory)


def test_runner_calls_the_sentinel_dispatcher_of_a_synthetic_row(env, monkeypatch):
    calls = []
    built = []

    def factory(data_dir, base_ref, timeouts, project_root):
        built.append((Path(data_dir), base_ref, dict(timeouts), project_root))

        def sentinel(provider, model, prompt, dispatch_id):
            calls.append((provider, dispatch_id))
            return PASS_REPORT

        return sentinel

    _register_synthetic_row(monkeypatch, factory)
    # The default (governed) dispatcher must not run for a row the table maps elsewhere.
    monkeypatch.setattr(
        "plan_gate_panel._make_default_dispatcher",
        lambda *a, **k: pytest.fail("the default dispatcher ran instead of the sentinel"),
    )

    _runner(env).run(gate=SENTINEL_GATE, request_payload=_payload(SENTINEL_GATE), pr_number=1)

    assert len(calls) == 1
    assert calls[0][0] == SENTINEL_LANE
    data_dir, base_ref, timeouts, _ = built[0]
    assert data_dir == env["reports_dir"].parent
    assert base_ref == "origin/feature/test"
    assert set(timeouts) >= {"timeout_seconds", "total_deadline_seconds"}


@pytest.mark.parametrize("lane,key", [
    ("kimi", table.DISPATCHER_GOVERNED),
    ("glm-harness", table.DISPATCHER_GOVERNED),
    ("deepseek-harness", table.DISPATCHER_GOVERNED),
    ("claude", table.DISPATCHER_CLAUDE_LANE),
])
def test_every_registered_lane_resolves_through_the_table(lane, key):
    assert table.LANE_DISPATCHER_KEYS[lane] == key
    assert callable(table.DISPATCHER_FACTORIES[key])
    assert table.unresolved_detail("any_gate", lane) is None


def test_every_harness_lane_row_of_the_registry_has_a_dispatcher():
    for gate, (kind, lane) in rec.GATE_PROVIDERS.items():
        if kind == rec.GATE_PROVIDER_HARNESS_LANE:
            assert table.unresolved_detail(gate, lane) is None, gate


def test_default_factories_pass_the_documented_arguments(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        "plan_gate_panel._make_default_dispatcher",
        lambda data_dir, timeout, **kw: seen.update(g=(data_dir, timeout, kw)) or "governed",
    )
    monkeypatch.setattr(
        "claude_gate_lane.make_claude_gate_dispatcher",
        lambda data_dir, **kw: seen.update(c=(data_dir, kw)) or "claude",
    )
    timeouts = {"timeout_seconds": 11, "total_deadline_seconds": 7}
    assert table.build_dispatcher("g", "kimi", tmp_path, "origin/b", timeouts) == "governed"
    assert seen["g"] == (str(tmp_path), 11, {
        "role": "review-gate", "base_ref": "origin/b", "total_deadline_seconds": 7,
    })
    assert table.build_dispatcher("g", "claude", tmp_path, "origin/b", timeouts, tmp_path) == "claude"
    assert seen["c"] == (tmp_path, {
        "base_ref": "origin/b", "total_deadline_seconds": 7, "project_root": tmp_path,
    })


@pytest.mark.parametrize("lane", [None, "", 0, "no-such-lane", ["kimi"]])
def test_a_lane_without_a_key_has_no_dispatcher(lane):
    assert table.unresolved_detail("g", lane) is not None
    with pytest.raises(table.UnknownDispatcher):
        table.build_dispatcher("g", lane, Path("."), "origin/b", {})


@pytest.mark.parametrize("key", [None, "", "not_in_the_table", 7])
def test_a_key_the_table_does_not_hold_has_no_dispatcher(monkeypatch, key):
    monkeypatch.setitem(table.LANE_DISPATCHER_KEYS, "odd-lane", key)
    assert table.unresolved_detail("g", "odd-lane") is not None
    with pytest.raises(table.UnknownDispatcher):
        table.build_dispatcher("g", "odd-lane", Path("."), "origin/b", {})


@pytest.mark.parametrize("key", [None, "not_in_the_table"])
def test_runner_books_not_executable_and_never_falls_back(env, monkeypatch, key):
    _register_synthetic_row(monkeypatch, None, key=key)
    if key is None:
        monkeypatch.delitem(table.LANE_DISPATCHER_KEYS, SENTINEL_LANE, raising=False)
    monkeypatch.setattr(
        "plan_gate_panel._make_default_dispatcher",
        lambda *a, **k: pytest.fail("a row without a dispatcher fell back on the default"),
    )

    result = _runner(env).run(gate=SENTINEL_GATE, request_payload=_payload(SENTINEL_GATE), pr_number=1)

    assert result["status"] == "not_executable"
    assert result["reason"] == "gate_runner_missing"
    assert SENTINEL_LANE in result["reason_detail"]
    if key is not None:
        assert key in result["reason_detail"]


def test_gate_runner_names_no_provider_in_code():
    """Executable code only: comments and docstrings may still tell the history."""
    source = (SCRIPTS_DIR / "gate_runner.py").read_text(encoding="utf-8")
    offenders = []
    previous = tokenize.INDENT
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and previous in (tokenize.INDENT, tokenize.NEWLINE, tokenize.NL):
            previous = tok.type  # a docstring or a bare string statement
            continue
        if tok.type not in (tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            previous = tok.type
        text = tok.string.lower()
        if tok.type in (tokenize.NAME, tokenize.STRING) and any(n in text for n in FORBIDDEN_IN_ENGINE):
            offenders.append((tok.start[0], tok.string))
    assert offenders == []


def test_new_functions_stay_within_seventy_lines():
    import ast

    for path in (SCRIPTS_DIR / "lib" / "gate_dispatch_table.py",):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef):
                assert node.end_lineno - node.lineno + 1 <= 70, node.name
