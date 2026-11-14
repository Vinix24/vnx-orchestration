"""Characterisation of PRQueueManager.patch_dispatch (golf schoon R13b).

Pins the observable behaviour before and after the extract-method refactor:
return value, staging file content, audit log entry, stdout lines.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

from pr_queue_manager import PRQueueManager

DISPATCH = (
    "[[TARGET:A]]\n"
    "Manager Block\n"
    "Role: backend-developer\n"
    "Priority: P1\n"
    "PR-ID: PR-7\n"
    "Dispatch-ID: d-1\n"
    "\n"
    "Instruction:\n"
    "old body line\n"
    "\n"
    "Dependencies: none\n"
    "\n"
    "[[DONE]]\n"
)


@pytest.fixture
def mgr(tmp_path):
    m = object.__new__(PRQueueManager)
    m.dispatch_dir = tmp_path / "dispatches"
    (m.dispatch_dir / "staging").mkdir(parents=True)
    m.vnx_state_dir = tmp_path / "state"
    m.legacy_state_dir = tmp_path / "legacy"
    m.dual_write_legacy = False
    m.project_root = tmp_path
    (m.dispatch_dir / "staging" / "d-1.md").write_text(DISPATCH)
    return m


def _read(m):
    return (m.dispatch_dir / "staging" / "d-1.md").read_text()


def _audit(m):
    lines = (m.vnx_state_dir / "dispatch_audit.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines]


def test_missing_file(mgr, capsys):
    assert mgr.patch_dispatch("nope", {"role": "x"}) is False
    assert "❌ Staging dispatch not found: nope" in capsys.readouterr().out


def test_header_replace_and_md_suffix(mgr, capsys):
    assert mgr.patch_dispatch("d-1.md", {"role": "qa", "priority": "P0"}) is True
    c = _read(mgr)
    assert "Role: qa\n" in c and "Priority: P0\n" in c
    out = capsys.readouterr().out
    assert "✅ Patched dispatch: d-1" in out
    assert "   role: backend-developer → qa" in out
    a = _audit(mgr)[0]
    assert a["action"] == "patch_dispatch" and a["dispatch_id"] == "d-1"
    assert a["pr_id"] == "PR-7" and a["actor"] == "T0"
    assert a["changes"] == ["role: backend-developer → qa", "priority: P1 → P0"]
    assert a["patch_fields"] == {"role": "qa", "priority": "P0"}


def test_header_hyphen_field_added_before_dispatch_id(mgr):
    assert mgr.patch_dispatch("d-1", {"task-class": "x"}) is True
    c = _read(mgr)
    assert "Task-Class: x\nDispatch-ID: d-1" in c
    assert _audit(mgr)[0]["changes"] == ["task-class: (added) x"]


def test_header_field_not_insertable(mgr, capsys):
    (mgr.dispatch_dir / "staging" / "d-1.md").write_text("Instruction:\nx\n\n[[DONE]]\n")
    assert mgr.patch_dispatch("d-1", {"role": "x"}) is False
    out = capsys.readouterr().out
    assert "⚠️  Field 'role' not found and could not insert" in out
    assert "❌ No changes applied" in out


def test_instruction_replace_keeps_extra_blank_line(mgr, capsys):
    assert mgr.patch_dispatch("d-1", {"instruction": "new"}) is True
    c = _read(mgr)
    assert "Instruction:\nnew\n\n\nDependencies: none" in c
    assert "instruction: replaced (13 → 3 chars)" in capsys.readouterr().out


def test_instruction_append_and_prepend(mgr, capsys):
    assert mgr.patch_dispatch("d-1", {"append-instruction": "more"}) is True
    assert "Instruction:\nold body line\n\nmore\n\nDependencies" in _read(mgr)
    assert mgr.patch_dispatch("d-1", {"prepend-instruction": "first"}) is True
    assert "Instruction:\nfirst\n\nold body line" in _read(mgr)
    out = capsys.readouterr().out
    assert "instruction: appended 4 chars" in out
    assert "instruction: prepended 5 chars" in out


def test_instruction_missing_markers_warn(mgr, capsys):
    (mgr.dispatch_dir / "staging" / "d-1.md").write_text("Role: a\nDispatch-ID: d-1\n")
    for key, msg in (
        ("instruction", "⚠️  Could not locate Instruction body in dispatch"),
        ("append-instruction", "⚠️  Could not locate Instruction body in dispatch"),
        ("prepend-instruction", "⚠️  Could not locate Instruction header in dispatch"),
    ):
        assert mgr.patch_dispatch("d-1", {key: "z"}) is False
        out = capsys.readouterr().out
        assert msg in out and "❌ No changes applied" in out


def test_instruction_file_relative_and_absolute(mgr, tmp_path, capsys):
    (tmp_path / "body.txt").write_text("  from file \n")
    assert mgr.patch_dispatch("d-1", {"instruction-file": "body.txt"}) is True
    assert "Instruction:\nfrom file\n\n\nDependencies" in _read(mgr)
    assert "instruction: replaced from file body.txt (9 chars)" in capsys.readouterr().out
    assert mgr.patch_dispatch("d-1", {"instruction-file": str(tmp_path / "body.txt")}) is True


def test_instruction_file_missing_continues(mgr, capsys):
    assert mgr.patch_dispatch(
        "d-1", {"instruction-file": "absent.txt", "role": "qa"}
    ) is True
    out = capsys.readouterr().out
    assert "❌ Instruction file not found: absent.txt" in out
    assert "Role: qa" in _read(mgr)
    assert len(_audit(mgr)[0]["changes"]) == 1


def test_instruction_file_without_body_marker(mgr, tmp_path, capsys):
    (mgr.dispatch_dir / "staging" / "d-1.md").write_text("Role: a\n")
    (tmp_path / "b.txt").write_text("x")
    assert mgr.patch_dispatch("d-1", {"instruction-file": "b.txt"}) is False
    assert "⚠️  Could not locate Instruction body in dispatch" in capsys.readouterr().out


def test_no_changes_writes_nothing(mgr, capsys):
    before = _read(mgr)
    assert mgr.patch_dispatch("d-1", {}) is False
    assert _read(mgr) == before
    assert not (mgr.vnx_state_dir / "dispatch_audit.jsonl").exists()
    assert "❌ No changes applied" in capsys.readouterr().out


def test_exception_returns_false(mgr, capsys):
    mgr._log_audit = lambda entry: (_ for _ in ()).throw(RuntimeError("boom"))
    assert mgr.patch_dispatch("d-1", {"role": "qa"}) is False
    assert "❌ Patch failed: boom" in capsys.readouterr().out
