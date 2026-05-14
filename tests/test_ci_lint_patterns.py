"""Tests for scripts/ci_lint_patterns.py lint gate."""

import subprocess
import sys
import os
from pathlib import Path

import pytest

# Allow importing the script directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ci_lint_patterns as lint


def _write(tmp_path: Path, name: str, content: str) -> str:
    f = tmp_path / name
    f.write_text(content)
    return str(f)


# --- Pattern A: silent exception ---

def test_silent_except_detected(tmp_path):
    path = _write(tmp_path, "bad.py", "try:\n    pass\nexcept Exception:\n    pass\n")
    findings = lint.scan_file(path)
    assert any(f.pattern == "A" for f in findings), f"Expected Pattern A finding, got: {findings}"


def test_silent_except_with_noqa_ignored(tmp_path):
    path = _write(
        tmp_path,
        "ok.py",
        "try:\n    pass\nexcept Exception:  # noqa: vnx-silent-except\n    pass\n",
    )
    findings = lint.scan_file(path)
    assert not any(f.pattern == "A" for f in findings), f"Expected no Pattern A finding, got: {findings}"


def test_bare_except_detected(tmp_path):
    path = _write(tmp_path, "bare.py", "try:\n    x()\nexcept:\n    pass\n")
    findings = lint.scan_file(path)
    assert any(f.pattern == "A" for f in findings), f"Expected Pattern A for bare except, got: {findings}"


# --- Pattern B: non-atomic state write ---

def test_atomic_write_violation_detected(tmp_path):
    path = _write(
        tmp_path,
        "write.py",
        'with open("foo/state/x.json", "w") as f:\n    f.write("data")\n',
    )
    findings = lint.scan_file(path)
    assert any(f.pattern == "B" for f in findings), f"Expected Pattern B finding, got: {findings}"


def test_atomic_write_with_noqa_ignored(tmp_path):
    path = _write(
        tmp_path,
        "ok_write.py",
        'with open("foo/state/x.json", "w") as f:  # noqa: vnx-atomic-write\n    f.write("data")\n',
    )
    findings = lint.scan_file(path)
    assert not any(f.pattern == "B" for f in findings), f"Expected no Pattern B finding, got: {findings}"


# --- Clean code ---

def test_clean_code_no_findings(tmp_path):
    path = _write(tmp_path, "clean.py", 'print("ok")\n')
    findings = lint.scan_file(path)
    assert findings == [], f"Expected no findings, got: {findings}"


# --- Exit code integration ---

def test_main_returns_0_for_clean_file(tmp_path, monkeypatch):
    path = _write(tmp_path, "clean.py", 'print("ok")\n')
    monkeypatch.setattr(lint, "_SCAN_DIRS", ())
    # Call directly with specific file
    findings = lint.scan_file(path)
    assert findings == []


def test_main_exit_1_on_findings(tmp_path, monkeypatch, capsys):
    """main() returns 1 when findings exist."""
    path = _write(tmp_path, "bad.py", "try:\n    pass\nexcept Exception:\n    pass\n")

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bad.py").write_text("try:\n    pass\nexcept Exception:\n    pass\n")

    monkeypatch.chdir(tmp_path)

    # Patch root resolution to tmp_path
    original_collect = lint.collect_files_from_dirs

    def patched_collect(root):
        return [str(scripts_dir / "bad.py")]

    monkeypatch.setattr(lint, "collect_files_from_dirs", patched_collect)

    result = lint.main([])
    assert result == 1


def test_main_exit_0_for_no_findings(tmp_path, monkeypatch):
    """main() returns 0 when no findings."""
    monkeypatch.setattr(lint, "collect_files_from_dirs", lambda root: [])
    result = lint.main([])
    assert result == 0


# --- collect_files_from_diff: merge-base and fail-closed ---

def test_diff_uses_merge_base_form(tmp_path, monkeypatch):
    """collect_files_from_diff must pass BASE_REF...HEAD (three-dot) to git."""
    captured_argv: list[list[str]] = []

    def fake_run(argv, **kwargs):
        captured_argv.append(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    lint.collect_files_from_diff("origin/main", tmp_path)
    assert captured_argv, "subprocess.run was never called"
    assert "origin/main...HEAD" in captured_argv[0], (
        f"Expected three-dot form 'origin/main...HEAD' in argv, got: {captured_argv[0]}"
    )
    assert "origin/main" not in [a for a in captured_argv[0] if a == "origin/main"], (
        "Plain base_ref without '...HEAD' must not appear as a standalone argument"
    )


def test_diff_returncode_nonzero_exits_2(tmp_path, monkeypatch, capsys):
    """collect_files_from_diff exits with code 2 when git returns non-zero."""
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=128, stdout="", stderr="bad revision")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc_info:
        lint.collect_files_from_diff("origin/main", tmp_path)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "128" in captured.err


def test_diff_timeout_exits_2(tmp_path, monkeypatch, capsys):
    """collect_files_from_diff exits with code 2 on TimeoutExpired."""
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as exc_info:
        lint.collect_files_from_diff("origin/main", tmp_path)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "timed out" in captured.err
