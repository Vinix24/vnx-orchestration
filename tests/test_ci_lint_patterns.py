"""Tests for scripts/ci_lint_patterns.py lint gate."""

import io
import sys
from pathlib import Path

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


# --- main() exit codes for default (full-tree) mode ---

def test_main_exit_1_on_findings(tmp_path, monkeypatch):
    """main() returns 1 when findings exist."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "bad.py").write_text("try:\n    pass\nexcept Exception:\n    pass\n")

    def patched_collect(root):
        return [str(scripts_dir / "bad.py")]

    monkeypatch.setattr(lint, "collect_files_from_dirs", patched_collect)
    result = lint.main([])
    assert result == 1


def test_main_exit_0_for_no_findings(monkeypatch):
    """main() returns 0 when no findings."""
    monkeypatch.setattr(lint, "collect_files_from_dirs", lambda root: [])
    result = lint.main([])
    assert result == 0


# --- --scan-stdin mode (concatenated added-line blob) ---

def test_scan_stdin_silent_except_detected(monkeypatch):
    """--scan-stdin: catches silent-except added in a diff blob."""
    blob = "try:\n    pass\nexcept Exception:\n    pass\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 1


def test_scan_stdin_atomic_write_detected(monkeypatch):
    """--scan-stdin: catches non-atomic state write in a diff blob."""
    blob = 'with open("foo/state/x.json", "w") as f:\n    f.write("data")\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 1


def test_scan_stdin_clean_no_findings(monkeypatch):
    """--scan-stdin: returns 0 on clean added-line blob."""
    blob = 'def hello():\n    return 42\n'
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 0


def test_scan_stdin_empty_input(monkeypatch):
    """--scan-stdin: returns 0 on empty stdin (no added lines)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    result = lint.main(["--scan-stdin"])
    assert result == 0


def test_scan_stdin_noqa_in_blob_ignored(monkeypatch):
    """--scan-stdin: noqa comment on same line as except suppresses Pattern A."""
    blob = "try:\n    pass\nexcept Exception:  # noqa: vnx-silent-except\n    pass\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(blob))
    result = lint.main(["--scan-stdin"])
    assert result == 0


# --- --files-from-stdin mode (full file scan via stdin paths) ---

def test_files_from_stdin_silent_except_detected(tmp_path, monkeypatch):
    """--files-from-stdin: catches silent-except in a file fed via stdin."""
    bad = _write(tmp_path, "bad.py", "try:\n    pass\nexcept Exception:\n    pass\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{bad}\n"))
    result = lint.main(["--files-from-stdin"])
    assert result == 1


def test_files_from_stdin_atomic_write_detected(tmp_path, monkeypatch):
    """--files-from-stdin: catches non-atomic state write in a file fed via stdin."""
    bad = _write(
        tmp_path,
        "write.py",
        'with open("foo/state/x.json", "w") as f:\n    f.write("data")\n',
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{bad}\n"))
    result = lint.main(["--files-from-stdin"])
    assert result == 1


def test_files_from_stdin_clean_no_findings(tmp_path, monkeypatch):
    """--files-from-stdin: returns 0 on clean files."""
    clean = _write(tmp_path, "clean.py", 'print("ok")\n')
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{clean}\n"))
    result = lint.main(["--files-from-stdin"])
    assert result == 0


def test_files_from_stdin_skips_blanks_and_missing(tmp_path, monkeypatch):
    """--files-from-stdin: blank lines ignored; missing files silently skipped."""
    clean = _write(tmp_path, "clean.py", 'print("ok")\n')
    stdin_text = f"\n{clean}\n\n/nonexistent/path/file.py\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    result = lint.main(["--files-from-stdin"])
    assert result == 0
