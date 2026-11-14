"""Characterisation tests for golf-schoon R05b: gather_intelligence.main."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import gather_intelligence as gi
from gather_intelligence import T0IntelligenceGatherer

def _gatherer(db=None, tag_engine=None, agents=("alpha", "beta")):
    obj = object.__new__(T0IntelligenceGatherer)
    obj.quality_db = db
    obj.quality_db_path = Path("/nonexistent/quality.db")
    obj.tag_engine = tag_engine
    obj.agent_directory = list(agents)
    obj.vnx_path = Path("/tmp/vnx")
    obj.project_root = Path("/tmp/project")
    return obj

def _run(gatherer, argv, capsys):
    with patch.object(gi, "T0IntelligenceGatherer", return_value=gatherer), \
            patch.object(sys, "argv", ["gather_intelligence.py", *argv]):
        code = gi.main()
    out = capsys.readouterr().out
    return code, out


# ---- main ------------------------------------------------------------------

def test_main_list_agents_json(capsys):
    code, out = _run(_gatherer(), ["list-agents"], capsys)
    assert code == gi.EXIT_OK
    assert json.loads(out) == {"agents": ["alpha", "beta"]}

def test_main_validate_missing_arg(capsys):
    code, out = _run(_gatherer(), ["validate"], capsys)
    assert code == gi.EXIT_VALIDATION
    assert json.loads(out)["error"] == "Missing agent name"

def test_main_validate_valid_and_invalid(capsys):
    g = _gatherer()
    code, out = _run(g, ["validate", "alpha"], capsys)
    assert code == gi.EXIT_OK and json.loads(out)["valid"] is True
    code, out = _run(g, ["validate", "zzz-nope"], capsys)
    assert code == gi.EXIT_VALIDATION and json.loads(out)["valid"] is False
    code, out = _run(g, ["validate", "zzz-nope", "--human"], capsys)
    assert code == gi.EXIT_VALIDATION
    assert "Invalid agent: zzz-nope" in out and "Error:" in out and "Suggestion:" in out

def test_main_gather_missing_args(capsys):
    code, out = _run(_gatherer(), ["gather", "task"], capsys)
    assert code == gi.EXIT_VALIDATION
    assert json.loads(out)["error"] == "Missing required arguments"

def test_main_gather_passes_arguments_and_exit_code(capsys):
    g = _gatherer()
    g.gather_for_dispatch = MagicMock(return_value={"dispatch_blocked": False, "pattern_count": 2})
    code, out = _run(g, ["gather", "t", "T1", "ag", "gt", "did"], capsys)
    assert code == gi.EXIT_OK and json.loads(out)["pattern_count"] == 2
    g.gather_for_dispatch.assert_called_once_with("t", "T1", "ag", "gt", dispatch_id="did")
    g.gather_for_dispatch = MagicMock(return_value={"dispatch_blocked": True, "error": "boom"})
    code, out = _run(g, ["gather", "t", "T1", "--human"], capsys)
    assert code == gi.EXIT_VALIDATION
    g.gather_for_dispatch.assert_called_once_with("t", "T1", None, None, dispatch_id=None)
    assert "Dispatch blocked: True" in out and "Error: boom" in out
    assert "Pattern count: 0" in out and "Prevention rules: 0" in out

def test_main_patterns(capsys):
    g = _gatherer()
    pats = [{"title": "T", "quality_score": 80, "relevance_score": 0.5, "tags": "x",
             "file_path": "f.py", "description": "d" * 150}]
    g.query_relevant_patterns = MagicMock(return_value=pats)
    code, out = _run(g, ["patterns", "a", "b"], capsys)
    assert code == gi.EXIT_OK
    assert json.loads(out) == {"query": "a b", "patterns": pats}
    code, out = _run(g, ["patterns", "a", "--human"], capsys)
    assert "Found 1 relevant patterns for: 'a'" in out
    assert "Pattern 1:" in out and "  Quality: 80%" in out and "  Relevance: 0.50" in out
    code, out = _run(g, ["patterns"], capsys)
    assert code == gi.EXIT_VALIDATION and json.loads(out)["error"] == "Missing task description"

def test_main_keywords(capsys):
    g = _gatherer()
    code, out = _run(g, ["keywords", "fix", "the", "bug"], capsys)
    assert code == gi.EXIT_OK
    payload = json.loads(out)
    assert payload["text"] == "fix the bug"
    assert payload["keywords"] == g.extract_keywords("fix the bug")
    code, out = _run(g, ["keywords"], capsys)
    assert code == gi.EXIT_VALIDATION and json.loads(out)["error"] == "Missing text"

def test_main_tags_without_and_with_engine(capsys):
    g = _gatherer()
    code, out = _run(g, ["tags", "critical", "jwt"], capsys)
    payload = json.loads(out)
    assert code == gi.EXIT_OK and payload["task"] == "critical jwt"
    assert payload["analysis"] is None and "critical-severity" in payload["tags"]
    g.tag_engine = object()
    g.analyze_tags_for_task = MagicMock(return_value={"k": 1})
    code, out = _run(g, ["tags", "jwt"], capsys)
    assert json.loads(out)["analysis"] == {"k": 1}
    code, out = _run(g, ["tags", "jwt", "--human"], capsys)
    assert "\nTag Analysis:" in out and json.dumps({"k": 1}, indent=2) in out
    code, out = _run(g, ["tags"], capsys)
    assert code == gi.EXIT_VALIDATION and json.loads(out)["error"] == "Missing task description"

def test_main_prevention(capsys):
    g = _gatherer()
    rules = [{"tag_combination": ["a", "b"], "rule_type": "warn", "confidence": 0.5,
              "recommendation": "do"}]
    g.query_prevention_rules = MagicMock(return_value=rules)
    code, out = _run(g, ["prevention", "x", "y"], capsys)
    assert code == gi.EXIT_OK and json.loads(out) == {"task": "x y", "prevention_rules": rules}
    code, out = _run(g, ["prevention", "x", "--human"], capsys)
    assert "Found 1 prevention rules:" in out and "Tags: a, b" in out and "Confidence: 0.50" in out
    code, out = _run(g, ["prevention"], capsys)
    assert code == gi.EXIT_VALIDATION and json.loads(out)["error"] == "Missing task description"

def test_main_record_adoption(capsys):
    g = _gatherer()
    g.record_adoption_from_receipt = MagicMock(return_value={"adoptions": 1, "checked": 3})
    code, out = _run(g, ["record-adoption", "d1", "T2", "/r.md"], capsys)
    assert code == gi.EXIT_OK and json.loads(out) == {"adoptions": 1, "checked": 3}
    g.record_adoption_from_receipt.assert_called_once_with("d1", "T2", "/r.md")
    code, out = _run(g, ["record-adoption", "d1", "T2", "/r.md", "--human"], capsys)
    assert out.strip() == "Adoptions recorded: 1/3"
    code, out = _run(g, ["record-adoption", "d1"], capsys)
    assert code == gi.EXIT_VALIDATION
    assert json.loads(out) == {"error": "Usage: record-adoption <dispatch_id> <terminal> <report_path>"}

COMMANDS = ["list-agents", "validate", "gather", "patterns", "keywords", "tags",
            "prevention", "record-adoption"]

def test_main_unknown_command(capsys):
    code, out = _run(_gatherer(), ["bogus"], capsys)
    assert code == gi.EXIT_VALIDATION
    assert json.loads(out) == {"error": "Unknown command: bogus", "available_commands": COMMANDS}
    code, out = _run(_gatherer(), ["bogus", "--human"], capsys)
    assert "Available commands: " + ", ".join(COMMANDS) in out

def test_main_no_command_json_no_db(capsys):
    code, out = _run(_gatherer(), [], capsys)
    assert code == gi.EXIT_OK
    assert json.loads(out) == {
        "version": "1.4.0", "loaded_agents": 2, "database_connected": False,
        "tag_engine_available": False, "patterns_available": None,
        "tag_statistics": None, "commands": COMMANDS}

def test_main_no_command_with_db_and_engine(capsys, tmp_path):
    db = sqlite3.connect(tmp_path / "q.db")
    db.execute("CREATE TABLE code_snippets (id INTEGER)")
    db.executemany("INSERT INTO code_snippets VALUES (?)", [(1,), (2,), (3,)])
    engine = MagicMock()
    engine.get_statistics.return_value = {"total_combinations": 4, "total_rules": 5}
    g = _gatherer(db=db, tag_engine=engine)
    code, out = _run(g, [], capsys)
    payload = json.loads(out)
    assert code == gi.EXIT_OK and payload["patterns_available"] == 3
    assert payload["database_connected"] is True and payload["tag_engine_available"] is True
    assert payload["tag_statistics"] == {"total_combinations": 4, "total_rules": 5}
    code, out = _run(g, ["--human"], capsys)
    lines = out.splitlines()
    assert lines[0] == "VNX Intelligence Gatherer v1.4.0"
    assert "Loaded agents: 2" in lines and "Database connected: True" in lines
    assert "Patterns available: 3" in lines and "Tag combinations: 4" in lines
    assert "Prevention rules: 5" in lines
    assert lines[-1] == "Commands: " + ", ".join(COMMANDS)

def test_main_no_command_swallows_db_and_engine_errors(capsys, tmp_path):
    db = sqlite3.connect(tmp_path / "q.db")  # no code_snippets table
    engine = MagicMock()
    engine.get_statistics.side_effect = RuntimeError("x")
    code, out = _run(_gatherer(db=db, tag_engine=engine), [], capsys)
    payload = json.loads(out)
    assert code == gi.EXIT_OK and payload["patterns_available"] is None
    assert payload["tag_statistics"] is None
