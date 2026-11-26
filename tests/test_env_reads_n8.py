"""N8 reader boundary; seed the PR-9a exception list before its whole-tree scan."""
import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "scripts" / "env_read_allowlist.json"


def test_paid_lane_env_reads_are_only_explicit_exceptions():
    tree = ast.parse((ROOT / "scripts/lib/paid_lane_budget.py").read_text())
    constants = {node.targets[0].id: node.value.value for node in tree.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, ast.Constant)}
    reads = [constants[node.args[0].id] for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.environ.get"]
    allowed = json.loads(ALLOWLIST.read_text())
    assert set(reads) == {"VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET", "VNX_STATE_DIR"}
    assert set(reads) <= allowed.keys()
    bypass = allowed["VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET"]
    assert bypass["class"] == allowed["VNX_OVERRIDE_WORKER_CLAUDE"]["class"] == "operator_bypass"
    assert bypass["reason"] == "operator bypass, one window"
    assert "VNX_OVERRIDE_PAID_LANE_DAILY_BUDGET_USD" not in allowed


@pytest.mark.parametrize("module", ["paid_lane_budget", "receipt_classifier"])
def test_amount_reader_uses_config_runtime(module):
    tree = ast.parse((ROOT / f"scripts/lib/{module}.py").read_text())
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "get_daily_budget_usd")
    calls = [ast.unparse(node) for node in ast.walk(reader) if isinstance(node, ast.Call)]
    assert "config_runtime.get(ENV_DAILY_BUDGET)" in calls
    assert not any("os.environ" in call or "os.getenv" in call for call in calls)
